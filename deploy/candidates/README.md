# Candidate declarations — git-native versioning + sync

> Built per `docs/adr/0014-agent-system-redesign-topology.md`'s **Decision 2c** (the
> "third pass," corrected after live-confirming the Agents API's native
> update-in-place versioning) and `docs/prd/agent-system-redesign.md` FR-9…FR-12,
> AC-9…AC-12. This directory is **NOT a CDK app** and stands up **NO AWS
> infrastructure** — no Lambda, no stack, no cloud resource of any kind. It is a plain
> git-tracked directory convention plus a Python CLI tool (`sync.py`) the operator
> runs locally, calling the Anthropic API directly.

## What a "candidate" is

A **candidate** is a declarative description of a Claude Platform agent (or a
coordinator + sub-agents multi-agent graph) that can research/write/narrate a daily AI
brief — a specific combination of model, system prompt, task prompt, skill
reference(s), and tunable parameters. Every candidate lives in its own directory here,
`deploy/candidates/<slug>/`, tracked in git like any other source file.

**`deploy/candidates/production-baseline/` re-expresses the real, live production
configuration as a candidate — built, synced, triggered, and validated in Phase 5**
(PRD §8 Phase 5, FR-14/AC-14; see "Phase 5 live validation," below, for the full
build/sync/trigger/comparison record). It has its own real, separate `agent_id`
(`agent_017KynQYfK2gNXtJBDeWe81B`), distinct from the real live production agent
(`agent_01EswBTose8dnTAUDbGvzdLq`), which this candidate can never mutate or
invoke. Separately, `smoke-test-example/` (documented above) and two clearly-fake
**synthetic test fixtures** (`tests/fixtures/example-single-agent/`,
`tests/fixtures/example-multi-agent/`) exist purely to exercise the sync script's
logic in tests — never deploy the synthetic fixtures for real.

## Directory schema

A single-agent candidate:

```
deploy/candidates/<slug>/
  candidate.json      # slug, description, composition, schedule intent, and "agent_id"
  agent.json          # non-prose structure: name, description, tools, mcp_servers
  model.txt           # the model id -- diffable alone
  system-prompt.md     # the agent system prompt -- diffable alone
  task-prompt.md        # the deployment initial_prompt (the run task) -- diffable alone
  skills.json           # [{skill_id, version}] concrete pinned versions -- diffable alone
  parameters.json       # effort / thinking budget / other tunables -- diffable alone
  skill/                 # OPTIONAL: candidate-owned skill source (SKILL.md, sources.md, ...)
```

A multi-agent candidate adds one file:

```
deploy/candidates/<slug>/
  ... (all of the above, describing the COORDINATOR) ...
  multiagent.json      # the coordinator's sub-agent roster (see below)
```

### Why each file is separate (FR-9/AC-9: independently-diffable dimensions)

Today's production config bakes model, prompt, and delivery orchestration into one
~3KB inline `initial_prompt` string in `deployment.json` — a single change anywhere
shows up as one opaque blob diff. Every dimension here is its own small file
specifically so a `git diff` on a candidate directory makes it immediately obvious
*which* dimension changed:

| File | Holds | Why separate |
|---|---|---|
| `candidate.json` | `slug`, `description`, `composition`, `schedule_intent`, and the coordinator/sole agent's stable **`agent_id`** | Metadata about the candidate itself, not its behavior. The `agent_id` field is written **once**, at first sync, and never changes afterward — an unchanged id never pollutes a real declaration diff. |
| `agent.json` | `name`, `description`, `tools`, `mcp_servers` — non-prose structure only | Kept deliberately free of prose so a tool/MCP-server change diffs cleanly, separate from any prompt change. |
| `model.txt` | The model id (e.g. `claude-sonnet-5`), one line | A model swap is often the whole point of a candidate experiment — it should be a one-line diff, not buried in a JSON blob. |
| `system-prompt.md` | The agent's `system` prompt | The identity/approach prompt — changes independently from the per-run task. |
| `task-prompt.md` | The deployment's `initial_prompt` (the per-run task) | Distinct from the system prompt: this is "what to do this run," reusable across many sessions of the same agent identity. Optional (may be empty) for a sub-agent that has no run-triggering role of its own. |
| `skills.json` | `[{skill_id, version}, ...]` — **concrete, pinned** skill version(s), never the moving `"latest"` label | A skill-content change is its own, separately-reviewable event (see "Skill content" below); recording a concrete numeric version makes the candidate reproducible. |
| `parameters.json` | Effort / thinking-budget / other tunables | Orthogonal to model/prompt — a parameter sweep should diff only this file. |
| `skill/` (optional) | The candidate's OWN skill source, if it ships one instead of referencing an existing Skills-API resource | See "Skill content," below. |

### Multi-agent candidates use the SAME schema (FR-10/AC-10)

A multi-agent candidate does **not** need a fundamentally different directory shape.
The **coordinator** is simply the top-level agent — its `agent.json` /
`model.txt` / `system-prompt.md` / `task-prompt.md` / `skills.json` /
`parameters.json` describe the coordinator exactly like a single-agent candidate's
sole agent. The **only** addition is `multiagent.json`, which declares the
coordinator's sub-agent roster inline:

```json
{
  "type": "coordinator",
  "agents": [
    {
      "entry": { "type": "custom" },
      "name": "researcher-sub-agent",
      "description": "...",
      "model": "claude-...",
      "system_prompt": "...",
      "task_prompt": "",
      "tools": [...],
      "mcp_servers": [],
      "skills": [],
      "parameters": {},
      "agent_id": "agent_..."
    }
  ]
}
```

Each roster entry carries its **own** model/system-prompt/skills/parameters — a
sub-agent is declared exactly as richly as the coordinator, just inline rather than in
its own set of files (there's only ever one coordinator per candidate, so one extra
level of file-splitting per sub-agent wasn't judged worth the added directory depth;
see "Judgment calls," below). Each roster entry also carries its **own** stable
`agent_id`, written once at that sub-agent's first sync, exactly like the
coordinator's `agent_id` in `candidate.json`. A single-agent candidate simply has no
`multiagent.json` at all.

### Skill content

A candidate can either:
- **Reference an existing Skills-API resource** — `skills.json` names a `skill_id`
  with a concrete `version` already known, and no `skill/` subdirectory exists.
