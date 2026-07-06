"""Write the sync script's OUTPUTS back into a candidate's tracked files -- the newly
minted `agent_id`(s) at first sync, and an updated `skills.json` after a skill-version
push.

Per Decision 2c and the task instructions: this module rewrites files as an ordinary
filesystem edit ONLY. It never runs `git add`/`git commit` itself -- committing the
resulting change is left to the operator (see `deploy/candidates/README.md`'s
"After running sync" section for why: the operator should review the diff -- usually
just a new `agent_id`/skill-version field -- before committing, exactly like any other
generated-but-reviewed change in this repo, and a script silently committing on the
operator's behalf could paper over a partial/bad sync).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def write_candidate_agent_id(candidate_json_path: Path, agent_id: str) -> None:
    """Write the coordinator/sole agent's newly-created `agent_id` into
    `candidate.json`, preserving every other existing field and key order as much as
    `json` round-tripping allows."""
    data = json.loads(candidate_json_path.read_text(encoding="utf-8"))
    data["agent_id"] = agent_id
    _write_json(candidate_json_path, data)


def write_sub_agent_ids(multiagent_json_path: Path, agent_ids_by_index: dict[int, str]) -> None:
    """Write newly-created sub-agent `agent_id`s into `multiagent.json`'s roster, by
    roster index (the order `loader.load_candidate()` reads them in, which matches the
    JSON array's own order)."""
    data = json.loads(multiagent_json_path.read_text(encoding="utf-8"))
    roster = data.get("agents", [])
    for index, agent_id in agent_ids_by_index.items():
        roster[index]["agent_id"] = agent_id
    data["agents"] = roster
    _write_json(multiagent_json_path, data)


def write_skills_json(skills_json_path: Path, skills: list[dict[str, Any]]) -> None:
    """Overwrite `skills.json` with a concrete, pinned `[{skill_id, version}, ...]`
    list -- used after a skill-version push resolves a real numeric version."""
    skills_json_path.write_text(json.dumps(skills, indent=2) + "\n", encoding="utf-8")
