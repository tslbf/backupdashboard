"""N-able Cove (Backup Manager) via the JSON-RPC API at api.backup.management.

Port of Pull-NABLEJobStatus.ps1, including its column codes:

    I18    device name
    D9F18  last session end   (epoch; D1F18 is the Files & Folders fallback)
    D9F12  last session length in seconds  (D1F12 fallback)
    D9F17  last session status code        (see outcomes.NABLE_STATUS_CODES)

These devices are the UK estate, so SourceConfig seeds this source's default
timezone as Europe/London — the reason report_date exists at all. The epochs
themselves are UTC and therefore unambiguous; the timezone only decides which
backup *night* a run is filed under.

Limitation worth knowing: EnumerateAccountStatistics reports each device's
*current* state, not a session history. So this collector can only ever observe
the latest run — history accumulates from the moment it starts polling (each new
session start time becomes a new event). Anything older has to come from the
legacy BackupReporting import.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from ..config import Settings
from ..ingest import ServerCache, upsert_event
from ..outcomes import UNKNOWN, nable_status_name, normalize
from .base import Collector

log = logging.getLogger(__name__)

COLUMNS = ["I18", "D9F18", "D9F17", "D9F12", "D1F12", "D1F18"]
RECORDS_PER_CALL = 5000


def from_epoch(value) -> datetime | None:
    """Cove hands back seconds or milliseconds depending on the field."""
    if value in (None, "", 0, "0"):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number >= 1_000_000_000_000:
        number //= 1000
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _as_int(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


class NableCollector(Collector):
    source = "nable"
    display_name = "N-able Cove"

    def is_configured(self, settings: Settings) -> bool:
        return bool(settings.nable_partner and settings.nable_username and settings.nable_password)

    def collect(self, session: Session, settings: Settings) -> int:
        with httpx.Client(timeout=120) as client:
            visa = self._login(client, settings)
            partner_id = self._resolve_partner_id(client, settings, visa)
            rows = self._enumerate(client, settings, visa, partner_id)

        cache = ServerCache(session, self.source)
        count = 0
        for values in rows:
            count += self._ingest(session, cache, values)
        session.commit()
        return count

    # --- API ---------------------------------------------------------------

    def _rpc(
        self, client: httpx.Client, settings: Settings, method: str, params: dict, visa: str | None
    ) -> dict:
        body: dict = {"jsonrpc": "2.0", "id": "jsonrpc", "method": method, "params": params}
        if visa:
            body["visa"] = visa
        resp = client.post(settings.nable_endpoint, json=body)
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("error"):
            error = payload["error"]
            message = error.get("message") if isinstance(error, dict) else error
            raise RuntimeError(f"Cove {method} failed: {message}")
        return payload

    def _login(self, client: httpx.Client, settings: Settings) -> str:
        payload = self._rpc(
            client,
            settings,
            "Login",
            {
                "partner": settings.nable_partner,
                "username": settings.nable_username,
                "password": settings.nable_password,
            },
            visa=None,
        )
        visa = payload.get("visa")
        if not visa:
            raise RuntimeError("Cove login returned no visa — check partner/username/password")
        return visa

    def _resolve_partner_id(self, client: httpx.Client, settings: Settings, visa: str) -> int:
        """Prefer the ID the API resolves for the partner name; a stale configured
        ID silently returns someone else's devices, or none."""
        try:
            payload = self._rpc(
                client, settings, "GetPartnerInfo", {"name": settings.nable_partner}, visa
            )
            resolved = (payload.get("result") or {}).get("result") or {}
            partner_id = _as_int(resolved.get("Id"))
            if partner_id:
                if settings.nable_partner_id and partner_id != settings.nable_partner_id:
                    log.info(
                        "cove: using resolved partner id %s (config says %s)",
                        partner_id,
                        settings.nable_partner_id,
                    )
                return partner_id
        except (httpx.HTTPError, RuntimeError) as exc:
            log.warning("cove GetPartnerInfo failed, falling back to configured id: %s", exc)
        if not settings.nable_partner_id:
            raise RuntimeError("Could not resolve a Cove partner id — set NABLE_PARTNER_ID")
        return settings.nable_partner_id

    def _enumerate(
        self, client: httpx.Client, settings: Settings, visa: str, partner_id: int
    ) -> list[dict]:
        """Page through device statistics, flattening both response shapes."""
        out: list[dict] = []
        start = 0
        while True:
            payload = self._rpc(
                client,
                settings,
                "EnumerateAccountStatistics",
                {
                    "query": {
                        "PartnerId": partner_id,
                        "SelectionMode": "Merged",
                        "StartRecordNumber": start,
                        "RecordsCount": RECORDS_PER_CALL,
                        "Columns": COLUMNS,
                    }
                },
                visa,
            )
            result = payload.get("result") or {}
            objects = result.get("result") or []
            raw_rows = result.get("data") or []

            page: list[dict] = []
            if objects and isinstance(objects, list) and isinstance(objects[0], dict):
                for row in objects:
                    values: dict = {}
                    settings_block = row.get("Settings")
                    for entry in settings_block if isinstance(settings_block, list) else [settings_block]:
                        if isinstance(entry, dict):
                            values.update(entry)
                    if values:
                        page.append(values)
            elif raw_rows:
                # Alternate shape: each row is a list of "CODE=value" strings.
                for row in raw_rows:
                    values = {}
                    for pair in row:
                        text = str(pair)
                        index = text.find("=")
                        if index > 0:
                            values[text[:index]] = text[index + 1 :]
                    if values:
                        page.append(values)

            out.extend(page)
            if len(page) < RECORDS_PER_CALL:
                return out
            start += RECORDS_PER_CALL

    # --- ingest ------------------------------------------------------------

    def _ingest(self, session: Session, cache: ServerCache, values: dict) -> int:
        server = cache.get(values.get("I18"))
        if server is None:
            return 0

        end_utc = from_epoch(values.get("D9F18")) or from_epoch(values.get("D1F18"))
        if end_utc is None:
            return 0

        duration = _as_int(values.get("D9F12")) or 0
        if duration <= 0:
            duration = _as_int(values.get("D1F12")) or 0
        duration = max(0, duration)

        code = _as_int(values.get("D9F17"))
        outcome = normalize(self.source, code) if code is not None else UNKNOWN

        upsert_event(
            session,
            cache,
            server=server,
            source=self.source,
            start_utc=end_utc - timedelta(seconds=duration),
            end_utc=end_utc,
            outcome=outcome,
            result_raw=nable_status_name(code),
            duration_sec=duration,
            job_name="Cove backup",
            details={"status_code": code},
        )
        return 1