- **Push a new version to an already-existing Skills-API resource it owns** — a
  `skill/` subdirectory (e.g. `SKILL.md`, `sources.md`) plus a `skills.json` entry
  naming that `skill_id` with **no** `version` yet (a "please create a version for
  me" placeholder). The sync script zips `skill/`'s contents and calls
  `POST /v1/skills/{skill_id}/versions` on the candidate's first sync, then writes
  the resulting concrete version number back into `skills.json`.
- **Own a genuinely BRAND-NEW skill** (Phase 3 addition) — a `skill/` subdirectory,
  with `skills.json` starting **empty** (`[]`, no `skill_id` at all yet anywhere).
  The sync script calls `POST /v1/skills` (a **different** endpoint from the one
  above — creates both the skill's id AND its first version in one call) and writes
  both the newly-minted `skill_id` and the version back into `skills.json`. This is
  exactly how `smoke-test-example/` (below) was first synced for real.

**Both skill-push cases require the SAME zip shape, live-confirmed 2026-07-06 (Phase
3) — this corrects an earlier, untested assumption.** The zip's files must sit
inside **one top-level folder**, and that folder's name must **exactly match** the
`name:` field in the zipped `SKILL.md`'s YAML front matter. Phase 2 (which never
created a real Skill resource — see "Judgment calls," below) assumed the
version-push endpoint (`POST /v1/skills/{id}/versions`) accepted a *flattened* zip
with no wrapping folder, unlike the creation endpoint. A real Phase 3 push using
that flattened shape failed with a genuine 400
(`"Zip must contain a top-level folder with all files inside it, including
SKILL.md"`), and a follow-up probe with a deliberately mismatched folder name
confirmed the folder-name-must-match check applies to *both* endpoints identically.
`deploy/managed-agent/README.md`'s own documented version-push command
(`cd deploy/managed-agent/skills; zip -r -q ... daily-ai-brief -x "*.DS_Store"`,
run from **one directory above** `daily-ai-brief/`) already produced this exact
shape by construction — which is why that real, earlier push never hit the bug this
phase found. `sync.py`'s `_zip_skill_source()` now builds this same wrapping-folder
shape for both skill-push cases.

## Running the sync script

```bash
cd deploy/candidates
python3 -m venv .venv          # once
.venv/bin/pip install -r requirements-dev.txt

export ANTHROPIC_API_KEY=$(cat ~/.anthropic-managed-agents/ant-api-key.txt)
.venv/bin/python3 sync.py <path-to-candidate-directory>

# e.g., against the synthetic test fixtures (SAFE -- these are what the test suite
# also uses; running sync.py against them for real WILL create real, throwaway Agent
# resources on the live account, since there is no delete/archive primitive
# confirmed for agents -- see "Judgment calls" below before doing this):
.venv/bin/python3 sync.py tests/fixtures/example-single-agent
```

The Anthropic API key is **never** hardcoded, logged, or committed — it is read from
`$ANTHROPIC_API_KEY` at run time, this repo's established local-CLI convention (see
`deploy/managed-agent/README.md`'s Skills-API version-push section for the same
pattern).

### First sync vs. update — how the script decides

The script inspects `candidate.json`'s `agent_id` field:

