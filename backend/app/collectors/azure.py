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

# backupJobs pages by a per-job continuation cursor, so a busy vault can return
# one record per round trip and a few days of history becomes thousands of
# requests. $top asks for more per page; ARM is free to ignore it, which is why
# the progress logging and the cap below both exist regardless.
PAGE_SIZE = 1000
# A backstop, not a limit anyone should hit: without it a paging bug or a cursor
# that never terminates walks forever with no way to tell from the outside.
MAX_PAGES = 2000
LOG_EVERY = 25
# Consecutive pages entirely older than the window before giving up on the rest
# of the chain. Only ever applied after at least one in-window record has been
# seen — see the note in `_collect_vault`.
STALE_PAGES_BEFORE_STOP = 20


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


def _arm_time(value: datetime) -> str:
    """`2026-08-09 12:00:00 PM` — what backupJobs' $filter wants.

    Built by hand rather than with `%I:%M:%S %p`, because `%p` is locale-
    dependent: on a Windows box with a non-English locale it can come back
    empty or translated, and the filter would be silently ignored again.
    """
    hour = value.hour % 12 or 12
    meridiem = "AM" if value.hour < 12 else "PM"
    return f"{value:%Y-%m-%d} {hour:02d}:{value:%M:%S} {meridiem}"


def backup_type(properties: dict) -> str | None:
    """Full / Differential / Incremental / Log, when the job says.

    It is not a first-class field: workload jobs carry it in
    `extendedInfo.propertyBag` under a key with a space in it, and jobs that
    have only one kind of backup (an IaaS VM, a file share) omit it entirely.
    """
    bag = (properties.get("extendedInfo") or {}).get("propertyBag") or {}
    for key, value in bag.items():
        if str(key).strip().lower().replace(" ", "") == "backuptype":
            return str(value).strip() or None
    return None


