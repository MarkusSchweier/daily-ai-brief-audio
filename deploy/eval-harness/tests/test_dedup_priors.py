"""Unit tests for harness/dedup_priors.py, including the judge methodology v2
(2026-07-07) feed fix: priors filtered strictly relative to the brief actually
being evaluated (brief_date), not the delivery endpoint's own wall-clock "today".
"""

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
    result = dedup_priors.fetch_recent_prior_briefs(
        brief_date="2026-07-07", signing_key="", delivery_base_url="https://example.test", client=_FakeClient()
    )
    assert result == []


def test_returns_empty_list_when_delivery_base_url_is_unset():
    result = dedup_priors.fetch_recent_prior_briefs(
        brief_date="2026-07-07", signing_key="a-secret", delivery_base_url="", client=_FakeClient()
    )
    assert result == []


def test_fetches_and_extracts_dated_entries_in_order():
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

    result = dedup_priors.fetch_recent_prior_briefs(
        brief_date="2026-07-07", count=3, signing_key="a-secret", delivery_base_url="https://delivery.example.test", client=client
    )

    assert result == [
        {"date": "2026-07-06", "markdown": "# Yesterday"},
        {"date": "2026-07-05", "markdown": "# Day before"},
    ]


def test_over_fetches_count_plus_the_margin_from_the_endpoint():
    """The v2 feed fix requests MORE than `count` so that dropping same-or-future
    entries still leaves enough genuine priors."""
    response = _FakeResponse(json_body={"briefs": []})
    client = _FakeClient(response=response)

    dedup_priors.fetch_recent_prior_briefs(
        brief_date="2026-07-07", count=3, signing_key="a-secret", delivery_base_url="https://delivery.example.test", client=client
    )

    url, _headers = client.calls[0]
    assert url == "https://delivery.example.test/recent-briefs?count=5"  # 3 + _OVER_FETCH_MARGIN(2)


def test_strips_a_trailing_slash_from_the_base_url():
    response = _FakeResponse(json_body={"briefs": []})
    client = _FakeClient(response=response)

    dedup_priors.fetch_recent_prior_briefs(
        brief_date="2026-07-07", signing_key="a-secret", delivery_base_url="https://delivery.example.test/", client=client
    )

    url, _headers = client.calls[0]
    assert url == "https://delivery.example.test/recent-briefs?count=5"


def test_empty_briefs_list_returns_empty_list_not_an_error():
    response = _FakeResponse(json_body={"briefs": []})
    client = _FakeClient(response=response)

    result = dedup_priors.fetch_recent_prior_briefs(
        brief_date="2026-07-07", signing_key="a-secret", delivery_base_url="https://delivery.example.test", client=client
    )

    assert result == []


def test_a_transport_failure_degrades_to_an_empty_list_not_a_raise():
    client = _FakeClient(raise_exc=RuntimeError("connection reset"))

    result = dedup_priors.fetch_recent_prior_briefs(
        brief_date="2026-07-07", signing_key="a-secret", delivery_base_url="https://delivery.example.test", client=client
    )

    assert result == []


def test_a_transport_failure_prints_a_greppable_stderr_diagnostic(capsys):
    """review-fix, reviewer Low: the silent degrade-to-[] used to be
    undiagnosable -- 'no priors configured' and 'the fetch broke' looked
    identical to the caller. A genuine failure now prints a DEDUP_PRIORS_FETCH_FAILED
    marker to stderr (this repo's established UPPERCASE_MARKER convention)."""
    client = _FakeClient(raise_exc=RuntimeError("connection reset"))

    dedup_priors.fetch_recent_prior_briefs(
        brief_date="2026-07-07", signing_key="a-secret", delivery_base_url="https://delivery.example.test", client=client
    )

    err = capsys.readouterr().err
    assert "DEDUP_PRIORS_FETCH_FAILED" in err
    assert "connection reset" in err


def test_an_http_error_status_degrades_to_an_empty_list_not_a_raise():
    response = _FakeResponse(status_code=500)
    client = _FakeClient(response=response)

    result = dedup_priors.fetch_recent_prior_briefs(
        brief_date="2026-07-07", signing_key="a-secret", delivery_base_url="https://delivery.example.test", client=client
    )

    assert result == []


def test_an_http_error_status_also_prints_the_stderr_diagnostic(capsys):
    response = _FakeResponse(status_code=500)
    client = _FakeClient(response=response)

    dedup_priors.fetch_recent_prior_briefs(
        brief_date="2026-07-07", signing_key="a-secret", delivery_base_url="https://delivery.example.test", client=client
    )

    assert "DEDUP_PRIORS_FETCH_FAILED" in capsys.readouterr().err


def test_missing_env_vars_do_not_print_a_diagnostic(capsys):
    """The 'not configured at all' path is a normal, expected state (dedup just
    wasn't wired up for this operator yet) -- distinct from a genuine fetch
    failure, and must stay silent."""
    dedup_priors.fetch_recent_prior_briefs(
        brief_date="2026-07-07", signing_key="", delivery_base_url="", client=_FakeClient()
    )

    assert capsys.readouterr().err == ""


