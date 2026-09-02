"""Command-line entry points:

  python -m app.cli init-db            create tables + seed source config
  python -m app.cli collect all        run every configured collector once
  python -m app.cli collect veeam      run one collector
  python -m app.cli collect legacy     backfill from BackupReporting.dbo.BackupEvents
  python -m app.cli refresh            rebuild every night's rollup from events
  python -m app.cli recompute          re-stamp report dates, then refresh
  python -m app.cli report [--date]    print a night's summary to the console
  python -m app.cli seed-demo          load demo data (for evaluating the UI)
  python -m app.cli purge <source>     delete one source's events
  python -m app.cli protect            encrypt a secret for .env (Windows DPAPI)
  python -m app.cli probe veeam        diagnose a Veeam connection (TCP/TLS/HTTP)
  python -m app.cli probe veeam --sessions [--find NAME]
                                       what the collector sees in the session list
  python -m app.cli notify [--print]   send the morning digest (or just show it)
"""
from __future__ import annotations

import argparse
import logging
import sys

from .collectors import ALL_COLLECTORS, run_collector
from .config import get_settings
from .db import init_db, session_factory


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="backupdashboard")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    collect = sub.add_parser("collect")
    collect.add_argument("source", choices=["all", *ALL_COLLECTORS.keys()])
    sub.add_parser("refresh")
    sub.add_parser("recompute")
    report_cmd = sub.add_parser("report")
    report_cmd.add_argument("--date", help="YYYY-MM-DD (default: last night)")
    sub.add_parser("seed-demo")
    purge = sub.add_parser("purge")
    purge.add_argument("source", choices=[*ALL_COLLECTORS.keys(), "all"])
    probe = sub.add_parser("probe")
    probe.add_argument("target", choices=["veeam", "azure"])
    probe.add_argument(
        "--hours",
        type=int,
        default=None,
        help="window to survey: azure defaults to 24, veeam --sessions to VEEAM_LOOKBACK_HOURS",
    )
    probe.add_argument(
        "--sessions",
        action="store_true",
        help="veeam: list the sessions in the window and the machines their logs name; stores nothing",
    )
    probe.add_argument(
        "--find",
        metavar="NAME",
        help="veeam: show every session name and log line that mentions NAME (implies --sessions)",
    )
    probe.add_argument(
        "--port",
        type=int,
        help="veeam: override the port (9419 is the VBR REST API, 9398 Enterprise Manager)",
    )
    notify_cmd = sub.add_parser("notify")
    notify_cmd.add_argument("--date", help="YYYY-MM-DD (default: last night)")
    notify_cmd.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="print the digest instead of sending it",
    )
    protect_cmd = sub.add_parser("protect")
    protect_cmd.add_argument(
        "--machine",
        action="store_true",
        help="machine scope: any account on THIS machine can decrypt (default: only the current user)",
    )
    protect_cmd.add_argument(
        "--show",
        action="store_true",
        help="visible input (paste-friendly; some Windows consoles can't paste into hidden prompts)",
    )
    args = parser.parse_args()

    # Neither of these touches the database — and probe in particular has to
    # work on a box where the database is the thing that isn't set up yet.
    if args.command == "protect":
        return _protect(args)
    if args.command == "probe":
        if args.target == "veeam":
            if args.sessions or args.find:
                return _survey_veeam(args.port, args.hours, args.find)
            return _probe_veeam(args.port)
        return _probe_azure(args.hours or 24)

    init_db()

    if args.command == "init-db":
        print("database initialized")
    elif args.command == "collect":
        return _collect(args)
    elif args.command == "refresh":
        from .rollups import refresh_all, refresh_server_summaries

        with session_factory()() as session:
            refresh_server_summaries(session)
            written = refresh_all(session)
        print(f"rebuilt {written} server-night rows")
    elif args.command == "recompute":
        return _recompute()
    elif args.command == "report":
        return _report(args.date)
    elif args.command == "seed-demo":
        from .demo_data import seed_demo

        seed_demo()
        print("demo data loaded")
    elif args.command == "purge":
        return _purge(args.source)
    elif args.command == "notify":
        return _notify(args)
    return 0


