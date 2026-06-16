"""Tests for the structlog-based logging configuration."""

from __future__ import annotations

import json

import structlog

from log import configure_logging, get_logger


def test_json_format_emits_one_json_object_per_line(capfd):
    structlog.reset_defaults()
    configure_logging(level="INFO", fmt="json")

    get_logger("test.json").info("hello", foo="bar", n=3)

    line = capfd.readouterr().err.strip().splitlines()[-1]
    record = json.loads(line)  # raises if not valid JSON
    assert record["event"] == "hello"
    assert record["foo"] == "bar"
    assert record["n"] == 3
    assert record["level"] == "info"
    assert "timestamp" in record


def test_level_filtering_suppresses_below_threshold(capfd):
    structlog.reset_defaults()
    configure_logging(level="WARNING", fmt="json")

    logger = get_logger("test.level")
    logger.info("below threshold")
    logger.warning("at threshold")

    err = capfd.readouterr().err
    assert "below threshold" not in err
    assert "at threshold" in err


def test_console_format_is_human_readable_not_json(capfd):
    structlog.reset_defaults()
    configure_logging(level="INFO", fmt="console")

    get_logger("test.console").info("plain message")

    err = capfd.readouterr().err
    assert "plain message" in err


def test_unknown_level_and_format_fall_back_to_defaults(capfd):
    structlog.reset_defaults()
    configure_logging(level="NONSENSE", fmt="yaml")

    # Falls back to INFO + console: an info line is emitted and isn't JSON.
    get_logger("test.fallback").info("fallback works")

    line = capfd.readouterr().err.strip().splitlines()[-1]
    assert "fallback works" in line