def test_reads_env_vars_when_not_passed_explicitly(monkeypatch):
    monkeypatch.setenv(dedup_priors.RECENT_BRIEFS_SIGNING_KEY_ENV_VAR, "env-secret")
    monkeypatch.setenv(dedup_priors.DELIVERY_BASE_URL_ENV_VAR, "https://from-env.example.test")
    response = _FakeResponse(json_body={"briefs": [{"date": "2026-07-06", "markdown": "# X"}]})
    client = _FakeClient(response=response)

    result = dedup_priors.fetch_recent_prior_briefs(brief_date="2026-07-07", client=client)

    assert result == [{"date": "2026-07-06", "markdown": "# X"}]
    url, _headers = client.calls[0]
    assert url.startswith("https://from-env.example.test/recent-briefs")


# --- v2 feed fix: same-day/future exclusion, one-per-day, date passthrough, cap -----


def test_same_day_as_the_brief_is_excluded():
    """The real, observed contamination case (2026-07-07): a 'prior' that is
    actually the SAME-DAY production brief must never be handed to the judge as
    a genuine prior, regardless of what the delivery endpoint's own wall-clock
    filter did or didn't exclude."""
    response = _FakeResponse(
        json_body={
            "briefs": [
                {"date": "2026-07-07", "markdown": "# Same day as the brief under test -- NOT a prior"},
                {"date": "2026-07-06", "markdown": "# A genuine prior"},
            ]
        }
    )
    client = _FakeClient(response=response)

    result = dedup_priors.fetch_recent_prior_briefs(
        brief_date="2026-07-07", signing_key="a-secret", delivery_base_url="https://delivery.example.test", client=client
    )

    assert result == [{"date": "2026-07-06", "markdown": "# A genuine prior"}]


def test_a_future_date_relative_to_the_brief_is_excluded():
    response = _FakeResponse(
        json_body={
            "briefs": [
                {"date": "2026-07-08", "markdown": "# A future brief -- must never appear as a prior"},
                {"date": "2026-07-06", "markdown": "# A genuine prior"},
            ]
        }
    )
    client = _FakeClient(response=response)

    result = dedup_priors.fetch_recent_prior_briefs(
        brief_date="2026-07-07", signing_key="a-secret", delivery_base_url="https://delivery.example.test", client=client
    )

    assert result == [{"date": "2026-07-06", "markdown": "# A genuine prior"}]


def test_only_one_brief_per_prior_day_is_kept():
    """Defensive: even if the endpoint somehow emitted two entries for the same
    date, only the first (most-recent, per the endpoint's own ordering) is kept."""
    response = _FakeResponse(
        json_body={
            "briefs": [
                {"date": "2026-07-06", "markdown": "# First entry for 07-06"},
                {"date": "2026-07-06", "markdown": "# Duplicate entry for 07-06 -- must be dropped"},
                {"date": "2026-07-05", "markdown": "# 07-05"},
            ]
        }
    )
    client = _FakeClient(response=response)

    result = dedup_priors.fetch_recent_prior_briefs(
        brief_date="2026-07-07", count=3, signing_key="a-secret", delivery_base_url="https://delivery.example.test", client=client
    )

    assert result == [
        {"date": "2026-07-06", "markdown": "# First entry for 07-06"},
        {"date": "2026-07-05", "markdown": "# 07-05"},
    ]


def test_result_is_capped_at_count_even_after_over_fetching():
    response = _FakeResponse(
        json_body={
            "briefs": [
                {"date": "2026-07-06", "markdown": "# 07-06"},
                {"date": "2026-07-05", "markdown": "# 07-05"},
                {"date": "2026-07-04", "markdown": "# 07-04"},
                {"date": "2026-07-03", "markdown": "# 07-03"},
            ]
        }
    )
    client = _FakeClient(response=response)

    result = dedup_priors.fetch_recent_prior_briefs(
        brief_date="2026-07-07", count=2, signing_key="a-secret", delivery_base_url="https://delivery.example.test", client=client
    )

    assert result == [
        {"date": "2026-07-06", "markdown": "# 07-06"},
        {"date": "2026-07-05", "markdown": "# 07-05"},
    ]


def test_each_returned_entry_carries_its_own_date_for_the_judge_prompt():
    response = _FakeResponse(json_body={"briefs": [{"date": "2026-07-06", "markdown": "# X"}]})
    client = _FakeClient(response=response)

    result = dedup_priors.fetch_recent_prior_briefs(
        brief_date="2026-07-07", signing_key="a-secret", delivery_base_url="https://delivery.example.test", client=client
    )

    assert result[0]["date"] == "2026-07-06"
    assert result[0]["markdown"] == "# X"
