# PRD: Make the daily AI brief label-neutral (remove Anthropic skew)

- Status: **Shipped (2026-07-03)** — AC-1 through AC-8 all satisfied; live Skills API version
  `1783096569199829` is what the scheduled deployment now runs. See §5 for evidence per AC.
- Author: product-manager (Claude)  ·  Date: 2026-07-03
- Linked ADRs:
  [0008 Three-way lockstep + live Skills-API version push for skill-content changes](../adr/0008-skill-content-change-lockstep-and-live-version-push.md) (**Accepted** — formalizes the repeatable sync-and-push procedure; the "neutral" rubric itself needs no ADR, see §7).
- Source: GitHub issue
  [#12 "Remove Anthropic-specificity in ai briefing pipeline"](https://github.com/MarkusSchweier/daily-ai-brief-audio/issues/12)

## 1. Problem

The `daily-ai-brief` skill was originally written for a **single reader** — the owner, in an
"Applied AI Solutions Architect Manager at **Anthropic**" role. To serve that reader, the skill
prompt bakes in an **Anthropic lens**:

- Every relevant deep-dive is told to add "one sentence on what it means for **Anthropic** (its
  position, customers, or the applied-AI/SA function)" (`SKILL.md` lines 37–40, 130, 212).
- The story-**selection / ranking rubric** explicitly up-weights "anything **directly about
  Anthropic**" as its own priority tier, ahead of general frontier-lab news
  (`SKILL.md` §5 "Rank & select", line ~199).
- The skill's front-matter `description`, its guardrails, and its trailing "Reader context"
  all frame the brief around the owner's Anthropic role (`SKILL.md` line 3, line 259,
  lines 279–282; the local wrapper `SKILL.md` also carries the same "Reader context (Markus):
  Applied AI Manager at Anthropic" line).

Since the **public-subscriptions** feature shipped, the brief now **fans out to a subscriber
list**, not just the owner. An Anthropic-centric framing and a selection bias toward Anthropic
news are **inappropriate for a general audience** and no longer match how the product is used.

### Why now
The subscriber fan-out is live and growing. Every subscriber currently receives a brief that
editorializes for one lab and over-samples that lab's news. The owner wants the brief
**completely neutral** going forward: no lab-specific commentary lens, and no selection weighting
that structurally over-represents any single lab. This is a **content/prompt-logic-only** change
— the smallest edit that fixes the framing without touching delivery, infrastructure, or the
subscriber feature.

## 2. Goals & non-goals

### Goals
- **Remove the Anthropic-lens commentary** from the brief entirely: no per-item "what this means
  for Anthropic" sentences, and no framing/guardrail/description language that positions the brief
  around Anthropic (or any single lab) as the reference point.
- **Rebalance story selection** so no single lab (Anthropic included) is **structurally
  over-represented**: the ranking rubric and the source-tier list must treat frontier labs
  **even-handedly**, judging each story on its own newsworthiness rather than on which lab it is
  about.
- **Preserve comprehensiveness** — this is an explicit **non-regression** requirement, not just an
  aspiration. The brief must remain as broad and deep as today (same tier coverage, same
  8–15 headlines / 5–10 deep-dive targets, same accuracy/validation guardrails). This change
  **removes a bias, it does not reduce coverage**, and in particular **must not drop or down-weight
  Anthropic news below what its newsworthiness warrants** — Anthropic is treated like any other
  frontier lab, no more and no less.
- **Update both copies of the skill in lockstep** — the in-repo ported copy
  (`deploy/managed-agent/skills/daily-ai-brief/SKILL.md` + `sources.md`) and the local Desktop
  copy (`~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md`) — mirroring the existing
  `audio_email.py` lockstep-copy convention, and **push a new version of the live Skills API
  resource** so the running Managed Agents deployment picks up the change.
- **Validate the change before it goes live on the real schedule** (a before/after comparison so a
  reviewer can confirm the skew is gone and coverage is unregressed) — see the open question in §7
  on how "neutral" is verified.

### Non-goals (explicitly out of scope)
- **No changes to delivery or infrastructure.** Polly synthesis, SES send, the S3 bucket/archival,
  the HTML/listening-script derivation mechanics, and `pipeline/audio_email.py` /
  `pipeline/brief_history.py` are **untouched**. This PRD changes *what the brief says and which
  stories it selects*, not *how it is produced or delivered*.
- **No changes to the subscriber feature.** Nothing under `deploy/subscribers/` (the
  subscribe/confirm/unsubscribe website and API) changes; the fan-out still reads the same
  `brief-subscribers` table and sends the same mail.
