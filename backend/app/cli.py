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
        "--hours", type=int, default=24, help="azure: window to survey (default 24)"
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
        return _probe_veeam() if args.target == "veeam" else _probe_azure(args.hours)

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


def _probe_veeam() -> int:
    """Answer 'why 10054?' with a measurement instead of a guess.

    The socket error says the far end hung up and nothing else — not whether it
    was TLS, the wrong port, or nothing listening. This walks the layers in
    order and prints what each one did.
    """
    from .collectors.veeam import probe_host

    # Its own INFO line would land in the middle of the report, out of order,
    # since each host is probed in full before anything is printed.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = get_settings()
    hosts = settings.veeam_server_list()
    if not hosts:
        print("VEEAM_SERVERS is empty in backend\\.env — nothing to probe")
        return 1

    worst = 0
    for host in hosts:
        print(f"\n=== {host}:{settings.veeam_port} ===")
        result = probe_host(host, settings.veeam_port)

        print(f"  TCP connect      {result['tcp']}")
        if result["tcp"] != "ok":
            print(
                "\n  Nothing is accepting connections on that port. Check the Veeam\n"
                "  RESTful API service is running and the firewall allows it:\n"
                f"    Test-NetConnection {host} -Port {settings.veeam_port}"
            )
            worst = 1
            continue

        print("  TLS handshakes:")
        for attempt in result["attempts"]:
            mark = "ok  " if attempt["ok"] else "FAIL"
            print(f"    [{mark}] {attempt['label']}")
            print(f"           {attempt['detail']}")

        if result["http"]:
            print(f"  REST service     {result['http']}")
            print("                   (401 is a pass — the API answered)")

        if not any(a["ok"] for a in result["attempts"]):
            print(
                "\n  The port accepts connections but no handshake completes, so this\n"
                "  is TLS. If even 'OpenSSL defaults' fails, the service on that port\n"
                "  may not be speaking TLS at all — check it is the REST API and not\n"
                "  something else."
            )
            worst = 1
        elif not result["attempts"][0]["ok"]:
            print(
                "\n  The collector's own settings failed but something else worked.\n"
                "  Send this output on — tls_context() needs to match the line that\n"
                "  succeeded."
            )
            worst = 1
    print()
    return worst


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

    print(
        "\nA 'Log' row is a transaction-log backup — every 15 minutes per database,\n"
        "which is what makes the numbers enormous. They are skipped on collection\n"
        "unless AZURE_INCLUDE_LOG_BACKUPS=true.\n"
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
