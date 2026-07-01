"""daily-ai-brief-audio — narrate the daily AI brief (Polly) and email it (SES).

Pipeline (see docs/audio-mail-integration.md):
    listening script -> Polly async StartSpeechSynthesisTask -> MP3 in S3
    -> download via OutputUri -> MIME email (HTML brief + MP3) -> SES send_raw_email

This package is the skeleton; the pipeline is built via the /feature workflow.
"""

__version__ = "0.1.0"