def is_log_backup(properties: dict) -> bool:
    """The 15-minute transaction-log job, as opposed to the nightly backup.

    SQL Server and SAP HANA in a VM back their logs up every 15 minutes by
    default. ARM reports each one as `operation: Backup`, so no server-side
    filter separates them from the nightly full — one database alone produces
    ~384 of them in a 96-hour window.
    """
    return (backup_type(properties) or "").lower() == "log"


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
                vaults = self._pages(
                    client,
                    f"{ARM}/subscriptions/{sub_id}/providers/Microsoft.RecoveryServices/vaults"
                    f"?api-version={VAULTS_API}",
                )
                log.info(
                    "azure: subscription %s has %s vault(s)",
                    subscription.get("displayName") or sub_id,
                    len(vaults),
                )
                for vault in vaults:
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

    def _iter_pages(self, client: httpx.Client, url: str, label: str = ""):
        """Yield one page of records at a time, saying so as it goes.

        A generator rather than a list so the caller can stop early — when ARM
        ignores `$filter` a vault's whole retained history is on the other end of
        this chain, and reading all of it into memory to discard 99% of it is
        both slow and pointless.

        Long silences are indistinguishable from a hang, and this is the one
        place in the app that can legitimately run for minutes.
        """
        next_url: str | None = url
        pages = 0
        total = 0
        seen: set[str] = set()

        while next_url:
            # A cursor that repeats is a loop; walking it forever would look
            # exactly like slow progress.
            if next_url in seen:
                log.warning("azure %s: nextLink repeated after %s pages — stopping", label, pages)
                break
            seen.add(next_url)

            resp = client.get(next_url)
            resp.raise_for_status()
            body = resp.json()
            records = body.get("value") or []
            next_url = body.get("nextLink")
            pages += 1
            total += len(records)

            yield records

            if label and (pages % LOG_EVERY == 0):
                log.info("azure %s: %s pages, %s records so far", label, pages, total)
            if pages >= MAX_PAGES:
                log.warning(
                    "azure %s: stopped at the %s-page cap with %s records — some history "
                    "was not read. Narrow AZURE_LOOKBACK_HOURS or raise MAX_PAGES.",
                    label,
                    MAX_PAGES,
                    total,
                )
                break

        if label:
            log.info("azure %s: %s records over %s pages", label, total, pages)

    def _pages(self, client: httpx.Client, url: str, label: str = "") -> list[dict]:
        """Every record in the chain. Fine for subscriptions and vaults, which
        are tens of rows; jobs go through `_iter_pages` instead."""
        out: list[dict] = []
        for records in self._iter_pages(client, url, label):
            out.extend(records)
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
        log.info("azure: reading jobs from vault %s", vault)
        include_logs = cache._settings.azure_include_log_backups
        count = 0
        skipped = 0
        logs = 0
        stale = 0
        returned = 0
        seen_in_window = False
        stale_pages = 0
        gave_up = False

        try:
            pages = self._iter_pages(
                client, self._jobs_url(sub_id, group, vault, from_utc, to_utc), label=vault
            )
            for page in pages:
                returned += len(page)
                in_window = 0

                for job in page:
                    properties = job.get("properties") or {}
                    if (properties.get("operation") or "") != "Backup":
                        continue
                    start = parse_dt(properties.get("startTime"))
                    end = parse_dt(properties.get("endTime"))
                    if start is None or end is None:
                        continue
                    # Enforced here as well as in the filter, because ARM will
                    # return HTTP 200 and the whole history for a filter it did
                    # not understand. Counted, so that shows up as a number
                    # instead of as a collection that never ends.
                    if end < from_utc or start > to_utc:
                        stale += 1
                        continue
                    in_window += 1
                    if not include_logs and is_log_backup(properties):
                        logs += 1
                        continue
                    server = cache.get(properties.get("entityFriendlyName"))
                    if server is None:
                        skipped += 1
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
                    # Storing is a per-event upsert, so a big vault spends real
                    # time here too — commit in batches and keep saying so.
                    if count % 200 == 0:
                        session.commit()
                        log.info("azure %s: stored %s jobs so far", vault, count)

                # Stop walking history we already have. Jobs come back newest
                # first, so once the window is behind us every remaining page is
                # older still — and with an ignored filter that is years of them.
                #
                # Deliberately conditional on having seen the window first: if a
                # vault ever returns oldest-first, this never triggers and the
                # whole chain is read, which is slow but correct. Guessing the
                # order and being wrong would silently lose last night.
                if in_window:
                    seen_in_window = True
                    stale_pages = 0
                elif seen_in_window:
                    stale_pages += 1
                    if stale_pages >= STALE_PAGES_BEFORE_STOP:
                        gave_up = True
                        pages.close()
                        break
        except httpx.HTTPStatusError as exc:
            # A vault the principal can list but not read jobs on is a permissions
            # gap on that vault, not a reason to abandon the whole subscription.
            log.warning("azure: vault %s/%s jobs unreadable: %s", group, vault, exc)
            session.rollback()
            return 0

        session.commit()
        if gave_up:
            log.info(
                "azure %s: stopped after %s consecutive pages older than the %sh window",
                vault,
                stale_pages,
                cache._settings.azure_lookback_hours,
            )
        # One line that accounts for every record ARM returned. "It just goes
        # and goes" is answerable from this alone: if `jobs` is two orders of
        # magnitude larger than what was stored, the breakdown says where it
        # went.
        log.info(
            "azure %s: %s jobs from ARM — stored %s%s%s%s",
            vault,
            returned,
            count,
            f", skipped {logs} log backups" if logs else "",
            f", {stale} outside the window" if stale else "",
            f", {skipped} with no usable server name" if skipped else "",
        )
        if stale > count:
            log.warning(
                "azure %s: %s of %s jobs were outside the %sh window — ARM is ignoring "
                "$filter and serving the vault's whole retained history. Collection "
                "still gets the right answer, but it reads far more than it needs to.",
                vault,
                stale,
                returned,
                cache._settings.azure_lookback_hours,
            )
        return count

    def _jobs_url(
        self, sub_id: str, group: str, vault: str, from_utc: datetime, to_utc: datetime
    ) -> str:
        """The `$filter` this endpoint actually parses.

        It is not ordinary OData, and it does not tell you when you get it
        wrong: an ISO-8601 range with `ge`/`le` returns HTTP 200 and the vault's
        entire retained job history, which is how a 58-server estate produced
        60,252 records for a 24-hour question.

        Two things are unusual, both of them documented and neither guessable:

        - **`eq` expresses the range, not equality.** `startTime eq X and
          endTime eq Y` means "between X and Y". `ge`/`le` are silently ignored.
        - **The timestamp is a 12-hour clock with AM/PM**, space-separated —
          `2026-08-09 12:00:00 PM`, not `2026-08-09T12:00:00Z`.

        This is what `Get-AzRecoveryServicesBackupJob -From -To` builds, which is
        why the PowerShell this replaces never hit the problem.
        """
        job_filter = (
            f"startTime eq '{_arm_time(from_utc)}' "
            f"and endTime eq '{_arm_time(to_utc)}' "
            "and operation eq 'Backup'"
        )
        return (
            f"{ARM}/subscriptions/{sub_id}/resourceGroups/{group}"
            f"/providers/Microsoft.RecoveryServices/vaults/{vault}/backupJobs"
            f"?api-version={JOBS_API}&$top={PAGE_SIZE}&$filter={_encode(job_filter)}"
        )


