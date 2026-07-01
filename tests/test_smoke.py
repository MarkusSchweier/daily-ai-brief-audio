"""Smoke test so lint/type/test + CI have something to run before the pipeline lands."""

import daily_brief_audio


def test_package_imports() -> None:
    assert daily_brief_audio.__version__ == "0.1.0"