def _notify(args) -> int:
    """Send the morning digest by hand — or print it, which is how you check the
    wording and the recipients without mailing the estate."""
    from .notify import build_digest, send_digest

    settings = get_settings()
    with session_factory()() as session:
        digest = build_digest(session, settings, args.date)

    if args.print_only:
        print(f"\nSubject: {digest.subject}\n")
        print(digest.text)
        return 0

    if not settings.notify_configured():
        print(
            "SMTP_HOST and SMTP_TO are not both set in backend\\.env — nothing to send.\n"
            "Use --print to see what the digest would say."
        )
        return 1

    try:
        sent = send_digest(digest, settings)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not send: {type(exc).__name__}: {exc}")
        return 1
    if not sent:
        print("Nothing sent — clean night and NOTIFY_WHEN=problems.")
        return 0
    print(f"Sent to {', '.join(settings.notify_recipients())}: {digest.subject}")
    return 0


def _collect(args) -> int:
    settings = get_settings()
    if args.source == "all":
        # 'legacy' is a backfill source; `collect all` is the scheduled path and
        # must not re-walk the whole historical table every hour.
        targets = [
            c
            for name, c in ALL_COLLECTORS.items()
            if name != "legacy" and c.is_configured(settings)
        ]
    else:
        targets = [ALL_COLLECTORS[args.source]]
    if not targets:
        print("no collectors are configured — set source credentials in backend/.env")
        return 1
    failed = False
    for collector in targets:
        run = run_collector(collector)
        print(f"{collector.source}: {run.status} ({run.records} records) {run.message or ''}")
        failed = failed or run.status != "success"
    return 1 if failed else 0


def _recompute() -> int:
    """Re-stamp every event's report date, then rebuild the nights.

    Needed after changing a timezone default, the cutoff hour, or importing rows
    that were written before a server's timezone was corrected.
    """
    from .ingest import restamp_server
    from .models import Server
    from .rollups import refresh_all, refresh_server_summaries

    with session_factory()() as session:
        changed = 0
        for server in session.query(Server).all():
            changed += restamp_server(session, server)
        session.commit()
        refresh_server_summaries(session)
        written = refresh_all(session)
    print(f"re-stamped {changed} events, rebuilt {written} server-night rows")
    return 0


def _report(day: str | None) -> int:
    """Console version of the landing page — handy for a scheduled task email."""
    from .api import overview

    with session_factory()() as session:
        data = overview(date_param=day, session=session)

    counts = data["counts"]
    print(f"\nBackup report for {data['report_date']} ({data['display_timezone']})")
    print(
        f"  servers: {data['servers_total']}   protected: {data['servers_protected']}"
        f" ({data['protected_pct']}%)"
    )
    print(
        "  success={success} warning={warning} failed={failed} "
        "no-backup={missed} running={running} unknown={unknown}".format(**counts)
    )
    if not data["problems"]:
        print("\n  No problems. \n")
        return 0
    print(f"\n  {len(data['problems'])} need attention:")
    width = max(len(p["server"]) for p in data["problems"])
    for problem in data["problems"]:
        streak = f"  ({problem['streak']} nights)" if problem["streak"] > 1 else ""
        print(
            f"    {problem['outcome']:<8} {problem['server']:<{width}}  "
            f"{problem['source_name']:<12} {problem['result_raw'] or '—'}{streak}"
        )
    print()
    return 1


def _purge(source: str) -> int:
    from .models import BackupEvent, Server, ServerDay

    with session_factory()() as session:
        query = session.query(BackupEvent)
        days = session.query(ServerDay)
        if source != "all":
            query = query.filter(BackupEvent.source == source)
            days = days.filter(ServerDay.source == source)
        removed = query.delete(synchronize_session=False)
        days.delete(synchronize_session=False)
        # Servers that only existed because of this source go too.
        orphans = (
            session.query(Server)
            .filter(~Server.events.any())
            .delete(synchronize_session=False)
        )
        session.commit()
    print(f"purged {removed} events and {orphans} servers with no remaining history")
    return 0


