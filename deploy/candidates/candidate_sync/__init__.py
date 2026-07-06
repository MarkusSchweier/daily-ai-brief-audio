"""candidate_sync -- the git-native candidate versioning mechanism (Decision 2c,
docs/adr/0014-agent-system-redesign-topology.md; PRD docs/prd/agent-system-redesign.md
FR-9..FR-12, AC-9..AC-12).

This package is deliberately NOT a CDK app and stands up NO AWS infrastructure. It is
a plain Python library + CLI (see `../sync.py`) that:

  1. Loads a candidate's declaration from a `deploy/candidates/<slug>/` directory (a
     set of small, independently-diffable files -- see `loader.py`).
  2. Talks to the Claude Platform Agents/Skills APIs directly over HTTPS (`api_client.py`),
     mirroring the exact working pattern already proven in
     `deploy/eval/functions/trigger/handler.py`.
  3. Decides, for a loaded candidate, whether this is a first sync (create) or an
     update-in-place, and in what order to make calls for a multi-agent candidate
     (`sync.py`'s `sync_candidate()`).

See `deploy/candidates/README.md` for the full schema/runbook documentation.
"""
