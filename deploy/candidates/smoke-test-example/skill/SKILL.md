---
name: smoke-test-skill
description: A deliberately trivial, permanent smoke-test skill for the agent-system-redesign candidate mechanism (FR-4/FR-5 live proof). Use when asked to "run the smoke test."
---

# Smoke Test Skill

**Version 2** (pushed live 2026-07-06 to prove FR-5/AC-5: a Skills-API version push
alone, with NO agent recreation and NO container/image rebuild, reaches a real
running candidate).

Write exactly this one sentence to the requested output file:

> The smoke test skill now says hello from version two.

Then `cat` the file back so its content lands in the session's tool-result event
stream.

See `sources.md` for this skill's (trivial, fake) source list -- mirrors the
two-file shape of the real `daily-ai-brief` production skill
(`deploy/managed-agent/skills/daily-ai-brief/`), scaled down to a single line.
