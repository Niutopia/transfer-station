#!/usr/bin/env python3
"""Repair the current snapshot's retryable items without crawling listing pages."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from progress_history import archive_completed_progress
from task_lock import TaskLock, update_lock


PROJECT = Path(__file__).resolve().parents[1]
CRAWLER = PROJECT / "public-video-crawler"
DATA = PROJECT / "data"
STAGING = PROJECT / "中转站"
SNAPSHOT = CRAWLER / "videos-with-media.json"
REPAIR_INPUT = DATA / "repair-pending.json"
REPAIR_MANIFEST = DATA / "repair-download-manifest.json"
RUN_HISTORY = DATA / "run-history.jsonl"
REFRESH_MONITOR = PROJECT / "scripts" / "refresh-monitor.py"
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".webm", ".ts", ".mkv", ".mov", ".avi"}

sys.path.append(str(CRAWLER))
import crawler  # noqa: E402


def load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def success_keys(path: Path) -> set[str]:
    try:
        return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
    except (OSError, UnicodeError):
        return set()


def existing_keys(directory: Path) -> set[str]:
    if not directory.exists():
        return set()
    return {
        path.stem
        for path in directory.rglob("*")
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS and path.stat().st_size > 0
    }


def is_mismatch_failure(item: object) -> bool:
    return bool(
        isinstance(item, dict)
        and (
            item.get("kind") == "media_mismatch"
            or "详情页媒体与榜单不一致" in str(item.get("error") or "")
        )
    )


def collect_repair_candidates(
    snapshot: object,
    completed_keys: set[str],
) -> tuple[list[dict[str, object]], set[str]]:
    if not isinstance(snapshot, dict):
        return [], set()
    metadata = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else {}
    failures = metadata.get("resolve_failures") if isinstance(metadata, dict) else []
    blocked_keys = {
        str(item.get("viewkey"))
        for item in failures if is_mismatch_failure(item) and item.get("viewkey")
    } if isinstance(failures, list) else set()
    videos = snapshot.get("videos") if isinstance(snapshot.get("videos"), list) else []
    candidates: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in videos:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("viewkey") or "")
        if not key or key in seen or key in completed_keys or key in blocked_keys:
            continue
        seen.add(key)
        candidates.append(dict(raw))
    return candidates, blocked_keys


def as_video(raw: dict[str, object]) -> crawler.Video:
    return crawler.Video(
        viewkey=str(raw.get("viewkey") or ""),
        canonical_url=str(raw.get("canonical_url") or ""),
        title=str(raw.get("title") or ""),
        thumbnail_url=str(raw.get("thumbnail_url") or ""),
        duration=str(raw.get("duration") or ""),
        source_pages=[int(page) for page in raw.get("source_pages", []) if isinstance(page, int)],
    )


def update_snapshot(
    snapshot: dict[str, object],
    attempted: list[crawler.Video],
    failures: list[dict[str, str]],
) -> None:
    attempted_by_key = {video.viewkey: video for video in attempted}
    videos = snapshot.get("videos") if isinstance(snapshot.get("videos"), list) else []
    for raw in videos:
        if not isinstance(raw, dict):
            continue
        video = attempted_by_key.get(str(raw.get("viewkey") or ""))
        if video is None:
            continue
        raw["media_url"] = video.media_url
        raw["resolved_at"] = video.resolved_at

    metadata = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else {}
    previous_failures = metadata.get("resolve_failures") if isinstance(metadata.get("resolve_failures"), list) else []
    attempted_keys = set(attempted_by_key)
    metadata["resolve_failures"] = [
        item for item in previous_failures
        if isinstance(item, dict) and str(item.get("viewkey") or "") not in attempted_keys
    ] + failures
    snapshot["metadata"] = metadata
    atomic_json(SNAPSHOT, snapshot)


def run_logged(command: list[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", buffering=1) as log_file:
        os.chmod(log_path, 0o600)
        process = subprocess.Popen(command, cwd=CRAWLER, text=True, stdout=log_file, stderr=subprocess.STDOUT)
        return process.wait()


def write_event(event: dict[str, object]) -> None:
    RUN_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with RUN_HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def run_repair(lock_path: Path) -> int:
    started_at = datetime.now().astimezone()
    stamp = started_at.strftime("%Y%m%d-%H%M%S")
    snapshot = load_json(SNAPSHOT, {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    completed = success_keys(DATA / "download-success.txt") | existing_keys(STAGING)
    candidate_dicts, _ = collect_repair_candidates(snapshot, completed)
    archive_completed_progress(DATA / "download-progress.json", DATA / "last-completed-progress.json")

    attempted = [as_video(raw) for raw in candidate_dicts]
    update_lock(lock_path, phase="resolving")
    failures: list[dict[str, str]] = []
    if attempted:
        failures = crawler.resolve_media(
            crawler.build_opener(),
            attempted,
            timeout=30.0,
            delay=0.5,
            user_agent=crawler.DEFAULT_USER_AGENT,
            concurrency=4,
            retries=4,
            rate_limiter=crawler.RateLimiter(0.5),
            continue_on_error=True,
        )
    (DATA / "crawl-progress.json").unlink(missing_ok=True)
    update_snapshot(snapshot, attempted, failures)

    resolved = [video for video in attempted if video.media_url]
    crawler.write_json(REPAIR_INPUT, resolved, {
        "output_scope": "repair_only",
        "attempted": len(attempted),
        "resolved": len(resolved),
        "resolve_failures": failures,
    })

    update_lock(lock_path, phase="downloading" if resolved else "finalizing")
    download_code = 0
    if resolved:
        download_code = run_logged([
            sys.executable, "download.py", str(REPAIR_INPUT),
            "--output-dir", str(STAGING),
            "--manifest", str(REPAIR_MANIFEST),
            "--work-dir", str(DATA / "partials"),
            "--success-history", str(DATA / "download-success.txt"),
            "--content-history", str(DATA / "download-content-history.json"),
            "--media-cache", str(DATA / "video-history.json"),
            "--progress", str(DATA / "download-progress.json"),
            "--concurrency", "4", "--retries", "5", "--link-max-age", "180", "--delay", "0.5",
        ], DATA / "logs" / f"repair-download-{stamp}.log")
    else:
        atomic_json(REPAIR_MANIFEST, [])

    manifest = load_json(REPAIR_MANIFEST, [])
    rows = manifest if isinstance(manifest, list) else []
    mismatch_count = sum(1 for item in failures if is_mismatch_failure(item))
    unresolved_count = len(failures) - mismatch_count
    failed_downloads = sum(1 for row in rows if isinstance(row, dict) and row.get("status") == "failed")
    downloaded = sum(1 for row in rows if isinstance(row, dict) and row.get("status") in {"downloaded", "skipped"})
    duplicates = sum(1 for row in rows if isinstance(row, dict) and row.get("status") == "duplicate")
    downloaded_bytes = sum(int(row.get("bytes") or 0) for row in rows if isinstance(row, dict) and row.get("status") == "downloaded")
    finished_at = datetime.now().astimezone()
    result_ok = download_code == 0 and unresolved_count == 0 and failed_downloads == 0
    write_event({
        "timestamp": finished_at.isoformat(timespec="seconds"),
        "startedAt": started_at.isoformat(timespec="seconds"),
        "durationSeconds": round((finished_at - started_at).total_seconds(), 1),
        "taskType": "repair",
        "resultStatus": "success" if result_ok else "failed",
        "crawlExitCode": 0 if result_ok else 1,
        "downloadExitCode": download_code,
        "downloadRequested": False,
        "rawLinks": len(attempted),
        "uniqueVideos": len(attempted),
        "skippedVideos": 0,
        "newVideos": 0,
        "retryVideos": len(attempted),
        "downloadedVideos": downloaded,
        "duplicateVideos": duplicates,
        "blockedVideos": mismatch_count,
        "failedVideos": unresolved_count + failed_downloads,
        "downloadedBytes": downloaded_bytes,
    })
    return 0 if result_ok else 1


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    lock_path = DATA / ".crawling.lock"
    task_lock = TaskLock(lock_path, "repair-pending")
    if not task_lock.acquire():
        print("another task is already running", file=sys.stderr)
        return 75
    try:
        return run_repair(lock_path)
    finally:
        task_lock.release()
        subprocess.run([sys.executable, str(REFRESH_MONITOR)], cwd=PROJECT, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
