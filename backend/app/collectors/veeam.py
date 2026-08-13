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
import socket
import ssl
from datetime import datetime, timedelta, timezone
from socket import timeout as socket_timeout

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


# One knob, the same shape as the PowerShell's
# `ServicePointManager.SecurityProtocol`: name a protocol and that is the only
# one offered. "auto" leaves OpenSSL's own range alone.
TLS_VERSIONS = {
    "1.0": ssl.TLSVersion.TLSv1,
    "1.1": ssl.TLSVersion.TLSv1_1,
    "1.2": ssl.TLSVersion.TLSv1_2,
    "1.3": ssl.TLSVersion.TLSv1_3,
}
# OpenSSL 3 will not even offer TLS 1.0/1.1 at its default security level, so
# asking for one of those implies dropping to 0.
_LEGACY = (ssl.TLSVersion.TLSv1, ssl.TLSVersion.TLSv1_1)


def tls_context(verify: bool, version: str = "1.2") -> ssl.SSLContext:
    """The TLS settings a Veeam appliance actually accepts.

    Python 3.11 links OpenSSL 3.x, whose defaults are stricter than the Windows
    TLS stack an older VBR server presents: it will not negotiate, and the
    server simply drops the connection. That surfaces as a bare socket reset —
    `[WinError 10054] An existing connection was forcibly closed by the remote
    host` — with nothing about certificates in it, which sends you looking in
    the wrong place entirely.

    `version` pins one protocol as both floor and ceiling, which is what
    `SecurityProtocol = Tls12` does in the PowerShell — it offers 1.2 and
    nothing else. Pinning only the floor leaves OpenSSL opening with a TLS 1.3
    ClientHello, which an old Schannel resets rather than negotiating down.

    Security level is the other half: SECLEVEL=2 (the OpenSSL 3 default)
    refuses the older suites and smaller DH parameters these appliances offer,
    and refuses TLS 1.0/1.1 outright. The loosening is scoped to the unverified
    case — the self-signed default — because it also permits weaker
    certificates, and someone who has put a trusted certificate on the
    appliance is asking for the opposite.

    Which version a given appliance needs is a question with an answer:
    `python -m app.cli probe veeam` tries them all and names the one that works.
    """
    context = ssl.create_default_context()
    pinned = TLS_VERSIONS.get(str(version).strip().lower())
    if pinned is not None:
        context.minimum_version = pinned
        context.maximum_version = pinned
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            context.set_ciphers(f"DEFAULT@SECLEVEL={0 if pinned in _LEGACY else 1}")
        except ssl.SSLError:  # a build without the legacy suites — nothing to loosen
            pass
    return context


def _probe_contexts(configured: str) -> list[tuple[str, ssl.SSLContext, str]]:
    """Every handshake worth trying, with the setting that would select it.

    TLS 1.0 and 1.1 are in here because a VBR server on Windows Server 2012 R2
    may have nothing newer enabled, and OpenSSL 3 refuses to so much as offer
    them — so the appliance sees a ClientHello with no protocol in common and
    hangs up, which is a reset that looks identical to every other reset.
    """
    candidates = [
        (f"TLS {version}", tls_context(False, version), f"VEEAM_TLS_VERSION={version}")
        for version in ("1.2", "1.1", "1.0", "1.3")
    ]
    candidates.append(
        ("OpenSSL defaults, unverified", tls_context(False, "auto"), "VEEAM_TLS_VERSION=auto")
    )
    candidates.append(
        (f"TLS {configured} with certificate verification", tls_context(True, configured),
         "VEEAM_VERIFY_TLS=true")
    )
    # Whatever is configured goes first, so the top line is the one the
    # collector will actually use.
    candidates.sort(key=lambda c: c[2] != f"VEEAM_TLS_VERSION={configured}")
    return candidates


def classify_handshake_failure(exc: BaseException) -> str:
    """Whether a failed handshake says anything about TLS.

    This is the distinction that matters, and it is carried entirely by the
    exception type:

    - `tls`   — the server sent a TLS alert. It read the ClientHello, disliked
                something in it, and said so. A version or cipher problem.
    - `cert`  — the handshake worked; the certificate was not trusted.
    - `reset` — the server sent no TLS bytes at all and dropped the connection.
                A protocol mismatch produces an alert, not a reset, so this is
                not a TLS problem: something is killing the connection.
    """
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "cert"
    # SSLEOFError is an SSLError, but it means "closed without saying anything",
    # which belongs with the resets rather than with the alerts.
    if isinstance(exc, ssl.SSLEOFError):
        return "reset"
    if isinstance(exc, ssl.SSLError):
        return "tls"
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, EOFError)):
        return "reset"
    if isinstance(exc, (TimeoutError, socket_timeout)):
        return "timeout"
    return "other"


