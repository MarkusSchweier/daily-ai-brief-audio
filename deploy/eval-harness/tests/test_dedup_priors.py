"""Unit tests for harness/dedup_priors.py."""

from __future__ import annotations

from harness import dedup_priors


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json_body = json_body or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"fake {self.status_code}")

    def json(self):
        return self._json_body


class _FakeClient:
    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise_exc = raise_exc
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, headers=None, **kwargs):
        self.calls.append((url, headers or {}))
        if self._raise_exc:
            raise self._raise_exc
        return self._response


def test_returns_empty_list_when_signing_key_is_unset():
    result = dedup_priors.fetch_recent_prior_briefs_markdown(
        signing_key="", delivery_base_url="https://example.test", client=_FakeClient()
    )
    assert result == []


def test_returns_empty_list_when_delivery_base_url_is_unset():
    result = dedup_priors.fetch_recent_prior_briefs_markdown(
        signing_key="a-secret", delivery_base_url="", client=_FakeClient()
    )
    assert result == []


def test_fetches_and_extracts_markdown_bodies_in_order():
    response = _FakeResponse(
        json_body={
            "contractVersion": 1,
            "briefs": [
                {"date": "2026-07-06", "markdown": "# Yesterday"},
                {"date": "2026-07-05", "markdown": "# Day before"},
            ],
        }
    )
    client = _FakeClient(response=response)

    result = dedup_priors.fetch_recent_prior_briefs_markdown(
        count=3, signing_key="a-secret", delivery_base_url="https://delivery.example.test", client=client
    )

    assert result == ["# Yesterday", "# Day before"]
    url, headers = client.calls[0]
    assert url == "https://delivery.example.test/recent-briefs?count=3"
    assert headers["Authorization"].startswith("Bearer ")


def test_strips_a_trailing_slash_from_the_base_url():
    response = _FakeResponse(json_body={"briefs": []})
    client = _FakeClient(response=response)

    dedup_priors.fetch_recent_prior_briefs_markdown(
        signing_key="a-secret", delivery_base_url="https://delivery.example.test/", client=client
    )

    url, _headers = client.calls[0]
    assert url == "https://delivery.example.test/recent-briefs?count=3"


def test_empty_briefs_list_returns_empty_list_not_an_error():
    response = _FakeResponse(json_body={"briefs": []})
    client = _FakeClient(response=response)

    result = dedup_priors.fetch_recent_prior_briefs_markdown(
        signing_key="a-secret", delivery_base_url="https://delivery.example.test", client=client
    )

    assert result == []


def test_a_transport_failure_degrades_to_an_empty_list_not_a_raise():
    client = _FakeClient(raise_exc=RuntimeError("connection reset"))

    result = dedup_priors.fetch_recent_prior_briefs_markdown(
        signing_key="a-secret", delivery_base_url="https://delivery.example.test", client=client
    )

    assert result == []


def test_a_transport_failure_prints_a_greppable_stderr_diagnostic(capsys):
    """review-fix, reviewer Low: the silent degrade-to-[] used to be
    undiagnosable -- 'no priors configured' and 'the fetch broke' looked
    identical to the caller. A genuine failure now prints a DEDUP_PRIORS_FETCH_FAILED
    marker to stderr (this repo's established UPPERCASE_MARKER convention)."""
    client = _FakeClient(raise_exc=RuntimeError("connection reset"))

    dedup_priors.fetch_recent_prior_briefs_markdown(
        signing_key="a-secret", delivery_base_url="https://delivery.example.test", client=client
    )

    err = capsys.readouterr().err
    assert "DEDUP_PRIORS_FETCH_FAILED" in err
    assert "connection reset" in err


def test_an_http_error_status_degrades_to_an_empty_list_not_a_raise():
    response = _FakeResponse(status_code=500)
    client = _FakeClient(response=response)

    result = dedup_priors.fetch_recent_prior_briefs_markdown(
        signing_key="a-secret", delivery_base_url="https://delivery.example.test", client=client
    )

    assert result == []


def test_an_http_error_status_also_prints_the_stderr_diagnostic(capsys):
    response = _FakeResponse(status_code=500)
    client = _FakeClient(response=response)

    dedup_priors.fetch_recent_prior_briefs_markdown(
        signing_key="a-secret", delivery_base_url="https://delivery.example.test", client=client
    )

    assert "DEDUP_PRIORS_FETCH_FAILED" in capsys.readouterr().err


def test_missing_env_vars_do_not_print_a_diagnostic(capsys):
    """The 'not configured at all' path is a normal, expected state (dedup just
    wasn't wired up for this operator yet) -- distinct from a genuine fetch
    failure, and must stay silent."""
    dedup_priors.fetch_recent_prior_briefs_markdown(signing_key="", delivery_base_url="", client=_FakeClient())

    assert capsys.readouterr().err == ""


def test_reads_env_vars_when_not_passed_explicitly(monkeypatch):
    monkeypatch.setenv(dedup_priors.RECENT_BRIEFS_SIGNING_KEY_ENV_VAR, "env-secret")
    monkeypatch.setenv(dedup_priors.DELIVERY_BASE_URL_ENV_VAR, "https://from-env.example.test")
    response = _FakeResponse(json_body={"briefs": [{"date": "2026-07-06", "markdown": "# X"}]})
    client = _FakeClient(response=response)

    result = dedup_priors.fetch_recent_prior_briefs_markdown(client=client)

    assert result == ["# X"]
    url, _headers = client.calls[0]
    assert url.startswith("https://from-env.example.test/recent-briefs")
