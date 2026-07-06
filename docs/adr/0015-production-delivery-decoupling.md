# 0015. Production delivery decoupling: route the self-hosted MicroVM's daily send through the `deploy/delivery/` boundary (full decouple, four-artifact contract)

- Status: **Accepted — owner signed off 2026-07-06 ("go ahead").** The owner approved this design and
  directed a fully autonomous build with two standing constraints: **no subscriber fan-out during
  testing** (owner-only sends at most), and **extensive testing of the API**. Implementation proceeds,
  but the **production cut-over itself** (the image rebuild + `MicroVmExecutionRole` strip deploy +
  scheduled-deployment swap — Decisions D1/D3/D4 going *live*) is staged and **deferred to explicit
  owner action when awake**: the build and all testing happen against the `deploy/delivery/` stack and
  offline, leaving the live weekday schedule on the current in-VM monolith until the owner performs the
  final cut-over (D10).
- Date: 2026-07-06
- Deciders: owner (direction + scope), architect (Claude, design)
- Supersedes/amends: **ADR-0014 Decision 1's "Phase 7 cut-over is a no-op."** ADR-0014 ratified the
  hybrid (cloud for candidate/eval, `self_hosted` retained for production) and recorded that the
  Phase-7 production cut-over would be a **no-op** — production would keep delivering in-VM. This ADR
  **reverses that specific sub-decision**: production **content generation stays self-hosted** (the
  hybrid is unchanged), but production **delivery** (Polly narration, SES send, subscriber fan-out,
  S3 archival, deterministic HTML derivation) moves out of the MicroVM to the already-built
  `deploy/delivery/` boundary. The hybrid topology decision itself (where **content generation**
  runs) is **not** changed. Realizes the agent-system-redesign PRD's §8 Phase 7 ("Conditional
  production cut-over"), now made unconditional for the **delivery half** only.

## Context

Today's production path is the coupled monolith ADR-0004/0006/0007 built: one self-hosted MicroVM
image runs the whole weekday pipeline — research → write → narrate (Polly) → send (SES + subscriber
fan-out) → archive (S3) — in a single execution context, and `MicroVmExecutionRole`
(`deploy/managed-agent/cdk/managed_agent/stack.py`) holds **both** the Anthropic environment-key read
(worker auth) **and** the full AWS delivery permission set (Polly synth; S3 read/write on
`cowork-polly-tts-740353583786`; SES send gated by `ses:FromAddress: aibriefing@mschweier.com`;
DynamoDB `Query` on `brief-subscribers/index/status-index`). The thing that generates the brief can
email a real subscriber — the exact coupling FR-1 exists to remove.

The agent-system-redesign epic already built the decoupled delivery boundary (`deploy/delivery/`,
Phase 1) and it is **deployed live** but **locked**: `POST /deliver` + `GET /deliver/{deliveryId}`
(async trigger/poll, ADR-0014 Decision 2a), plus `GET /recent-briefs` (ADR-0014 Decision 2d), behind
API Gateway at `https://6nbe4wsng6.execute-api.us-east-1.amazonaws.com`. `derive_html()` (FR-2a),
`synthesize_audio()`, `send_all()`, and the archival helpers all live in
`deploy/delivery/functions/deliver/`. `GET /recent-briefs` is live and used (by cloud candidates);
`POST /deliver` is deployed but its bearer secret is **undistributed**, so nothing calls it. The
deterministic HTML template and the Polly/SES send path therefore sit fully built but connected to
nothing.

The owner, having weighed Option 1 (bring deterministic HTML in-VM) vs Option 2 (route delivery
through the API), chose **Option 2, full decouple** (2026-07-06), with an explicit scope
clarification: **the MicroVM must hand all four content artifacts across the boundary — brief
markdown, listening-script text, `candidates.json`, and `source-usage.json`** — so the delivery side
can derive HTML, synthesize audio, send, **and archive all four**.