- **No changes to the Managed Agents migration infrastructure.** The self-hosted CDK stack
  (`deploy/managed-agent/cdk/`), the microVM image (`microvm/`), the launcher Lambda, the
  webhook, Secrets Manager secrets, IAM roles, the `agent.json` / `deployment.json` **schedule,
  environment, and agent identity**, and the beta-header/version pinning are all **out of scope**.
  Only the **skill content** the agent already references changes; pushing a new Skills API
  *version* of the same `skill_id` is not a change to the migration infrastructure.
- **No change to the reader's "expert, technically fluent" audience assumption.** The brief stays
  written for a technically fluent reader (dense, specific, no hand-holding). What changes is that
  it is no longer framed for *one employer's* vantage point.
- **No content-scope changes beyond de-skewing.** Not adding new sections, new source tiers, new
  topics, or a different writing style; not changing the Polly voice, cadence, or schedule.
- **No retiring of the local Desktop task.** It remains the monitored fallback during the
  migration's parallel-run window; this PRD keeps it running (and de-skewed), it does not disable
  it.

## 3. Users & use cases

- **Confirmed subscriber (general audience)** — the reason for this change.
  - *US-1:* "As a subscriber, I receive a **neutral** AI brief that doesn't editorialize about
    what each story means for one particular lab, so it reads as an even-handed industry digest."
  - *US-2:* "As a subscriber, I still get **comprehensive** coverage — the same breadth of labs,
    papers, benchmarks, products, and policy as before — just without a house lens."
- **Owner (operator/recipient)** — also receives the brief; wants it neutral for everyone.
  - *US-3:* "As the owner, my copy and every subscriber's copy read the same and are label-neutral;
    Anthropic news still appears when it's genuinely newsworthy, just not over-weighted or
    editorialized."
  - *US-4:* "As the owner, both the live Managed Agents run and the local fallback produce the
    de-skewed brief — they don't diverge — so whichever path runs, subscribers get the neutral
    version."
- **Reviewer / future maintainer** — verifies and later edits the skill.
  - *US-5:* "As a reviewer, I can compare a brief produced by the old prompt against one from the
    de-skewed prompt and confirm the Anthropic-lens sentences and selection bias are gone while
    coverage is unregressed, before it ships on the real schedule."
  - *US-6:* "As a maintainer, the two skill copies stay consistent by the documented lockstep
    convention, and the repo records how the live Skills API resource was re-versioned, so I can
    make the next change from git."

## 4. Functional requirements

Numbered; each maps to acceptance criteria in §5. "The system shall …".

### Remove the Anthropic-lens commentary
1. The skill shall **not instruct the writer to add any lab-specific commentary sentence** to
   deep-dive items. Specifically, the "one Anthropic-lens sentence" instructions in the item
   format (`SKILL.md` line ~130), the "Write" step (line ~212), and the reader-role framing
   (lines ~37–40) shall be **removed or rewritten to be lab-neutral**, so no produced brief
   contains "what this means for Anthropic"–style editorializing for Anthropic **or any other single
   lab**.
2. The skill's **front-matter `description`, guardrails, and "Reader context" sections** shall be
   rewritten to remove Anthropic-specific framing: the `description`'s "with an Anthropic lens"
   (`SKILL.md` line 3), the guardrail "the Anthropic lens is analysis, not spin" (line ~259), and
   the "Reader context (Markus): Applied AI Manager at Anthropic …" trailer (lines ~279–282, and
   the equivalent line in the local wrapper `SKILL.md`) shall be replaced with **neutral,
   general-audience** equivalents that still convey "technically fluent reader, dense factual
   style" without naming an employer/lens.
3. Removing the lens shall **not remove the general analytical guidance** that makes items useful
   (e.g. "why this matters" context that is *lab-neutral* — competitive/industry significance
   framed for a general reader is fine). The change is to strip the **single-lab** vantage point,
   not to strip all analysis.

### Rebalance story selection / source weighting
4. The **ranking & selection rubric** (`SKILL.md` §5, line ~199) shall be rewritten so that
   "directly about Anthropic" is **no longer its own elevated priority tier**. Frontier-lab news
   shall be ranked by **newsworthiness even-handedly across labs** (OpenAI, Google/DeepMind, Meta,
   Mistral, xAI, Anthropic, major open-weight labs, etc. treated on the same footing).
