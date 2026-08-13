"""The morning digest.

The three PowerShell scripts this app replaces emailed a CSV. A dashboard only
works if somebody opens it, so without this the rewrite is a step backwards on
the one day you forget to look — which is exactly the day it matters.

Two decisions worth keeping:

**It sends on a clean night too, by default.** An email that only arrives when
something is wrong cannot be distinguished from an email system that has
stopped working, and "nothing arrived" is then indistinguishable from "nothing
is wrong". That is the same failure this app exists to catch, applied to
itself — so the default is a daily send, and a morning with no email in the
inbox is itself the signal. `NOTIFY_WHEN=problems` opts out.

**It is plain text first.** The body is built as text and the HTML part mirrors
it, because a digest read on a phone at 7am has to survive whatever the mail
client does to it. Nothing in the HTML depends on CSS support: no dark theme,
no web fonts, inline styles only.
"""
from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape

from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .outcomes import FAILED, MISSED, WARNING

log = logging.getLogger(__name__)

# Worst first, matching the dashboard's own ordering.
_LABELS = {FAILED: "FAILED", MISSED: "NO BACKUP", WARNING: "WARNING"}


@dataclass
class Digest:
    subject: str
    text: str
    html: str
    problem_count: int
    report_date: str

    @property
    def worth_sending(self) -> bool:
        return self.problem_count > 0


def build_digest(session: Session, settings: Settings | None = None, day: str | None = None) -> Digest:
    """Render the morning digest from exactly what the landing page shows.

    Built on `api.overview` rather than its own queries, so the email and the
    dashboard can never disagree about how many servers were protected — which
    is the kind of discrepancy that destroys trust in both.
    """
    from .api import overview

    settings = settings or get_settings()
    data = overview(date_param=day, session=session)

    protected = data["servers_protected"]
    total = data["servers_total"]
    pct = data["protected_pct"]
    problems = data["problems"]
    stale = data["attention"]["stale_success"]
    never = data["attention"]["never_succeeded"]

    if problems:
        headline = f"{len(problems)} need{'s' if len(problems) == 1 else ''} attention"
    else:
        headline = "all clear"
    subject = f"{settings.notify_subject_prefix} {data['report_date']} — {headline}"

    # --- text ---------------------------------------------------------------
    lines = [
        f"{protected} of {total} servers protected" + (f" ({pct}%)" if pct is not None else ""),
        f"Backup night of {data['report_date']}, noon to noon in each server's own timezone.",
        "",
    ]
    if problems:
        width = max(len(p["server"]) for p in problems)
        lines.append(f"NEEDS ATTENTION ({len(problems)})")
        for problem in problems:
            note = problem["result_raw"] or "no run recorded"
            streak = f"  ({problem['streak']} nights)" if problem["streak"] > 1 else ""
            lines.append(
                f"  {_LABELS.get(problem['outcome'], problem['outcome'].upper()):<9} "
                f"{problem['server']:<{width}}  {problem['source_name']:<13} {note}{streak}"
            )
    else:
        lines.append("Every expected backup completed.")
    lines.append("")

    if never:
        lines.append(f"NEVER HAD A GOOD BACKUP ({len(never)})")
        lines += [f"  {row['server']}" for row in never[:10]]
        lines.append("")
    if stale:
        lines.append(f"NO RECENT RESTORE POINT ({len(stale)})")
        lines += [f"  {row['server']:<24} last good {row['days']} days ago" for row in stale[:10]]
        lines.append("")

    lines.append(f"Counts: {_counts_line(data['counts'])}")
    lines.append("")
    lines.append(f"Full dashboard: {settings.public_url()}/")

    return Digest(
        subject=subject,
        text="\n".join(lines),
        html=_html(data, problems, never, stale, settings.public_url()),
        problem_count=len(problems),
        report_date=data["report_date"],
    )


def _counts_line(counts: dict) -> str:
    return "  ".join(f"{key}={value}" for key, value in counts.items() if value)