def _probe_veeam(port: int | None = None) -> int:
    """Answer 'why 10054?' with a measurement instead of a guess.

    The socket error says the far end hung up and nothing else — not whether it
    was TLS, the wrong protocol version, the wrong port, or a service that is
    not this one. This walks the layers in order and prints what each did.
    """
    from .collectors.veeam import probe_host, scan_ports

    # Its own INFO line would land in the middle of the report, out of order,
    # since each host is probed in full before anything is printed.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = get_settings()
    hosts = settings.veeam_server_list()
    if not hosts:
        print("VEEAM_SERVERS is empty in backend\\.env — nothing to probe")
        return 1
    port = port or settings.veeam_port

    worst = 0
    for host in hosts:
        print(f"\n=== {host}:{port} ===")
        result = probe_host(host, port, settings.veeam_tls_version)

        print(f"  TCP connect      {result['tcp']}")
        if result["tcp"] != "ok":
            if "timed out" in result["tcp"]:
                # Dropped, not refused. A host with nothing on the port answers
                # immediately with a reset; silence is a firewall.
                print(
                    "\n  Timed out rather than refused — the packets are being dropped,\n"
                    "  which is a firewall rather than the host. A machine with nothing\n"
                    "  listening on a port refuses the connection straight away."
                )
            else:
                print(
                    "\n  Nothing is accepting connections there. Check the Veeam RESTful\n"
                    f"  API service is running:  Test-NetConnection {host} -Port {port}"
                )
            print(f"\n  Ports on {host}:")
            for row in scan_ports(host):
                print(f"    {row['port']:<6} {row['tcp']:<44} {row['description']}")
                if row["tls"]:
                    print(f"           TLS: {row['tls']}")
            worst = 1
            continue

        print("  TLS handshakes:")
        for attempt in result["attempts"]:
            mark = "ok  " if attempt["ok"] else "FAIL"
            print(f"    [{mark}] {attempt['label']:<38}  {attempt['setting']}")
            print(f"           {attempt['detail']}")

        if result["http"]:
            print(f"  REST service     {result['http']}")
            print("                   (401 is a pass — the API answered)")
        if result["plain_http"]:
            print(f"  plain HTTP       {result['plain_http']}")

        if not any(a["ok"] for a in result["attempts"]):
            worst = 1
            if all(a.get("kind") == "reset" for a in result["attempts"]):
                # The decisive detail, and it is in the exception type: a
                # protocol or cipher mismatch is answered with a TLS *alert*.
                # A reset means the server never sent a TLS byte at all.
                print(
                    "\n  >>> This is not a TLS problem.\n\n"
                    "  Every attempt was reset before the server sent a single TLS\n"
                    "  byte — a version or cipher mismatch answers with an alert, not\n"
                    "  a reset. It is not plain HTTP either. So something accepts the\n"
                    "  TCP connection and then kills it the moment data arrives.\n\n"
                    "  In order of likelihood:\n"
                    "   1. A firewall between this host and the appliance. Many\n"
                    "      complete the TCP handshake themselves and reset once real\n"
                    "      data arrives — which is exactly this shape. A port that\n"
                    "      TIMES OUT rather than being refused is more evidence of\n"
                    "      one: see the port scan below.\n"
                    "   2. The Veeam RESTful API service is not running, and\n"
                    "      something else in the path is completing the connection.\n"
                    "   3. The service is running but only accepts certain sources.\n\n"
                    "  The decisive test: run this same probe from the machine the\n"
                    "  PowerShell script runs on. Working there and not here makes it\n"
                    "  the network path, not the code — and the fix is to extend that\n"
                    "  host's firewall rule to this one."
                )
            else:
                print(
                    "\n  The port accepts connections but not one handshake completes,\n"
                    "  not even TLS 1.0. Whatever is listening is probably not the\n"
                    "  Veeam REST API — see the port scan below."
                )
            print(f"\n  Ports on {host}:")
            for row in scan_ports(host):
                print(f"    {row['port']:<6} {row['tcp']:<44} {row['description']}")
                if row["tls"]:
                    print(f"           TLS: {row['tls']}")
        elif not result["attempts"][0]["ok"]:
            print(
                f"\n  >>> Put this in backend\\.env:   {result['recommend']}\n"
                "      What the collector offers now was refused; that was accepted."
            )
            worst = 1
        else:
            print("\n  The collector's current TLS settings work against this host.")
    print()
    return worst


