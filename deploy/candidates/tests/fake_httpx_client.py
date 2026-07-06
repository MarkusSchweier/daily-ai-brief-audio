"""A hand-rolled fake standing in for `httpx.Client`, mirroring the exact pattern this
repo already uses at `deploy/eval/tests/test_cost_miner.py`'s `_FakeHttpxClient` (a
plain object recording every call and returning scripted responses) rather than
`httpx.MockTransport` or the `respx` library -- consistent with this repo's own
established, already-working test-mocking convention for this exact family of API
calls, and adds no new test dependency.

Each test wires up a `FakeHttpxClient` with a `responses` dict keyed by
`(method, path)` -> a callable that receives the call's kwargs and returns either a
`FakeResponse` or raises to simulate a failure. Every call (in order) is also recorded
into `.calls`, which is what the ordering-sensitive tests assert against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


class FakeHttpError(Exception):
    """Raised by a scripted handler to simulate a transport-level failure (e.g. agent
    creation failing outright) -- distinct from FakeResponse.raise_for_status()'s
    HTTPStatusError, which simulates a real HTTP error status code."""


@dataclass
class FakeResponse:
    status_code: int
    _json: Any

    def json(self) -> Any:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("POST", "https://api.anthropic.com/fake")
            response = httpx.Response(self.status_code, request=request, json=self._json)
            raise httpx.HTTPStatusError(
                f"fake {self.status_code} error", request=request, response=response
            )


@dataclass
class RecordedCall:
    method: str
    path: str
    kwargs: dict[str, Any]


@dataclass
class FakeHttpxClient:
    """A minimal stand-in for httpx.Client exposing only .get()/.post(), the two
    methods candidate_sync.api_client actually uses."""

    handlers: dict[tuple[str, str], list[Callable[..., FakeResponse]]] = field(default_factory=dict)
    calls: list[RecordedCall] = field(default_factory=list)

    def when(self, method: str, path: str, *responses: Callable[..., FakeResponse] | FakeResponse) -> None:
        """Register one or more scripted responses for a given (method, path). If
        multiple are registered, they're returned in order across repeated calls
        (used by the 409-then-retry test)."""
        queue = self.handlers.setdefault((method.upper(), path), [])
        for response in responses:
            queue.append(response if callable(response) else (lambda r=response, **_kw: r))

    def get(self, path: str, **kwargs: Any) -> FakeResponse:
        return self._dispatch("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> FakeResponse:
        return self._dispatch("POST", path, **kwargs)

    def _dispatch(self, method: str, path: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(RecordedCall(method=method, path=path, kwargs=kwargs))
        key = (method, path)
        if key not in self.handlers or not self.handlers[key]:
            raise AssertionError(f"no scripted response registered for {method} {path}")
        handler = self.handlers[key].pop(0) if len(self.handlers[key]) > 1 else self.handlers[key][0]
        return handler(**kwargs)

    def call_signature(self) -> list[tuple[str, str]]:
        """A simplified (method, path) list, in call order -- what the ordering tests
        assert against."""
        return [(c.method, c.path) for c in self.calls]

    def __enter__(self) -> "FakeHttpxClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None
