"""Knowing when a collector has broken.

A broken Veeam collector does not look broken: Veeam has real history, so every
Veeam server simply reads "No backup", which reads as forty backup problems
rather than one credentials problem. And a run where one of two VBR hosts was
down used to be recorded as "success", which is the one status nobody checks.
"""
from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from app.collectors import Collector, PartialCollection, run_collector
from app.collectors.veeam import VeeamCollector, failure_hint
from app.config import Settings
from app.health import collector_problems
from app.models import CollectorRun
from app.notify import build_digest
from app.timeframes import utcnow


def cfg(**overrides) -> Settings:
    base = {
        "veeam_servers": "vbr1,vbr2",
        "veeam_username": "svc",
        "veeam_password": "pw",
        "collect_time": "08:00",
        "smtp_host": "smtp.example",
        "smtp_to": "ops@example.com",
        "display_timezone": "America/New_York",
    }
    base.update(overrides)
    return Settings(**base)


class Fake(Collector):
    source = "veeam"
    display_name = "Veeam"

    def __init__(self, behaviour):
        self.behaviour = behaviour

    def is_configured(self, settings):
        return True

    def collect(self, session, settings):
        return self.behaviour()


@pytest.fixture
def alerts(monkeypatch):
    sent: list[tuple] = []
    settings = cfg()
    monkeypatch.setattr("app.collectors.base.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.notify.send_collector_alert", lambda *args: sent.append(args) or True
    )
    return sent


def boom():
    raise RuntimeError("401 Unauthorized")


def partial():
    raise PartialCollection(12, "vbr2: ConnectTimeout")


class TestRunStatus:
    def test_partial_is_recorded_as_partial_not_success(self, session, alerts):
        run = run_collector(Fake(partial))
        assert run.status == "partial"
        assert run.records == 12
        assert "vbr2" in run.message

    def test_an_alert_goes_out_when_a_source_starts_failing(self, session, alerts):
        run_collector(Fake(lambda: 5))
        run_collector(Fake(boom))
        assert len(alerts) == 1
        assert alerts[0][0] == "Veeam" and alerts[0][1] == "error"

    def test_only_once_while_it_stays_broken(self, session, alerts):
        """Cove polls every four hours; an email per poll trains you to filter them."""
        for _ in range(4):
            run_collector(Fake(boom))
        assert len(alerts) == 1

    def test_it_alerts_again_after_recovering_and_breaking_again(self, session, alerts):
        run_collector(Fake(boom))
        run_collector(Fake(lambda: 5))
        run_collector(Fake(partial))
        assert len(alerts) == 2

    def test_the_morning_run_leaves_it_to_the_digest(self, session, alerts):
        run_collector(Fake(boom), alert=False)
        assert alerts == []


def add_run(session, status, hours_ago=1, records=10, message=None):
    session.add(
        CollectorRun(
            source="veeam",
            started_at=utcnow() - timedelta(hours=hours_ago),
            finished_at=utcnow() - timedelta(hours=hours_ago),
            status=status,
            records=records,
            message=message,
        )
    )
    session.commit()


class TestHealth:
    def kinds(self, session, **settings):
        return [(p.source, p.kind) for p in collector_problems(session, cfg(**settings))]

    def test_healthy(self, session):
        add_run(session, "success")
        assert self.kinds(session) == []

    def test_a_failed_last_run(self, session):
        add_run(session, "success", hours_ago=30)
        add_run(session, "error", hours_ago=5, message="401")
        add_run(session, "error", hours_ago=1, message="401")
        [problem] = collector_problems(session, cfg())
        assert problem.kind == "error"
        # Since the *first* failure after the last success — how long it has been broken.
        assert utcnow() - problem.since > timedelta(hours=4)

    def test_partial(self, session):
        add_run(session, "partial", message="vbr2 down")
        assert self.kinds(session) == [("veeam", "partial")]

    def test_stale_catches_a_scheduler_that_stopped(self, session):
        add_run(session, "success", hours_ago=50)
        assert self.kinds(session) == [("veeam", "stale")]

    def test_success_with_nothing_in_it_is_suspicious(self, session):
        add_run(session, "success", records=0)
        assert self.kinds(session) == [("veeam", "empty")]

    def test_unconfigured_sources_are_not_judged(self, session):
        add_run(session, "error")
        assert self.kinds(session, veeam_servers="") == []

    def test_manual_only_sources_are_not_judged_for_staleness(self, session):
        add_run(session, "success", hours_ago=500)
        assert self.kinds(session, collect_time="") == []