5. The **source list `sources.md`** shall be reviewed and, where it structurally privileges
   Anthropic (e.g. ordering/prominence within Tier 1, or any Anthropic-specific weighting), edited
   so **no single lab is given structurally higher priority** than its peers. All frontier-lab
   primary sources shall remain present (this is a de-skew, not a source removal — see FR-6).
6. The change shall **preserve comprehensiveness (non-regression):** all existing source tiers and
   outlets shall remain (nothing is dropped), the same target volume (8–15 headlines, 5–10 deep
   dives) and the same accuracy/validation/dedup guardrails shall remain, and **Anthropic news
   shall still be included when genuinely newsworthy** — it is neither excluded nor demoted below
   its merit; it is simply no longer over-sampled.

### Lockstep update across both copies + live resource
7. The system shall apply the FR-1…FR-6 edits to **both** skill copies so they remain byte-for-byte
   equivalent in intent:
   (a) the in-repo ported copy `deploy/managed-agent/skills/daily-ai-brief/SKILL.md` and its
   `sources.md`; and
   (b) the local Desktop copy `~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md` (and any
   Anthropic-lens language in its wrapping steps / "Reader context" line) — preserving the existing
   lockstep-copy convention documented in the project `CLAUDE.md`.
8. The system shall **push a new version of the live Skills API resource** (`skill_id
   skill_01H2qu83NwnJ5zqcbrqsCcJ6`, referenced by agent `agent_01EswBTose8dnTAUDbGvzdLq` and
   deployment `depl_0132ARBCdsSRh6hxocbbW7ac`) from the updated in-repo copy, using the same
   direct Skills-API push mechanism already used in this repo (POST/create-new-version per the
   beta `managed-agents-2026-04-01` header), so the running scheduled deployment produces the
   de-skewed brief. The agent's `skills[].version: "latest"` reference shall resolve to the new
   version (confirm no pinned-version change is needed in `agent.json`).
9. The repo shall **record how the live Skills API resource was re-versioned** (the command/mechanism
   and the resulting new skill version id), consistent with how prior skill pushes are recorded in
   this repo's history — so a future maintainer can repeat it. No Anthropic API key or secret shall
   be committed or printed in doing so.

### Validate before it goes live
10. Before the de-skewed skill runs on the **real schedule**, the change shall be **validated by a
    before/after comparison**: a brief (or representative brief excerpt) produced by the *current*
    prompt versus one produced by the *de-skewed* prompt, reviewed to confirm (a) the Anthropic-lens
    commentary sentences are gone, (b) selection is no longer skewed toward Anthropic, and
    (c) coverage breadth/depth is unregressed. The exact pass/fail rubric for "neutral" is an
    **open question for the human** (see §7); the validation step shall use whatever rubric is
    agreed there.

## 5. Acceptance criteria

Given/When/Then, testable against the two skill copies and (for AC-6/AC-7) the live Managed Agents
deployment in account `740353583786`, `us-east-1`.

### Commentary removed
- **AC-1 (no lab-lens instruction remains):** Given the updated in-repo `SKILL.md`, When it is
  inspected, Then it contains **no instruction to add an "Anthropic-lens" / "what this means for
  Anthropic" sentence** (nor the equivalent for any other single lab) in the item format, the Write
  step, or the reader-role framing (FR-1).
- **AC-2 (no Anthropic framing in description/guardrails/reader-context):** Given the updated
  `SKILL.md`, When its front-matter `description`, guardrails, and "Reader context" are inspected,
  Then they contain **no Anthropic-specific framing** ("with an Anthropic lens", "the Anthropic
  lens is analysis", "Applied AI Manager at Anthropic" as the reader identity) and instead describe
  a neutral, technically-fluent general audience (FR-2).
- **AC-3 (analysis not stripped wholesale):** Given the updated `SKILL.md`, When the Write/item
  guidance is inspected, Then lab-neutral "why it matters" analytical guidance is still present —
  only the single-lab vantage point was removed (FR-3).

### Selection rebalanced, comprehensiveness preserved
- **AC-4 (Anthropic no longer an elevated selection tier):** Given the updated `SKILL.md` §5 and
  `sources.md`, When they are inspected, Then "directly about Anthropic" is **not** its own elevated
  priority tier and no single lab is given structurally higher source priority than its peers
  (FR-4, FR-5).
- **AC-5 (comprehensiveness non-regression):** Given the updated `SKILL.md` and `sources.md`, When
  compared to the pre-change versions, Then **all source tiers and outlets are still present**
  (none dropped), the target headline/deep-dive volumes and the accuracy/validation/dedup guardrails
  are unchanged, and Anthropic's primary source (and every other lab's) is still listed (FR-6).

