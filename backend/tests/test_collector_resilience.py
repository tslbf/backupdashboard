"""Things that only showed up against the real vendor endpoints.

Both of these were found by running the collectors at LB Foster rather than by
anything in the rest of this suite, which is exactly why they are pinned here.
"""
from __future__ import annotations

import ssl

import httpx
import pytest

from app.collectors.azure import MAX_PAGES, AzureCollector
from app.collectors.veeam import tls_context


class TestArmPaging:
    """backupJobs pages by a per-job cursor, so a busy vault returns roughly one
    record per round trip. A few days of history is thousands of requests, and
    with no progress logging it is indistinguishable from a hang."""

    def _client(self, handler) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://arm.test")

    def test_it_follows_every_page(self):
        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params.get("page", 0))
            if page < 4:
                return httpx.Response(
                    200,
                    json={
                        "value": [{"id": page}],
                        "nextLink": f"https://arm.test/jobs?page={page + 1}",
                    },
                )
            return httpx.Response(200, json={"value": [{"id": page}]})

        with self._client(handler) as client:
            rows = AzureCollector()._pages(client, "https://arm.test/jobs?page=0", label="vault")
        assert [r["id"] for r in rows] == [0, 1, 2, 3, 4]

    def test_a_repeating_cursor_stops_instead_of_looping_forever(self):
        """A nextLink that points at itself would otherwise page until the heat
        death of the server, looking exactly like slow progress."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(
                200, json={"value": [{"id": 1}], "nextLink": "https://arm.test/jobs?stuck=1"}
            )

        with self._client(handler) as client:
            rows = AzureCollector()._pages(client, "https://arm.test/jobs?stuck=1", label="vault")

        assert calls["n"] == 1, "the repeated cursor should be caught on sight"
        assert len(rows) == 1

    def test_paging_is_capped(self):
        """A backstop against a cursor that never terminates but never repeats."""

        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params.get("page", 0))
            return httpx.Response(
                200,
                json={"value": [{"id": page}], "nextLink": f"https://arm.test/j?page={page + 1}"},
            )

        with self._client(handler) as client:
            rows = AzureCollector()._pages(client, "https://arm.test/j?page=0", label="vault")
        assert len(rows) == MAX_PAGES

    def test_an_error_mid_chain_is_not_swallowed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if "page=2" in str(request.url):
                return httpx.Response(403, json={"error": {"message": "Forbidden"}})
            page = int(request.url.params.get("page", 0))
            return httpx.Response(
                200,
                json={"value": [{"id": page}], "nextLink": f"https://arm.test/j?page={page + 1}"},
            )

        with self._client(handler) as client:
            with pytest.raises(httpx.HTTPStatusError):
                AzureCollector()._pages(client, "https://arm.test/j?page=0", label="vault")


class TestVeeamTls:
    """Python 3.11 links OpenSSL 3.x, whose defaults an older Windows TLS stack
    will not negotiate — it drops the connection, and the only symptom is
    `[WinError 10054] An existing connection was forcibly closed by the remote
    host`, which says nothing about TLS at all."""

    def test_unverified_gets_a_context_that_matches_the_powershell(self):
        context = tls_context(verify=False)

        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode == ssl.CERT_NONE
        assert context.check_hostname is False
        # The script this replaces pinned TLS 1.2; anything older is not offered.
        assert context.minimum_version == ssl.TLSVersion.TLSv1_2

    def test_the_security_level_is_lowered_so_older_suites_still_negotiate(self):
        context = tls_context(verify=False)
        strict = ssl.create_default_context()

        assert len(context.get_ciphers()) > len(strict.get_ciphers()), (
            "SECLEVEL=1 should widen the offered suites; without it OpenSSL 3 "
            "refuses what these appliances present and the server hangs up"
        )

    def test_verification_stays_on_when_asked_for(self):
        """Setting VEEAM_VERIFY_TLS=true must actually verify — the loosening
        above is scoped to the self-signed default, not a global downgrade."""
        assert tls_context(verify=True) is True
