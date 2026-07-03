# 0010. Restore durable webhook idempotency in the launcher Lambda

- Status: Accepted
- Date: 2026-07-03
- Deciders: architect (Claude), Markus (human)

## Context

There is **no PRD** for this change — it is a direct security/reliability fix, not a product
feature. It reverses a previously-documented deliberate decision (the launcher's own module
docstring) and adds new AWS infrastructure + IAM, which is exactly the kind of significant,
security-relevant choice this project records as an ADR.

The launcher Lambda (`deploy/managed-agent/microvm/launcher/launcher.py`) boots one ephemeral
microVM per `session.status_run_started` webhook delivery from Anthropic. AWS's reference
implementation (`aws-samples/sample-lambda-microvm-claude-managed-agents`) guards this side effect
with a DynamoDB-backed **Powertools Idempotency** table keyed on the webhook `event_id`. When we
ported the launcher (PR #18, ADR-0006), we **deliberately dropped** that table, reasoning that the
pipeline fires a single scheduled session per weekday, webhook volume is negligible, and a
double-launch would at worst run the brief twice — "revisit if double-launches are ever actually
observed." The security-engineer flagged this as non-blocking finding **M1** on PR #18.

**Double-launches have now been observed live.** During a manual validation run on 2026-07-03, a
single session-start event produced multiple webhook deliveries (Anthropic retries any non-2xx and
can deliver concurrently). CloudWatch logs showed **two successful `RunMicrovm` calls ~4 minutes
apart for the same `session_id`**, plus several further attempts that hit the account-level microVM
memory quota. No duplicate email resulted *that time* — only because Anthropic's own session
work-item claiming happened to serialize the two winning microVMs far enough apart (4 min ≫ the
~2 s reclaim window). A near-simultaneous duplicate delivery could plausibly let **two microVMs
both claim and execute the same session's work concurrently**: two full pipeline runs, two Polly
syntheses, and — worst case — **duplicate SES sends to the owner and every subscriber**. That is a
real, recipient-visible failure the "revisit if observed" condition was written to catch. It has
been met.

The reference's approach was verified directly (fetched `src/functions/launcher.py` and
`template.yaml` from `main`) and its dependency `aws-lambda-powertools>=3.0` was confirmed to
install cleanly as **pure wheels** under this project's platform-locked bundling constraints
(`--platform manylinux2014_aarch64 --implementation cp --python-version 3.13 --abi cp313
--only-binary=:all:`): version 3.31.0 resolves with the `idempotency` submodule and only light
pure-Python transitive deps (`jmespath`, `typing_extensions`). This is **not** the
`standardwebhooks` sdist-only packaging trap documented in
`deploy/managed-agent/microvm/launcher/requirements.txt` — it needs no unlocked second pip pass.

## Decision

**We will restore the reference implementation's DynamoDB-backed idempotency by faithfully porting
its Powertools Idempotency approach.** Specifically:

