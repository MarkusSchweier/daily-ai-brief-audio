"""Shared constants for the launcher.

Ported verbatim from AWS's reference implementation
(github.com/aws-samples/sample-lambda-microvm-claude-managed-agents,
src/functions/shared/constants.py) per docs/adr/0006's "adapt, don't reinvent"
decision. Do not hand-edit ARN templates or the beta header without re-checking
the reference/AWS docs — these are confirmed-correct API surface, not guesses.
"""

# AWS-managed network connector ARN templates. These are fixed, AWS-owned resources
# (not created by this stack) that RunMicroVm attaches to give the microVM its
# default public ingress/egress (ADR-0006: no VPC/NAT/allowlist required).
ALL_INGRESS_TEMPLATE = "arn:aws:lambda:{region}:aws:network-connector:aws-network-connector:ALL_INGRESS"
INTERNET_EGRESS_TEMPLATE = "arn:aws:lambda:{region}:aws:network-connector:aws-network-connector:INTERNET_EGRESS"


def all_ingress_arn(region: str) -> str:
    """ALL_INGRESS connector ARN for the given region."""
    return ALL_INGRESS_TEMPLATE.format(region=region)


def internet_egress_arn(region: str) -> str:
    """INTERNET_EGRESS connector ARN for the given region."""
    return INTERNET_EGRESS_TEMPLATE.format(region=region)


# A once-a-day ~10-minute pipeline run is well within this ceiling (PRD §6
# "Session runtime... non-issue"); kept at the reference's max (8h) rather than
# tightened, since a slow research/Polly-wait day should still complete rather
# than be killed by an artificially low ceiling.
DEFAULT_MAX_LIFETIME_SECONDS = 28800  # 8 hours

DEFAULT_LAUNCH_TPS_LIMIT = 5

RUN_HOOK_PAYLOAD_VERSION = "1"

# Managed Agents beta API version this integration is built against (PRD FR-2,
# ADR-0006). Record here, not just in docs, so a future contract change is easy
# to grep for. The webhook payload itself does not carry this header (webhooks
# are unauthenticated-transport / signature-verified, not API calls) but any
# Deployments-API call this repo makes (deployment.json) must send it.
MANAGED_AGENTS_BETA_HEADER = "managed-agents-2026-04-01"

# CONFIRMED LIVE BUG (2026-07-03), now fixed: the reference implementation's 300s
# (5-minute) idle window assumes something periodically invokes the MicroVM's own
# AWS-facing endpoint to prove liveness -- the classic scale-to-zero pattern. This
# worker (worker.mjs) does the opposite: it POLLS Anthropic's Sessions API outward
# (WorkPoller/EnvironmentWorker) and is never itself invoked inbound. So AWS's idle
# clock starts at boot and ticks continuously regardless of how much real
# research/tool-call activity is happening inside the microVM -- a real end-to-end
# test run was killed by this ("MicroVM exceeded maximum lifetime", actual runtime
# ~367s = 300s idle + 60s suspended + autoResumeEnabled=False -> terminated) less
# than 6 minutes in, mid-pipeline, well before Polly synthesis or SES send. Fixed by
# setting maxIdleDurationSeconds to the same ceiling as DEFAULT_MAX_LIFETIME_SECONDS
# above, so the (meaningless, for this poll-based worker) idle-suspend mechanism
# never fires before the real 8-hour hard cap does.
DEFAULT_IDLE_POLICY = {
    "maxIdleDurationSeconds": DEFAULT_MAX_LIFETIME_SECONDS,
    "suspendedDurationSeconds": 60,
    "autoResumeEnabled": False,
}

DEFAULT_LOGGING_CONFIG = {
    "cloudWatch": {
        "logGroup": "/aws/lambda/microvms/claude-daily-brief-worker",
    }
}

SESSION_RUN_STARTED = "session.status_run_started"
