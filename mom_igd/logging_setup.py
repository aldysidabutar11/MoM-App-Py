"""Logging configuration with secret redaction.

Two properties matter here:

* **No secret ever reaches a log sink.** :class:`RedactingFilter` scrubs the live
  session token from every record, including records emitted by third-party
  libraries such as uvicorn, before a handler can format them.
* **File logging is opt-in.** ``setup_logging`` only writes to disk when the
  caller passes a log file, so importing or diagnosing the application never
  creates the runtime tree as a side effect.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Final, Iterable

from mom_igd.security import REDACTED, SessionToken

__all__ = ["RedactingFilter", "get_logger", "setup_logging"]

LOGGER_NAME: Final[str] = "mom_igd"

_CONSOLE_FORMAT: Final[str] = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_FILE_FORMAT: Final[str] = (
    "%(asctime)s %(levelname)-8s %(process)d %(name)s %(filename)s:%(lineno)d: %(message)s"
)
_DATE_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%S"

# Query-string keys that must never appear in a logged URL.
_FORBIDDEN_QUERY_KEYS: Final[tuple[str, ...]] = ("token=", "session_token=", "api_key=")


class RedactingFilter(logging.Filter):
    """Remove known secrets from log records.

    Applied to handlers (not loggers) so that records propagating from
    third-party loggers are also scrubbed.
    """

    def __init__(self, secrets: Iterable[str | SessionToken] = ()) -> None:
        super().__init__()
        self._secrets: list[str] = []
        for item in secrets:
            self.add_secret(item)

    def add_secret(self, secret: str | SessionToken | None) -> None:
        if secret is None:
            return
        raw = secret.value if isinstance(secret, SessionToken) else secret
        if raw and raw not in self._secrets:
            self._secrets.append(raw)

    def _scrub(self, text: str) -> str:
        for raw in self._secrets:
            text = text.replace(raw, REDACTED)
        lowered = text.lower()
        for key in _FORBIDDEN_QUERY_KEYS:
            index = lowered.find(key)
            while index != -1:
                start = index + len(key)
                end = start
                while end < len(text) and text[end] not in "&\" '\n\t":
                    end += 1
                text = text[:start] + REDACTED + text[end:]
                lowered = text.lower()
                index = lowered.find(key, start + len(REDACTED))
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._scrub(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: self._scrub(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._scrub(value) if isinstance(value, str) else value
                    for value in record.args
                )
        return True


def setup_logging(
    level: str = "INFO",
    *,
    log_file: Path | None = None,
    session_token: SessionToken | None = None,
    stream: object | None = None,
) -> RedactingFilter:
    """Configure the ``mom_igd`` logger tree and return the redaction filter.

    Args:
        level: One of ``DEBUG``..``CRITICAL``.
        log_file: Optional file to append to. The parent directory must already
            exist -- logging never creates the runtime tree.
        session_token: Token to scrub from every record.
        stream: Console stream; defaults to ``sys.stderr`` so that stdout stays
            clean for machine-readable ``--json`` output.

    Returns:
        The :class:`RedactingFilter` installed on every handler, so callers can
        register additional secrets later.
    """
    redactor = RedactingFilter()
    redactor.add_secret(session_token)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    console = logging.StreamHandler(stream if stream is not None else sys.stderr)
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt=_DATE_FORMAT))
    console.addFilter(redactor)
    logger.addHandler(console)

    if log_file is not None:
        if not log_file.parent.is_dir():
            raise FileNotFoundError(
                f"Log directory {log_file.parent} does not exist. Call "
                "RuntimePaths.ensure() first; logging must not create the "
                "runtime tree implicitly."
            )
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, datefmt=_DATE_FORMAT))
        file_handler.addFilter(redactor)
        logger.addHandler(file_handler)

    # Route uvicorn's loggers through our handlers so their records are scrubbed
    # too, and so the application has a single logging configuration.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        third_party = logging.getLogger(name)
        third_party.handlers = []
        third_party.propagate = False
        for handler in logger.handlers:
            third_party.addHandler(handler)
        third_party.setLevel(logger.level)

    logger.propagate = False
    return redactor


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child of the application logger."""
    if not name or name == LOGGER_NAME:
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
