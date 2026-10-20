"""Structured JSON logging (structlog). Console gets readable lines, file gets JSONL."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog


def configure_logging(level: str = "INFO", json_file: str | None = None) -> None:
    lvl = getattr(logging, level.upper(), logging.INFO)
    handlers: list[logging.Handler] = []
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(message)s"))
    handlers.append(console)
    if json_file:
        Path(json_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(json_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(message)s"))
        fh.addFilter(lambda r: getattr(r, "_json", False))
        handlers.append(fh)
        console.addFilter(lambda r: not getattr(r, "_json", False))

    # Third-party HTTP clients log every request at INFO; keep only their warnings and errors.
    def quiet_libs(r: logging.LogRecord) -> bool:
        return r.levelno >= logging.WARNING or not r.name.startswith(("httpx", "httpcore", "openai"))

    for h in handlers:
        h.addFilter(quiet_libs)
    root = logging.getLogger()
    root.handlers = handlers
    root.setLevel(lvl)
    for noisy in ("httpx", "httpcore", "openai", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    json_renderer = structlog.processors.JSONRenderer()
    console_renderer = structlog.dev.ConsoleRenderer(colors=False)
    std_logger = logging.getLogger("safco")

    def emit(_, method_name, event_dict):
        # Write a JSON line to the file handler and a readable line to console.
        level_no = getattr(logging, method_name.upper(), logging.INFO)
        if json_file:
            std_logger.log(level_no, json_renderer(None, method_name, dict(event_dict)), extra={"_json": True})
        std_logger.log(level_no, console_renderer(None, method_name, dict(event_dict)))
        raise structlog.DropEvent

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            emit,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        cache_logger_on_first_use=True,
    )


def get_logger(stage: str):
    return structlog.get_logger().bind(stage=stage)
