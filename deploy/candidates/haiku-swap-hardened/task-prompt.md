Step 0 -- read recent prior briefs (production parity, no AWS access needed):
run
`curl -s -H "Authorization: Bearer __RECENT_BRIEFS_TOKEN__" "__DELIVERY_BASE_URL__/recent-briefs?count=3"`
to fetch the last few briefs as JSON (`{"briefs": [{"date", "markdown"}, ...]}`).
For each entry returned, write its `markdown` to
`/workspace/AI Brief - <date>.md` (using that entry's own `date`) -- the exact
filename convention the skill itself expects. If the response's `briefs` list
is empty (e.g. a cold-start store), that's normal -- proceed with no priors,
exactly as production does on a young store.

Step 1 -- MANDATORY, before any research: first run `ls /workspace/` and
STATE the complete list of prior brief files present and their count (e.g.
"3 priors: 2026-07-08, 2026-07-07, 2026-07-06"). Then run `cat` on EACH ONE of
those files by name, one command per file (e.g.
`cat "/workspace/AI Brief - <date>.md"`), so every prior brief's actual content
is in your context now. Every file in your stated list must be catted -- a
prior you listed but did not cat is a defect. You will need them in Steps 2
and 3. Do not skip this step even if you believe you remember the priors.

Step 2 -- invoke the daily-ai-brief skill to research and write today's brief,
per the skill's own output contract: write today's Markdown brief to
`/workspace/AI Brief - YYYY-MM-DD.md` (today's date -- a DIFFERENT file from
any prior-brief file, which uses THAT entry's own date), save the listening
script explicitly to `/workspace/listening-script.txt`, and (per the skill's
own contract) also write `/workspace/candidates.json` (every story/topic
considered, included or excluded) and `/workspace/source-usage.json` (every
`sources.md` entry and whether it was featured today). EXPLICIT LABELLING RULE
(this is where Step 1's reading pays off): any story in today's brief that
repeats or continues coverage from ANY prior brief you read in Step 1 MUST be
explicitly labelled as a follow-up/continuing story, naming the prior day,
in the brief's own style. A repeated story without a label is a defect.

Step 3 -- SELF-CHECK, before finishing (do not skip):
(a) run `ls -la /workspace/` and verify the four outputs so far exist: today's
brief, listening-script.txt, candidates.json, source-usage.json (fix anything
missing; the fifth output, overlap-notes.md, is written in (c) below -- do NOT
create it yet);
(b) cross-check today's brief against EACH prior brief ONE AT A TIME, by
filename: for prior file 1, re-scan every story in today's brief against THAT
prior only and note the overlaps; then repeat for prior file 2; then prior
file 3 (and so on for every file from Step 1's list). Do NOT cross-check "the
priors" collectively in one pass -- process each prior file individually;
skipping a file in this loop is the exact defect this step exists to prevent.
If any repeated/continuing story is missing its follow-up label, add the
label NOW and save the brief;
(c) write `/workspace/overlap-notes.md`. Its FIRST line must enumerate the
priors checked: "Priors checked (one at a time): <date>, <date>, <date>" --
matching Step 1's list exactly. Then one line per story in today's brief,
stating either "no prior overlap" or "overlaps <prior date>: <what is new
today> -- labelled: yes/no" (after (b), every overlap line must end
"labelled: yes"). This file is the auditable record that the per-prior
cross-check actually happened.

Do NOT convert the brief to HTML and do NOT attempt any narration, email, or
delivery of any kind -- none of that is this candidate's job.

Step 4 -- after all files exist, run `cat` on each one individually (one
command per file): today's brief, listening-script.txt, candidates.json,
source-usage.json, and overlap-notes.md -- so their exact content is captured
in this session's own event log. Finish with a short summary confirming what
was produced (the brief's title, its rough length, how many stories carry
follow-up labels, and the five files written) -- per the skill's own contract,
a `computer://` link (or this runtime's `/workspace` path) to the brief file.
