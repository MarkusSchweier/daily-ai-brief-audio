---
name: example-skill-not-real
description: A synthetic skill source used only by deploy/candidates/tests/test_sync.py to exercise the sync script's skill-push path. Not a real candidate.
---

# EXAMPLE SKILL -- NOT A REAL CANDIDATE

This is a synthetic skill source used only by
`deploy/candidates/tests/test_sync.py` to exercise the sync script's skill-push
path. It is never zipped and pushed against the live Anthropic API in tests --
`create_skill_version()`/`create_skill()` are mocked.

## Fake instructions

Do nothing real. This skill does not exist on Claude Platform.