def _survey_veeam(port: int | None, hours: int | None, find: str | None) -> int:
    """Answer "why isn't machine X on the dashboard?" from the session list.

    The collector only ever knows a machine by name, and the only place the
    name appears is a session's log. This prints what each host's log actually
    said in the window — which session types it reported and which the
    collector reads, which machines were named, which sessions named none —
    and, with `--find`, every line that mentions the machine you are missing.
    Nothing is stored.
    """
    from .collectors.veeam import survey_sessions

    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    hosts = settings.veeam_server_list()
    if not hosts:
        print("VEEAM_SERVERS is empty in backend\\.env — nothing to survey")
        return 1
    if port:
        settings = settings.model_copy(update={"veeam_port": port})
    hours = hours or settings.veeam_lookback_hours

    worst = 0
    for host in hosts:
        print(f"\n=== {host}:{settings.veeam_port} — sessions of the last {hours}h ===")
        try:
            result = survey_sessions(settings, host, hours, find)
        except Exception as exc:  # noqa: BLE001
            print(f"  failed: {type(exc).__name__}: {exc}")
            print("  (connection problems: run `probe veeam` without --sessions)")
            worst = 1
            continue
        _print_survey(result)
        if result["dropped"] or (find and not result["hits"]):
            worst = 1
    print()
    return worst


def _print_survey(result: dict) -> None:
    types = result["types"]
    if not types:
        print("  no sessions at all in the window — the collector had nothing to read.")
        print("  A longer window: --hours 720")
        return

    print("  session types the host reported (kept = a finished backup session the collector reads):")
    for kind, tally in sorted(types.items(), key=lambda kv: -kv[1]["seen"]):
        print(f"    {kind:<34} {tally['seen']:>5} seen  {tally['kept']:>5} kept")

    machines = sorted(result["machines"])
    print(f"\n  machines the logs named ({len(machines)}):")
    if machines:
        line, lines = "", []
        for name in machines:
            if len(line) + len(name) + 2 > 92:
                lines.append(line)
                line = ""
            line += (", " if line else "") + name
        lines.append(line)
        for text in lines:
            print(f"    {text}")
    else:
        print("    (none — no kept session's log had a `Processing <name>` line)")

    kept = [s for s in result["sessions"] if s["skipped"] is None]
    print(f"\n  sessions the collector kept ({len(kept)}), newest first:")
    for s in kept[:60]:
        end = str(s["end"] or "")[:16].replace("T", " ")
        if s["log_error"]:
            filed = f"log unreadable: {s['log_error']}"
        elif s["via"] == "log":
            filed = ", ".join(s["names"])
        elif s["via"] == "job":
            filed = f"{s['names'][0]}  (from the JOB NAME — the log named no machine)"
        else:
            filed = "DROPPED — the log named no machine and the job name is not a hostname"
        print(f"    {end}  {s['type']:<16} {str(s['result'] or '?'):<8} {str(s['job'])!r:<34} -> {filed}")
    if len(kept) > 60:
        print(f"    … and {len(kept) - 60} more")

    if result["fallback"] or result["dropped"]:
        print("\n  sessions whose log named no machine:")
        for job, n in result["fallback"].most_common():
            print(f"    {job!r:<40} x{n:<3} filed under the job name")
        for job, n in result["dropped"].most_common():
            print(f"    {job!r:<40} x{n:<3} DROPPED")
        print(
            "  A job that protects several machines and is filed under its own name is\n"
            "  reporting none of them. Send the kept-session lines above on if the log\n"
            "  wording differs from `Processing <name>`."
        )

    find = result["find"]
    if not find:
        return
    hits = result["hits"]
    print(f"\n  --find {find!r}:")
    if not hits:
        print(
            f"    {find!r} does not appear in any session name, log title or description\n"
            f"    on this host in the last {result['hours']}h. Either its job has not run in that\n"
            "    window (--hours 720 looks back a month), it is protected by a different\n"
            "    VBR server than the ones in VEEAM_SERVERS, or its sessions are of a type\n"
            "    this API version does not list — Veeam Agent sessions (AgentBackup,\n"
            "    EndpointBackup) exist only in the 1.3 vocabulary; check the type list above."
        )
        return
    for h in hits:
        end = str(h["end"] or "")[:16].replace("T", " ")
        state = "kept" if h["skipped"] is None else f"SKIPPED: {h['skipped']}"
        print(f"    {end}  {h['type']:<16} {str(h['job'])!r:<34} [{state}]")
        print(f"        {h['field']}: {h['text']}")
    needle = find.strip().lower()
    named = any(needle in n.lower() for s in result["sessions"] for n in s["names"])
    if named:
        print(
            "\n  >>> The collector reads this machine. If it is still not on the dashboard,\n"
            "      check the Servers page with hidden servers included, then run\n"
            "      `python -m app.cli collect veeam` and `refresh`."
        )
    elif all(h["skipped"] for h in hits):
        print(
            "\n  >>> Every mention is in a session type the collector skips. Send this\n"
            "      output on: the session type needs adding to the collector."
        )
    else:
        print(
            "\n  >>> The machine is in a kept session but its log line did not match the\n"
            "      `Processing <name>` pattern. Send this output on — the exact wording\n"
            "      above is what the pattern needs."
        )


