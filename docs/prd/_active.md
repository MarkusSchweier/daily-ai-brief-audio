# Active PRD

The current active PRD for this project:

@instant-welcome-brief.md

---

Status: **Design complete — ready for the Developer (2026-07-03).** New sign-ups should receive the
latest edition of the brief the moment they confirm their email, with a short welcome header stating
the weekday send time; the weekday send time (06:07 Europe/Berlin, unchanged) is centralized into
one canonical source that the email prose and the deployment schedule agree on. Cross-subsystem:
adds an audio-key pointer to the Managed Agents `briefs/` archival (single place — the local Desktop
task doesn't archive) and a scoped welcome-send plus least-privilege SES/S3 IAM. All three §7
decisions are now resolved: the two low-stakes product questions by the human (cold-start behavior;
welcome wording), and the one architecture question by the Architect — **the welcome send is
async-decoupled: the confirm Lambda async-invokes (`InvocationType='Event'`) a dedicated
welcome-send Lambda that holds the SES/S3 grants (ADR-0009 Accepted).** FR-13/FR-14's IAM now lands
on the welcome Lambda's role, not the confirm Lambda's. Next: the Developer implements phases 1–4
across the two CDK apps; the security-engineer reviews the new IAM before PR. **Independent of
`neutral-briefing-pipeline.md`** below — a separate, not-yet-merged PR this feature neither depends
on nor touches.

Previous PRD — `neutral-briefing-pipeline.md` (separate branch, not-yet-merged): removed the
Anthropic lens and selection skew from the daily brief's **content logic** (AC-1..AC-8, ADR-0008).
Content/prompt-only; no overlap with this feature's delivery/confirm-flow scope, and not a
dependency of it.

Earlier PRD — `managed-agents-migration.md` (merged): migrated the daily brief pipeline to
self-hosted Claude Managed Agents (ADRs 0004–0007 Accepted); the local Desktop task stays running
as a monitored fallback during the parallel-run window.

Earlier PRD — `public-subscriptions.md` (Complete): all three ADRs (0001–0003) Accepted; feature
fully built, reviewed, and security-cleared on branch `feat/public-subscriptions`, deployed and
validated per `deploy/subscribers/README.md`.

Start a new one with: `/feature "<describe the feature>"`