### Lockstep + live resource
- **AC-6 (both copies consistent): SATISFIED (2026-07-03).** In-repo copy edited (commit `cca4a3d`);
  local Desktop wrapper's skill-invocation-relevant line (`Reader context` trailer) mirrored to
  match. Independently re-verified by the reviewer subagent's structural pass: zero "Anthropic"
  mentions anywhere in the local wrapper file. A related, initially-missed **fourth** location — a
  separately-registered local Claude Desktop **Cowork** skill (a different resource entirely from
  this Skills API, confirmed via a `404` when queried with the Console API key) — was discovered,
  an incorrect first package (accidentally carrying deployment-specific `/workspace`/provenance-note
  content) was caught and corrected before causing a live regression, and the corrected `.skill`
  package was re-imported by the human. See ADR-0008 for the full three(-plus-one)-way sync.
- **AC-7 (live skill re-versioned): SATISFIED (2026-07-03).** New version `1783096569199829`
  pushed to `skill_01H2qu83NwnJ5zqcbrqsCcJ6` (`POST /v1/skills/{skill_id}/versions`, replacing
  `1783077535207053`). Confirmed, not assumed: re-fetched the skill resource and verified
  `latest_version` equals the new version id; listed all versions and confirmed both are present;
  re-fetched agent `agent_01EswBTose8dnTAUDbGvzdLq` and confirmed its `skills[].version: "latest"`
  needed no change. Mechanism recorded in `deploy/managed-agent/README.md` §3a (FR-8, FR-9) with no
  secret committed.