def probe_host(
    host: str, port: int, configured: str = "1.2", timeout: float = 10.0
) -> dict:
    """Find out what a VBR appliance will actually negotiate.

    `[WinError 10054]` says only that the far end hung up; it does not say
    whether that was TLS, the wrong protocol version, the wrong port, or
    something that is not the REST API at all. This separates those: a plain
    socket first, then every candidate handshake, then — if one works — a
    question the REST API can answer without credentials, and if none works, a
    plain-HTTP request to find out whether the port is even speaking TLS.
    """
    host = host.strip().replace("https://", "").replace("http://", "")
    result: dict = {
        "host": host,
        "port": port,
        "tcp": None,
        "attempts": [],
        "http": None,
        "plain_http": None,
        "recommend": None,
    }

    try:
        with socket.create_connection((host, port), timeout=timeout):
            result["tcp"] = "ok"
    except Exception as exc:  # noqa: BLE001
        result["tcp"] = f"failed: {exc}"
        return result

    working: ssl.SSLContext | None = None
    for label, context, setting in _probe_contexts(configured):
        try:
            with socket.create_connection((host, port), timeout=timeout) as raw:
                with context.wrap_socket(raw, server_hostname=host) as tls:
                    result["attempts"].append(
                        {
                            "label": label,
                            "setting": setting,
                            "ok": True,
                            "detail": f"{tls.version()}  {tls.cipher()[0]}",
                        }
                    )
                    if working is None:
                        working = context
                        result["recommend"] = setting
        except Exception as exc:  # noqa: BLE001
            result["attempts"].append(
                {
                    "label": label,
                    "setting": setting,
                    "ok": False,
                    "kind": classify_handshake_failure(exc),
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )

    if working is not None:
        # A 401 here is a pass: it proves the REST service is the thing on the
        # other end, which a bare handshake does not.
        try:
            with httpx.Client(verify=working, timeout=timeout) as client:
                resp = client.get(f"https://{host}:{port}/api/v1/serverInfo")
                result["http"] = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            result["http"] = f"failed: {type(exc).__name__}: {exc}"
    else:
        # Nothing handshook. Either the port is not TLS at all, or it is a
        # service that is not this one — both worth knowing, and neither
        # distinguishable from "TLS is misconfigured" without asking.
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.get(f"http://{host}:{port}/api/v1/serverInfo")
                result["plain_http"] = (
                    f"HTTP {resp.status_code} over plain HTTP — this port is not TLS"
                )
        except Exception as exc:  # noqa: BLE001
            result["plain_http"] = f"not plain HTTP either ({type(exc).__name__})"
    return result


# Ports a Veeam installation might be answering on, and what each would mean.
KNOWN_PORTS = {
    9419: "VBR RESTful API (v11+) — what this collector wants",
    9398: "Enterprise Manager RESTful API (older deployments)",
    9392: "Enterprise Manager web UI",
    9393: "Enterprise Manager (secondary)",
    443: "HTTPS — a reverse proxy in front of one of the above",
}


def scan_ports(host: str, timeout: float = 4.0) -> list[dict]:
    """Which of Veeam's ports this host will talk to, and how far each gets.

    Worth having when a port both accepts connections and refuses to speak:
    the answer to "is the API somewhere else?" is a measurement, and the
    difference between *refused* (nothing listening) and *timed out* (a
    firewall dropping the packets) is itself diagnostic.
    """
    host = host.strip().replace("https://", "").replace("http://", "")
    rows: list[dict] = []
    for port, description in KNOWN_PORTS.items():
        row = {"port": port, "description": description, "tcp": None, "tls": None}
        try:
            with socket.create_connection((host, port), timeout=timeout):
                row["tcp"] = "open"
        except (TimeoutError, socket_timeout):
            row["tcp"] = "timed out (dropped — a firewall, not the host)"
            rows.append(row)
            continue
        except ConnectionRefusedError:
            row["tcp"] = "refused (nothing listening)"
            rows.append(row)
            continue
        except Exception as exc:  # noqa: BLE001
            row["tcp"] = f"{type(exc).__name__}"
            rows.append(row)
            continue

        context = tls_context(verify=False, version="auto")
        try:
            with socket.create_connection((host, port), timeout=timeout) as raw:
                with context.wrap_socket(raw, server_hostname=host) as tls:
                    row["tls"] = f"{tls.version()}  {tls.cipher()[0]}"
        except Exception as exc:  # noqa: BLE001
            row["tls"] = f"{classify_handshake_failure(exc)}: {type(exc).__name__}"
        rows.append(row)
    return rows


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
                hint = ""
                if "10054" in str(exc) or "forcibly closed" in str(exc).lower():
                    hint = (
                        " — the server hung up. That is usually TLS: it is currently "
                        f"offering only TLS {settings.veeam_tls_version}. Run "
                        "`python -m app.cli probe veeam`, which tries every protocol "
                        "version against port "
                        f"{settings.veeam_port} and names the one this appliance accepts."
                    )
                log.error("veeam host %s failed: %s%s", host, exc, hint)
                failures.append(f"{host}: {exc}{hint}")

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
            base_url=base,
            timeout=120,
            verify=tls_context(settings.veeam_verify_tls, settings.veeam_tls_version),
            headers=headers,
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