**A concrete gap this surfaces, that the current `POST /deliver` contract does not yet handle.** The
live contract (`handler.py` `_validate_request_body`, `SUPPORTED_CONTRACT_VERSION = 1`) accepts only
`brief_markdown` + `listening_script` + `metadata`. Its archival leg archives `candidates.json` and
`source-usage.json` by reading the **delivery Lambda's own** `WORKING_FOLDER` (`/tmp`) —
`archive_candidates_file()` / `archive_source_usage_file()` do
`open(os.path.join(working_folder, filename))` (`brief_history.py:233/239/282/288`). That was correct
when the code was a lift-from `audio_email.py` running in the same place as the files, but in the
**decoupled** model those two files are produced in the **MicroVM's** workspace, not on the delivery
Lambda's filesystem — so as-is, decoupled production would **silently archive neither**
`candidates.json` nor `source-usage.json`. The owner's "all four artifacts" instruction is exactly
what closes this: they must travel **in the request body**, which is a reviewed **contract-version
bump (1 → 2)**.

## Decision

Route production delivery through `deploy/delivery/`'s `POST /deliver` boundary, fully, per the
following concrete design. Each sub-decision maps to build work and to a validation gate (§Rollout).

### D1 — Full decouple: the MicroVM holds zero AWS delivery IAM
`MicroVmExecutionRole` is stripped to **`ReadEnvironmentKey` (Secrets Manager, the Anthropic
environment key) + its own CloudWatch Logs writes only.** The `PollySynthesis`, `S3AudioReadWrite`
(+`s3:ListBucket`), SES-send, and DynamoDB-`Query` statements are **removed**. Those grants already
exist on the delivery Lambda's role and stay there, unchanged and no broader. After this, the
production content-generation context has the **same** AWS delivery posture as a cloud candidate:
none. (AC-1 / FR-1, now applied to production.)

### D2 — Four-artifact request contract, version 2
`POST /deliver`'s body contract bumps to `contractVersion: 2`, carrying **all four** content
artifacts plus metadata:
- `brief_markdown` (string, required) — unchanged.
- `listening_script` (string, required) — unchanged.
- `candidates` (JSON, required) — the stories-considered selection artifact, **new in v2**.
- `source_usage` (JSON, required) — the per-brief source-usage record (FR-8a), **new in v2**.
- `metadata` (object): `email_subject`, `brief_date`, `enable_subscriber_fanout`, and the D7
  idempotency key.

The archival leg is reworked to write `candidates.json` and `source-usage.json` **from the request
body** (their JSON serialized straight to `s3://…/briefs/<date>/`), **not** by reading the Lambda's
local `WORKING_FOLDER`. `archive_todays_brief()` (brief.md + brief.html + listening-script + audio
pointer) is unchanged. Net: the delivery side archives the full `briefs/<date>/` set exactly as the
in-VM path does today, so nothing downstream (welcome-send, feedback, `GET /recent-briefs`, tomorrow's
step 0) regresses. `brief_html` remains **not** a caller input (FR-2a — delivery derives it).

### D3 — Caller = `audio_email.py` refactored to a deterministic trigger-then-poll API client
The production delivery step stops doing Polly/SES/S3/DynamoDB work in-VM. `audio_email.py`'s
send-mode becomes a thin, deterministic **API client**: read the four artifacts from the workspace,
`POST /deliver` with the v2 body, then poll `GET /deliver/{deliveryId}` to a terminal
`succeeded`/`failed`. The trigger/poll loop lives in **code**, not in the agent's `initial_prompt`
(no LLM in the delivery-orchestration loop). `deployment.json`'s `initial_prompt` step 2 (the agent's
ad-hoc Markdown→HTML) and step 3's in-VM `audio_email.py` delivery mechanics are replaced accordingly
(a reviewed `deployment.json` change + a microVM image rebuild — the send code shrinks, `boto3`
delivery imports drop out).

