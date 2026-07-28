"""Session token handling and log redaction.

Covers Phase 1 test category 17 from the non-HTTP side.
"""

from __future__ import annotations

import io
import logging
import pickle

import pytest

from mom_igd.logging_setup import RedactingFilter, get_logger, setup_logging
from mom_igd.security import REDACTED, SESSION_TOKEN_HEADER, SessionToken, redact


# ------------------------------------------------------------- generation


def test_tokens_are_random_and_long_enough() -> None:
    tokens = {SessionToken().value for _ in range(50)}
    assert len(tokens) == 50, "tokens must not repeat"
    assert all(len(value) >= 32 for value in tokens)


def test_short_or_non_string_tokens_are_rejected() -> None:
    with pytest.raises(ValueError, match="at least"):
        SessionToken("too-short")
    with pytest.raises(ValueError):
        SessionToken(12345)  # type: ignore[arg-type]


def test_matches_is_exact() -> None:
    token = SessionToken()
    assert token.matches(token.value)
    assert not token.matches(token.value + "x")
    assert not token.matches(token.value[:-1])
    assert not token.matches("")
    assert not token.matches(None)


def test_header_uses_the_documented_name() -> None:
    token = SessionToken()
    assert token.header() == {SESSION_TOKEN_HEADER: token.value}
    assert SESSION_TOKEN_HEADER == "X-MoM-Session-Token"


def test_equality_compares_values_without_exposing_them() -> None:
    value = SessionToken().value
    assert SessionToken(value) == SessionToken(value)
    assert SessionToken(value) != SessionToken()


# ------------------------------------------------ accidental-disclosure guards


def test_str_repr_and_format_are_redacted() -> None:
    token = SessionToken()
    assert str(token) == REDACTED
    assert token.value not in repr(token)
    assert token.value not in f"{token}"
    assert token.value not in f"token={token!r}"
    assert token.value not in "{}".format(token)  # noqa: UP032 - explicitly testing format()


def test_token_cannot_be_pickled_to_disk() -> None:
    with pytest.raises(TypeError, match="never be pickled"):
        pickle.dumps(SessionToken())


def test_redact_helper_scrubs_values_and_tokens() -> None:
    token = SessionToken()
    text = f"calling with {token.value} and secret123"
    scrubbed = redact(text, token, "secret123")
    assert token.value not in scrubbed
    assert "secret123" not in scrubbed
    assert scrubbed.count(REDACTED) == 2


# ----------------------------------------------------------- log redaction


def test_logging_filter_scrubs_the_token_from_messages() -> None:
    token = SessionToken()
    stream = io.StringIO()
    setup_logging("DEBUG", session_token=token, stream=stream)
    log = get_logger("test")
    log.info("token is %s", token.value)
    log.warning("interpolated %s here", f"prefix-{token.value}-suffix")
    log.error(f"fstring {token.value}")
    output = stream.getvalue()
    assert token.value not in output
    assert REDACTED in output


def test_logging_filter_scrubs_credentials_from_query_strings() -> None:
    stream = io.StringIO()
    setup_logging("DEBUG", session_token=None, stream=stream)
    log = get_logger("test")
    log.info("GET /doctor?token=abc123DEF456 HTTP/1.1")
    log.info("GET /x?session_token=zzz&other=1")
    output = stream.getvalue()
    assert "abc123DEF456" not in output
    assert "zzz" not in output
    assert "other=1" in output, "only the credential must be removed"


def test_secrets_added_after_setup_are_also_scrubbed() -> None:
    stream = io.StringIO()
    redactor = setup_logging("DEBUG", session_token=None, stream=stream)
    later = SessionToken()
    redactor.add_secret(later)
    get_logger("test").info("value %s", later.value)
    assert later.value not in stream.getvalue()


def test_uvicorn_loggers_are_routed_through_the_redacting_handlers() -> None:
    token = SessionToken()
    stream = io.StringIO()
    setup_logging("DEBUG", session_token=token, stream=stream)
    logging.getLogger("uvicorn.error").info("leak attempt %s", token.value)
    output = stream.getvalue()
    assert token.value not in output
    assert "leak attempt" in output


def test_redacting_filter_is_installed_on_every_handler() -> None:
    stream = io.StringIO()
    setup_logging("INFO", session_token=SessionToken(), stream=stream)
    log = get_logger()
    assert log.handlers
    for handler in log.handlers:
        assert any(isinstance(f, RedactingFilter) for f in handler.filters)


def test_setup_logging_refuses_a_missing_log_directory(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="does not exist"):
        setup_logging("INFO", log_file=tmp_path / "no-such-dir" / "app.log")


def test_log_file_is_written_when_the_directory_exists(paths) -> None:
    token = SessionToken()
    target = paths.log_file("audit-test.log")
    setup_logging("INFO", log_file=target, session_token=token)
    get_logger("test").info("hello %s", token.value)
    for handler in get_logger().handlers:
        handler.flush()
    contents = target.read_text(encoding="utf-8")
    assert "hello" in contents
    assert token.value not in contents, "the on-disk log must be redacted too"


@pytest.fixture(autouse=True)
def _reset_logging():
    """Leave the logging tree clean so tests cannot influence one another."""
    yield
    for name in ("mom_igd", "uvicorn", "uvicorn.error", "uvicorn.access"):
        log = logging.getLogger(name)
        for handler in list(log.handlers):
            log.removeHandler(handler)
            handler.close()
