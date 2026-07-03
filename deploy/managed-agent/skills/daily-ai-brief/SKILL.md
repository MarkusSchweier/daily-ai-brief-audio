---
name: daily-ai-brief
description: Generate a daily AI briefing covering the latest AI research and industry developments, written in dense news-bite style for a general, technically fluent audience. Gathers from a comprehensive source list, dedupes across outlets, finds free coverage for paywalled scoops, and writes a tiered Markdown brief to the working folder. Use when the user asks for "today's AI brief / briefing / digest", an "AI news roundup", or when run as the scheduled daily task.
---

# Daily AI Brief

## Provenance and faithfulness note

This is a **verbatim port** of the real `daily-ai-brief` skill (found on the owner's machine
at `~/Library/Application Support/Claude/local-agent-mode-sessions/skills-plugin/.../skills/daily-ai-brief/`,
identical to `~/Claude Working Folder/Daily AI Briefs/daily-ai-brief-SKILL-updated.md`) —
**not** the earlier reconstruction this file previously contained. That reconstruction was
built without access to this source and only captured tier *names*, missing the actual named
outlets/URLs in `sources.md`, the paywall-handling procedure, the ranking rubric, and the
quality guardrails below. This corrects that (docs/adr/0007 amendment).

**The only change from the real skill: `WORKING_FOLDER` below is `/workspace`** (the microVM's
workdir, per `worker.mjs`), not the real skill's local Mac path — everything else, including
every word of the workflow, the source list, and the guardrails, is unchanged.

**Audio/delivery is deliberately NOT part of this skill**, matching the real skill's own design
("converting it to speech and delivering it is the caller's job... handled by the caller/task,
not by this skill"). Polly synthesis, SES send, and S3 archival are the **deployment's** job
(`deploy/managed-agent/deployment.json`'s initial prompt), layered on top of this skill exactly
as the local Desktop task's own SKILL.md wraps around invoking this same skill today (its
STEP 1 invokes the skill; STEPs 5-8 are the wrapping delivery task, external to the skill).

---

Produce one Markdown briefing per day on the most important developments in AI — both
**industry/business** (funding, deals, partnerships, strategy, org moves, chips, infra) and
**technical** (new papers, models, concepts, product/API releases, benchmark results) — written
so a busy, technically fluent reader can skim the headlines in 60 seconds and dive deep on what
matters.

The reader is a **technically fluent AI industry generalist** — no single employer or lab lens.
When an item touches the competitive landscape, models, enterprise/applied AI, agents, safety, or
chips/infra, add lab-neutral "why it matters" context (competitive dynamics, industry
significance, what it signals for the field) rather than analysis anchored to one company's
vantage point. Keep this analytical and even-handed, not boosterish — about any lab.

> Optional audio: this brief can also be delivered as narration. When an audio version is
> requested, additionally produce a **listening script** per the "Optional: audio /
> listening-script output" section below. The Markdown brief remains the primary deliverable;
> the listening script is a derived artifact. How the script is turned into speech and delivered
> (e.g. text-to-speech + email) is handled by the caller/task, not by this skill.

---

## Configuration

- **WORKING_FOLDER:** `/workspace`
  This is the only path you need to know. The dated brief is written here, and recent prior
  briefs are read from here — up to the last few days' worth (via the deployment's S3-backed
  "read-recent-briefs" step, which runs before this skill and writes each one under its own
  actual date; see the deployment prompt, not this skill, for that mechanism). If this folder
  ever moves, change it in this one place.
- **Source list:** `sources.md`, located in **this skill's own folder** (the directory this
  `SKILL.md` lives in). Read it from there — do not assume an absolute path, so the skill keeps
  working if it's relocated or reinstalled.

---

## Tooling

This skill runs **entirely on web search + web fetch** — they are the engine, not an optional
add-on. Use whatever the current environment exposes; the names differ by runtime:
- **Claude Code / Cowork:** `WebSearch` and `WebFetch`.
- **Claude API / web app:** `web_search` and `web_fetch`.

References to `WebSearch`/`WebFetch` below mean *"the environment's web-search / web-fetch
capability."* If the available tools are named differently, map to the equivalent rather than
failing. Use **fetch** to pull a known URL or RSS/Atom feed; use **search** for date-scoped
discovery and for finding free coverage of paywalled stories. If no web access is available at
all, **stop and tell the user** — without live retrieval the skill cannot produce a real brief,
and it must never fabricate one from memory.

---

## Output contract

**File:** `{WORKING_FOLDER}/AI Brief - YYYY-MM-DD.md`
(Use today's local date, ISO `YYYY-MM-DD` for chronological file sorting. If a file for today
already exists, overwrite it.)

**Always write the file** — never just print the brief in chat. End your turn with a one-line
summary and a `computer://` link to the file.

**Structure (tiered):**

