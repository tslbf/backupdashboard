"""Veeam Backup & Replication sessions via the REST API on port 9419.

Port of Pull-VeeamJobStatus.ps1. Same shape: OAuth2 password grant, page the
session list newest-first, keep finished backup sessions, then read each
session's log to find out which machines it actually protected.

The log is the only thing here that names a machine, so its shape matters — and
it is not what the PowerShell saw. `GET /api/v1/sessions/{id}/logs` answers
`{"totalRecords": n, "records": [...]}`, not the `{"data": [...]}` of the list
endpoints, and each record is `{id, status, startTime, updateTime, title,
description}` with the per-machine line being a *title* that reads exactly
`Processing winsrv100`. Reading the wrong key, the wrong field, or a pattern
that wants quotes does not error: the log is simply empty, every session falls
back to being filed under its *job* name, and a machine that shares a job with
others does not exist as far as the dashboard is concerned. That is how a
server added to Veeam went unreported for weeks.
`python -m app.cli probe veeam --sessions --find <name>` shows what the log
actually says about a machine.

Known limitation, carried over from the script it replaces: Veeam reports one
result per *session*, and a session can protect several VMs. Every machine in a
session therefore inherits that session's verdict, so one failed VM in a
five-VM job shows all five as failed. Per-object results live on
`/api/v1/sessions/{id}/taskSessions`, which the API grew in 1.2-rev1 and this
collector does not read yet — until then a Veeam failure is a pointer to the
job, not proof about each guest.
"""
from __future__ import annotations

import logging
import re
import socket
import ssl
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from socket import timeout as socket_timeout

import httpx
from sqlalchemy.orm import Session

from ..config import Settings
from ..ingest import ServerCache, normalize_name, upsert_event
from ..outcomes import normalize
from ..timeframes import utcnow
from .base import Collector

log = logging.getLogger(__name__)

PAGE_SIZE = 500


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


# Fields on a log record that would name the object outright. None of them
# exist on the VBR REST API's SessionLogRecordModel; they are kept so a record
# from another shape (Enterprise Manager, a hand-built fixture) is still read.
_NAME_FIELDS = ("objectName", "entityName", "vmName", "computerName", "objectDisplayName")

# Where a log record's text lives. The VBR REST API record is exactly
# {id, status, startTime, updateTime, title, description}, and the per-machine
# line is the *title*. `message` is what the Enterprise Manager API called it
# and what the PowerShell regexed — it is not on this API's records at all.
_TEXT_FIELDS = ("title", "description", "message")

# What a job session's log says about each machine it protects is exactly
#
#     Processing winsrv100
#
# — one record per object; no quotes, no "VM" or "computer", the name and
# nothing else. (The API reference's own example log: "Processing ubuntu88",
# "Processing winsrv100", "Processing dbserver01".) Every other record in the
# same log is about the job rather than a machine — "Job started at …",
# "Building list of machines to process", "VM size: 86 GB (48 GB used)",
# "All VMs have been queued for processing", "Load: Source 86% > Proxy 54% > …",
# "Primary bottleneck: Source", "Job finished with warning at …" — and none of
# them begins with "Processing".
_PROCESSING = re.compile(r"^\s*Processing\s+(.+?)\s*$", re.I)
# Words that can follow "Processing" without naming a machine. A summary line
# worded "Processing finished at …" must not create a server called FINISHED.
_NOT_A_NAME = re.compile(
    r"^(?:finished|started|completed|stopped|failed|skipped|of|for|the|has|is|was|"
    r"will|VMs?|machines?|objects?|items?|tasks?)\b",
    re.I,
)
# A failed object sometimes carries its reason on the same line.
_TRAILING_REASON = re.compile(r"\s+(?:Error|Warning|Details?)\s*:.*$", re.I)