def _probe_azure(hours: int) -> int:
    """What is in the vaults, without storing any of it.

    Sixty servers should not produce tens of thousands of job records. This says
    which of the two reasons it is: jobs nobody counts as a nightly backup (a
    15-minute transaction log), or history from outside the window because ARM
    ignored the filter.
    """
    from .collectors.azure import survey

    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    if not (settings.azure_tenant_id and settings.azure_client_id):
        print("Azure is not configured in backend\\.env")
        return 1

    print(f"\nSurveying the last {hours}h. Nothing is written to the database.")
    for vault in survey(settings, hours):
        print(f"\n=== {vault['vault']} ===")
        if vault.get("error"):
            print(f"  unreadable: {vault['error']}")
            continue
        print(f"  subscription     {vault['subscription']}")
        print(f"  jobs returned    {vault['jobs']}")
        print(f"  distinct entities {vault['entities']}")
        if vault["outside_window"]:
            print(
                f"  outside window   {vault['outside_window']}"
                "   <- ARM ignored $filter; it is paging the whole history"
            )
        print("  by management type / backup type:")
        for kind, total in vault["kinds"].items():
            print(f"    {total:>8}  {kind}")
        print("  by operation:")
        for operation, total in vault["operations"].items():
            print(f"    {total:>8}  {operation}")

    print(
        "\nReading this:\n"
        "  'outside window'  ARM ignored the time filter and served the vault's whole\n"
        "                    retained history. Collection enforces the window itself\n"
        "                    and stops once it is past it, so this is wasted reading\n"
        "                    rather than wrong data.\n"
        "  an operation      other than Backup means the operation clause was ignored\n"
        "                    too, so none of the filter is being parsed.\n"
        "  a 'Log' row       transaction-log backups, every 15 minutes per database.\n"
        "                    Skipped on collection unless AZURE_INCLUDE_LOG_BACKUPS=true.\n"
    )
    return 0


def _protect(args) -> int:
    import getpass

    from .secrets import protect

    if args.show:
        secret = input("Secret to protect (visible): ")
    else:
        secret = getpass.getpass("Secret to protect (input hidden): ")
    if not secret:
        print("nothing entered")
        return 1
    if any(ord(c) < 32 for c in secret):
        print(
            "ERROR: the input contains control characters — this happens when pasting\n"
            "into a hidden prompt on some Windows consoles (Ctrl+V becomes \\x16).\n"
            "Re-run with:  python -m app.cli protect --show   (visible input, paste works)"
        )
        return 1
    token = protect(secret, machine_scope=args.machine)
    scope = "machine" if args.machine else f"user ({getpass.getuser()})"
    print(f"\nDPAPI-protected ({scope} scope). Put this in backend\\.env, e.g.:\n")
    print(f"VEEAM_PASSWORD={token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
