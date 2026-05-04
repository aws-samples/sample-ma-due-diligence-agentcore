"""Unit tests for ``mna.logging_config`` JSON formatter."""

from __future__ import annotations

import json
import logging

import pytest

from mna.logging_config import JsonFormatter, configure_logging, get_logger


@pytest.fixture(autouse=True)
def _reset_mna_logger():
    logger = logging.getLogger("mna")
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate
    logger.handlers.clear()
    yield
    logger.handlers.clear()
    for handler in original_handlers:
        logger.addHandler(handler)
    logger.setLevel(original_level)
    logger.propagate = original_propagate


def _make_record(**overrides) -> logging.LogRecord:
    kwargs = {
        "name": "mna.test",
        "level": logging.INFO,
        "pathname": "test.py",
        "lineno": 10,
        "msg": "hello %s",
        "args": ("world",),
        "exc_info": None,
    }
    kwargs.update(overrides)
    return logging.LogRecord(**kwargs)


class TestJsonFormatter:
    def test_emits_timestamp_level_module_message(self) -> None:
        formatter = JsonFormatter()
        record = _make_record()

        payload = json.loads(formatter.format(record))

        assert payload["level"] == "INFO"
        assert payload["message"] == "hello world"
        assert payload["logger"] == "mna.test"
        assert "timestamp" in payload
        assert "module" in payload

    def test_includes_extra_fields(self) -> None:
        formatter = JsonFormatter()
        record = _make_record()
        record.agent = "supervisor"
        record.session_id = "abc123"

        payload = json.loads(formatter.format(record))

        assert payload["agent"] == "supervisor"
        assert payload["session_id"] == "abc123"

    def test_non_serializable_extra_falls_back_to_repr(self) -> None:
        formatter = JsonFormatter()
        record = _make_record()
        record.widget = object()  # not JSON-serializable

        payload = json.loads(formatter.format(record))

        assert "widget" in payload
        assert payload["widget"].startswith("<object")


class TestConfigureLogging:
    def test_installs_json_handler_once(self) -> None:
        first = configure_logging()
        second = configure_logging()

        assert first is second
        json_handlers = [
            h
            for h in first.handlers
            if isinstance(h, logging.StreamHandler) and isinstance(h.formatter, JsonFormatter)
        ]
        assert len(json_handlers) == 1

    def test_get_logger_returns_namespaced_child(self) -> None:
        logger = get_logger("config")
        assert logger.name == "mna.config"

    def test_get_logger_accepts_mna_prefixed_name(self) -> None:
        logger = get_logger("mna.tools.kb_retrieve")
        assert logger.name == "mna.tools.kb_retrieve"