class TestDigest:
    def test_the_subject_leads_with_the_broken_collector(self, session):
        add_run(session, "error", message="HTTPStatusError: 401 — Veeam rejected the login")
        digest = build_digest(session, cfg())
        assert "Veeam collector is failing" in digest.subject
        assert "COLLECTOR PROBLEMS" in digest.text
        assert "rejected the login" in digest.text
        assert "Collector problems" in digest.html

    def test_problems_only_mode_still_sends_for_a_broken_collector(self, session):
        add_run(session, "error", message="x")
        assert build_digest(session, cfg(notify_when="problems")).worth_sending

    def test_a_healthy_collector_adds_nothing(self, session):
        add_run(session, "success")
        digest = build_digest(session, cfg())
        assert "COLLECTOR" not in digest.text
        assert "collector" not in digest.subject


class TestVeeamPartialHosts:
    def test_one_dead_host_is_partial_not_success(self, session, monkeypatch):
        collector = VeeamCollector()

        def per_host(self, session, settings, cache, host, since):
            if host == "vbr2":
                raise httpx.ConnectTimeout("timed out")
            return 7

        monkeypatch.setattr(VeeamCollector, "_collect_host", per_host)
        with pytest.raises(PartialCollection) as caught:
            collector.collect(session, cfg())
        assert caught.value.records == 7
        assert "vbr2" in str(caught.value)

    def test_every_host_dead_is_an_error(self, session, monkeypatch):
        def per_host(self, *args):
            raise httpx.ConnectTimeout("timed out")

        monkeypatch.setattr(VeeamCollector, "_collect_host", per_host)
        with pytest.raises(RuntimeError):
            VeeamCollector().collect(session, cfg())


class TestFailureHints:
    def status_error(self, status, path="/api/v1/sessions", body=""):
        request = httpx.Request("POST", f"https://vbr1:9419{path}")
        response = httpx.Response(status, request=request, text=body)
        return httpx.HTTPStatusError("x", request=request, response=response)

    def test_a_rejected_login(self):
        assert "rejected the login" in failure_hint(
            self.status_error(401, "/api/oauth2/token"), cfg()
        )

    def test_missing_role(self):
        assert "Backup Viewer" in failure_hint(self.status_error(403), cfg())

    def test_api_version(self):
        hint = failure_hint(self.status_error(400, body="Unsupported x-api-version"), cfg())
        assert "VEEAM_API_VERSION" in hint

    def test_timeout(self):
        assert "firewall" in failure_hint(httpx.ConnectTimeout("t"), cfg())

    def test_the_reset_hint_is_kept(self):
        hint = failure_hint(ConnectionResetError("[WinError 10054] forcibly closed"), cfg())
        assert "probe veeam" in hint


class TestLoginBody:
    def test_veeams_reason_is_kept(self, session, monkeypatch):
        def handler(request):
            return httpx.Response(401, json={"errorCode": "Unauthorized", "message": "MFA required"})

        real = httpx.Client

        def client(*args, **kwargs):
            kwargs.pop("verify", None)
            return real(*args, transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr("app.collectors.veeam.httpx.Client", client)
        with pytest.raises(RuntimeError) as caught:
            VeeamCollector().collect(session, cfg(veeam_servers="vbr1"))
        assert "MFA required" in str(caught.value)
        assert "rejected the login" in str(caught.value)
