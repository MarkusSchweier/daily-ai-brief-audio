# MicroVM image for the daily-brief self-hosted Managed Agents worker.
#
# Adapted from AWS's reference implementation
# (github.com/aws-samples/sample-lambda-microvm-claude-managed-agents,
# src/microvm-image/Dockerfile) per docs/adr/0006. See worker/worker.mjs for the
# lifecycle-hook entrypoint and its TODO marking where the daily-brief pipeline
# code (research/writing half per ADR-0007, audio/email half = deploy/audio_email.py)
# gets wired in — that port is a separate developer task, not built by this stack.
#
# At IMAGE BUILD TIME the worker starts and is snapshotted running (this is what
# gives Lambda MicroVMs their sub-second boot: the snapshot already has Node,
# the worker process, and all installed deps warm).
# At RUN TIME the /run lifecycle hook (POST from the Lambda MicroVMs runtime,
# not the launcher directly) delivers the session dispatch payload built by
# launcher.py's build_run_hook_payload(); the worker acknowledges, then handles
# the session's tool calls (this is where the daily-brief pipeline actually runs).
ARG BASE_IMAGE=public.ecr.aws/lambda/microvms:al2023-minimal
FROM ${BASE_IMAGE}

# Worker + pipeline runtime dependencies.
#   - nodejs/npm: the Anthropic SDK worker (EnvironmentWorker/WorkPoller) is a
#     Node package, matching the reference implementation's worker.
#   - python3/pip: the ported daily-brief pipeline code (ADR-0007's research/
#     writing skill invocation + deploy/audio_email.py, both Python) runs as a
#     tool call the worker's `handleSession()` shells out to — see the TODO in
#     worker.mjs. Not needed by the worker process itself, but the pipeline code
#     the agent's tool calls invoke needs a Python 3.13 runtime present in the
#     image (matching the Lambda runtime used elsewhere in this repo's CDK, e.g.
#     deploy/subscribers/brief_subscribers/stack.py).
RUN dnf install -y --setopt=install_weak_deps=0 bash unzip tar gzip && \
    (command -v curl >/dev/null 2>&1 || dnf install -y curl) && \
    dnf install -y nodejs npm python3.13 python3.13-pip && \
    node --version && \
    python3.13 --version && \
    dnf clean all

WORKDIR /workspace

# Install the worker package (Node — Anthropic SDK's EnvironmentWorker/WorkPoller).
COPY worker/package.json /opt/worker/package.json
RUN cd /opt/worker && npm install --omit=dev

COPY worker/worker.mjs /opt/worker/worker.mjs

# --- TODO (separate developer task, ADR-0007 + this repo's audio/email half) ---
# Once the research/writing skill is ported (deploy/managed-agent/skills/daily-ai-brief/
# per ADR-0007) and the pipeline's Python entrypoint exists, install/copy it here, e.g.:
#
#   COPY skills/daily-ai-brief /opt/skills/daily-ai-brief
#   COPY audio_email.py /opt/pipeline/audio_email.py
#   COPY requirements.txt /opt/pipeline/requirements.txt
#   RUN python3.13 -m pip install --no-cache-dir -r /opt/pipeline/requirements.txt
#
# boto3 in this image authenticates automatically via the microVM execution
# role's IMDSv2-delivered temporary credentials (ADR-0004) — no credential
# wiring needed here, just `import boto3` in the pipeline code as
# deploy/audio_email.py already does.
# --------------------------------------------------------------------------------

VOLUME /mnt/session/outputs

# Start the worker at build time so it is captured in the snapshot (this is what
# the Lambda MicroVMs "launch from snapshot" model requires — see worker.mjs's
# module docstring for the lifecycle-hook contract).
ENTRYPOINT ["node", "/opt/worker/worker.mjs"]