### D4 — Step 0 (recent priors) moves to `GET /recent-briefs`
Production's `read-recent-briefs` step stops reading S3 directly (that grant is being removed, D1) and
instead calls the **existing** `GET /recent-briefs` route — the very route built for cloud candidates
(ADR-0014 Decision 2d) — reaching read/write parity with them. This requires the read-capability
signed token to be minted for the production run (D6).

### D5 — IAM: delivery role unchanged, MicroVM role stripped
No new delivery grants are minted anywhere — the delivery Lambda already holds exactly the scoped set
(`deploy/iam-policy.json` equivalents: Polly synth; S3 rw on the one bucket; SES send gated by
`ses:FromAddress: aibriefing@mschweier.com`; DynamoDB `Query` on the `status-index` GSI). D1's strip
of `MicroVmExecutionRole` is the only IAM change. **No new static access key** is created.

### D6 — Secret provisioning for the two tokens the MicroVM now needs
The MicroVM run needs two secrets it does not hold today: the **`POST /deliver` send bearer** and the
**`GET /recent-briefs` read token** signing key. Both are provided the same way the Anthropic
environment key already is — a Secrets Manager secret read by the launcher and injected into the run
environment — created empty in CDK and **populated out-of-band** (no secret in git). The read token
is short-lived and minted per-run (the existing `recent_briefs_token` scheme); the send bearer is the
delivery stack's existing `delivery_auth` secret, now also distributed to production.

