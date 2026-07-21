#!/usr/bin/env python3
"""Maintain an append-only view of successful downloads from task logs."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path


DOWNLOAD_LOG_PATTERN = re.compile(r"^download-(\d{8})-(\d{6})\.log$")


def _load_items(path: Path) -> dict[str, dict[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    raw_items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(raw_items, dict):
        return {}
    return {
        str(key): value
        for key, value in raw_items.items()
        if key and isinstance(value, dict) and value.get("date")
    }


def _log_date(path: Path) -> tuple[str, str] | None:
    match = DOWNLOAD_LOG_PATTERN.fullmatch(path.name)
    if not match:
        return None
    try:
        timestamp = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").astimezone()
    except ValueError:
        return None
    return timestamp.date().isoformat(), timestamp.isoformat(timespec="seconds")


def sync_download_history(index_path: Path, log_dir: Path) -> list[dict[str, object]]:
    """Merge first-time successful downloads into a durable history index."""
    items = _load_items(index_path)
    changed = not index_path.exists()
    try:
        log_paths = sorted(log_dir.glob("download-*.log"))
    except OSError:
        log_paths = []

    for log_path in log_paths:
        log_time = _log_date(log_path)
        if not log_time:
            continue
        day, downloaded_at = log_time
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            try:
                result = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(result, dict) or result.get("status") != "downloaded":
                continue
            viewkey = str(result.get("viewkey") or "").strip()
            if not viewkey or viewkey in items:
                continue
            try:
                byte_count = max(0, int(result.get("bytes") or 0))
            except (TypeError, ValueError):
                byte_count = 0
            items[viewkey] = {
                "viewkey": viewkey,
                "date": day,
                "downloadedAt": downloaded_at,
                "bytes": byte_count,
            }
            changed = True

    if changed:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = index_path.with_suffix(index_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "items": items}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, index_path)
    return list(items.values())