1. **New DynamoDB table** in `deploy/managed-agent/cdk/managed_agent/stack.py`, built by a new
   `_build_idempotency_table()` method following the same conventions as the existing resources in
   that stack (like the Secrets Manager secrets, a private helper returning the construct, wired in
   `__init__`, exposed via `add_to_policy` statements on the launcher role). Shape:
   - Construct id **`IdempotencyTable`**; `table_name = f"{self.project_name}-idempotency"`
     (i.e. `daily-brief-agent-idempotency`).
   - `aws_cdk.aws_dynamodb`: `partition_key = Attribute(name="id", type=STRING)`; **no** sort key.
   - `billing_mode = PAY_PER_REQUEST` (very low, bursty webhook volume — no capacity to plan).
   - `time_to_live_attribute = "expiration"` (Powertools' default TTL attribute name).
   - `encryption = TableEncryption.AWS_MANAGED` (SSE on; matches the reference's `SSEEnabled`).
   - **`removal_policy = RemovalPolicy.DESTROY`** — this is transient dedup state, disposable with
     the stack (the reference sets `DeletionPolicy: Delete`). This deliberately differs from the
     `RETAIN` used on the secrets/buckets in this stack, because losing this table on teardown loses
     nothing of value. The Developer must add a short comment saying so, since it breaks the stack's
     otherwise-uniform RETAIN convention.

2. **No new Lambda and no new role.** The **existing** `LauncherFunctionRole` (built in
   `_build_launcher_function()`) gains exactly four scoped statements on the one table ARN —
   `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem`, `dynamodb:DeleteItem`, `Resource`
   = the table's ARN only (a new `add_to_policy` with `sid="IdempotencyStore"`). The launcher gets a
   new `IDEMPOTENCY_TABLE` environment variable set to the table name. Nothing else on that role
   changes.

3. **Code:** restore the reference's idempotency wiring in `launcher.py` — a
   `DynamoDBPersistenceLayer(table_name=..., expiry_attr="expiration")`, an `IdempotencyConfig`
   with `event_key_jmespath="event_id"` and `expires_after_seconds` set to the TTL below, and an
   `@idempotent_function`-wrapped executor around `_launch_and_dispatch`. The `handler` reads
   `IDEMPOTENCY_TABLE` and installs the wrapped executor; `Launcher.handle` catches
   `IdempotencyAlreadyInProgressError` and returns **`{"statusCode": 200, "body": "in progress"}`**.
   Preserve this port's own structured `_log` JSON logging and its **fail-closed** behavior — do
   **not** adopt the reference's `SIGNING_SECRET_ARN`-optional handler; keep this port's stricter
   "no signing secret ⇒ 500" check. The Powertools `Logger` is not required; keep `_log`.

4. **TTL = 24 hours** (`_IDEMPOTENCY_TTL_SECONDS = 86400`), **not** the reference's 8-hour
   `DEFAULT_MAX_LIFETIME_SECONDS`. Rationale below.

5. **200 for the already-in-progress duplicate is correct** and we keep it. Anthropic retries on any
   non-2xx and auto-disables the endpoint after ~20 consecutive failures; a duplicate delivery that
   we have already handled (or are handling) is *success* from the caller's perspective — returning
   200 tells Anthropic "handled, stop retrying," which is exactly what dedup wants. Returning a 4xx
   would risk the endpoint being disabled; a 5xx would invite the very retry storm we are guarding
   against.

### TTL decision (24 hours)

The idempotency record must outlive the **entire window during which a retried/concurrent delivery
of the same `event_id` could still arrive**, but need not outlive it by much — the record is pure
dedup state, and a longer TTL only widens the window in which a legitimately *distinct* future
run's collision could be (wrongly) suppressed. Two forces:

- **Floor:** Anthropic's webhook retries and its own webhook-freshness check bound how late a
  redelivery of the *same* event can appear. That window is on the order of minutes-to-hours, well
  under a day. The launcher already rejects stale deliveries via the SDK's freshness check in
  `verify_signature`, so a redelivery arriving *after* freshness expiry never reaches the idempotent
  executor at all — meaning the TTL only needs to cover the *fresh*-redelivery window.
- **Ceiling:** this pipeline emits **one scheduled session per weekday**. Distinct real runs are
  always ≥ ~24 h apart (longer across weekends). Each carries its own unique `event_id`, so
  cross-run collision is not a correctness concern regardless of TTL — but keeping the TTL at ~one
  inter-run gap means a stale record is always expired well before it could ever be confused for
  operational state, and the table self-cleans.

**24 hours** sits comfortably above the fresh-redelivery floor and at roughly one inter-run gap,
and is materially tighter than the reference's 8 h tied to microVM max-lifetime. We deliberately
**decouple the idempotency TTL from `DEFAULT_MAX_LIFETIME_SECONDS`**: the reference conflated them,
but the microVM's 8 h execution ceiling (ADR-0006's idle-policy fix) and "how long could a
duplicate webhook still arrive" are unrelated quantities. Define it as a named constant
`_IDEMPOTENCY_TTL_SECONDS = 86400` in `launcher.py` (or `shared/constants.py`) with a comment
pointing here, so it is not silently re-coupled to the lifetime constant on a future edit.

### Packaging

Add `aws-lambda-powertools>=3.0` to
`deploy/managed-agent/microvm/launcher/requirements.txt` as a normal **locked** dependency (it and
its transitives — `jmespath`, `typing_extensions` — are pure wheels, confirmed to resolve under the
`--only-binary=:all:` platform lock). It goes through `_LocalPipBundling`'s *first* (locked) pip
pass, **not** the `standardwebhooks` unlocked-second-pass carve-out. Add a one-line requirements
comment noting it is wheel-clean (unlike `standardwebhooks`) so a future reader does not assume it
needs the carve-out. No change to the bundling logic in `stack.py` is required.

## Alternatives considered

- **Do nothing / keep the drop.** The original bet. **Rejected**: its explicit revisit-trigger
  ("if double-launches are ever actually observed") has fired, with a plausible path to
  recipient-visible duplicate email — the exact harm the reference guards against. Leaving it is now
  an accepted known defect, not a deferred hypothetical.

- **In-memory / per-instance dedup set** (a module-level `set` of seen `event_id`s). **Rejected**:
  does not survive cold starts and is not shared across concurrently-warm Lambda instances — it
  misses both of the observed failure modes (a retry landing on a fresh container, and two
  concurrent deliveries fanning to two instances). It would give false confidence.

- **A conditional `PutItem` we write by hand** (`attribute_not_exists(id)`) instead of Powertools.
  Fewer dependencies. **Rejected**: it re-implements — imperfectly — the concurrency-safe,
  TTL-managed, in-progress-vs-complete state machine Powertools already provides (the reference's
  whole point), for a trivial packaging saving that does not even apply here (Powertools is
  wheel-clean). Diverging from the reference on a beta integration also costs us the reference as a
  maintenance template. Faithful port is the "boring, well-supported" choice.

- **Idempotency at a different layer** (e.g. dedup inside the microVM worker, or relying solely on
  Anthropic's ~2 s work-item reclaim). **Rejected**: the reclaim window is precisely what *failed*
  to protect us in the observed incident (4 min ≫ 2 s), and moving the guard into the worker means
  a microVM has already been booted — it does not prevent the duplicate `RunMicrovm`, only maybe the
  duplicate work, and only if the worker-side logic is itself correct. The launcher is the right,
  earliest chokepoint.

- **TTL = 8 h (copy the reference).** **Rejected** in favor of an explicit 24 h decoupled from
  microVM lifetime — see the TTL decision above.

## Consequences

Positive:
- Exactly one microVM launches per webhook `event_id`, durably across cold starts and concurrent
  instances — closing security finding M1 and removing the duplicate-SES-send risk to the owner and
  all subscribers.
- Faithful to the reference implementation, so the reference remains our maintenance/upgrade
  template for this beta integration (consistent with ADR-0006's "adapt, don't reinvent").
- Least-privilege preserved: four item-level actions on one table ARN, added to the existing
  launcher role — no new principal, no new trust relationship.
- The table self-cleans via TTL and is disposable with the stack.

Negative / follow-ups:
- **New always-present AWS resource** (one PAY_PER_REQUEST DynamoDB table) and a new
  `IDEMPOTENCY_TABLE` env var the launcher now depends on. Cost is negligible at this volume but
  non-zero.
- **`removal_policy=DESTROY` breaks the stack's uniform RETAIN convention** — deliberate (transient
  state) and must be commented in code so it is not "corrected" later.
- **New runtime dependency** (`aws-lambda-powertools`) enlarges the launcher bundle and adds a beta
  integration's dependency surface; pinned by a version floor and confirmed wheel-clean.
- **Reverses a shipped, documented decision.** The launcher module docstring's "adaptation note"
  (which currently says the port *drops* the idempotency table) must be updated to say it is now
  restored, referencing this ADR — otherwise the code will contradict its own history.
- **Reversible.** If the beta surface changes or the table proves unnecessary, removing it is a
  contained change (drop the table, the four IAM statements, the env var, and the wrapper). Nothing
  here is a one-way door.

## Verification note

Rests on the reference implementation's confirmed pattern and on Powertools Idempotency's documented
DynamoDB schema (partition key `id`, TTL attribute `expiration`) plus Anthropic's documented webhook
retry/freshness behavior — no account-specific service limit gated this, so no `aws-docs` MCP lookup
was required. At implementation time the Developer should add a launcher test that a second delivery
of the same `event_id` returns 200 without a second `RunMicrovm`, and confirm `cdk synth` emits the
table with the four scoped item actions on the launcher role.
