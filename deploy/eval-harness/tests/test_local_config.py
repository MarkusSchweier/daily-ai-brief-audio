"""Tests for harness.local_config -- the env-var -> well-known-local-file -> default
resolution added after the owner's first UI-triggered run failed (2026-07-07): the
Flask UI's server process had no $RECENT_BRIEFS_SIGNING_KEY exported, so the
trigger subprocess failed loud before spending anything. The resolver lets a
long-lived server process trigger runs without inherited exports, mirroring the
Anthropic-API-key file convention."""

from __future__ import annotations

from harness import local_config


def _point_key_file_at(monkeypatch, tmp_path, content: str | None):
    key_file = tmp_path / "recent-briefs-signing-key.txt"
    if content is not None:
        key_file.write_text(content, encoding="utf-8")
    monkeypatch.setattr(local_config, "SIGNING_KEY_FILE", key_file)
    return key_file


def test_signing_key_env_var_wins(monkeypatch, tmp_path):
    _point_key_file_at(monkeypatch, tmp_path, "file-key-should-lose")
    monkeypatch.setenv(local_config.RECENT_BRIEFS_SIGNING_KEY_ENV_VAR, "env-key-wins")
    assert local_config.resolve_recent_briefs_signing_key() == "env-key-wins"


def test_signing_key_falls_back_to_the_well_known_file(monkeypatch, tmp_path):
    _point_key_file_at(monkeypatch, tmp_path, "  file-key\n")
    monkeypatch.delenv(local_config.RECENT_BRIEFS_SIGNING_KEY_ENV_VAR, raising=False)
    # Stripped -- a trailing newline from `aws ... --output text > file` must not
    # end up inside the HMAC input.
    assert local_config.resolve_recent_briefs_signing_key() == "file-key"


def test_signing_key_missing_everywhere_is_none_not_an_exception(monkeypatch, tmp_path):
    _point_key_file_at(monkeypatch, tmp_path, None)  # file does not exist
    monkeypatch.delenv(local_config.RECENT_BRIEFS_SIGNING_KEY_ENV_VAR, raising=False)
    assert local_config.resolve_recent_briefs_signing_key() is None


def test_signing_key_empty_file_is_none(monkeypatch, tmp_path):
    _point_key_file_at(monkeypatch, tmp_path, "   \n")
    monkeypatch.delenv(local_config.RECENT_BRIEFS_SIGNING_KEY_ENV_VAR, raising=False)
    assert local_config.resolve_recent_briefs_signing_key() is None


def test_delivery_base_url_env_var_overrides_default(monkeypatch):
    monkeypatch.setenv(local_config.DELIVERY_BASE_URL_ENV_VAR, "https://example.invalid")
    assert local_config.resolve_delivery_base_url() == "https://example.invalid"


def test_delivery_base_url_defaults_to_the_committed_constant(monkeypatch):
    monkeypatch.delenv(local_config.DELIVERY_BASE_URL_ENV_VAR, raising=False)
    assert local_config.resolve_delivery_base_url() == local_config.DEFAULT_DELIVERY_BASE_URL
    assert local_config.DEFAULT_DELIVERY_BASE_URL.startswith("https://")


def test_sources_hint_names_both_sources():
    hint = local_config.signing_key_sources_hint()
    assert local_config.RECENT_BRIEFS_SIGNING_KEY_ENV_VAR in hint
    assert "recent-briefs-signing-key.txt" in hint
