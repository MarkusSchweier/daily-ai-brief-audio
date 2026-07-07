Step 0 -- read recent prior briefs (production parity, no AWS access needed):
run
`curl -s -H "Authorization: Bearer __RECENT_BRIEFS_TOKEN__" "__DELIVERY_BASE_URL__/recent-briefs?count=3"`
to fetch the last few briefs as JSON (`{"briefs": [{"date", "markdown"}, ...]}`).
For each entry returned, write its `markdown` to
`/workspace/AI Brief - <date>.md` (using that entry's own `date`) -- the exact
filename convention the skill itself expects, so it finds these priors via its
normal `WORKING_FOLDER` search with no special-casing. If the response's
`briefs` list is empty (e.g. a cold-start store), that's normal -- proceed to
research with no priors to avoid repeating, exactly as production does on a
young store.

Then drive the daily-ai-brief pipeline by DELEGATING each phase to the sub-agent
responsible for it, in order, rather than doing the skill work yourself. Each
sub-agent invokes the SAME daily-ai-brief skill scoped to its slice of the
skill's own numbered Daily workflow, and reads its inputs from and writes its
outputs to the shared `/workspace` working folder (the same folder Step 0 wrote
the priors into, and the folder the skill uses by default):
  1. the research sub-agent -- skill workflow steps 1-3 (set up; gather a wide
     net over the last ~24-48h; paywall handling), writing its gathered,
     source-attributed findings to `/workspace`;
  2. the selection sub-agent -- skill workflow steps 4-5 (dedupe & cluster; rank
     & select), which also writes `/workspace/candidates.json` (every
     story/topic considered, included or excluded);
  3. the writing sub-agent -- skill workflow steps 6-7 (write; finish), which
     writes today's Markdown brief to `/workspace/AI Brief - YYYY-MM-DD.md`
     (today's date -- a DIFFERENT file from any prior-brief file written in
     Step 0, which uses THAT entry's own date) and `/workspace/source-usage.json`
     (every `sources.md` entry and whether it was featured today);
  4. the listening-script sub-agent -- the skill's "audio / listening-script
     output" section, which writes `/workspace/listening-script.txt`.

Do NOT convert the brief to HTML and do NOT attempt any narration, email, or
delivery of any kind -- none of that is this candidate's job. Stop once the
brief, the listening script, `candidates.json`, and `source-usage.json` are all
written.

After the sub-agents have written each of the four files, run `cat` on each one
individually (one command per file, e.g. `cat /workspace/AI Brief - YYYY-MM-DD.md`)
and show the result, so their exact content is captured in this session's own
event log. Finish with a short summary confirming what was produced (the brief's
title, its rough length, and the four files written) -- per the skill's own
contract, a `computer://` link (or this runtime's `/workspace` path) to the
brief file.
