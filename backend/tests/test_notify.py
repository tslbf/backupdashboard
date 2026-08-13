"""The morning digest, and the summary another dashboard embeds.

The scripts this app replaced emailed a CSV. A dashboard only works if somebody
opens it, so the digest is not a nice-to-have — it is the capability the rewrite
would otherwise have removed.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.api import SUMMARY_VERSION, summary
from app.config import Settings
from app.notify import build_digest, send_digest
from app.outcomes import FAILED, MISSED, SUCCESS
from app.rollups import refresh_days, refresh_server_summaries
from app.timeframes import current_report_date

from test_rollups import ET, UK, add_event

TODAY = current_report_date("America/New_York")
NIGHTS = [(TODAY - timedelta(days=n)).isoformat() for n in range(6, -1, -1)]


def settings(**overrides) -> Settings:
    base = {
        "smtp_host": "smtp.lbfosterco.com",
        "smtp_to": "tscott@lbfoster.com",
        "display_timezone": "America/New_York",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def estate(session):
    for day in NIGHTS:
        add_event(session, "PGHSQL01", day, SUCCESS)
        add_event(session, "PGHERP02", day, FAILED)
        add_event(session, "LONFILE01", day, SUCCESS, source="nable", tz=UK, hour=23)
    for day in NIGHTS[:4]:
        add_event(session, "BEDAPP01", day, SUCCESS)  # then stops — a "No backup"
    refresh_server_summaries(session)
    refresh_days(session, NIGHTS)
    return session


@pytest.fixture
def clean(session):
    for day in NIGHTS:
        add_event(session, "PGHSQL01", day, SUCCESS)
        add_event(session, "LONFILE01", day, SUCCESS, source="nable", tz=UK, hour=23)
    refresh_server_summaries(session)
    refresh_days(session, NIGHTS)
    return session


class TestDigestContent:
    def test_the_subject_says_what_happened(self, estate):
        digest = build_digest(estate, settings())

        assert digest.report_date == TODAY.isoformat()
        assert "need" in digest.subject
        assert TODAY.isoformat() in digest.subject

    def test_a_clean_night_says_so_in_the_subject(self, clean):
        """Read on a phone, the subject is often the whole message."""
        assert "all clear" in build_digest(clean, settings()).subject

    def test_it_leads_with_what_is_protected(self, estate):
        """Same decision as the landing page: a list that opens with failures
        reads as a broken estate even when 98% of it is fine."""
        first = build_digest(estate, settings()).text.splitlines()[0]
        assert "servers protected" in first

    def test_every_problem_is_named(self, estate):
        text = build_digest(estate, settings()).text

        assert "PGHERP02" in text, "the failing server"
        assert "BEDAPP01" in text, "the one that stopped running"
        assert "FAILED" in text and "NO BACKUP" in text

    def test_a_streak_is_carried(self, estate):
        """One bad night and seven in a row are different problems."""
        assert "7 nights" in build_digest(estate, settings()).text

    def test_the_numbers_match_the_dashboard(self, estate):
        """Built on `overview` for exactly this reason — an email that disagrees
        with the page destroys trust in both."""
        from app.api import overview

        data = overview(date_param=None, session=estate)
        text = build_digest(estate, settings()).text

        assert f"{data['servers_protected']} of {data['servers_total']}" in text

    def test_the_html_part_carries_the_same_facts(self, estate):
        digest = build_digest(estate, settings())

        assert "PGHERP02" in digest.html
        assert "servers protected" in digest.html
        # No dark theme and nothing to fetch: a mail client is not a browser.
        assert "var(--" not in digest.html
        assert "<link" not in digest.html and "@media" not in digest.html

    def test_html_escapes_whatever_the_vendor_said(self, session):
        """`result_raw` and the server name are both vendor strings, and they
        reach an inbox — neither is trusted to be free of markup."""
        from app.models import BackupEvent, Server

        add_event(session, "ODDSRV", NIGHTS[-1], FAILED)
        event = session.query(BackupEvent).one()
        event.result_raw = "<b>boom</b>"
        session.query(Server).one().name = "ODD<SRV>"
        session.commit()
        refresh_server_summaries(session)
        refresh_days(session, NIGHTS)

        html = build_digest(session, settings()).html

        assert "<b>boom</b>" not in html
        assert "&lt;b&gt;boom&lt;/b&gt;" in html
        assert "ODD<SRV>" not in html
        assert "ODD&lt;SRV&gt;" in html


class TestWhenItSends:
    def _sent(self, digest, monkeypatch, cfg) -> bool:
        """Send without a relay: the SMTP class is replaced, so what is asserted
        is the decision to send, not smtplib."""
        calls = []

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                calls.append(("connect", host, port))

            def starttls(self):
                calls.append(("starttls",))

            def login(self, user, password):
                calls.append(("login", user))

            def send_message(self, message):
                calls.append(("send", message["Subject"], message["To"]))

            def quit(self):
                pass

        monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
        monkeypatch.setattr("smtplib.SMTP_SSL", FakeSMTP)
        result = send_digest(digest, cfg)
        return result, calls

    def test_a_clean_night_still_sends_by_default(self, clean, monkeypatch):
        """The email is a heartbeat as well as a report. One that only arrives
        when something is wrong cannot be told apart from a mail system that has
        quietly died — which is this app's own failure mode, applied to itself."""
        sent, calls = self._sent(build_digest(clean, settings()), monkeypatch, settings())

        assert sent is True
        assert any(c[0] == "send" for c in calls)

    def test_problems_only_stays_quiet_on_a_clean_night(self, clean, monkeypatch):
        cfg = settings(notify_when="problems")
        sent, calls = self._sent(build_digest(clean, cfg), monkeypatch, cfg)

        assert sent is False
        assert not any(c[0] == "send" for c in calls)

    def test_problems_only_still_sends_when_there_are_problems(self, estate, monkeypatch):
        cfg = settings(notify_when="problems")
        sent, _ = self._sent(build_digest(estate, cfg), monkeypatch, cfg)

        assert sent is True

    def test_nothing_is_sent_without_a_relay(self, estate, monkeypatch):
        cfg = settings(smtp_host="")
        sent, calls = self._sent(build_digest(estate, cfg), monkeypatch, cfg)

        assert sent is False
        assert calls == []

    def test_nothing_is_sent_without_a_recipient(self, estate, monkeypatch):
        cfg = settings(smtp_to="")
        sent, _ = self._sent(build_digest(estate, cfg), monkeypatch, cfg)
        assert sent is False

    def test_login_is_skipped_when_there_is_no_user(self, estate, monkeypatch):
        """An internal relay that accepts unauthenticated mail from the app
        server is the common case here."""
        _, calls = self._sent(build_digest(estate, settings()), monkeypatch, settings())
        assert not any(c[0] == "login" for c in calls)

    def test_starttls_is_skipped_for_implicit_tls(self, estate, monkeypatch):
        """Port 465 is TLS from the first byte; calling STARTTLS on it fails."""
        cfg = settings(smtp_ssl=True, smtp_port=465)
        _, calls = self._sent(build_digest(estate, cfg), monkeypatch, cfg)

        assert not any(c[0] == "starttls" for c in calls)


