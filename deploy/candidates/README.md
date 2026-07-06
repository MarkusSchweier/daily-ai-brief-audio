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

**`deploy/candidates/production-baseline/` (re-expressing the real, live production
configuration as a candidate) does NOT exist yet.** That is a later, separate,
already-tracked phase of the agent-system-redesign epic (PRD §8 Phase 5) — it needs
the actual current production model/prompt/skill values, which this phase
deliberately does not touch. What exists here so far are two clearly-fake **synthetic
test fixtures** (`tests/fixtures/example-single-agent/`,
`tests/fixtures/example-multi-agent/`) used only to exercise the sync script's logic
in tests — never deploy them for real.

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
- **Own its own skill source** — a `skill/` subdirectory (e.g. `SKILL.md`,
  `sources.md`) plus a `skills.json` entry with a `skill_id` and **no** `version` yet
  (a "please create a version for me" placeholder). The sync script zips `skill/`'s
  contents and pushes a new Skills-API version on the candidate's first sync, then
  writes the resulting concrete version number back into `skills.json`.

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
  each sub-agent's id into `multiagent.json`'s roster).
- **`agent_id` already present → update in place.** For each agent, the script reads
  its **current live state** (`GET /v1/agents/{id}`) and compares it against the local
  declaration. Only a genuinely-**changed** agent gets updated
  (`POST /v1/agents/{id}` with the exact `version` just read — never a cached/assumed
  value). If that update returns `409` (someone/something else updated the agent in
  between), the script re-reads the current version once and retries — it never
  blindly overwrites.

An **unchanged** declaration is a full **no-op at the mutation level**: zero
create/update calls are made (the script still issues one read-only `GET` per agent
to *detect* "unchanged" in the first place — see "Judgment calls," below, for why that
GET is unavoidable and is not itself a violation of the "no bespoke duplicate-of-git
index" principle).

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

## Local validation

```bash
cd deploy/candidates
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python3 -m py_compile sync.py candidate_sync/*.py
.venv/bin/python3 -m pytest tests/ -v
```