### D7 — Idempotency across a caller retry (new; the current guard is insufficient alone)
The live idempotency guard (`_claim_delivery`, a conditional `UpdateItem`) dedupes the **async
self-invoke worker leg** for a given `deliveryId` — it does **not** protect against the **caller**
POSTing `/deliver` twice (e.g. a client timeout then retry), which today would mint two `deliveryId`s
and **double-send**. Because the caller is now an automated production client that may retry, add a
**caller-supplied idempotency key** (the run's `brief_date`, or a per-run token) to `POST /deliver`;
the trigger leg dedupes on it fail-closed (a second trigger for the same key returns the **existing**
`deliveryId` instead of creating a new delivery). The client's discipline is: **trigger once; on a
returned `deliveryId`, only ever poll — never re-trigger**; re-trigger only if the POST itself failed
before returning an id, where the idempotency key then prevents a duplicate. This mirrors ADR-0010's
webhook-idempotency posture for the launcher.

### D8 — Fail-safe preserved across the network boundary
CLAUDE.md's invariant — "never lose the brief over an audio/email glitch" — must hold across the new
hop. Two halves:
- **Delivery side stays internally fail-safe** (already true, kept): a Polly failure degrades to
  text-only (`audio_ok=False`, never blocks the send); per-recipient SES failures are isolated;
  archival is best-effort and never gates the send.
- **VM client side (new):** a `POST /deliver` that is unreachable / errors before returning an id is
  retried with bounded backoff (safe under D7's idempotency key); a delivery that reaches terminal
  `failed`, or never reaches terminal within a generous poll deadline, is **surfaced loudly** (logged
  as a hard failure and, where possible, an owner alert) — never silently swallowed. The brief is
  **not** lost: the four artifacts remain in the workspace/archive, and a failed production delivery
  is a visible, re-drivable event. The security-engineer confirms a missing/invalid bearer cannot
  fall open to an unauthenticated send (echoing the existing fail-closed `delivery_auth`).

### D9 — Confirmation email stays on the delivery side
`send_confirmation_email()` already runs inside `_run_delivery()` (the delivery Lambda), so the
owner's post-send "brief sent to N subscribers" confirmation moves with delivery automatically — no
change needed, and the VM no longer needs SES for it.

### D10 — Staged cut-over, never a hard swap (FR-14)
The in-VM delivery path stays revertable until the API path is validated. Cut-over is staged:
(1) validate the four-artifact contract + fail-safe end-to-end off the live schedule; (2) an
owner-only run (fan-out off) that goes through `POST /deliver` for real and lands in the owner's inbox;
(3) only then enable fan-out and let the live weekday schedule use the API path; (4) keep the old
image/deployment recoverable. The owner-facing brief and weekday cadence are validated **unchanged**
before the API path supersedes the in-VM one.

## Alternatives considered

- **Delivery-only decouple (send moves, reads/archival stay in-VM).** Rejected by the owner: it keeps
  S3 (and the archival responsibility) on the MicroVM, so `MicroVmExecutionRole` cannot be reduced to
  the environment key, only partially realizing FR-1, and it splits archival ownership between the VM
  and the delivery API. Full decouple is the coherent target for the security + single-delivery-
  codebase goals that motivated Option 2.
- **Keep contract v1; have the delivery side fetch `candidates.json`/`source-usage.json` from S3.**
  Rejected: on a production run those files are produced in the MicroVM, not yet in S3 at send time
  (S3 is the archival *destination*, not a handoff channel), and the whole point is to remove the
  VM's S3 access. Passing all four artifacts in the request body (D2) is the clean handoff and matches
  the owner's explicit instruction.
- **Agent-driven `curl` trigger/poll in `initial_prompt`.** Rejected: puts the delivery-orchestration
  loop (and retry/idempotency discipline) in non-deterministic LLM hands; D3's code-based client is
  testable and reviewable.
- **Do nothing / stay on the in-VM monolith (the ADR-0014 "Phase 7 no-op").** This is what this ADR
  reverses. Considered and set aside on the owner's decision; the trade-offs (distributed-system
  complexity vs FR-1 + single delivery codebase) were weighed explicitly before choosing Option 2.

## Consequences

Positive:
- **FR-1 fully realized for production:** the content-generation context can no longer email a
  subscriber — its role is the environment key only (D1/AC-1).
- **One delivery codebase:** `audio_email.py`'s in-VM Polly/SES/archival is retired in favor of the
  single `delivery_core.py`, removing the `audio_email.py` ↔ `delivery_core.py` duplication (and
  removing the temptation, from Option 1, to add a *third* `derive_html` copy in-VM).
- **Deterministic, consistent HTML in production** as a by-product (FR-2a), from one fixed template.
- **Smaller production image / narrower blast radius** for the content agent; delivery is exercised
  and monitored as its own unit.

Negative / follow-ups:
- **Production becomes a distributed system:** a network hop, an async trigger/poll handshake, a
  bearer/read-token dependency, and new failure modes (API down, token invalid, poll timeout) that a
  single in-VM process did not have. D7/D8 exist to contain this; the security + reviewer passes must
  confirm them.
- **Contract-v2 migration + a microVM image rebuild + a reviewed `deployment.json` change** are on the
  critical path.
- **Two production-critical stacks** (`deploy/managed-agent/` and `deploy/delivery/`) to deploy and
  monitor instead of one.
- **A new production secret to manage/rotate** (the send bearer), plus per-run minting of the read
  token.
- **ADR-0014 must be amended** (Decision 1's "Phase 7 no-op" reversed for the delivery half) and its
  index status updated; the reviewer confirms no stale "production delivers in-VM / Phase 7 is a
  no-op" note remains.

## Verification note

The async/API-Gateway transport and idempotency mechanics were already validated against AWS docs for
ADR-0014 Decision 2a (the 30s HTTP-API integration ceiling; Lambda async at-least-once semantics);
this ADR reuses that transport unchanged, so no new `aws-docs` lookup is required for it. The Developer
must, at build time, verify end-to-end: the v2 four-artifact contract round-trips and archives all
four to `briefs/<date>/`; the D7 idempotency key genuinely dedupes a duplicate trigger (a real
double-POST test, not assumed); the D8 fail-safe holds (a simulated `/deliver` outage and a simulated
Polly failure each leave the brief recoverable and loudly reported, never silently dropped); and the
D1 IAM strip is real (the deployed `MicroVmExecutionRole` shows only `ReadEnvironmentKey` + logs).
The security-engineer confirms the send bearer is fail-closed, the delivery grants are no broader than
today, and no new static key is minted.
