"""Veeam Backup & Replication sessions via the REST API on port 9419.

Port of Pull-VeeamJobStatus.ps1. Same shape: OAuth2 password grant, page the
session list newest-first, keep finished backup sessions, then read each
session's log to find out which machines it actually protected.

Known limitation, carried over from the script it replaces: Veeam reports one
result per *session*, and a session can protect several VMs. Every machine in a
session therefore inherits that session's verdict, so one failed VM in a
five-VM job shows all five as failed. Per-object results would need the task-
session endpoints, which aren't in this API version's surface — until then a
Veeam failure is a pointer to the job, not proof about each guest.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from ..config import Settings
from ..ingest import ServerCache, upsert_event
from ..outcomes import normalize
from ..timeframes import utcnow
from .base import Collector

log = logging.getLogger(__name__)

PAGE_SIZE = 500
LOG_PAGE_SIZE = 500

# Fields on a log line that name the object being processed, cheapest first.
_NAME_FIELDS = ("objectName", "entityName", "vmName", "computerName", "objectDisplayName")

# Message patterns, in the order the PowerShell tried them.
_NAME_PATTERNS = [
    re.compile(r"Processing\s+(?:VM|computer|object|server)\s+'([^']+)'", re.I),
    re.compile(r"Processed\s+(?:VM|computer|object|server)\s+'([^']+)'", re.I),
    re.compile(r"'(.*?)'\s+processing\s+finished", re.I),
    re.compile(r"Job finished for (?:computer|object|server)\s+'([^']+)'", re.I),
    re.compile(r"Starting backup for (?:computer|server)\s+'([^']+)'", re.I),
    re.compile(r"Finished backup for (?:computer|server)\s+'([^']+)'", re.I),
    re.compile(r"Object\s+'([^']+)'", re.I),
    re.compile(r"(?:Guest|Machine)\s*[:=]\s*([A-Za-z0-9._-]+)", re.I),
]

# Fallbacks for single-machine jobs, where the job name is the only clue.
_JOB_PATTERNS = [
    re.compile(r"^\s*([A-Za-z0-9._-]+)\s+Backup(?:$|[\s_\-].*)", re.I),
    re.compile(r"^\s*Backup\s+([A-Za-z0-9._-]+)", re.I),
    re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$"),
]


def parse_dt(value: str | None) -> datetime | None:
    """Veeam returns ISO 8601 with an offset; normalize to naive UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def extract_names(log_items: list[dict], job_name: str | None) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        if value and value.strip() and value.strip() not in seen:
            seen.add(value.strip())
            names.append(value.strip())

    for item in log_items:
        for field in _NAME_FIELDS:
            add(item.get(field))
        message = item.get("message")
        if not message:
            continue
        for pattern in _NAME_PATTERNS:
            match = pattern.search(str(message))
            if match:
                add(match.group(1))
                break

    if not names and job_name:
        for pattern in _JOB_PATTERNS:
            match = pattern.match(job_name.strip())
            if match:
                add(match.group(1) if match.groups() else job_name.strip())
                break
    return names


def _is_backup_session(session_row: dict) -> bool:
    """Finished backup sessions only — configuration backups are not machines."""
    if not session_row.get("endTime"):
        return False
    kind = f"{session_row.get('subtype') or ''} {session_row.get('sessionType') or ''}"
    if "backup" not in kind.lower():
        return False
    name = str(session_row.get("name") or "")
    if re.search(r"configuration", kind, re.I) or re.search(r"\bconfiguration\b", name, re.I):
        return False
    return True


def _session_result(session_row: dict) -> str | None:
    result = session_row.get("result")
    if isinstance(result, dict):
        return result.get("result") or result.get("message")
    if isinstance(result, str) and result:
        return result
    return session_row.get("status")


