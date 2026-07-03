"""Unit tests for the shared helper module (email normalization, tokens, compare)."""

import time

import subscriber_common as common


def test_normalize_email_lowercases_and_trims():
    assert common.normalize_email("  Foo.Bar@Example.COM  ") == "foo.bar@example.com"


def test_normalize_email_handles_empty():
    assert common.normalize_email("") == ""
    assert common.normalize_email(None) == ""


def test_is_valid_email_accepts_reasonable_addresses():
    assert common.is_valid_email("person@example.com")
    assert common.is_valid_email("first.last+tag@sub.example.co.uk")


def test_is_valid_email_rejects_garbage():
    assert not common.is_valid_email("")
    assert not common.is_valid_email("not-an-email")
    assert not common.is_valid_email("missing-domain@")
    assert not common.is_valid_email("@missing-local.com")
    assert not common.is_valid_email("has spaces@example.com")
    assert not common.is_valid_email("no-tld@example")


def test_is_valid_email_rejects_overlong_addresses():
    long_local = "a" * 250
    assert not common.is_valid_email(f"{long_local}@example.com")


def test_generate_token_is_unique_and_url_safe():
    token_a = common.generate_token()
    token_b = common.generate_token()
    assert token_a != token_b
    assert len(token_a) > 32
    # URL-safe base64 alphabet only (no '+', '/', or padding weirdness expected here).
    assert all(c.isalnum() or c in "-_" for c in token_a)


def test_constant_time_equals_matches_and_mismatches():
    assert common.constant_time_equals("abc", "abc")
    assert not common.constant_time_equals("abc", "abd")
    assert not common.constant_time_equals(None, "abc")
    assert not common.constant_time_equals("abc", None)


def test_clamp_name_trims_and_bounds_length():
    assert common.clamp_name("  Ada  ") == "Ada"
    long_name = "x" * 500
    assert len(common.clamp_name(long_name)) == common.MAX_NAME_LENGTH


def test_now_epoch_is_close_to_wall_clock():
    assert abs(common.now_epoch() - int(time.time())) <= 2


def test_build_response_shapes_lambda_proxy_response():
    resp = common.build_response(200, "<p>ok</p>")
    assert resp["statusCode"] == 200
    assert resp["body"] == "<p>ok</p>"
    assert resp["headers"]["Content-Type"] == "text/html; charset=utf-8"


def test_weekday_send_time_label_renders_the_canonical_value():
    # PRD instant-welcome-brief.md AC-9: the welcome email's stated time is produced FROM
    # these constants, so changing them changes the rendered label with no other edit.
    assert common.weekday_send_time_label() == "06:07 (Europe/Berlin)"
    assert common.WEEKDAY_SEND_HOUR == 6
    assert common.WEEKDAY_SEND_MINUTE == 7
    assert common.WEEKDAY_SEND_TIMEZONE == "Europe/Berlin"
