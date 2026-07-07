"""Core, delivery-agnostic evaluation logic for the daily AI brief eval harness.

PORTED (ADR-0016 "Eval-harness re-integration", D1, Phase 1 of the phased
implementation plan) from `deploy/eval/eval_core/` — the four judges,
`record.py`, and `calibration.py` are pure Python with no AWS/CDK import, so
they move here UNCHANGED in substance (only this docstring and each module's
own header gained a porting note). `deploy/eval/` itself is left untouched;
it is retired only as a later, owner-gated step (ADR-0016 phase 5) after this
package is validated. See `docs/adr/0013-eval-harness-backbone-build-vs-adopt.md`
for the ORIGINAL design context these modules were built under, and
`docs/adr/0016-eval-harness-reintegration.md` for why they moved: the old
harness's AWS-native retrieval (S3 polling) no longer applies now that content
generation is decoupled from AWS delivery (ADR-0014/ADR-0015) — but the pure
scoring/record/calibration logic never depended on that retrieval path, so it
is reused as-is.

Held separate from `harness/` (this package's NEW code: cost attribution
against `candidate_sync`-retrieved artifacts, the trigger/retrieve/record CLI,
the Flask UI) so the ported core logic stays clearly delineated from what
ADR-0016 newly built.
"""
