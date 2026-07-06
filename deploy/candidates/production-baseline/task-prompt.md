Invoke the daily-ai-brief skill to research and write today's brief, per the
skill's own output contract: write today's Markdown brief to
`/workspace/AI Brief - YYYY-MM-DD.md` (today's date), save the listening script
explicitly to `/workspace/listening-script.txt`, and (per the skill's own
contract) also write `/workspace/candidates.json` (every story/topic considered,
included or excluded) and `/workspace/source-usage.json` (every `sources.md`
entry and whether it was featured today).

Note: unlike a real scheduled production run, this candidate does NOT have
access to any prior briefs (there is no S3/AWS access available in this
environment) -- skip any "read recent prior briefs" step entirely and proceed
straight to today's research. It is fine, and expected, that this run cannot
check for or avoid repeating a very recent story.

Do NOT convert the brief to HTML and do NOT attempt any narration, email, or
delivery of any kind -- none of that is this candidate's job. Stop once the
brief, the listening script, `candidates.json`, and `source-usage.json` are all
written.

After writing each of the four files, run `cat` on each one individually (one
command per file, e.g. `cat /workspace/AI Brief - YYYY-MM-DD.md`) and show the
result, so their exact content is captured in this session's own event log.
Finish with a short summary confirming what was produced (the brief's title,
its rough length, and the four files written) -- per the skill's own contract,
a `computer://` link (or this runtime's `/workspace` path) to the brief file.
