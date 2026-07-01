# `.claude/` in this repo (optional overrides only)

The reusable stack — agents, slash commands, skills, hooks, and the permission/safety
baseline — lives **centrally** in `~/.claude/` and applies to every project automatically.
This repo's `.claude/` directory is **only** for things that must differ here.

Add files here to override the global ones (project layer wins on name conflict):

- `.claude/agents/<name>.md` — replace a global agent for this repo only.
- `.claude/commands/<name>.md` — add or override a slash command for this repo.
- `.claude/settings.json` — tighten permissions for this repo (e.g. deny a deploy that's
  fine elsewhere). Merges with the global settings.
- `.claude/skills/<name>/SKILL.md` — repo-specific knowledge.

### Example: tighten permissions for a higher-risk repo

Project settings **merge over** the global baseline, and a project `deny` cannot be
overridden by the global `allow`. For a sensitive repo (prod infra, customer data), drop a
`.claude/settings.json` like this to gate more than the default:

```json
{
  "permissions": {
    "defaultMode": "default",
    "ask": [
      "Bash(git push:*)",
      "Edit(./infra/**)",
      "Edit(./**/*.tf)"
    ],
    "deny": [
      "Bash(aws:*)",
      "Read(./config/prod/**)"
    ]
  }
}
```

This switches the repo from auto-accepting edits to prompting, makes every push and infra edit
ask first, and hard-blocks raw `aws` calls and reading prod config — while every other repo
keeps the permissive global defaults.

If you don't need any overrides, you can delete this directory — `CLAUDE.md`, `.mcp.json`,
and `docs/` are enough, and the global config supplies the rest.

> Don't put machine-specific model/credential config here. That belongs in
> `~/.claude/settings.local.json` (user scope), so it's set once per machine.
