"""Unit tests for telemetry: redaction, JSON logging, traceparent."""

from __future__ import annotations

import logging

from circus_tent.telemetry import SecretRedactor, setup_logging, traceparent_from


def test_redacts_sk_keys() -> None:
    r = SecretRedactor()
    out = r.redact("key is sk-abcdefghijklmnopqrstuvwxyz1234567890 in text")
    assert "sk-abcdefghijklmnopqrstuvwxyz1234567890" not in out
    assert "REDACTED" in out


def test_redacts_bearer_values() -> None:
    r = SecretRedactor()
    out = r.redact("Authorization: Bearer abc123secretTokenValue")
    assert "abc123secretTokenValue" not in out


def test_redact_empty_and_plain() -> None:
    r = SecretRedactor()
    assert r.redact("") == ""
    assert r.redact("nothing sensitive here") == "nothing sensitive here"


def test_setup_logging_json_and_idempotent() -> None:
    from circus_tent import telemetry as tel

    setup_logging("INFO")
    setup_logging("INFO")  # no duplicate handlers
    root = logging.getLogger()
    handlers = [h for h in root.handlers if isinstance(h, tel._RedactingStreamHandler)]
    assert len(handlers) == 1
    logger = logging.getLogger("test.telemetry")
    logger.info("hello", extra={"event": "unit_test"})
    for h in handlers:
        h.flush()
        h.close()
    root.removeHandler(handlers[0])


def test_traceparent_parsing() -> None:
    tp = traceparent_from(
        {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}
    )
    assert tp == "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    assert traceparent_from({}) is None
    assert traceparent_from({"traceparent": "garbage"}) is None