class TestRecipients:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("a@x.com", ["a@x.com"]),
            ("a@x.com,b@x.com", ["a@x.com", "b@x.com"]),
            ("a@x.com; b@x.com", ["a@x.com", "b@x.com"]),
            (" a@x.com , ", ["a@x.com"]),
            ("", []),
        ],
    )
    def test_parsing(self, raw, expected):
        assert settings(smtp_to=raw).notify_recipients() == expected

    def test_the_sender_falls_back_to_the_first_recipient(self):
        """Some relays reject an empty From, and a digest arriving from yourself
        beats one that does not arrive."""
        assert settings(smtp_from="").notify_sender() == "tscott@lbfoster.com"

    def test_an_explicit_sender_wins(self):
        assert settings(smtp_from="backups@lbfoster.com").notify_sender() == "backups@lbfoster.com"


class TestSummaryEndpoint:
    """The payload the asset dashboard embeds. It is a contract with a codebase
    this repo cannot see, so the shape is pinned here."""

    def test_it_carries_a_version(self, estate):
        assert summary(session=estate)["version"] == SUMMARY_VERSION

    def test_the_headline_is_already_worded(self, estate):
        """So two dashboards cannot phrase the same fact differently."""
        data = summary(session=estate)
        assert data["headline"] == f"{data['servers_protected']} of {data['servers_total']} protected"

    def test_it_reports_what_needs_attention(self, estate):
        data = summary(session=estate)

        assert data["needs_attention"] == 2
        assert data["worst_outcome"] == FAILED
        assert {p["server"] for p in data["problems"]} == {"PGHERP02", "BEDAPP01"}

    def test_a_clean_night_reports_success(self, clean):
        data = summary(session=clean)

        assert data["needs_attention"] == 0
        assert data["worst_outcome"] == SUCCESS
        assert data["problems"] == []

    def test_the_problem_list_is_capped(self, session):
        """A tile has room for a few rows; a caller wanting everything links
        through rather than pulling the estate over the wire."""
        for n in range(12):
            add_event(session, f"BROKEN{n:02d}", NIGHTS[-1], FAILED)
        refresh_server_summaries(session)
        refresh_days(session, NIGHTS)

        data = summary(session=session)

        assert data["needs_attention"] == 12
        assert len(data["problems"]) == 5

    def test_collector_health_travels_with_the_numbers(self, estate):
        """Stale data is worse than no data on a tile someone else owns — the
        caller has to be able to say the number came from a failed collector."""
        data = summary(session=estate)

        assert {c["source"] for c in data["collectors"]} >= {"veeam", "nable", "azure"}
        assert all("status" in c and "configured" in c for c in data["collectors"])

    def test_every_value_is_json_safe(self, estate):
        """It crosses a process boundary into another app."""
        import json

        json.dumps(summary(session=estate))


class TestSummaryHasNoUnknownOutcome:
    def test_an_empty_estate_does_not_claim_success(self, session):
        """Nothing collected is not the same as everything succeeded — that
        distinction is the whole premise of this app."""
        data = summary(session=session)

        assert data["servers_total"] == 0
        assert data["worst_outcome"] == "unknown"
