#!/usr/bin/env python3
"""Rescue videos that failed due to expired CDN links by re-resolving media URLs."""

import sys
import json
import subprocess
from pathlib import Path

from task_lock import TaskLock

PROJECT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT / "public-video-crawler"))
import crawler

DATA = PROJECT / "data"

def main():
    task_lock = TaskLock(DATA / ".crawling.lock", "rescue-expired")
    if not task_lock.acquire():
        print("Another crawl or download task is already running.", file=sys.stderr)
        return 75
    try:
        return run_rescue()
    finally:
        task_lock.release()


def run_rescue():
    manifest_path = DATA / "download-manifest.json"
    if not manifest_path.exists():
        print("No manifest found.")
        return 0

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failed_keys = {item.get("viewkey") for item in manifest if item.get("status") == "failed" and item.get("viewkey")}

    if not failed_keys:
        print("No failed videos to rescue!")
        return 0

    videos_with_media = json.loads((PROJECT / "public-video-crawler" / "videos-with-media.json").read_text(encoding="utf-8"))
    failed_videos_dicts = [v for v in videos_with_media.get("videos", []) if v.get("viewkey") in failed_keys]

    processing = [
        crawler.Video(
            viewkey=v["viewkey"],
            canonical_url=v["canonical_url"],
            title=v["title"],
            thumbnail_url=v["thumbnail_url"],
            duration=v["duration"],
            source_pages=v.get("source_pages", []),
            media_url=v.get("media_url", "")
        ) for v in failed_videos_dicts
    ]

    print(f"Found {len(processing)} failed videos. Re-resolving media URLs...")
    opener = crawler.build_opener()
    resolve_failures = crawler.resolve_media(
        opener,
        processing,
        timeout=30.0,
        delay=0.5,
        user_agent=crawler.DEFAULT_USER_AGENT,
        concurrency=4,
        retries=4,
        rate_limiter=crawler.RateLimiter(0.5),
        continue_on_error=True,
    )
    if resolve_failures:
        print(f"Could not refresh {len(resolve_failures)} videos; continuing with the resolved items.", file=sys.stderr)

    rescue_json = DATA / "rescue-pending.json"
    crawler.write_json(rescue_json, processing, {"rescue": True})
    print(f"Saved fresh media URLs to {rescue_json.name}.")

    print("Starting download for rescued videos...")
    download_command = [
        sys.executable, str(PROJECT / "public-video-crawler" / "download.py"),
        str(rescue_json),
        "--output-dir", str(PROJECT / "中转站"),
        "--manifest", str(DATA / "rescue-download-manifest.json"),
        "--work-dir", str(DATA / "partials"),
        "--success-history", str(DATA / "download-success.txt"),
        "--media-cache", str(DATA / "video-history.json"),
        "--progress", str(DATA / "download-progress.json"),
        "--concurrency", "4",
        "--retries", "5",
        "--link-max-age", "180",
        "--delay", "0.5"
    ]

    result = subprocess.run(download_command, check=False)
    if result.returncode != 0:
        print(f"Download failed (exit {result.returncode}):", file=sys.stderr)
        return 1

    # Refresh monitor
    subprocess.run([sys.executable, str(PROJECT / "scripts" / "refresh-monitor.py")], check=False)
    print("Rescue complete!")
    return 0

if __name__ == "__main__":
    sys.exit(main())
