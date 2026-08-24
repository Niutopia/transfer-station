#!/usr/bin/env python3
"""Helpers for deciding whether the local daily task already succeeded today."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path


def latest_run_event(path: Path) -> dict[str, object] | None:
    """Return the latest valid task event, regardless of outcome or day."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(event, dict) and event.get("timestamp"):
            return event
    return None


def event_task_type(event: object) -> str:
    """Normalize old history rows that predate the explicit taskType field."""
    if not isinstance(event, dict):
        return "crawl"
    explicit = str(event.get("taskType") or "")
    if explicit in {"crawl", "repair"}:
        return explicit
    return "crawl"


def event_result_status(event: object) -> str:
    """Normalize task outcomes, including successful but incomplete crawls."""
    if not isinstance(event, dict):
        return "none"
    explicit = str(event.get("resultStatus") or "")
    if explicit in {"success", "attention", "failed"}:
        return explicit
    if event.get("crawlExitCode") == 0 and event.get("downloadExitCode") in {0, None}:
        return "attention" if int(event.get("listingFailures") or 0) else "success"
    return "failed"


def event_counter(event: object, key: str, fallback: int = 0) -> int:
    """Read an event counter without treating an explicit zero as missing."""
    if not isinstance(event, dict) or key not in event:
        return fallback
    try:
        return int(event.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def latest_task_event(
    path: Path,
    task_type: str,
    *,
    statuses: frozenset[str] | set[str] | None = None,
) -> dict[str, object] | None:
    """Return the newest valid history row for one task type.

    ``statuses`` narrows the search to runs that ended in one of those result
    statuses, which is what callers asking "when did this last actually produce
    data" need.
    """
    if task_type not in {"crawl", "repair"}:
        raise ValueError("task_type must be crawl or repair")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            isinstance(event, dict)
            and event.get("timestamp")
            and event_task_type(event) == task_type
            and (statuses is None or event_result_status(event) in statuses)
        ):
            return event
    return None


def latest_manual_crawl_event(path: Path) -> dict[str, object] | None:
    """Return the latest dashboard crawl that conclusively updates Cookie state."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            isinstance(event, dict)
            and event.get("timestamp")
            and event_task_type(event) == "crawl"
            and event.get("trigger") == "manual"
            and (
                event.get("crawlExitCode") == 0
                or (
                    event.get("authFailure") is True
                    and isinstance(event.get("authDetectorVersion"), int)
                    and not isinstance(event.get("authDetectorVersion"), bool)
                    and event["authDetectorVersion"] >= 2
                )
            )
        ):
            return event
    return None


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
            and event_result_status(event) == "success"
        ):
            return event
    return None
