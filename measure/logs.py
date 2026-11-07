"""
Append-only JSONL event log.

One JSON object per line, one line per event, UTC timestamp on every record.
This is the raw evidence behind every later claim about PnL (ROADMAP §7):
quotes, orders, fills, decisions, risk blocks and neutralisation.

    {"ts":"2026-06-12T03:00:00+00:00","event":"quote","condition_id":"0x…",
     "yes_bid":0.48,"no_bid":0.5,"yes_quote":0.47,"no_quote":0.51,…}

Writes are serialised by a lock and appended in a single `open()` per
record, so a crash loses at most the event being written.  Nothing here
ever raises: measurement must not be able to stop trading.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Any, Dict, Iterator, Optional

from engine.clock import now as clock_now

DEFAULT_LOG_PATH = os.path.join("logs", "events.jsonl")


class EventLogger:
    """Append JSONL events to `path`."""

    def __init__(self, path: str = DEFAULT_LOG_PATH, echo: bool = False):
        self.path = path
        self.echo = echo
        self._lock = threading.Lock()

    # ── Core ─────────────────────────────────────────────────────────────

    def log(self, event: str, **fields: Any) -> Dict[str, Any]:
        """Append one event; returns the record that was written."""
        record: Dict[str, Any] = {"ts": clock_now().isoformat(), "event": event}
        record.update(fields)

        if self.echo:
            print(json.dumps(record, default=str))

        try:
            with self._lock:
                directory = os.path.dirname(self.path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(record, default=str, separators=(",", ":"))
                        + "\n"
                    )
        except OSError:
            # Never let logging break trading.
            pass
        return record

    # ── Typed helpers (readability at call sites) ────────────────────────

    def quote(self, **fields: Any) -> Dict[str, Any]:
        """A full quoting decision: book snapshot, inventory, quotes."""
        return self.log("quote", **fields)

    def order(self, **fields: Any) -> Dict[str, Any]:
        """An order submission and its CLOB acknowledgement."""
        return self.log("order", **fields)

    def fill(self, **fields: Any) -> Dict[str, Any]:
        """An execution (paper or live)."""
        return self.log("fill", **fields)

    def decision(self, **fields: Any) -> Dict[str, Any]:
        """A strategy decision, e.g. `edge=… -> take/skip`."""
        return self.log("decision", **fields)

    def risk_block(self, **fields: Any) -> Dict[str, Any]:
        """An order rejected by the risk layer."""
        return self.log("risk_block", **fields)


_LOGGERS: Dict[str, EventLogger] = {}
_LOGGERS_LOCK = threading.Lock()


def get_logger(path: Optional[str] = None) -> EventLogger:
    """Return a shared logger for `path` (defaults to `logs/events.jsonl`)."""
    resolved = path or DEFAULT_LOG_PATH
    with _LOGGERS_LOCK:
        logger = _LOGGERS.get(resolved)
        if logger is None:
            logger = EventLogger(resolved)
            _LOGGERS[resolved] = logger
        return logger


def read_events(path: Optional[str] = None) -> Iterator[Dict[str, Any]]:
    """Yield parsed events from a JSONL log (skipping malformed lines)."""
    resolved = path or DEFAULT_LOG_PATH
    try:
        with open(resolved, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def utc_stamp(moment: Optional[datetime] = None) -> str:
    """ISO-8601 UTC string for a moment (default: now)."""
    return (moment or clock_now()).isoformat()
