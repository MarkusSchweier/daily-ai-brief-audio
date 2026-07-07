"""New code built for ADR-0016 "Eval-harness re-integration" -- everything in this
package is NEW (not ported): the cost model (`cost.py`, D2), the per-eval-run
directory record store (`run_store.py`, D4), the recent-briefs dedup-priors fetch
(`dedup_priors.py`), and the trigger/retrieve/record CLI (`run.py`, D4).

Imports `deploy/candidates/candidate_sync` via a `sys.path` shim (see each module's
own header) rather than duplicating it, per ADR-0016 D1: "driving the existing
candidate_sync trigger/retrieve mechanism."
"""
