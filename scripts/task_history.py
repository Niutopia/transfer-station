#!/usr/bin/env python3
"""Helpers for deciding whether the local daily task already succeeded today."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path


def latest_successful_daily_run(path: Path, *, today: date | None = None) -> dict[str, object] | None:
    target_day = today or datetime.now().astimezone().date()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in reversed(lines):
        try:
            event = json.loads(line)
            timestamp = datetime.fromisoformat(str(event.get("timestamp") or ""))
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if timestamp.astimezone().date() != target_day:
            continue
        if (
            event.get("downloadRequested") is True
            and event.get("crawlExitCode") == 0
            and event.get("downloadExitCode") == 0
        ):
            return event
    return None