- **No `agent_id` yet → first sync (create).** For any candidate-owned skill (a
  `skill/` subdirectory present with an un-versioned `skills.json` entry), the script
  pushes a new Skills-API version **first**, records the concrete version into
  `skills.json`, **then** creates the agent(s) via `POST /v1/agents`, and writes the
  returned `agent_id`(s) back into `candidate.json` (and, for a multi-agent candidate,
  each sub-agent's id into `multiagent.json`'s roster). For a multi-agent candidate,
  each sub-agent is created **before** the coordinator (the coordinator's `entry.agent`
  field needs the sub-agent's real, freshly-minted id) — and if a sub-agent's id is
  **already present** (from a prior, partially-failed first-sync attempt where the
  sub-agent create succeeded but the subsequent coordinator create failed), the script
  reuses it rather than creating a second, duplicate, permanently-orphaned sub-agent —
  the same resumability guarantee as the skill-version push, below.
- **`agent_id` already present → update in place.** **First**, if the candidate owns
  a `skill/` directory, the script checks whether its content has changed since the
  last push — **entirely locally, no network call** — by comparing a SHA-256 hash of
  `skill/`'s files against a `content_hash` field the sync script itself recorded
  into `skills.json` at the last push (Phase 3 addition; see "Skill-content changes
  on an update sync," below, for the full mechanism and why this is *not* a
  forbidden bespoke index). If the hash differs, it pushes a new Skills-API version
  and rewrites `skills.json`'s `version`/`content_hash`. **Then**, for each agent
  (re-loaded fresh if a skill was just pushed, so it sees the new pinned version),
  the script reads its **current live state** (`GET /v1/agents/{id}`) and compares it
  against the local declaration. Only a genuinely-**changed** agent gets updated
  (`POST /v1/agents/{id}` with the exact `version` just read — never a cached/assumed
  value). If that update returns `409` (someone/something else updated the agent in
  between), the script re-reads the current version once and retries — it never
  blindly overwrites.

An **unchanged** declaration is a full **no-op at the mutation level**: zero
create/update calls are made (the script still issues one read-only `GET` per agent
to *detect* "unchanged" in the first place — see "Judgment calls," below, for why that
GET is unavoidable and is not itself a violation of the "no bespoke duplicate-of-git
index" principle).

### Skill-content changes on an update sync (Phase 3, AC-5) — the direct proof mechanism

A candidate-owned skill's **first** version is pushed only on **first sync**
(above). A **later** edit to that same `skill/` directory needs its own detection
mechanism on an **update** sync — Phase 2 never built this (it only ever pushed a
skill's very first version); Phase 3 added it, since proving AC-5 ("a Skills-API
push alone reaches a running candidate, no image rebuild") requires actually editing
an already-synced candidate's skill and re-syncing.

**How it's detected — no bespoke duplicate-of-git index.** The sync script computes
a SHA-256 hash over `skill/`'s file paths + contents (sorted, for determinism) and
compares it against a `content_hash` field it itself wrote into `skills.json`'s
pinned entry the last time it pushed. This is a **local-only comparison — no network
call needed** to detect "did the skill change." `content_hash` lives as an **extra
field on an already-git-tracked file** (`skills.json`, which already carries the
not-derivable-from-git `skill_id`/`version`) — not a new, separate side-file — so it
doesn't introduce the kind of bespoke duplicate-of-git index Decision 2c/FR-12
disfavors: it's derived from, and travels with, the git-tracked `skill/` content
itself. **`content_hash` is stripped before being sent to the Agents API**
(`AgentDeclaration.to_agent_body()` filters it out of each `skills[]` entry) — it is
purely local bookkeeping, never part of what the agent's live declaration actually
is, and leaking it into the request body would also make the "did this change"
comparison against the live agent spuriously fail on every sync.

**What happens on a real content change:** (1) push a new Skills-API version
(`POST /v1/skills/{id}/versions`) from the edited `skill/` content; (2) rewrite
`skills.json`'s `version` and `content_hash`; (3) re-load the candidate so the
in-memory declaration reflects the fresh pin; (4) compare the re-loaded declaration
against the agent's live state — it now differs (the live agent still references
the *old* skill version), so **the referencing agent is updated in place**
(`POST /v1/agents/{id}`, same `agent_id`, incremented `version`) to reference the
new skill version. **No agent recreation, no container/image rebuild, at any
point** — this is the exact chain that closes the ADR-0008 image-rebuild failure
mode ("a Skills-API push alone did not reach the running session because the skill
was baked into the image"): here, a skill push is *always* followed through to an
agent update that actually references it. See "Phase 3 live validation," below, for
a real run proving this end to end (including the actual output changing between
the two skill versions).

For a **multi-agent** candidate, this fits into the existing ordered two-step: if a
sub-agent's or the coordinator's own `skill/` content changed, that agent's skill
push + re-pin happens as part of its own update, ahead of (or as part of) the
existing sub-agent-then-coordinator ordering documented below.

**The script never runs `git add`/`git commit` itself.** After a sync that created or
updated anything, it prints a reminder to review and commit the resulting diff
yourself — usually just a new `agent_id` field or a newly-pinned skill version.

### The multi-agent two-step ordering — the easiest thing to get backwards

**A coordinator does NOT automatically pick up a new version of a sub-agent it
references.** This is confirmed, documented platform behavior (see the ADR's "What I
verified live" section): a coordinator's `multiagent.agents` roster keeps whichever
sub-agent version was current when the coordinator itself was last created/updated —
even if the roster reference omits an explicit `version`. If you update ONLY the
sub-agent and stop there, **the coordinator will keep running the OLD sub-agent
version indefinitely**, silently — nothing about a sub-agent update alone changes what
the coordinator actually delegates to.

So the sync script always performs an **ordered two-step** whenever a multi-agent
candidate's sub-agent(s) changed:

1. **Update the changed sub-agent(s) first.**
2. **Then** perform a **follow-up update of the coordinator itself** — even if the
   coordinator's own declaration (model/prompt/tools) is otherwise unchanged — purely
   so its roster re-pins to reference the sub-agent's new version.

The same ordering applies, in reverse emphasis, to **creation**: on a first sync, the
sub-agent(s) must be created **before** the coordinator, because the coordinator's
`multiagent.agents[].entry.agent` field needs the sub-agent's real, freshly-minted
`agent_id` — which doesn't exist until the sub-agent's own create call returns.

**If you only remember one thing about this script: after changing a sub-agent, the
coordinator ALSO needs a sync pass, or your change has no effect.** The script handles
this automatically — you never need to invoke it twice — but if you ever bypass the
script and call the Agents API by hand, this is the exact mistake to avoid.

## The shared `cloud` environment (agent-system-redesign epic Phase 3)

Per ADR-0014 Decision 1: **one** `cloud` Managed Agents environment is shared by
**every** candidate — there is no per-candidate environment, and no 1:1
agent/environment binding at the platform level (confirmed: a session references an
`agent` id and an `environment_id` **separately**). This environment is created
**once**, deliberately, as a **permanent** resource — there is no confirmed
delete/archive primitive for an environment (mirroring the same gap for agents), so
recreating it is not something to do lightly.

**How it was created (2026-07-06, confirmed live — not assumed from docs):**
`POST /v1/environments` (the same base URL/headers as every other Managed Agents
call: `x-api-key`, `anthropic-version: 2023-06-01`,
`anthropic-beta: managed-agents-2026-04-01`), body
`{"name": ..., "description": ..., "config": {"type": "cloud"}}`. Verified via a
read-only `GET /v1/environments` first (confirmed the resource collection and that
the existing production `self_hosted` environment was already listed there), then
the real `POST`. The response confirmed
`config: {"type": "cloud", "networking": {"type": "unrestricted"}, ...}` — matching
the ADR's own live finding that `unrestricted` networking is the account's default,
not something that needs to be explicitly requested.

**Its id is recorded in `deploy/candidates/environment.json`** — a single, small,
git-tracked JSON file (`{"environment_id": "env_...", "type": "cloud", ...}`), the
one shared config `trigger.py` reads at run time. It is **not** duplicated into each
candidate's own files — Decision 2c's "the `environment_id` is not a per-candidate
fact at all" is followed literally here.

**Do not recreate this environment.** If you ever believe it's become unusable, that
is a decision for a human, not a script — see the ADR's own note that this whole
class of resource (agents, environments) has no confirmed way to be torn down once
created.

## Triggering a candidate run (`trigger.py`)

`trigger.py` (a thin CLI over `candidate_sync/trigger.py`) generalizes
`deploy/eval/`'s already-proven trigger-and-poll mechanics (create a temporary,
non-cron Deployment against an agent + environment; `/run` it; poll the Sessions API
for a terminal status; archive the Deployment when done) to work against **any**
candidate's `agent_id` — not one hardcoded production pair — plus the ONE shared
`cloud` environment above. This is what makes FR-6/FR-7 concrete: triggering a
candidate needs **zero AWS infrastructure**, and (because there is no delivery path
reachable from a `cloud` sandbox at all in this redesign) a candidate run can
**never** reach a real subscriber.

```bash
cd deploy/candidates
export ANTHROPIC_API_KEY=$(cat ~/.anthropic-managed-agents/ant-api-key.txt)
.venv/bin/python3 trigger.py <path-to-candidate-directory> ["<optional task prompt override>"]

# e.g., against the real, permanent smoke-test-example candidate (see "Phase 3 live
# validation" below for what this actually produced):
.venv/bin/python3 trigger.py smoke-test-example
```

If no override is given, the candidate's own `task-prompt.md` is used (the same
file the sync script reads for the agent's declared per-run task). The CLI prints
the deployment id, session id, final status, and every file successfully recovered
via `cat` from the session's event stream.

**How a candidate's output is retrieved — the Sessions events API, NOT the Files
API.** ADR-0014 Decision 1 live-refuted the assumption that an agent-written file
becomes a downloadable Files-API `file_id` (confirmed: `GET /v1/files` stayed empty
after a probe agent wrote files; there is no `/v1/sessions/{id}/files`
sub-resource). The confirmed substitute: a `bash` tool_result from a plain
`cat <path>` command echoes the exact file body in the session's event stream
(`GET /v1/sessions/{id}/events`). So every candidate task prompt in this repo should
explicitly ask the agent to `cat` back whatever it writes, and
`candidate_sync.trigger.fetch_catted_file_contents()` parses the event stream for
those tool_result bodies — no AWS, no S3, no Files API involved at any point.

**A real race this phase found and fixed: the session-status endpoint can report
terminal BEFORE the events endpoint has caught up.** On a real run,
`GET /v1/sessions/{id}` reported `status: "idle"` on the very first poll, while
`GET /v1/sessions/{id}/events` at that exact moment returned only 4 partial events —
none of the agent's actual tool calls. Re-fetching moments later returned the full,
complete transcript. `trigger.py`'s `_wait_for_settled_events()` closes this gap by
retrying the events fetch (with a small bounded budget, injectable for tests) until
the event stream **itself** contains a terminal `session.status_*` event, rather
than trusting the separate session-status field alone. See "Phase 3 live
validation," below, for exactly how this was found.

**Cost/archival discipline (mirroring `deploy/eval/`'s own proven pattern):** every
triggered run creates a **temporary** Deployment, and `run_candidate()` **always**
archives it (`POST /v1/deployments/{id}/archive`) in a `finally` block — on success,
on a failed session, on a poll timeout, **and on `start_session()` itself raising**
(e.g. a transient 5xx on `POST /v1/deployments/{id}/run`) alike — so no callable
temporary deployment is ever left behind. Deployments are the one resource type in
this whole mechanism that genuinely **is** confirmed archivable (unlike
agents/environments, per README §6 of `deploy/managed-agent/README.md` and the
ADR's own note).

**Corrected (reviewer + security-engineer, independently converged on the same
bug):** `create_temporary_deployment()` and `start_session()` originally both ran
**before** the `try`/`finally` that owns the archive call, so a `start_session()`
failure — distinct from a FAILED session status or a poll timeout, both raised
**inside** the try block and already correctly archived — propagated with **zero**
archive call, a genuinely leaked, permanently-callable temporary deployment. Fixed
by moving `start_session()` inside the `try`; `deployment_id` is assigned once
(right after the create call succeeds) and the `finally` unconditionally archives
it, since by the time that `try`/`finally` is reached the deployment is guaranteed
to already exist. A regression test
(`test_run_candidate_archives_even_when_start_session_itself_raises`) reproduces
the exact scenario and was confirmed, directly, to fail against the pre-fix code
(zero archive calls) and pass against the fix.

## Reading historical state

Two complementary sources, per Decision 2c:

### Historical *declaration* state — git, no rollback

Because a candidate's declaration is just ordinary tracked files, any earlier version
is readable with plain git — **no repo checkout, no `git reset`, no rollback of any
kind**:

```bash
# What did this candidate's system prompt look like at an earlier commit/tag?
git show <commit-or-tag>:deploy/candidates/<slug>/system-prompt.md

# What did the WHOLE candidate.json look like then?
git show <commit-or-tag>:deploy/candidates/<slug>/candidate.json
```

`git show <ref>:<path>` reads a file's content at that historical ref directly — it
never touches `HEAD` or the working tree, so your current checkout is completely
unaffected. This directly answers the owner's original question: "how will the eval
system read a previous version of a prompt without rolling back the repo." (See
`tests/test_git_history_no_rollback.py` for a from-scratch, real-git proof of this
mechanism — not just an assertion in this doc.)

### Historical *live* state — Claude Platform's own version history

Because a candidate keeps exactly **one stable `agent_id` for its entire life**, and
each sync **updates** that same agent in place, Claude Platform tracks the agent's
full operational version history natively:

```bash
curl -s "https://api.anthropic.com/v1/agents/<agent_id>/versions" \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: managed-agents-2026-04-01"
```

This lists **every version the candidate has actually run as**, with full content and
`updated_at` timestamps — a second, complementary source: git is authoritative for
*what you intended to declare* at any point in history; Platform's version list is the
operational source of truth for *what actually ran* (which could, in principle, lag a
sync or diverge via a manual console edit — reading it directly is strictly more
truthful for "what ran" than re-deriving it from git).

## Why there's no `registry.json` and no per-sync git tag (FR-12/AC-12)

Two earlier design passes (recorded in the ADR's Decision 2c history) considered a
bespoke `registry.json` mapping slug → live resource ids, and later an annotated git
tag per sync event. Both are unnecessary once the Agents API's native update-in-place
versioning is used correctly: a candidate has **exactly one** live `agent_id`, ever,
generated once at first sync and never superseded. The **one** fact that isn't
derivable from git content alone — "what is this candidate's live agent id" — is
therefore recorded as a **plain `"agent_id"` field inside the candidate's own
`candidate.json`**, populated once and committed as an ordinary change. This is
deliberately **not** an index in the sense the owner pushed back on: it doesn't grow
(one field, not a slug→id table that accretes a row per candidate), it isn't rewritten
on every sync (an unchanged agent id means no diff), and it doesn't duplicate any
*content* git already versions (the model/prompts/skills/params live in their own
tracked files — the id is just "this candidate's one address").

## Judgment calls

- **The "unchanged declaration → no-op" GET is a real, necessary API call, not a
  contradiction of "zero calls."** Detecting "did this agent's declaration change
  since its last sync" requires comparing against *some* record of "last sync's
  state" — and the whole point of Decision 2c is that there is deliberately **no**
  local side-file/index duplicating that record. The live agent resource itself
  (fetched via `GET /v1/agents/{id}`) **is** that record. So the sync script issues
  exactly one read-only `GET` per agent every run, even when nothing changed, and
  treats that as the correct, minimal no-op floor: **zero create/update (mutation)
  calls**, not literally zero HTTP of any kind. This is the ADR's own precise
  wording ("Re-running against an unchanged declaration is a full no-op: … no create,
  no update call") — flagging this explicitly since a stronger "zero API calls at
  all" reading is achievable only by reintroducing exactly the kind of bespoke,
  duplicate-of-git local cache the ADR/PRD deliberately reject.
- **Sub-agents are declared inline in `multiagent.json`, not as their own
  sub-directories.** A per-sub-agent directory (`deploy/candidates/<slug>/agents/
  <sub-agent-slug>/…`) was considered, mirroring the top-level layout recursively.
  Inline was chosen because (a) the ADR's own layout sketch shows `multiagent.json`
  as a single roster file, (b) a sub-agent's prompt is usually much shorter than a
  coordinator's (in practice, a narrower, single-purpose role), so the "diffability"
  motivation for separate files is weaker per sub-agent, and (c) it avoids an
  arbitrary depth limit question ("what if a sub-agent could itself coordinate
  further sub-agents?" — not a real requirement here). If a real multi-agent
  candidate's sub-agent prompts grow large enough that inline JSON becomes
  unwieldy, revisit this — nothing in the sync script assumes inline-only; the
  loader's `_load_sub_agent()` is the single seam that would need to read from a
  sub-directory instead.
- **The sync script never `git commit`s.** Per the explicit task instruction: the
  script rewrites `candidate.json`/`multiagent.json`/`skills.json` as an ordinary
  filesystem edit and stops there. Reasoning: the operator should see and review the
  diff (usually tiny — one new field) before it becomes a permanent commit, exactly
  like reviewing any other generated-but-committed change in this repo; a script
  that commits on the operator's behalf could paper over a partially-failed sync
  (e.g. a coordinator update succeeding while an intended-but-failed subsequent step
  silently gets committed alongside it as if everything worked).
- **HTTP mocking uses this repo's own established hand-rolled fake-client pattern
  (`deploy/eval/tests/test_cost_miner.py`'s `_FakeHttpxClient`), not
  `httpx.MockTransport` or the `respx` library.** See
  `tests/fake_httpx_client.py`'s module docstring for the full reasoning — in short,
  this mirrors an already-working, already-reviewed pattern in this exact repo for
  this exact family of Anthropic API calls, and adds zero new test dependencies.
- **No real Agent or Skill resource is created by this phase's test suite or by
  running `sync.py` against the synthetic fixtures during development.** There is no
  confirmed way to delete/archive an Agent resource once created (an earlier ADR
  probe found `DELETE` 404s) — so a throwaway "prove this script works" agent
  created against the live API would likely become a **permanent** artifact on the
  real account. All logic is proven via mocked HTTP in `tests/`; the platform's own
  create/update/version-list primitives were **already** independently confirmed
  live in an earlier session (see the ADR's "What I verified live" section) — this
  phase's job is to prove the SCRIPT calls them correctly, which mocked tests do
  without that risk.
- **(Phase 3) `_zip_skill_source()` (formerly two functions, `_zip_skill_source()`
  and `_zip_skill_source_for_creation()`) was unified into ONE function after a real
  live push proved they needed identical treatment.** Phase 2 assumed, without
  testing against the real API, that a version push to an already-existing skill
  accepted a *flattened* zip (no wrapping folder) while a brand-new skill's creation
  required a wrapping folder matching `SKILL.md`'s `name:` field. A real Phase 3
  push using the flattened shape against an *existing* skill failed with the exact
  same 400 the creation endpoint gives, and a follow-up probe confirmed the
  folder-name-must-match rule applies to both. One function now serves both
  `create_skill()` and `create_skill_version()`.
- **A candidate-owned skill's `content_hash` is deliberately a field on
  `skills.json`, not a new side-file, and is deliberately stripped before being sent
  to the Agents API.** See "Skill-content changes on an update sync," above, for the
  full reasoning — flagged here too since it's the kind of thing a careless refactor
  could accidentally regress (leaking `content_hash` into `to_agent_body()`'s output
  would make the live-vs-local comparison spuriously "always different").
- **A single-agent candidate with no `content_hash` recorded at all (e.g. one first
  synced before this Phase-3 field existed) is treated as "cannot determine, skip" on
  an update sync — NOT as "changed."** This avoids an unwanted, surprise skill-version
  push the very next time an already-synced candidate (predating this feature) is
  re-synced with no actual skill-content edit. In practice this repo has no such
  candidate yet (`smoke-test-example` was first synced with `content_hash` support
  already in place) — this is a forward-looking safety choice, not a currently-hit
  case. **Corrected (reviewer, README/code mismatch):** the ORIGINAL implementation
  did not actually do this — `if current_hash == pinned_entry.get("content_hash"):
  return False` compares a real SHA-256 hex digest against `None` (what `.get(...)`
  returns when the field is absent), which is **always unequal**, so a missing
  `content_hash` was silently treated as "changed" and pushed a version every time —
  the exact opposite of the stated intent. Fixed with an explicit
  `if recorded_hash is None: return False` check ahead of the equality comparison. A
  regression test (`test_update_skips_skill_push_when_content_hash_was_never_recorded`)
  was confirmed, directly, to fail against the pre-fix code (an unscripted
  `POST /v1/skills/{id}/versions` call was attempted) and pass against the fix.
- **The events-settle retry (`_wait_for_settled_events()`) was added only after a
  REAL race was observed live, not speculatively.** The first real trigger of
  `smoke-test-example` printed "no cat'd file contents found" despite the run having
  genuinely succeeded — a direct debugging session traced this to the session-status
  endpoint reporting `idle` before the events endpoint had the full transcript. This
  is exactly the kind of bug this repo's other epics have found only by triggering
  live runs (see `deploy/eval/README.md`'s own "Judgment calls" section for the same
  pattern) — a mocked test suite alone would never have caught it, since the mock
  would only ever return what it was scripted to return.
- **`_iter_skill_source_files()` skips symlinks entirely (security-engineer, Low
  severity hardening).** `_compute_skill_content_hash()` and `_zip_skill_source()`
  originally walked `skill/` with plain `Path.rglob("*")` + `path.is_file()`, which
  follows symlinks by default — a symlink inside `skill/` pointing OUTSIDE
  `skill_source_dir` (e.g. at a sibling directory or an absolute path) would have its
  TARGET content silently hashed and zipped, letting a symlink change what's
  hashed/pushed without that content actually living inside the git-tracked `skill/`
  directory. Not exploitable today (only the repo owner authors these directories),
  but cheap to close: both functions now share `_iter_skill_source_files()`, which
  checks `path.is_symlink()` and skips any symlink outright — this repo's skill
  directories are plain files, so there is no legitimate reason for one to contain a
  symlink. Three tests confirm a symlinked file is excluded from the file list, the
  hash, and the built zip.
- **`smoke-test-example/skills.json`'s skill `version` is a JSON string
  (`"1783337264004829"`), not a number — this is CORRECT, not a bug, confirmed by
  checking the real live API.** A nit raised this as an inconsistency with the rest
  of the codebase's test literals (which use numeric version literals for skill
  versions). Checking directly against the real API settled it: both
  `GET /v1/skills/{id}` (`latest_version`) and `GET /v1/agents/{id}` (each `skills[]`
  entry's `version`) return the skill version as a **string** on the live account —
  `smoke-test-example/skills.json` faithfully reflects that. Normalizing it to a
  number would make the file diverge from what the live API actually returns and
  would introduce, not prevent, a spurious "differs" mismatch on the next real sync
  (`_live_declaration_differs()` compares the local declaration against a live GET
  response that is itself a string). The pre-existing NUMERIC literals in this test
  suite's MOCKED skill-push responses (e.g. `1783096569199829`) are therefore the
  inconsistent ones, relative to the real API — a pre-existing Phase 2 convention,
  left as-is here since correcting it is a separate, broader cleanup across many
  already-passing tests, not something this fix pass changes.
- **(Phase 5) The live `model` field is a NESTED OBJECT on read, a PLAIN STRING on
  write — `_live_declaration_differs()` compared them raw and always saw a
  mismatch, silently breaking the "unchanged declaration → no-op" guarantee for
  EVERY candidate.** Found on the very first real `sync.py production-baseline`
  re-run (see "Phase 5 live validation," below): a genuinely unchanged candidate
  reported `updated: agent ...` instead of `no-op`. Direct diagnosis (comparing
  `to_agent_body()`'s local `model` field against a fresh `GET /v1/agents/{id}`
  response field-by-field) isolated the exact cause: `to_agent_body()` sends
  `model` as a bare string (`"claude-sonnet-5"`, from `model.txt`) — which
  `POST /v1/agents` accepts — but a live `GET /v1/agents/{id}` always echoes
  `model` back as `{"id": "claude-sonnet-5", "speed": "standard"}`, confirmed
  independently on BOTH the newly-created `production-baseline` agent AND, as a
  second, unrelated data point, a **read-only** `GET` against the real live
  production agent (`agent_01EswBTose8dnTAUDbGvzdLq`) — so this is a universal
  read-side API behavior, not specific to one agent. Comparing the two raw
  shapes is therefore *always* unequal, meaning every prior "unchanged
  declaration" re-sync of every existing candidate in this repo (including
  `smoke-test-example`'s own Phase 3 re-syncs) had been silently issuing an
  unnecessary `POST /v1/agents/{id}` update the whole time — never a hard
  failure, just a wasted mutation call and an unwanted extra version bump on
  Claude Platform's own side, invisible unless someone actually checked the
  agent's version-history length against how many times it was "really" changed
  (which nobody had, until this run). This escaped every existing mocked test
  because `tests/test_sync.py`'s own "unchanged" fixtures (e.g.
  `test_update_single_agent_unchanged_declaration_is_a_full_no_op`) echo `model`
  back as the SAME bare string the loader produces — a mock built to match an
  untested assumption about the live shape, not the real one. **Fixed** by
  `_normalize_live_field_for_comparison()`, which extracts `live_value["id"]`
  when the live `model` value is a dict before comparing — correcting the
  *comparison*, not the request body (the write side genuinely does want a bare
  string; only the read side nests it). A new regression test,
  `test_update_is_a_full_no_op_against_the_REAL_live_nested_model_shape`, uses
  the real, live-confirmed nested shape and was confirmed, directly, to FAIL
  against the pre-fix code (it attempted an unmocked `POST`, tripping the fake
  client's "no scripted response registered" assertion — i.e. a real spurious
  mutation call would have been made) and PASS against the fix. All of this
  repo's OTHER existing "unchanged" tests (which use the wrong bare-string mock
  shape) still pass unmodified after the fix, since normalizing an
  already-bare-string is a no-op.
- **(Phase 5) A SECOND, distinct real race — the VERY FIRST session-status poll
  can report a SPURIOUS terminal `idle`, before the session has even started
  running, on a genuinely long-running task.** Found on the real
  `production-baseline` trigger (the first genuine, non-trivial — many-minutes
  — candidate task this repo has ever triggered; every prior real trigger was
  the trivial `smoke-test-example` one-sentence task): `trigger.py` failed
  almost immediately with a confusing `CandidateRunEventsNotSettledError`, even
  though a read-only check moments later showed the session still genuinely
  `running`, ~35 real seconds of active work logged. Direct diagnosis (a
  dedicated diagnostic probe: a FRESH trivial session, polled every ~0.3s
  starting the instant `POST /v1/deployments/{id}/run` returned) reproduced the
  exact root cause live: poll #1 read `status: "idle"`; polls #2 onward (all
  still within about a second) correctly read `"running"`; the session's own
  event stream later confirmed `session.status_running` fired several seconds
  BEFORE the genuine `session.status_idle` terminal event. So `idle` can appear
  as a stale, pre-`running` PLACEHOLDER on literally the very first status poll
  — indistinguishable, by status string alone, from a real terminal `idle`
  reached later. `run_candidate()`'s poll loop (`_TERMINAL_STATUSES` check, no
  delay before the first poll) trusted this raw status field outright and broke
  out of the loop after milliseconds, on a session that then went on to run for
  real for many minutes — the subsequent `_wait_for_settled_events()` call
  correctly found no terminal marker yet (there genuinely wasn't one),
  exhausted its OWN narrower retry budget (designed for the OPPOSITE race — "a
  session that just finished, whose events endpoint hasn't caught up yet," not
  "the session hasn't started at all"), and raised a confusing, misleading
  error that pointed at the wrong mechanism. **Fixed**: a status reported as
  terminal is no longer trusted alone — the poll loop now CONFIRMS it against
  the event stream itself actually containing a genuine terminal
  `session.status_*` marker (the same `_events_are_settled()` check
  `_wait_for_settled_events()` already used, reused here as the actual source
  of truth) before accepting it and breaking out; if the status looks terminal
  but the events stream doesn't yet agree, the loop just keeps polling like any
  other not-yet-terminal iteration. A new regression test,
  `test_run_candidate_does_not_accept_a_spurious_first_poll_idle_status`,
  scripts exactly this sequence (spurious `idle` + no terminal event → must NOT
  be accepted; later genuine `idle` + a terminal event present → accepted) and
  was confirmed, directly, to FAIL against the pre-fix code (it broke out after
  a single sleep with the wrong interval, instead of the two sleeps the
  corrected loop actually performs) and PASS against the fix. Both real bugs
  this Phase found (this one and the `model`-shape no-op bug above) share a
  root cause worth naming plainly: **every prior real trigger against this
  mechanism used the trivial `smoke-test-example` task, which finishes in
  roughly a dozen real seconds — far too fast to ever expose either race.**
  Both needed a genuinely long-running, multi-minute real task — exactly what
  `production-baseline`'s real research/writing work is — to surface at all.

## Phase 3 live validation (2026-07-06) — the FR-4/FR-5 proof, run for real

Unlike Phase 2 (fully mocked — see "Judgment calls" above), Phase 3 is the first to
make real, live Anthropic API calls: a real environment, a real agent, a real skill,
and two real triggered sessions. Recorded here in full, matching the tone/style of
`deploy/eval/README.md`'s own "Judgment calls" section — the point being that several
of the bugs below were found **only** by actually triggering live runs, exactly the
discipline this repo's other epics have already established.

**1. The shared `cloud` environment was created once, for real.**
`POST /v1/environments` with `{"config": {"type": "cloud"}}` returned
`environment_id: env_01W3Envi4NfK7ypQMfoZccRY` (recorded in `environment.json`).
Confirmed `config.networking: {"type": "unrestricted"}` came back by default,
matching the ADR's own live finding.

**2. `smoke-test-example/` was created — a permanent, deliberately synthetic
reference candidate.** `python3 sync.py smoke-test-example` (first sync, no
`agent_id` yet) pushed its `skill/` content as a **brand-new** Skills-API resource
(`POST /v1/skills` — the genuinely new endpoint this phase's `create_skill()` adds)
and created the agent:
  - `skill_id: skill_01BSnAuiUxRNqYRBKBhAw2dP`, first version `1783336556116141`.
  - `agent_id: agent_01ExTVacFoay8yrAdebiRoj7`.
  Both written back into the candidate's tracked files by the sync script itself.

**3. First real trigger — and the events-settle race, found and fixed.** The first
`trigger.py smoke-test-example` run completed (session reached `idle`) but printed
"no cat'd file contents found," despite the run having genuinely written and cat'd
its output. Direct debugging (re-fetching `GET /v1/sessions/{id}` and
`GET /v1/sessions/{id}/events` immediately after the CLI exited, then again moments
later) confirmed a real race: on the very FIRST status poll, the session already
reported `status: "idle"`, while the events endpoint at that exact instant returned
only 4 partial events — none of the agent's actual tool_use/tool_result pairs.
Re-fetching moments later returned the full transcript (24 events), ending in a
`session.status_idle` event. Fixed by adding `_wait_for_settled_events()` — retrying
the events fetch until the stream itself contains a terminal `session.status_*`
event, not trusting the separate status field alone. After the fix, a re-run
correctly retrieved:

```
--- /workspace/smoke-test-output.txt ---
The smoke test skill says hello from version one.
```

**4. The zip-shape bug — found attempting the skill-version-update proof.**
Editing `smoke-test-example/skill/SKILL.md` (changing "version one" to "version
two" throughout, and bumping the in-file version note) and re-running `sync.py`
initially failed with a real 400:
`"Zip must contain a top-level folder with all files inside it, including
SKILL.md"` — from `POST /v1/skills/{id}/versions`, an endpoint Phase 2 had assumed
(never tested live) accepted a flattened zip. A follow-up manual probe with a
deliberately mismatched folder name against the same endpoint confirmed a SECOND
constraint: `"The folder name '<x>' must match the skill name '<y>' in SKILL.md."`
— proving the version-push endpoint enforces the IDENTICAL two constraints the
creation endpoint does. Fixed by unifying `_zip_skill_source()` to always build the
wrapping-folder shape (see "Skill content," above, and the "Judgment calls" bullet
on this).

**5. The skill-version-update sync, re-run successfully.** With the zip-shape bug
fixed, `python3 sync.py smoke-test-example` correctly:
  - Detected the local `content_hash` differed from what was recorded (a real,
    genuine content change) — no network call needed to detect this.
  - Pushed a new Skills-API version: `skill_01BSnAuiUxRNqYRBKBhAw2dP` → version
    `1783337264004829`.
  - Detected the agent's declaration now differed from its live state (the new
    skill version wasn't yet referenced) and updated the **same** `agent_id` in
    place — `agent_01ExTVacFoay8yrAdebiRoj7` went from **version 1 → version 2**
    (confirmed via `GET /v1/agents/{id}/versions`, which returned both versions,
    each referencing its own skill version: v1 → skill version
    `1783336556116141`, v2 → skill version `1783337264004829`).
  - **No new `agent_id` was created at any point** — `candidate.json`'s `agent_id`
    field is byte-for-byte unchanged from step 2.

**6. Second real trigger — the sharp AC-5 proof.** `trigger.py smoke-test-example`
was run again (same candidate, same shared environment, no code change to the
trigger mechanism itself), and correctly retrieved:

```
--- /workspace/smoke-test-output.txt ---
The smoke test skill now says hello from version two.
```

This is the direct, sharp confirmation the whole redesign exists to establish: a
Skills-API version push — with **no agent recreation** and **no container/image
rebuild of any kind** (there is no image in this topology at all) — reached a real,
running candidate. The exact ADR-0008 failure mode this redesign was built to fix
("a Skills-API push alone did not reach the running session because the skill was
baked into the image") **did not occur.**

**7. Archival hygiene confirmed.** After all of the above, `GET /v1/deployments`
listed exactly ONE active deployment — the real, unrelated, live production
`daily-ai-brief-scheduled` deployment. Every temporary deployment this phase's
`trigger.py` runs created (including ones from ad hoc debugging sessions while
tracking down the events-settle race) was correctly archived — none leaked.

## Phase 5 live validation (2026-07-06) — the production-baseline safety
## baseline, PRD FR-14/AC-14

Where Phase 3 proved the candidate MECHANISM works (a synthetic, trivial
smoke-test agent), Phase 5 is the first real, non-trivial content-generation
run: `deploy/candidates/production-baseline/` re-expresses TODAY'S real, live
production `daily-ai-brief-agent` configuration (model, an adapted system
prompt, the real pinned `daily-ai-brief` skill, the same tools/mcp_servers) as
a candidate on the decoupled `cloud` topology, and validates its output against
what real production actually produced this same morning — the safety baseline
this whole redesign needs before any future production cut-over is even
considered (PRD FR-14/AC-14, ADR-0014 rollout Phase 5).

### 1. The candidate, synced for real

`production-baseline/`'s declaration (`candidate.json`, `agent.json`,
`model.txt`, `system-prompt.md`, `task-prompt.md`, `skills.json`,
`parameters.json` — no `skill/` subdirectory, since it references the
EXISTING `daily-ai-brief` Skills-API resource by id + a concrete pinned
version, `skill_01H2qu83NwnJ5zqcbrqsCcJ6` @ `1783340601977967`, rather than
owning its own skill source) was synced via `python3 sync.py production-baseline`.
This created a genuinely NEW, SEPARATE agent resource:

- **`agent_id: agent_017KynQYfK2gNXtJBDeWe81B`** — confirmed, via a read-only
  `GET`, to be its own distinct resource with `version: 1`, entirely separate
  from the real live production agent (`agent_01EswBTose8dnTAUDbGvzdLq`), which
  was never touched, read-written, or invoked by anything in this phase beyond
  one read-only reference `GET` (to confirm the real production config this
  candidate re-expresses) and one read-only `GET .../versions` on its skill.

The system prompt was deliberately ADAPTED, not copied byte-for-byte, from the
real production agent's own 858-character system prompt: the
`/opt/pipeline`-specific narration/email-delivery clause was dropped (there is
no `/opt/pipeline` on a `cloud` sandbox, and per FR-1 content generation must
hold no delivery role at all), and the closing "research through delivery ...
produced and sent" was changed to "research through writing ... produced" —
every other instruction (anti-fabrication, "no human watching, don't stop to
ask," "treat the skill as authoritative") was preserved verbatim, confirmed via
a direct word-diff against the real prompt.

### 2. Real bugs found — a genuinely long-running task exposes what a trivial one can't

**Every prior real trigger against this mechanism (Phase 3's `smoke-test-example`)
completed in roughly a dozen real seconds.** `production-baseline`'s real
research/writing task is this repo's first genuinely long-running (many-minutes)
candidate run, and it immediately exposed THREE real, previously-undiscovered
bugs — none synthetic, none guessed at, each found by directly diagnosing a real
failure against the live API, in the same "fail loudly, then fix, then prove it
live" discipline this repo's other epics have already established:

1. **The `model`-shape no-op bug** (`_normalize_live_field_for_comparison()`,
   `candidate_sync/sync.py`) — a live `GET /v1/agents/{id}` echoes `model` as a
   nested object (`{"id": ..., "speed": ...}`), never the bare string
   `to_agent_body()` sends on write, so `_live_declaration_differs()` always saw
   a mismatch and silently issued a spurious update on every "unchanged" re-sync
   of every candidate. See "Judgment calls," above, for the full story.
2. **The spurious-first-poll-`idle` race** (`run_candidate()`'s poll loop,
   `candidate_sync/trigger.py`) — the VERY FIRST `GET /v1/sessions/{id}` call,
   with zero delay after starting the session, can report a stale `idle`
   placeholder before the session has even started running, wrongly accepted as
   terminal on a task that then ran for real for many more minutes. Confirmed
   live via a dedicated diagnostic probe (a fresh trivial session polled
   repeatedly from the instant `/run` returned); see "Judgment calls," above.
3. **The quoted-`cat`-path parsing gap** (`_parse_plain_cat_command()`,
   `candidate_sync/trigger.py`) — the brief file this repo's own skill output
   contract names, `AI Brief - YYYY-MM-DD.md`, has LITERAL SPACES in its
   filename; the agent correctly double-quoted its `cat` invocation for it and
   every other path it wrote, but the pre-fix parser rejected any bare space
   with no quote-aware exception — silently dropping the brief file entirely
   (the single most important artifact a candidate produces) and mis-parsing
   three other quoted paths into dict keys that still carried literal quote
   characters. See "Judgment calls," above.

All three bugs share one root cause worth stating plainly: **the trivial
smoke-test task finishes too fast, and never writes a path with a space in it,
to ever exercise any of these code paths.** Only a genuinely long-running,
realistic content-generation task — exactly what `production-baseline`
provides — could have found them. Each was confirmed, directly, to reproduce
against the pre-fix code and resolve against the fix, with a dedicated
regression test for each (`test_update_is_a_full_no_op_against_the_REAL_live_nested_model_shape`,
`test_run_candidate_does_not_accept_a_spurious_first_poll_idle_status`,
`test_parse_plain_cat_command_accepts_a_double_quoted_path_with_spaces` +
three related rejection-case tests).

**Two reviewer follow-ups on these same three bugs, both resolved with a live
check, not a guess:**

- **Does the `model`-shape mismatch extend to `tools`/`mcp_servers`/`skills`?**
  The original fix's docstring called `model` "the ONE field" the write-side and
  read-side disagree on in shape — an overclaim, since that was only ever
  confirmed for `model` itself. `tools` in particular is exactly the kind of
  richer, nested shape (with `default_config`/`configs`/`permission_policy`)
  where a live GET could plausibly echo back filled-in defaults or reordered
  keys. Checked directly, not assumed: a live, field-by-field diff of a fresh
  `GET /v1/agents/{id}` against `to_agent_body()`'s own output, performed for
  BOTH `production-baseline` AND, independently and read-only, the real live
  production agent (`agent_01EswBTose8dnTAUDbGvzdLq`) — confirmed `tools`,
  `mcp_servers`, and `skills` are each structurally IDENTICAL on read vs. write
  for both agents. No further normalization was needed; `model` remains the
  only field requiring it. The docstring on
  `_normalize_live_field_for_comparison()` now states precisely what was
  checked rather than the earlier "the one field" overclaim, and a new
  regression test,
  `test_update_is_a_full_no_op_using_the_real_confirmed_live_tools_and_mcp_servers_shapes`,
  pins this confirmed-identical shape using the REAL live-observed
  `tools`/`mcp_servers`/`skills` values (not the fixture's own placeholder
  values, which happened to already match either way and so could never have
  caught a real mismatch).
- **The quoted-`cat`-path fix only handled double quotes — single quotes
  reproduce the identical silent-drop bug.** `_parse_plain_cat_command()`'s
  double-quote branch checked `remainder.startswith('"')` specifically;
  `cat 'path with spaces'` (single-quoted, equally idiomatic bash, and nothing
  in `task-prompt.md`'s example constrains the agent to double quotes)
  fell through to the unquoted-form branch, which rejects any bare space,
  silently returning `None` — reproducing, via a different quote character,
  the exact "silently drops the brief" failure the double-quote fix was
  written to close. Fixed by mirroring the double-quote branch exactly for
  single quotes (the same unterminated-quote/embedded-quote/metacharacter
  rejection rules, just with `'` instead of `"`, kept as a parallel,
  independently-readable branch rather than a single generalized helper). Two
  new acceptance tests
  (`test_parse_plain_cat_command_accepts_a_single_quoted_path_with_spaces`,
  `...without_spaces`) plus four new rejection-case entries confirm this,
  each confirmed to fail against the double-quote-only fix and pass against
  this follow-up.

Both follow-ups were confirmed, directly, to fail against the pre-follow-up
code and pass against the fix; the full `deploy/candidates` suite (82 tests)
and `deploy/delivery` suite (164 tests) both remain fully green.

### 3. The real triggered run

Once the fixes above were in place, `production-baseline`'s real, full run
completed successfully:

- **Session id:** `sesn_011fEEWn4c9f9QAYdQvzCQwn`
- **Temporary deployment id:** `depl_01LFmEKRNK9gkXVVTwiNnUbh` (created,
  `/run`, then correctly archived — confirmed `archived_at` set, and confirmed
  archiving a deployment does NOT interrupt an already-started session, since
  this candidate's own genuinely-still-running session at that point was
  independently confirmed to keep executing normally afterward).
- **Wall-clock duration:** `duration_seconds: 757.7` (~12.6 minutes, session
  creation to terminal — this includes the several minutes an earlier
  `trigger.py` invocation spent erroring out on bug #2 above BEFORE the poll
  loop fix was in place; `active_seconds: 396.06` (~6.6 minutes) is the actual
  model-active time, a more representative figure for "how long does this
  candidate's real research/writing task take").
- **Real token/cost usage** (from the session's own `usage` field, retrieved
  via a read-only `GET /v1/sessions/{id}` — no separate cost-mining tool
  needed): `output_tokens: 34482`, `input_tokens: 49`,
  `cache_read_input_tokens: 5242324`,
  `cache_creation.ephemeral_5m_input_tokens: 356172`. The overwhelming
  cache-read-vs-fresh-input ratio is expected and healthy for a research-heavy
  agentic task (repeated tool-result content reused across many turns rather
  than re-sent).
- **Archival hygiene reconfirmed:** a subsequent `GET /v1/deployments` listed
  exactly ONE active deployment on the whole account — the real, unrelated
  live production `daily-ai-brief-scheduled` deployment. The temporary
  deployment above was correctly archived despite the mid-run poll-loop bug,
  proving the "always archives, even on a mid-run internal error" guarantee
  held through a genuinely new failure mode, not just the ones it was
  originally written against.

### 4. Retrieved artifacts

All four files the task prompt asked for were retrieved via the Sessions
events API (`fetch_catted_file_contents()`, after the quoted-path parsing fix)
— plus the candidate's own `cat`'d copies of `SKILL.md`/`sources.md`
(confirming exactly which skill content it actually read, byte for byte):

- `AI Brief - 2026-07-06.md` — 16,689 chars / 2,239 words
- `listening-script.txt` — 8,508 chars / 1,321 words
- `candidates.json` — 72 entries (12 `included`, 60 `excluded`)
- `source-usage.json` — 51 entries spanning all 9 of `sources.md`'s tiers (12
  `featured: true`, 39 `featured: false`)

### 5. Comparison against real production's own brief from the same morning

A genuine, real production brief from THIS SAME MORNING's live scheduled run
(fetched from `s3://cowork-polly-tts-740353583786/briefs/2026-07-06/`, the
SAME AWS credential pattern used throughout this repo) is the comparison
baseline. Per the task's own framing, this is a check for **structural and
qualitative equivalence, not byte-identical content** — the candidate ran
independently, hours later, and did its own real research, so different
specific headlines are expected (AI news develops throughout a day) and are
NOT a defect.

**Structure — matches exactly.** Both use the identical tiered Markdown shape
`SKILL.md` specifies: a `# Daily AI Brief — {Weekday}, {Month} {D}, {YYYY}`
title, an italic tl;dr line, a `## 📌 Headlines` bulleted list, then
`## {emoji} {Category}` deep-dive sections, then a closing "Sources checked"
line. Both correctly OMIT sections with no items that day (the skill's own
documented rule) — the candidate's brief includes a
`## 🛠️ Products, Tools & Releases` section the production brief happens to omit
that day, and neither includes a `## 📊 Benchmarks & Evals` section; this is
expected per-day content variance, not a structural mismatch.

**Length/depth — same ballpark.** Candidate: 2,239 words / 12 headlines / 10
deep-dive items. Production: 2,129 words / 12 headlines / 7 deep-dive items.
Both are well within the skill's own "8–15 headlines, 5–10 deep dives" target.

**Listening script — same ballpark, both within the skill's own spec.**
Candidate: 1,321 words. Production: 1,188 words. Both are genuinely derived
from their own respective brief's content (spot-checked: every deep-dive topic
in the candidate's script appears in its own brief, in the same order); both
correctly satisfy every one of the skill's own listening-script constraints —
confirmed directly, not asserted: no URLs, no Markdown syntax, no "Sources:"
lines, no emoji. The candidate's script runs slightly over the skill's
800–1,200-word target (1,321 vs. the upper bound); production's (1,188) sits
right at it — a minor, real, honestly-reported difference, not a hard failure.

**Tone/style — consistent.** Both are dense, fact-led, lab-neutral, with the
same "why it matters" pattern the skill's own item-format spec requires
(concrete facts first, then one neutral sentence on industry significance) —
confirmed by reading both briefs in full, not inferred from metadata alone.

**`candidates.json` — a real, honest difference worth flagging, not glossed
over.** Production's `candidates.json` has 25 total entries, ALL marked
`included` (zero `excluded` entries) — i.e. it only recorded the stories that
made the final cut. The candidate's `candidates.json` has 72 entries, 60 of
them explicitly `excluded` — a genuinely more complete record, matching the
skill's own stated contract ("listing every story/topic you considered ...
not just the ones that made the final brief") more faithfully. Both runs used
the exact same, confirmed-latest pinned skill version
(`1783340601977967`), so this is NOT a skill-content difference — it's a real,
observed behavioral variance between two independent agent runs in how
exhaustively each logged its own exclusions. Flagged here honestly, as a real
finding about run-to-run consistency of this additive artifact, not something
to paper over.

**`source-usage.json` — new in this skill version, well-formed, complete.**
This is the first real run to exercise Phase 4's source-usage instruction.
Cross-checked directly against the candidate's own `cat`'d copy of
`sources.md`: all 9 real tier headings are represented, with no gaps, and
every source marked `featured: true` (TechCrunch, Anthropic, Reuters, NVIDIA
Blog, xAI, Hugging Face Daily Papers, smol.ai, Financial Times, The
Information, Bloomberg, Hacker News front page, Hugging Face Trending) does
genuinely appear cited in the brief's own Sources lines — confirmed by direct
inspection, not assumed. Production's brief was generated by an earlier skill
version predating this instruction, so no production-side `source-usage.json`
exists to compare against; this artifact is validated purely on its own
internal consistency (against the candidate's own recovered `sources.md` and
brief), which it passes cleanly.

**Overall verdict:** the candidate's output is structurally and qualitatively
equivalent to real production's — same tiered shape, same tone, same
skill-contract compliance, comparable length/depth — with the two flagged,
honest differences above (a longer listening script; a more complete
`candidates.json`) being real, reportable observations rather than defects
that would block a future cut-over decision.

### 6. The accepted recent-priors-reading gap

As directed, this run deliberately skipped `deployment.json`'s real step 0
(`audio_email.py read-recent-briefs`, S3-backed) — there is no AWS access at
all on a `cloud` sandbox, and no delivery-side "read recent priors" endpoint
exists yet (new scope, explicitly out of this phase). The candidate's own
`task-prompt.md` states this explicitly, and `candidate.json`'s description
documents it as an accepted, temporary limitation of this FIRST candidate on
the `cloud` topology — a real gap worth solving before any real production
cut-over (likely via a new delivery-side read endpoint mirroring the existing
write/archive endpoints), not silently omitted. The practical consequence for
this one-off validation run was negligible: comparing the candidate's brief
against production's own (both from the same day) found no repeated story
between them, so the missing "avoid repeating yesterday's story" safeguard
never actually mattered for this particular run — though it would need
solving before any candidate could safely run on a REAL production schedule.

### 7. `derive_html()` cross-validated against this candidate's fresh, real output

Called directly as a pure function (no HTTP, no real delivery call) against
the candidate's real brief Markdown — extending Phase 1's original 3-fixture
regression evidence with a 4th, brand-new real data point
(`deploy/delivery/tests/fixtures/2026-07-06-production-baseline-candidate-brief.md`).
Confirmed: well-formed HTML (no parser errors; correct doctype/head/body/title/
closing tags), structurally consistent with the existing template (the same
table-based layout, in-body scoped `<style>` block, and content-conversion
fidelity — every heading/bold/italic/link/hr from the real input converts
correctly). One genuinely NEW Markdown construct appeared that none of the
original three fixtures used — a single inline-code span
(`` `source-usage.json` `` in the brief's own closing "Sources checked" line)
— flagged explicitly per this phase's own instruction, not silently patched:
confirmed this is CORE Markdown syntax (not extension-gated, unlike
`tables`/`fenced_code`), so it already converts correctly
(`<code>source-usage.json</code>`) under `_convert_markdown_body()`'s existing
zero-extensions design, with no code change required. All 164 of
`deploy/delivery`'s tests (48 in this file alone, now covering 4 real
fixtures) pass.

### 8. Explicitly NOT done in this phase

Per explicit instruction, this phase stopped at retrieving and comparing the
candidate's raw output and validating `derive_html()` as a pure function call.
It did NOT call the real `deploy/delivery/` `POST /deliver` endpoint, did NOT
trigger any real Polly synthesis or SES send, and did NOT wire this candidate
to the live delivery boundary in any way — a real send to the owner's real
inbox requires the owner's explicit go-ahead first, not an automatic step of
this validation.

## Local validation

```bash
cd deploy/candidates
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python3 -m py_compile sync.py trigger.py candidate_sync/*.py
.venv/bin/python3 -m pytest tests/ -v
```

This runs the FULL mocked test suite (Phase 2's sync-logic tests plus Phase 3's
trigger-tool and skill-creation/skill-update-detection tests) — no real network
calls, no real Anthropic API credentials needed. The one real, live proof (creating
the shared environment, `smoke-test-example`, and its two triggered runs — see
"Phase 3 live validation," above) was run once, by hand, as a genuine validation; it
is deliberately **not** part of this repeatable, offline `pytest` invocation.