def survey(settings: Settings, hours: int) -> list[dict]:
    """Read-only: what is actually in those vaults, without storing any of it.

    A collection that returns tens of thousands of records for sixty servers is
    either paging history it was not asked for or counting jobs nobody thinks of
    as backups. Both look identical from the outside — a number going up — so
    this breaks the same window down by management type and backup type and
    prints it, in one short run.
    """
    collector = AzureCollector()
    to_utc = utcnow()
    from_utc = to_utc - timedelta(hours=hours)
    out: list[dict] = []

    with httpx.Client(timeout=120) as client:
        client.headers["Authorization"] = f"Bearer {collector._token(client, settings)}"
        for subscription in collector._subscriptions(client, settings):
            sub_id = subscription.get("subscriptionId")
            if not sub_id:
                continue
            vaults = collector._pages(
                client,
                f"{ARM}/subscriptions/{sub_id}/providers/Microsoft.RecoveryServices"
                f"/vaults?api-version={VAULTS_API}",
            )
            for vault in vaults:
                group = resource_group_of(vault.get("id"))
                name = vault.get("name")
                if not group or not name:
                    continue
                try:
                    jobs = collector._pages(
                        client,
                        collector._jobs_url(sub_id, group, name, from_utc, to_utc),
                        label=name,
                    )
                except httpx.HTTPStatusError as exc:
                    out.append({"vault": name, "error": str(exc)})
                    continue

                kinds: dict[str, int] = {}
                operations: dict[str, int] = {}
                entities: set[str] = set()
                outside = 0
                for job in jobs:
                    properties = job.get("properties") or {}
                    kind = "{} / {}".format(
                        properties.get("backupManagementType") or "?",
                        backup_type(properties) or "(no backup type)",
                    )
                    kinds[kind] = kinds.get(kind, 0) + 1
                    # Whether `operation eq 'Backup'` survived says whether ARM
                    # parsed any of the filter or discarded the whole thing.
                    operation = str(properties.get("operation") or "?")
                    operations[operation] = operations.get(operation, 0) + 1
                    if properties.get("entityFriendlyName"):
                        entities.add(str(properties["entityFriendlyName"]))
                    start = parse_dt(properties.get("startTime"))
                    if start is not None and start < from_utc:
                        outside += 1
                out.append(
                    {
                        "vault": name,
                        "subscription": subscription.get("displayName") or sub_id,
                        "jobs": len(jobs),
                        "entities": len(entities),
                        "outside_window": outside,
                        "kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
                        "operations": dict(sorted(operations.items(), key=lambda kv: -kv[1])),
                    }
                )
    return out


def _encode(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")
