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

=== BEGIN GENERATED SOURCE URL LIST (from the daily-ai-brief skill's sources.md; do not edit by hand) ===

These are the curated source URLs from the daily-ai-brief skill's sources.md, listed here
verbatim and in the skill's own tier order.

Why they are in this message: web_fetch can only retrieve a URL that already appeared in
this conversation -- in this message, or in an earlier web_search/web_fetch result.
Reading sources.md from disk does NOT count: a curated URL that exists only in that file
cannot be fetched. Listing the URLs here is what makes them fetchable.

How to use this list:
- Fetch from it when the skill calls for a known source. This is NOT an instruction to
  fetch everything -- budget your fetches exactly as the skill says, working the top
  tiers first.
- Fetching a feed or index page listed here also makes the article links inside it
  fetchable, because they then count as an earlier web_fetch result. The feed -> article
  chain therefore works from any entry point below.
- If a fetch is rejected with url_not_in_prior_context, do NOT retry that URL. Fall back
  exactly as the skill's Gather step prescribes: that outlet's own site/index page (it is
  listed next to its feed below, and is fetchable for the same reason), and only then
  web_search.
- Fetch in small batches of at most about 8 URLs at a time. Read what came back before you
  fetch the next batch.
- Keep working without stopping. This run is unattended and ends the moment you reply
  without calling a tool, so never pause to summarize progress or to announce what you
  will do next: go straight on to your next tool call until the final step of this message
  is done.

Tier 1 — Frontier labs (primary sources)
https://www.anthropic.com/news
https://deepmind.google/discover/blog/
https://deepmind.google/blog/rss.xml
https://research.google/blog/
https://research.google/blog/rss/
https://ai.meta.com/blog/
https://www.microsoft.com/en-us/research/blog/
https://www.microsoft.com/en-us/research/feed/
https://mistral.ai/news/
https://openai.com/news/
https://openai.com/blog/rss.xml
https://qwenlm.github.io/blog/
https://api-docs.deepseek.com/news
https://x.ai/news

Tier 2 — Papers (technical backbone)
https://huggingface.co/papers
https://rss.arxiv.org/rss/cs.CL
https://rss.arxiv.org/rss/cs.LG
https://rss.arxiv.org/rss/cs.AI
https://paperswithcode.com/

Tier 3 — Benchmarks & leaderboards (scan; include only on real movement)
https://lmarena.ai/leaderboard
https://artificialanalysis.ai/
https://huggingface.co/open-llm-leaderboard
https://www.swebench.com/

Tier 4 — Tech press (industry + developer pulse)
https://techcrunch.com/category/artificial-intelligence/
https://techcrunch.com/category/artificial-intelligence/feed/
https://www.theverge.com/ai-artificial-intelligence
https://www.theverge.com/rss/index.xml
https://venturebeat.com/category/ai/
https://venturebeat.com/category/ai/feed/
https://arstechnica.com/ai/
https://feeds.arstechnica.com/arstechnica/index
https://www.reuters.com/technology/
https://www.axios.com/technology

Tier 5 — Scoops / paywalled (note + find free coverage)
https://www.theinformation.com/
https://www.bloomberg.com/technology
https://www.wsj.com/news/technology
https://www.ft.com/artificial-intelligence
https://www.nytimes.com/section/technology

Tier 6 — Curated newsletters (end-of-pass cross-check; don't just copy)
https://news.smol.ai/
https://thezvi.substack.com/
https://importai.substack.com/
https://www.deeplearning.ai/the-batch/
https://magazine.sebastianraschka.com/

Tier 7 — Community (developer signal, trending)
https://news.ycombinator.com/
https://hn.algolia.com/
https://www.reddit.com/r/LocalLLaMA/
https://www.reddit.com/r/MachineLearning/
https://github.com/trending
https://huggingface.co/models?sort=trending

Tier 8 — Policy, safety & society (scan; include on real news)
https://www.whitehouse.gov/ostp/
https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai
https://www.aisi.gov.uk/
https://www.nist.gov/artificial-intelligence
https://hai.stanford.edu/news

Tier 9 — Chips & infra (scan; cost/competitive landscape)
https://nvidianews.nvidia.com/
https://blogs.nvidia.com/
https://blogs.nvidia.com/feed/
https://www.amd.com/en/newsroom.html
https://aws.amazon.com/blogs/machine-learning/
https://aws.amazon.com/blogs/machine-learning/feed/
https://www.semianalysis.com/
https://pr.tsmc.com/english/news

=== END GENERATED SOURCE URL LIST ===