### Validation gate
- **AC-8 (before/after validated before schedule): SATISFIED (2026-07-03).** AC-1…AC-5's structural
  checks ran as the hard gate via an independent reviewer subagent pass, diffing the actual current
  file contents against the pre-edit commit (not trusting the PRD's own description) — all five
  passed with cited evidence. The "light human read" component was satisfied by the human reviewing
  the full before/after prompt-text diff directly in chat before authorizing the local-copy
  reimport and this live push (rather than a separate freshly-generated brief read) — consistent
  with the decided rubric's "light" weight; the next real scheduled run is the first actual produced
  brief under the new version and remains worth a quick sanity glance when it lands. **Decided
  (human, 2026-07-03): structural-only + a light human read** — no numeric share-of-coverage rule,
  since a hard cap risks suppressing genuinely dominant news on days one lab (Anthropic or otherwise)
  legitimately leads, which would fight FR-6/AC-5's comprehensiveness requirement.

## 6. Constraints & dependencies

- **Scope is content/prompt logic only.** No AWS resource, IAM, CDK, microVM, launcher, webhook,
  Polly, SES, S3, or DynamoDB change. No `deploy/subscribers/` change. No
  `agent.json`/`deployment.json` **schedule/environment/agent-identity** change (only a new Skills
  API *version* of the already-referenced skill).
- **Two copies must stay in lockstep** (project `CLAUDE.md` convention): the in-repo ported skill
  and the local Desktop skill. The local Desktop task is the **monitored fallback** during the
  Managed Agents migration's parallel-run window and **must not be broken** by this change.
- **Live, already-deployed Skills API resource.** `skill_01H2qu83NwnJ5zqcbrqsCcJ6` is deployed and
  referenced by the live agent/deployment; updating it is a **direct Skills-API version push**
  (there is no CDK for it), per the pattern already used in this repo's history. Requires an
  Anthropic API credential + the beta header `managed-agents-2026-04-01`; the credential is never
  committed or printed.
- **Managed Agents is in beta;** the Skills API version-push surface may change. Record the version
  built/pushed against.
- **AWS account** `740353583786`, `us-east-1` — no mutations expected from this PRD, but confirm the
  active account if any AWS-touching validation is run.
- **Grounded in current text.** The concrete skew instances this PRD targets are the specific lines
  identified in §1/§4 of the current `SKILL.md` and `sources.md`; the edit must be verified against
  the actual files at implementation time, not from this list alone (the skill may have been edited
  since).

## 7. Risks & open questions

- **[RESOLVED] How is "neutral" defined precisely enough to be testable?** — Decided (human,
  2026-07-03): **structural-only + a light human read** (option (a)). AC-1…AC-5's prompt-text checks
  (lens sentences removed, no elevated Anthropic tier, comprehensiveness unregressed) are the hard,
  objective gate; AC-8 adds a one-time human before/after read of produced briefs as a sanity check,
  not a numeric rule. No share-of-coverage heuristic or standing reviewer checklist — a hard
  quantitative cap risks suppressing genuinely dominant news on a day one lab legitimately leads,
  which would fight FR-6/AC-5's comprehensiveness requirement.
- **Over-correction risk.** De-skewing must not swing to *under*-covering Anthropic. FR-6/AC-5 guard
  this (Anthropic stays in when newsworthy), but the before/after validation should watch for it
  explicitly.
- **Lockstep drift.** Two copies plus a live Skills API version means three things to keep in sync;
  missing one (e.g. pushing the live version but forgetting the local copy, or vice-versa) leaves a
  skewed brief in play on one path. AC-6 + AC-7 together are the guard; the rollout must do all three
  in one change.
- **Live version push during beta.** The Skills-API version-push mechanism is beta and may drift; a
  failed/half-applied push could leave the live deployment on the old (skewed) skill silently. The
  push must be **confirmed** (AC-7 inspects the live resource), not assumed.
- **[RESOLVED] Whether an ADR is needed.** Decided (Architect, 2026-07-03): the "neutral" rubric
  needs **no** ADR (content-only, and the human already chose the simplest structural-only rubric —
  no new mechanism). The **live-version-push + three-way lockstep procedure** *does* get a short
  ADR — [ADR-0008](../adr/0008-skill-content-change-lockstep-and-live-version-push.md) — because it
  is a **repeatable, cross-cutting procedure** any future skill-content edit needs (not just this
  one), its version-push mechanism was **undocumented anywhere in the repo** (commit `606330f`
  created the live skill via API but committed no runbook), and its drift failure modes are
  **silent** (a stale live version, or one path skewed, with no error). ADR-0008 fixes the
  *procedure* (atomic three-way update, validate-before-push ordering, an explicit confirm step,
  and a recorded runbook) — not the content of any edit.
- **Interaction with the migration parallel-run.** The migration PRD's parallel-run compares the two
  paths' output; de-skewing both simultaneously keeps that comparison valid (both change together).
  Sequencing note, not a blocker.

## 8. Rollout & metrics

- **Phasing.**
  1. **Edit the in-repo skill** — apply FR-1…FR-6 to
     `deploy/managed-agent/skills/daily-ai-brief/SKILL.md` and `sources.md` on the feature branch.
  2. **Mirror to the local copy** — apply the same edits to
     `~/Claude/Scheduled/daily-ai-brief-weekday/SKILL.md` (lockstep, FR-7).
  3. **Validate before schedule** — run the before/after comparison (FR-10/AC-8) using the rubric
     agreed in §7; a reviewer confirms skew gone + coverage unregressed.
  4. **Push the live Skills API version** — re-version `skill_01H2qu83NwnJ5zqcbrqsCcJ6` from the
     updated in-repo copy and confirm the deployment resolves to it (FR-8/AC-7); record the
     mechanism + new version id (FR-9).
  5. **Observe** the next scheduled run(s) on both paths to confirm the de-skewed brief ships.
- **Ship gate.** AC-1…AC-8 all pass: no lab-lens instruction or Anthropic framing remains (AC-1/2/3),
  selection is even-handed with comprehensiveness preserved (AC-4/AC-5), both copies + the live
  version are updated consistently (AC-6/AC-7), and the before/after validation passed before the
  change ran on the real schedule (AC-8).
- **Success metric.** For a representative set of scheduled runs after ship: **zero** "what this
  means for Anthropic"–style commentary sentences and **no** Anthropic-specific framing appear in
  any brief; no single lab is over-represented in selection per the agreed rubric; and coverage
  breadth/depth (tiers covered, headline/deep-dive counts, validation guardrails) matches
  pre-change levels — i.e. the brief is neutral **without** losing comprehensiveness. Subscriber
  delivery and the owner's copy are unaffected.
- **Handoff.** The AC-8 "neutral" rubric is decided (§7: structural-only + a light human read) and
  the ADR question is decided: **ADR-0008** formalizes the three-way lockstep + live-version-push
  procedure; the rubric itself needs no ADR. Next, the Developer applies the FR-1…FR-6 edits to both
  skill copies (in-repo + local, per ADR-0008 steps 1–2), the Reviewer runs the before/after
  comparison (AC-8), and the live Skills API version is pushed and **confirmed** only after the
  validation passes (ADR-0008 steps 3–6), with the mechanism + new version id recorded in the repo.
