"""Azure Backup jobs via the ARM REST API (no Az modules).

Port of Pull-AzureBackupsJobStatus.ps1: client-credentials token, enumerate
subscriptions, then Recovery Services vaults, then each vault's backupJobs
filtered server-side to the lookback window and `operation eq 'Backup'`.

Unlike the script, every job in the window is kept rather than only the newest
per server — the whole point of this app is history, and one row per server per
run is what the trend charts are made of.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from ..config import Settings
from ..ingest import ServerCache, upsert_event
from ..outcomes import normalize
from ..timeframes import utcnow
from .base import Collector

log = logging.getLogger(__name__)

ARM = "https://management.azure.com"
SUBSCRIPTIONS_API = "2020-01-01"
VAULTS_API = "2023-04-01"
JOBS_API = "2023-02-01"


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    # ARM sometimes returns 7-digit fractional seconds; fromisoformat wants ≤ 6.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        for char in tail:
            if char.isdigit():
                digits += char
            else:
                break
        rest = tail[len(digits) :]
        text = f"{head}.{digits[:6]}{rest}" if digits else head + rest
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def resource_group_of(resource_id: str | None) -> str | None:
    if not resource_id:
        return None
    parts = resource_id.strip("/").split("/")
    for index, part in enumerate(parts):
        if part.lower() == "resourcegroups" and index + 1 < len(parts):
            return parts[index + 1]
    return None


class AzureCollector(Collector):
    source = "azure"
    display_name = "Azure Backup"

    def is_configured(self, settings: Settings) -> bool:
        return bool(
            settings.azure_tenant_id and settings.azure_client_id and settings.azure_client_secret
        )

    def collect(self, session: Session, settings: Settings) -> int:
        to_utc = utcnow()
        from_utc = to_utc - timedelta(hours=settings.azure_lookback_hours)
        cache = ServerCache(session, self.source)
        count = 0

        with httpx.Client(timeout=120) as client:
            client.headers["Authorization"] = f"Bearer {self._token(client, settings)}"

            subscriptions = self._subscriptions(client, settings)
            if not subscriptions:
                raise RuntimeError("No accessible subscriptions for this service principal")

            for subscription in subscriptions:
                sub_id = subscription.get("subscriptionId")
                if not sub_id:
                    continue
                for vault in self._pages(
                    client,
                    f"{ARM}/subscriptions/{sub_id}/providers/Microsoft.RecoveryServices/vaults"
                    f"?api-version={VAULTS_API}",
                ):
                    group = resource_group_of(vault.get("id"))
                    name = vault.get("name")
                    if not group or not name:
                        continue
                    count += self._collect_vault(
                        session, client, cache, sub_id, group, name, from_utc, to_utc
                    )
                    session.commit()

        session.commit()
        return count

    # --- API ---------------------------------------------------------------

    def _token(self, client: httpx.Client, settings: Settings) -> str:
        resp = client.post(
            f"https://login.microsoftonline.com/{settings.azure_tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": settings.azure_client_id,
                "client_secret": settings.azure_client_secret,
                "scope": f"{ARM}/.default",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if not token:
            raise RuntimeError("ARM returned no access token — check tenant/app id/secret")
        return token

    def _pages(self, client: httpx.Client, url: str) -> list[dict]:
        out: list[dict] = []
        next_url: str | None = url
        while next_url:
            resp = client.get(next_url)
            resp.raise_for_status()
            body = resp.json()
            out.extend(body.get("value") or [])
            next_url = body.get("nextLink")
        return out

    def _subscriptions(self, client: httpx.Client, settings: Settings) -> list[dict]:
        everything = self._pages(client, f"{ARM}/subscriptions?api-version={SUBSCRIPTIONS_API}")
        wanted = settings.azure_subscription_list()
        if not wanted:
            return everything
        chosen = [
            s
            for s in everything
            if s.get("subscriptionId") in wanted or s.get("displayName") in wanted
        ]
        missing = set(wanted) - {s.get("subscriptionId") for s in chosen} - {
            s.get("displayName") for s in chosen
        }
        if missing:
            log.warning("azure: configured subscriptions not visible: %s", ", ".join(sorted(missing)))
        return chosen

    def _collect_vault(
        self,
        session: Session,
        client: httpx.Client,
        cache: ServerCache,
        sub_id: str,
        group: str,
        vault: str,
        from_utc: datetime,
        to_utc: datetime,
    ) -> int:
        job_filter = (
            f"startTime ge '{from_utc:%Y-%m-%dT%H:%M:%SZ}' "
            f"and endTime le '{to_utc:%Y-%m-%dT%H:%M:%SZ}' "
            "and operation eq 'Backup'"
        )
        url = (
            f"{ARM}/subscriptions/{sub_id}/resourceGroups/{group}"
            f"/providers/Microsoft.RecoveryServices/vaults/{vault}/backupJobs"
            f"?api-version={JOBS_API}"
        )
        try:
            jobs = self._pages(client, f"{url}&$filter={_encode(job_filter)}")
        except httpx.HTTPStatusError as exc:
            # A vault the principal can list but not read jobs on is a permissions
            # gap on that vault, not a reason to abandon the whole subscription.
            log.warning("azure: vault %s/%s jobs unreadable: %s", group, vault, exc)
            return 0

        count = 0
        for job in jobs:
            properties = job.get("properties") or {}
            if (properties.get("operation") or "") != "Backup":
                continue
            start = parse_dt(properties.get("startTime"))
            end = parse_dt(properties.get("endTime"))
            if start is None or end is None:
                continue
            server = cache.get(properties.get("entityFriendlyName"))
            if server is None:
                continue
            status = properties.get("status")
            upsert_event(
                session,
                cache,
                server=server,
                source=self.source,
                start_utc=start,
                end_utc=end,
                outcome=normalize(self.source, status),
                result_raw=str(status) if status else None,
                duration_sec=max(0, int((end - start).total_seconds())),
                job_name=properties.get("backupManagementType") or "Azure Backup",
                native_id=job.get("name"),
                details={
                    "vault": vault,
                    "resource_group": group,
                    "subscription_id": sub_id,
                    "backup_management_type": properties.get("backupManagementType"),
                },
            )
            count += 1
        return count


def _encode(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")