def _html(data: dict, problems: list, never: list, stale: list, url: str) -> str:
    """Deliberately plain. Inline styles, a light ground, no fonts to fetch —
    a mail client is not a browser and half of them will strip anything else."""
    protected = f"{data['servers_protected']} of {data['servers_total']}"
    pct = f" ({data['protected_pct']}%)" if data["protected_pct"] is not None else ""
    tone = "#1f7a34" if not problems else "#111111"

    rows = "".join(
        "<tr>"
        f'<td style="padding:6px 14px 6px 0;white-space:nowrap;color:{_colour(p["outcome"])};'
        f'font-weight:600">{escape(_LABELS.get(p["outcome"], p["outcome"]))}</td>'
        f'<td style="padding:6px 14px 6px 0;font-weight:600">{escape(p["server"])}</td>'
        f'<td style="padding:6px 14px 6px 0;color:#555">{escape(p["source_name"])}</td>'
        f'<td style="padding:6px 0;color:#555">{escape(str(p["result_raw"] or "no run recorded"))}'
        + (f' · {p["streak"]} nights' if p["streak"] > 1 else "")
        + "</td></tr>"
        for p in problems
    )

    body = [
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'font-size:15px;color:#111;line-height:1.5">',
        f'<p style="font-size:26px;margin:0 0 2px;color:{tone}"><b>{protected}</b> '
        f'<span style="color:#666;font-size:18px">servers protected{pct}</span></p>',
        f'<p style="margin:0 0 20px;color:#666;font-size:13px">Backup night of '
        f'{escape(data["report_date"])} — noon to noon in each server\'s own timezone.</p>',
    ]
    if problems:
        body.append(
            f'<h3 style="margin:0 0 6px;font-size:14px;text-transform:uppercase;'
            f'letter-spacing:.06em;color:#666">Needs attention ({len(problems)})</h3>'
            f'<table cellpadding="0" cellspacing="0" style="font-size:14px;margin-bottom:22px">'
            f"{rows}</table>"
        )
    else:
        body.append(
            '<p style="margin:0 0 22px;color:#1f7a34;font-weight:600">'
            "● Every expected backup completed.</p>"
        )

    body.append(_list_block("Never had a good backup", [r["server"] for r in never]))
    body.append(
        _list_block(
            "No recent restore point",
            [f'{r["server"]} — last good {r["days"]} days ago' for r in stale],
        )
    )
    body.append(
        f'<p style="margin:24px 0 0;font-size:13px">'
        f'<a href="{escape(url)}/" style="color:#0b5fa5">Open the dashboard</a>'
        '<span style="color:#888"> · Backup Status, LB Foster Infrastructure</span></p></div>'
    )
    return "".join(body)


def _list_block(title: str, items: list[str]) -> str:
    if not items:
        return ""
    shown = "".join(f'<li style="margin:2px 0">{escape(i)}</li>' for i in items[:10])
    more = (
        f'<li style="margin:2px 0;color:#888">+{len(items) - 10} more</li>'
        if len(items) > 10
        else ""
    )
    return (
        f'<h3 style="margin:0 0 6px;font-size:14px;text-transform:uppercase;'
        f'letter-spacing:.06em;color:#666">{escape(title)} ({len(items)})</h3>'
        f'<ul style="margin:0 0 22px;padding-left:20px;font-size:14px">{shown}{more}</ul>'
    )


def _colour(outcome: str) -> str:
    """Print-safe status colours, not the dashboard's dark-theme ones — these
    are read on a white ground."""
    return {FAILED: "#c0271b", MISSED: "#b4472c", WARNING: "#8a6100"}.get(outcome, "#111111")


def send_digest(digest: Digest, settings: Settings | None = None) -> bool:
    """Returns True if a message was actually handed to the relay."""
    settings = settings or get_settings()
    if not settings.notify_configured():
        log.info("digest not sent: SMTP_HOST or SMTP_TO is empty")
        return False
    if settings.notify_when == "problems" and not digest.worth_sending:
        log.info("digest not sent: clean night and NOTIFY_WHEN=problems")
        return False

    message = EmailMessage()
    message["Subject"] = digest.subject
    message["From"] = settings.notify_sender()
    message["To"] = ", ".join(settings.notify_recipients())
    message.set_content(digest.text)
    message.add_alternative(digest.html, subtype="html")

    if settings.smtp_ssl:
        server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
    try:
        if settings.smtp_starttls and not settings.smtp_ssl:
            server.starttls()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(message)
    finally:
        server.quit()

    log.info(
        "digest sent to %s — %s", ", ".join(settings.notify_recipients()), digest.subject
    )
    return True


def send_morning_digest(day: str | None = None) -> bool:
    """The scheduled entry point. Never raises: a mail relay being down must not
    take the scheduler with it, and the data is already collected by this point."""
    from .db import session_factory

    settings = get_settings()
    if not settings.notify_configured():
        return False
    try:
        with session_factory()() as session:
            return send_digest(build_digest(session, settings, day), settings)
    except Exception as exc:  # noqa: BLE001
        log.error("morning digest failed to send: %s", exc)
        return False
