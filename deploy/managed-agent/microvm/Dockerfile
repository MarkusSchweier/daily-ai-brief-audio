# MicroVM image for the daily-brief self-hosted Managed Agents worker.
#
# Adapted from AWS's reference implementation
# (github.com/aws-samples/sample-lambda-microvm-claude-managed-agents,
# src/microvm-image/Dockerfile) per docs/adr/0006. See worker/worker.mjs for the
# lifecycle-hook entrypoint; the daily-brief pipeline code (research/writing skill
# per ADR-0007, audio/email half adapted from deploy/audio_email.py) is wired in
# below via COPY.
#
# BUILD CONTEXT / ARCHIVE LAYOUT: AWS's `create-microvm-image` requires the
# Dockerfile at the ROOT of the zipped image source (confirmed against the
# reference implementation's build-image.sh: "Dockerfile must be at the archive
# root"). The ported skill (../skills/daily-ai-brief/, ADR-0007) and pipeline
# code (../pipeline/, this file's sibling COPY sources) live one level up in the
# repo (deploy/managed-agent/skills/, deploy/managed-agent/pipeline/) rather than
# nested under microvm/, so they stay co-located with the rest of this repo's
# `deploy/managed-agent/` source-of-truth tree, not buried inside the image-build
# subfolder. README.md's build step therefore STAGES a temporary build directory
# (Dockerfile + worker/ from here, plus skills/ and pipeline/ copied in
# alongside) before zipping that staging directory's contents — see
# "Build and push the microVM container image" in README.md. Building this
# Dockerfile directly with `docker build .` from THIS directory (`microvm/`)
# also works for local testing, but skills/ and pipeline/ must first be staged
# here manually (or use `docker build -f Dockerfile ..` with a build context one
# level up and the COPY paths below adjusted) — README.md's staging step is the
# supported path for a real image build.
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
#   - python3/pip: the ported daily-brief pipeline code (the SKILL.md's STEP 6/
#     STEP 3 bash instructions, which invoke /opt/pipeline/audio_email.py) runs
#     as a tool call Claude issues once EnvironmentWorker.handleItem() picks up
#     the session — see worker.mjs's handleSession(). Not needed by the worker
#     process itself, but the pipeline code the agent's tool calls invoke needs
#     a Python 3.13 runtime present in the image (matching the Lambda runtime
#     used elsewhere in this repo's CDK, e.g.
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

# --- Daily-brief pipeline code (ADR-0007 research/writing skill port + the
# --- microVM-adapted audio/email half) -------------------------------------------
#
# The research/writing skill (deploy/managed-agent/skills/daily-ai-brief/SKILL.md,
# ADR-0007) is content the agent LOADS and follows, not code that runs — copied
# in so the deployment's `agent.skills: ["daily-ai-brief"]` reference (see
# deployment.json) resolves to a real file inside the image.
COPY skills/daily-ai-brief /opt/skills/daily-ai-brief

# The audio/email half (ADR-0004/ADR-0005-adapted port of deploy/audio_email.py)
# IS code the SKILL.md's STEP 6/STEP 3 shell out to via
# `python3.13 /opt/pipeline/audio_email.py` (send + archive) and
# `python3.13 /opt/pipeline/audio_email.py read-yesterday` (S3 "yesterday" read).
# brief_history.py is copied alongside it (same directory) because audio_email.py
# imports it via a same-directory sys.path insert — see that file's own docstring.
COPY pipeline/audio_email.py /opt/pipeline/audio_email.py
COPY pipeline/brief_history.py /opt/pipeline/brief_history.py
COPY pipeline/__init__.py /opt/pipeline/__init__.py
# boto3 is the only third-party dependency the pipeline code needs; no
# requirements.txt beyond it, matching deploy/audio_email.py's own dependency
# footprint (stdlib email/urllib/time + boto3 only).
RUN python3.13 -m pip install --no-cache-dir boto3

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