# The wordier phrasings, in the order the PowerShell tried them. None of these
# matches the bare `Processing <name>` above — which is why, before that pattern
# existed, no session log ever yielded a machine.
_STRICT_PATTERNS = [
    re.compile(r"Processing\s+(?:VM|computer|object|server)\s+'([^']+)'", re.I),
    re.compile(r"Processed\s+(?:VM|computer|object|server)\s+'([^']+)'", re.I),
    re.compile(r"'(.*?)'\s+processing\s+finished", re.I),
    re.compile(r"Job finished for (?:computer|object|server)\s+'([^']+)'", re.I),
    re.compile(r"Starting backup for (?:computer|server)\s+'([^']+)'", re.I),
    re.compile(r"Finished backup for (?:computer|server)\s+'([^']+)'", re.I),
]
# Loose enough to misread an error's free text ("Object 'C:\…'"), so these are
# only tried on a record's title, never on its description.
_LOOSE_PATTERNS = [
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


def name_from_text(text: str, *, loose: bool = True) -> str | None:
    """The machine one log line is about, or None if the line is about the job."""
    match = _PROCESSING.match(text)
    # A quote in the remainder means one of the wordier phrasings below, which
    # know where the name ends; the bare form is the name and nothing else.
    if match and "'" not in match.group(1) and '"' not in match.group(1):
        candidate = _TRAILING_REASON.sub("", match.group(1)).strip()
        if candidate and not _NOT_A_NAME.match(candidate):
            return candidate
    for pattern in _STRICT_PATTERNS + (_LOOSE_PATTERNS if loose else []):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def names_from_log(log_items: list[dict]) -> list[str]:
    """Every machine a session's log names, in log order, deduplicated."""
    names: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        text = str(value).strip() if value is not None else ""
        if text and text not in seen:
            seen.add(text)
            names.append(text)

    for item in log_items:
        for fld in _NAME_FIELDS:
            add(item.get(fld))
        for fld in _TEXT_FIELDS:
            text = item.get(fld)
            if not text:
                continue
            found = name_from_text(str(text), loose=(fld != "description"))
            if found:
                add(found)
                break  # one record is about one object; its description adds nothing
    return names


def name_from_job(job_name: str | None) -> str | None:
    """A single-machine job named after its machine — the last resort."""
    if not job_name:
        return None
    for pattern in _JOB_PATTERNS:
        match = pattern.match(job_name.strip())
        if match:
            return match.group(1) if match.groups() else job_name.strip()
    return None


def session_machines(log_items: list[dict], job_name: str | None) -> tuple[list[str], str]:
    """The machines a session is filed under, and where they came from.

    Returns `(names, via)` with `via` one of `log` (the log named them — the
    normal case), `job` (the log named nothing and the job name looks like a
    hostname) or `none` (nothing to file it under; the session is dropped).
    """
    names = names_from_log(log_items)
    if names:
        return names, "log"
    fallback = name_from_job(job_name)
    if fallback:
        return [fallback], "job"
    return [], "none"


def extract_names(log_items: list[dict], job_name: str | None) -> list[str]:
    return session_machines(log_items, job_name)[0]


def skip_reason(session_row: dict) -> str | None:
    """Why the collector ignores a session, or None if it reads it.

    Finished backup sessions only. `sessionType` is an enum whose backup members
    all carry the word — BackupJob, AgentBackup, EndpointBackup, BackupCopyJob,
    PlatformBackupJob — and whose others do not (ReplicaJob, RestoreVm,
    SureBackup is the exception and is a test, not a backup, but it names no
    machine as `Processing …` so nothing is filed from it). ConfigurationBackup
    is the VBR server backing up its own configuration: not a machine.
    """
    if not session_row.get("endTime"):
        return "still running"
    kind = f"{session_row.get('subtype') or ''} {session_row.get('sessionType') or ''}"
    if "backup" not in kind.lower():
        return "not a backup session"
    name = str(session_row.get("name") or "")
    if re.search(r"configuration", kind, re.I) or re.search(r"\bconfiguration\b", name, re.I):
        return "configuration backup"
    return None


def _is_backup_session(session_row: dict) -> bool:
    return skip_reason(session_row) is None


def _session_result(session_row: dict) -> str | None:
    result = session_row.get("result")
    if isinstance(result, dict):
        return result.get("result") or result.get("message")
    if isinstance(result, str) and result:
        return result
    return session_row.get("status")


def _hostname(host: str) -> str:
    return host.strip().replace("https://", "").replace("http://", "")


@contextmanager
def open_client(
    settings: Settings, host: str, *, transport: httpx.BaseTransport | None = None
) -> Iterator[httpx.Client]:
    """An authenticated client for one VBR host, closed on exit.

    A context manager rather than a bare client because httpx will not `with` a
    client that has already sent a request, and the token request has.
    """
    client = httpx.Client(
        base_url=f"https://{_hostname(host)}:{settings.veeam_port}",
        timeout=120,
        verify=tls_context(settings.veeam_verify_tls, settings.veeam_tls_version),
        headers={"Accept": "application/json", "x-api-version": settings.veeam_api_version},
        transport=transport,
    )
    try:
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
        yield client
    finally:
        client.close()


def iter_sessions(client: httpx.Client, since: datetime) -> Iterator[tuple[dict, datetime]]:
    """Sessions newest-first back to `since`, as (row, start-in-UTC) pairs."""
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
            return
        for row in data:
            start = parse_dt(row.get("creationTime") or row.get("startTime"))
            if start is None:
                continue
            # Newest-first, so the first row older than the window ends it.
            if start < since:
                return
            yield row, start
        if len(data) < PAGE_SIZE:
            return
        skip += PAGE_SIZE


def fetch_session_log(client: httpx.Client, session_id: str) -> list[dict]:
    """Every log record of one session.

    The endpoint returns the whole log in one response, shaped
    `{"totalRecords": n, "records": [...]}` — not the `{"data": [...]}` the list
    endpoints use — and takes no skip/limit. Reading `data` here found nothing,
    silently, for every session: the log was always an empty list, so the only
    names the collector ever produced came from the job-name fallback, and a
    machine without a job named after it did not exist.
    """
    resp = client.get(f"/api/v1/sessions/{session_id}/logs")
    resp.raise_for_status()
    body = resp.json()
    records = body.get("records")
    if records is None:
        records = body.get("data") or []
    return list(records)


@dataclass
class HostStats:
    """What one host's collection did — the numbers that make silence visible."""

    sessions: int = 0
    events: int = 0
    machines: set[str] = field(default_factory=set)
    # Job names of sessions whose log named no machine, by what happened next.
    fallback: Counter = field(default_factory=Counter)  # filed under the job name
    dropped: Counter = field(default_factory=Counter)  # nothing to file under
    log_failures: int = 0


def survey_sessions(
    settings: Settings,
    host: str,
    hours: int,
    find: str | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """What the collector would make of one host's last `hours` of sessions,
    without storing any of it.

    Answers "why is machine X not on the dashboard?" with a measurement: which
    session types the host reported and which the collector reads, which
    machines the logs named, which sessions named none — and, with `find`,
    every session name and log line that mentions X, kept or skipped.
    """
    since = utcnow() - timedelta(hours=hours)
    needle = find.strip().lower() if find and find.strip() else None
    out: dict = {
        "host": _hostname(host),
        "hours": hours,
        "find": find,
        "types": {},
        "sessions": [],
        "machines": set(),
        "fallback": Counter(),
        "dropped": Counter(),
        "hits": [],
    }
    with open_client(settings, host, transport=transport) as client:
        for row, _start in iter_sessions(client, since):
            kind = str(row.get("sessionType") or "?")
            reason = skip_reason(row)
            job = row.get("name")
            tally = out["types"].setdefault(kind, {"seen": 0, "kept": 0})
            tally["seen"] += 1
            entry = {
                "end": row.get("endTime"),
                "type": kind,
                "job": job,
                "result": _session_result(row),
                "skipped": reason,
                "names": [],
                "via": None,
                "log_error": None,
            }
            if needle and job and needle in str(job).lower():
                out["hits"].append({**entry, "field": "session name", "text": str(job)})

            items: list[dict] = []
            if reason is None or needle:
                try:
                    items = fetch_session_log(client, str(row.get("id")))
                except httpx.HTTPError as exc:
                    entry["log_error"] = f"{type(exc).__name__}: {exc}"
            if needle:
                for item in items:
                    for fld in _TEXT_FIELDS:
                        text = item.get(fld)
                        if text and needle in str(text).lower():
                            out["hits"].append({**entry, "field": fld, "text": str(text)[:200]})
            if reason is None:
                tally["kept"] += 1
                names, via = session_machines(items, job)
                entry["names"], entry["via"] = names, via
                out["machines"].update(normalize_name(n) or n for n in names)
                if via == "job":
                    out["fallback"][str(job or "?")] += 1
                elif via == "none":
                    out["dropped"][str(job or "?")] += 1
            out["sessions"].append(entry)
    return out


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
        stats = HostStats()
        with open_client(settings, host) as client:
            for row, start in iter_sessions(client, since):
                if skip_reason(row) is not None:
                    continue
                end = parse_dt(row.get("endTime"))
                session_id = row.get("id")
                if end is None or not session_id:
                    continue
                stats.sessions += 1
                stats.events += self._ingest_session(
                    session, cache, client, row, session_id, start, end, stats
                )
                if stats.sessions % PAGE_SIZE == 0:
                    session.commit()

        log.info(
            "veeam %s: %s backup sessions since %s UTC named %s machines (%s events)",
            _hostname(host),
            stats.sessions,
            since.strftime("%Y-%m-%d %H:%M"),
            len(stats.machines),
            stats.events,
        )
        # A session whose log names no machine is the failure mode that hides a
        # server, and it is silent by nature — so it gets said out loud, once
        # per host, with the job names, and with the command that explains it.
        if stats.fallback:
            log.warning(
                "veeam %s: %s session(s) whose log named no machine were filed under "
                "the job name instead: %s. If that job protects more than one "
                "machine, those machines are not being reported — run "
                "`python -m app.cli probe veeam --sessions --find <machine>`.",
                _hostname(host),
                sum(stats.fallback.values()),
                _summarize(stats.fallback),
            )
        if stats.dropped:
            log.warning(
                "veeam %s: %s session(s) dropped — the log named no machine and the "
                "job name is not a hostname: %s. Run `python -m app.cli probe veeam "
                "--sessions` to see what those logs say.",
                _hostname(host),
                sum(stats.dropped.values()),
                _summarize(stats.dropped),
            )
        if stats.log_failures:
            log.warning(
                "veeam %s: the log of %s session(s) could not be read; those sessions "
                "were skipped",
                _hostname(host),
                stats.log_failures,
            )
        return stats.events

    def _ingest_session(
        self,
        session: Session,
        cache: ServerCache,
        client: httpx.Client,
        row: dict,
        session_id: str,
        start: datetime,
        end: datetime,
        stats: HostStats | None = None,
    ) -> int:
        stats = stats if stats is not None else HostStats()
        try:
            log_items = self._fetch_logs(client, session_id)
        except httpx.HTTPError as exc:
            log.warning("veeam logs failed for session %s: %s", session_id, exc)
            stats.log_failures += 1
            return 0

        job_name = row.get("name")
        names, via = session_machines(log_items, job_name)
        if via == "job":
            stats.fallback[str(job_name or "?")] += 1
        elif via == "none":
            stats.dropped[str(job_name or "?")] += 1
            return 0

        raw = _session_result(row)
        outcome = normalize(self.source, raw)
        duration = max(0, int((end - start).total_seconds()))

        written = 0
        for name in names:
            server = cache.get(name)
            if server is None:
                continue
            stats.machines.add(server.name)
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
        return fetch_session_log(client, session_id)


def _summarize(counter: Counter, limit: int = 8) -> str:
    parts = [f"{job!r} x{n}" for job, n in counter.most_common(limit)]
    if len(counter) > limit:
        parts.append(f"and {len(counter) - limit} more")
    return ", ".join(parts)
