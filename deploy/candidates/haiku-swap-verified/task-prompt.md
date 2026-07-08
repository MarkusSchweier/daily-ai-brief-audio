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

Step 1 -- invoke the daily-ai-brief skill to research and write today's brief,
per the skill's own output contract: write today's Markdown brief to
`/workspace/AI Brief - YYYY-MM-DD.md` (today's date -- a DIFFERENT file from
any prior-brief file written in Step 0, which uses THAT entry's own date), and
(per the skill's own contract) also write `/workspace/candidates.json` (every
story/topic considered, included or excluded) and `/workspace/source-usage.json`
(every `sources.md` entry and whether it was featured today). Do NOT write the
listening script yet -- it is produced in Step 3, AFTER verification, so it
narrates the corrected brief.

Step 2 -- delegate verification: hand ONE task to your verifier sub-agent
(`daily-ai-brief-cost-hsv-verifier-sub-agent`), telling it today's date and
that today's brief, the dated prior briefs, and candidates.json are in
`/workspace`. Its own role defines the work: verify the brief's headlines and
factual claims against each story's cited sources and correct genuine
inaccuracies in place, add any missing follow-up/repeat-coverage labels against
the dated priors, never change which stories were selected, and write
`/workspace/verification-report.json`. Wait for it to finish before
continuing.

Step 3 -- read the (now corrected) `/workspace/AI Brief - YYYY-MM-DD.md` and
produce the listening script from it per the skill's own
audio/listening-script section, saving it explicitly to
`/workspace/listening-script.txt`.

Do NOT convert the brief to HTML and do NOT attempt any narration, email, or
delivery of any kind -- none of that is this candidate's job. Stop once the
brief, the listening script, `candidates.json`, `source-usage.json`, and
`verification-report.json` are all written.

After all files exist, run `cat` on each one individually (one command per
file, e.g. `cat /workspace/AI Brief - YYYY-MM-DD.md`) for: the brief,
listening-script.txt, candidates.json, source-usage.json, and
verification-report.json -- so their exact content is captured in this
session's own event log. Finish with a short summary confirming what was
produced (the brief's title, its rough length, how many corrections and labels
the verifier applied, and the five files written) -- per the skill's own
contract, a `computer://` link (or this runtime's `/workspace` path) to the
brief file.
