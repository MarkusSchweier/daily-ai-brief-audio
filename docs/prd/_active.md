# Active PRD

The current active PRD for this project:

@neutral-briefing-pipeline.md

---

Status: **Shipped (2026-07-03).** All of AC-1 through AC-8 satisfied — de-skew edits applied to
the in-repo copy, the local Desktop wrapper, and (after catching and correcting a packaging
mistake) a separately-registered local Cowork skill; independent reviewer pass confirmed the
structural checks; the live Skills API version was pushed and **confirmed** (not assumed) —
`skill_01H2qu83NwnJ5zqcbrqsCcJ6` now resolves `latest` to version `1783096569199829`, and the
scheduled deployment's agent picks it up with no `agent.json` change needed. Runbook for future
skill-content pushes recorded in `deploy/managed-agent/README.md` §3a per ADR-0008. Remaining:
none blocking — a quick sanity glance at the next real scheduled brief is worthwhile but not
gating.

Previous PRD — `managed-agents-migration.md` (merged): migrated the daily brief pipeline to
self-hosted Claude Managed Agents (ADRs 0004–0007 Accepted); the local Desktop task stays running
as a monitored fallback during the parallel-run window. This new PRD deliberately touches **only**
the brief's content logic — no delivery, infra, subscriber, or migration-infrastructure changes.

Earlier PRD — `public-subscriptions.md` (Complete): all three ADRs (0001–0003) Accepted; feature
fully built, reviewed, and security-cleared on branch `feat/public-subscriptions`, deployed and
validated per `deploy/subscribers/README.md`.

Start a new one with: `/feature "<describe the feature>"`