class VeeamCollector(Collector):
    source = "veeam"
    display_name = "Veeam"

    def is_configured(self, settings: Settings) -> bool:
        return bool(
            settings.veeam_server_list() and settings.veeam_username and settings.veeam_password
        )

    def collect(self, session: Session, settings: Settings) -> int:
        cache = ServerCache(session, self.source)
        since = utcnow() - timedelta(hours=settings.veeam_lookback_hours)
        total = 0
        failures: list[str] = []

        for host in settings.veeam_server_list():
            try:
                total += self._collect_host(session, settings, cache, host, since)
            except Exception as exc:  # noqa: BLE001
                # One unreachable VBR server must not cost us the other's data.
                log.error("veeam host %s failed: %s", host, exc)
                failures.append(f"{host}: {exc}")

        session.commit()
        if failures and total == 0:
            raise RuntimeError("; ".join(failures))
        if failures:
            log.warning("veeam partial collection, failed hosts: %s", "; ".join(failures))
        return total

    def _collect_host(
        self,
        session: Session,
        settings: Settings,
        cache: ServerCache,
        host: str,
        since: datetime,
    ) -> int:
        base = f"https://{host.strip().replace('https://', '').replace('http://', '')}:{settings.veeam_port}"
        headers = {"Accept": "application/json", "x-api-version": settings.veeam_api_version}

        with httpx.Client(
            base_url=base, timeout=120, verify=settings.veeam_verify_tls, headers=headers
        ) as client:
            token_resp = client.post(
                "/api/oauth2/token",
                data={
                    "grant_type": "password",
                    "username": settings.veeam_username,
                    "password": settings.veeam_password,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            token_resp.raise_for_status()
            client.headers["Authorization"] = f"Bearer {token_resp.json()['access_token']}"

            count = 0
            skip = 0
            while True:
                resp = client.get(
                    "/api/v1/sessions",
                    params={
                        "orderColumn": "creationTime",
                        "orderAsc": "false",
                        "limit": PAGE_SIZE,
                        "skip": skip,
                    },
                )
                resp.raise_for_status()
                data = resp.json().get("data") or []
                if not data:
                    break

                reached_cutoff = False
                for row in data:
                    start = parse_dt(row.get("creationTime") or row.get("startTime"))
                    if start is None:
                        continue
                    # Newest-first, so the first row older than the window ends it.
                    if start < since:
                        reached_cutoff = True
                        break
                    if not _is_backup_session(row):
                        continue
                    end = parse_dt(row.get("endTime"))
                    session_id = row.get("id")
                    if end is None or not session_id:
                        continue
                    count += self._ingest_session(
                        session, cache, client, row, session_id, start, end
                    )

                if reached_cutoff or len(data) < PAGE_SIZE:
                    break
                skip += PAGE_SIZE
                session.commit()
            return count

    def _ingest_session(
        self,
        session: Session,
        cache: ServerCache,
        client: httpx.Client,
        row: dict,
        session_id: str,
        start: datetime,
        end: datetime,
    ) -> int:
        try:
            log_items = self._fetch_logs(client, session_id)
        except httpx.HTTPError as exc:
            log.warning("veeam logs failed for session %s: %s", session_id, exc)
            return 0

        job_name = row.get("name")
        names = extract_names(log_items, job_name)
        if not names:
            return 0

        raw = _session_result(row)
        outcome = normalize(self.source, raw)
        duration = max(0, int((end - start).total_seconds()))

        written = 0
        for name in names:
            server = cache.get(name)
            if server is None:
                continue
            upsert_event(
                session,
                cache,
                server=server,
                source=self.source,
                start_utc=start,
                end_utc=end,
                outcome=outcome,
                result_raw=str(raw) if raw else None,
                duration_sec=duration,
                job_name=job_name,
                native_id=str(session_id),
                details={"vbr_host": str(client.base_url.host), "session_id": str(session_id)},
            )
            written += 1
        return written

    def _fetch_logs(self, client: httpx.Client, session_id: str) -> list[dict]:
        items: list[dict] = []
        skip = 0
        while True:
            resp = client.get(
                f"/api/v1/sessions/{session_id}/logs",
                params={"limit": LOG_PAGE_SIZE, "skip": skip, "orderAsc": "true"},
            )
            resp.raise_for_status()
            page = resp.json().get("data") or []
            items.extend(page)
            if len(page) < LOG_PAGE_SIZE:
                return items
            skip += LOG_PAGE_SIZE
