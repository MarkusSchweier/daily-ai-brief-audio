"""Shared pytest fixtures for deploy/candidates/'s test suite.

Mirrors deploy/eval/tests/conftest.py's pattern: put this app's own package
(`candidate_sync`) on sys.path so tests can `from candidate_sync... import ...`
regardless of the pytest invocation's cwd.
"""

from __future__ import annotations

import sys
from pathlib import Path

CANDIDATES_DIR = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

sys.path.insert(0, str(CANDIDATES_DIR))