```
# Daily AI Brief — {Weekday}, {Month} {D}, {YYYY}

_{One-sentence "today in AI" tl;dr — the single most important thing.}_

## 📌 Headlines
- **{Category emoji} {Tight 1-line headline}** — _{outlet}_
- ... (8–15 bullets, skimmable, ordered by importance; each maps to a deep-dive below if it has one)

---

## 🔬 Research & Models
{Deep-dive items — see item format below}

## 🏢 Industry, Deals & Strategy
{Deep-dive items}

## 🛠️ Products, Tools & Releases
{Deep-dive items}

## 📊 Benchmarks & Evals
{Deep-dive items — only when there's real movement}

## 🏛️ Policy, Safety & Society
{Deep-dive items}

---

_Sources checked: {n} feeds/sites across labs, arXiv, press, community. Generated {timestamp}._
```

Omit any section with no items that day (don't print empty sections). Put 5–10 items into deep
dives total — not everything in Headlines needs a deep dive, and not every section will have
content every day.

**Item format (deep dives)** — mirror the source newsletter's style:

```
### {Headline-style title} ({Month D, YYYY})
{Dense, factual Summary paragraph: 3–6 sentences. Lead with the concrete facts — numbers, names, model versions, benchmark scores, dollar amounts, dates. Then, where relevant, one lab-neutral sentence on why it matters (competitive dynamics, industry significance, what it signals for the field — not framed around any single company). Neutral, specific, no hype.}
**Sources:** [{Outlet}]({url}) · [{Outlet 2}]({url2})
```

Write summaries the way the attached AWS Competitor newsletter does: information-dense, no
filler, every sentence carries a fact. Prefer specifics ("scored 85.6% on CyberGym vs 83.8% for
the prior model") over vagueness ("performed well").

---

## Daily workflow

### 1. Set up
- Determine today's local date (via the date in context or `date` in bash).
- Read the source list `sources.md` from **this skill's own folder**. It's tiered by priority.
- Optional but recommended: read the **most recent prior brief** in `WORKING_FOLDER` (check
  today−1, and if absent walk back up to ~5 days to cover weekends/gaps) so you can **avoid
  repeating** stories and instead report genuine *updates* ("follow-up: ...").

### 2. Gather (cast a wide net, last ~24–48h)
Work down the tiers, favoring breadth then pruning:
- **Tier 1 (labs)** and **Tier 2 (papers — esp. Hugging Face Daily Papers + arXiv cs.CL/cs.LG/
  cs.AI RSS)** every day. These are the backbone.
- **Tier 4 (tech press)** and **Tier 7 (HN, Reddit, GitHub/HF trending)** every day for industry
  + developer pulse.
- **Tier 3 (benchmarks)**, **Tier 8 (policy)**, **Tier 9 (chips/infra)** — scan; include only
  when there's real news.
- **Tier 6 (curated newsletters)** — use as a **cross-check** at the end to catch anything you
  missed; don't just copy them.

Mechanics:
- Prefer **RSS/Atom feeds** (listed in sources.md) — fetch with `WebFetch`; they're structured
  and fetch-friendly.
- For sites without a feed, use `WebSearch` with date-scoped queries, e.g. `"<topic>" AI news`
  and rely on freshness; or search a site directly (`site:techcrunch.com AI`).
- **Filter by publication date.** Feeds and index pages routinely return items days or weeks old
  (arXiv RSS especially, and "trending" pages have no fixed window). For every candidate, check
  its publish/update timestamp and **discard anything outside the last ~48h** unless it's a
  genuine follow-up to a still-developing story. When a date is missing or ambiguous, treat the
  item as unverified and confirm via a second source before including it.
- **Budget the fetches.** You don't need every source — batch related fetches, work top tiers
  first, and **stop once you have strong, diverse coverage** (~25–40 candidates) across business
  + technical. Don't exhaustively crawl every feed every day.
- Capture per item: title, 1–2 line gist, outlet, URL, and **publish date**.

### 3. Paywall handling (important — don't skip paywalled scoops)
Outlets like **The Information, Bloomberg, WSJ, FT, NYT** break many top stories but usually
won't fetch. When you hit one:
1. Note the headline + outlet as the originating scoop.
2. Run a **`WebSearch` for the same story** to find **free coverage** (TechCrunch, The Verge,
   Axios, Reuters, AP, VentureBeat, company blog, or a newsletter recap). Open-source/company
   primary sources are best.
3. Write the summary from the free coverage, and in **Sources** list both the free source(s)
   **and** the original scoop (so the reader can dig in if they have access). Mark the
   paywalled one if helpful: `[The Information (paywall)](url)`.
4. If no free coverage exists yet, still include a short headline-only item noting "originally
   reported by {outlet}; no open coverage found yet."

### 4. Dedupe & cluster
- Many outlets cover the same event. **Merge** them into one item with multiple Sources rather
  than repeating.
- Cluster related items (e.g., three chip-supply deals) into one item when that tells a cleaner
  story.
- Drop pure rehashes of yesterday unless there's a material update.

### 5. Rank & select
Prioritize by **newsworthiness**, judged the same way regardless of which lab a story is about. A
rough rubric (higher = lead with it):
- New frontier model / major capability release / significant paper with results — from **any**
  frontier lab (OpenAI, Google/DeepMind, Meta, Mistral, xAI, Anthropic, major Chinese open-weight
  labs, etc.), evaluated on the same footing.
- Benchmark SOTA changes; agentic/coding/enterprise-AI developments (a broadly technical reader's
  domain).
- Big deals/funding/chips/infra that shift the competitive or cost landscape.
- Policy/safety actions affecting frontier labs.
No lab gets its own elevated tier just for being that lab — a story leads because of its
newsworthiness (scale, novelty, capability delta, market impact), not because of which company it
concerns. Down-weight: minor product tweaks, marketing, rumor without substance, stories with no
credible source.

Target **8–15 headlines** and **5–10 deep dives**. Quality over quantity — a tight brief beats a
bloated one.

### 6. Write
- Write the file per the **Output contract**. Build it section by section.
- Each deep-dive summary: lead with facts, then the lab-neutral "why it matters" sentence where
  it adds insight. Verify numbers against the source; don't invent figures, dates, or benchmark
  scores. If a claim is a rumor/unconfirmed, say so.
- Keep links as real URLs you actually retrieved. **Never fabricate URLs or sources.** If you
  couldn't verify a link, omit it or mark the item as unverified.
- Category emoji for headlines: 🔬 research/models · 🏢 industry/deals · 🛠️ product/tools ·
  📊 benchmarks · 🏛️ policy/safety.

### 7. Finish
- Save to the working folder path above.
- Reply with a one-sentence highlight and a `computer://` link to the file (or, in this
  runtime, its `/workspace` path). Keep the chat reply short — the brief is the deliverable.

---

## Optional: audio / listening-script output

Produce this **only when an audio/narrated version is requested** (e.g. by the scheduled task —
which it always is in this runtime). It is a separate, derived artifact — the Markdown brief is
still written exactly as above and remains primary. This skill produces only the *script*;
converting it to speech and delivering it is the caller's job.

The listening script is **plain text optimized for the ear**, not the Markdown:

- **No URLs, no emoji, no Markdown syntax, no "Sources:" lines.** None of that reads well aloud.
- **Length ~800–1,200 words** (≈5–8 minutes at ~150 wpm) — sized for a commute. Trim on quiet
  days.
- **Structure:** a spoken intro ("Your AI brief for {Weekday}, {Month} {D}. Top story today…"),
  then a quick run-through of the headlines, then the deep dives in flowing prose. Use natural
  sentence transitions rather than section headers.
- **Normalize for pronunciation:** "$2.5B" → "2.5 billion dollars"; "85.6%" → "eighty-five point
  six percent" where clarity helps; expand or letter-read acronyms (e.g. "RLHF") the first time
  if it aids comprehension.
- **Keep it self-contained:** a listener can't click anything, so phrase each item so it stands
  on its own without the link.

Save the script as plain UTF-8 text where the caller expects it (in this runtime,
`{WORKING_FOLDER}/listening-script.txt` — the caller's prompt specifies the exact path). Do not
embed any credentials, service names, or delivery mechanics in the script — those belong to the
task, not the brief.

---

## Quality bar & guardrails
- **Accuracy first.** This brief informs a professional's view of their field. Every number,
  name, and date must trace to a source you fetched. When unsure, hedge explicitly
  ("reportedly", "unconfirmed").
- **No hallucinated sources or links.** Real URLs only.
- **Even-handed.** Report every lab's wins and setbacks straight — analysis, not spin, and no
  single lab gets a structural head start in coverage or selection.
- **Dense, not padded.** Match the source newsletter's information density. Cut filler
  sentences.
- **Technical depth is welcome** — include architectures, methods, eval methodology, and
  caveats (e.g., "parity only on the agentic dimension, not across all capabilities").
- **Dedupe aggressively** so the reader isn't re-reading the same story five times.
- If a day is genuinely quiet, a shorter brief is fine — don't manufacture items.

## Tips
- arXiv RSS is high-volume: filter by title/abstract keywords (LLM, agent, reasoning, RL, eval,
  benchmark, scaling, MoE, long-context, RAG, multimodal, alignment, interpretability,
  inference, quantization, distillation, diffusion) and surface only the few with notable
  results or strong community attention (cross-check Hugging Face Daily Papers).
- Hugging Face Daily Papers is the best single technical-signal source — start there for
  research.
- smol.ai's "AI News" and Zvi's roundup are excellent end-of-pass cross-checks for "what did I
  miss in the discourse."
- Keep `sources.md` current: if a feed dies, fall back to WebSearch and fix the URL when you
  can.

## Reader context
A general, technically fluent audience: expert-level in Gen AI, LLMs, agentic AI, and modern
model/infra tooling — no hand-holding needed, no single employer's vantage point assumed. Dates
as DD.MM.YYYY. If a source fails to fetch, fall back to a date-scoped web search and continue.
