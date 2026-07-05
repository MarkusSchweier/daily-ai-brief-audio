"""Core, backbone-agnostic evaluation logic for the daily AI brief eval harness.

See docs/prd/eval-harness.md and docs/adr/0013-eval-harness-backbone-build-vs-adopt.md.
This package holds the pure-Python pieces (cost mining, judges, the structured record
schema, replicate aggregation) that the `deploy/eval/` CDK app's Lambdas import — kept
separate from `functions/` (the Lambda handler shims) and `brief_eval/` (the CDK stack
itself) so the core logic is testable without any AWS/CDK machinery in the loop.
"""
