"""Pipeline code that runs inside the self-hosted Managed Agents microVM.

`audio_email.py` — STEP 6 (Polly synth + SES owner-copy + subscriber fan-out), ported
from `deploy/audio_email.py` for the credential-free microVM runtime (docs/adr/0004).
`brief_history.py` — S3-backed cross-run "read yesterday / archive today" persistence
(docs/adr/0005), replacing the local `Daily AI Briefs/` working folder.
"""
