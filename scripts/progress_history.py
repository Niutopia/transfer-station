"""Persist the most recent completed download progress for the dashboard."""

from __future__ import annotations

import json
import os
from pathlib import Path


def archive_completed_progress(source: Path, destination: Path) -> bool:
    """Copy the most recent terminal progress snapshot atomically."""
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if (
        not isinstance(payload, dict)
        or payload.get("stage") not in {"complete", "failed", "cancelled"}
        or int(payload.get("total") or 0) <= 0
    ):
        return False

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except OSError:
        return False
    return True
