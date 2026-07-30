#!/usr/bin/env python3
"""Finish one validated partial download with parallel HTTP Range requests."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import sys
import threading
import time
import urllib.request
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
DATA = PROJECT / "data"
STAGING = PROJECT / "中转站"
CRAWLER = PROJECT / "public-video-crawler"
sys.path.insert(0, str(CRAWLER))

import crawler  # noqa: E402
import download  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("viewkey")
    parser.add_argument("--segments", type=int, default=12)
    args = parser.parse_args()
    if not 2 <= args.segments <= 16:
        parser.error("--segments must be between 2 and 16")
    return args


def main() -> int:
    args = parse_args()
    payload = json.loads((DATA / "repair-pending.json").read_text(encoding="utf-8"))
    item = next((row for row in payload.get("videos", []) if row.get("viewkey") == args.viewkey), None)
    if not isinstance(item, dict):
        raise download.DownloadError("viewkey is missing from the repair queue")

    partial = DATA / "partials" / f"{args.viewkey}.mp4.part"
    final = STAGING / f"{args.viewkey}.mp4"
    if final.exists() and final.stat().st_size > 0:
        print(json.dumps({"status": "skipped", "viewkey": args.viewkey, "bytes": final.stat().st_size}))
        return 0
    video = crawler.Video(
        viewkey=str(item.get("viewkey") or ""),
        canonical_url=str(item.get("canonical_url") or ""),
        title=str(item.get("title") or ""),
        thumbnail_url=str(item.get("thumbnail_url") or ""),
        duration=str(item.get("duration") or ""),
    )
    failures = crawler.resolve_media(
        crawler.build_opener(),
        [video],
        timeout=30,
        delay=0.5,
        user_agent=crawler.DEFAULT_USER_AGENT,
        concurrency=1,
        retries=4,
        rate_limiter=crawler.RateLimiter(0.5),
        continue_on_error=True,
    )
    if failures or not video.media_url:
        raise download.DownloadError("could not refresh a matching media URL")
    item["media_url"] = video.media_url
    item["resolved_at"] = video.resolved_at
    download.ensure_public_endpoint(str(item["media_url"]))

    meta = download._load_meta(partial)
    total = int(meta.get("totalBytes") or 0)
    prefix = partial.stat().st_size if partial.exists() else 0
    if not total:
        headers = {
            "User-Agent": download.USER_AGENT,
            "Accept": "video/*,application/octet-stream",
            "Range": "bytes=0-0",
            "Referer": str(item.get("canonical_url") or ""),
        }
        request = urllib.request.Request(str(item["media_url"]), headers=headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            content_range = download._parse_content_range(response.headers.get("Content-Range"))
            if response.status != 206 or not content_range or content_range[0:2] != (0, 0):
                raise download.DownloadError("server does not support validated Range downloads")
            total = content_range[2]
            meta = {
                "viewkey": args.viewkey,
                "canonicalUrl": item.get("canonical_url", ""),
                "mediaUrlHash": "",
                "etag": response.headers.get("ETag") or "",
                "lastModified": response.headers.get("Last-Modified") or "",
                "totalBytes": total,
                "updatedAt": download._iso_now(),
            }
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.touch(exist_ok=True)
        download._write_meta(partial, meta)
        prefix = partial.stat().st_size
    if prefix > total:
        raise download.DownloadError("partial is larger than the remote media")
    if prefix == total:
        history = download.ContentHistory(DATA / "download-content-history.json")
        result = download._finalize_verified_partial(partial, final, item, time.monotonic(), True, history)
        crawler.append_success_keys(DATA / "download-success.txt", [args.viewkey])
        print(json.dumps({"status": result["status"], "viewkey": args.viewkey, "bytes": result.get("bytes"), "sha256Verified": bool(result.get("sha256"))}))
        return 0

    remaining = total - prefix
    width = (remaining + args.segments - 1) // args.segments
    ranges = []
    for index in range(args.segments):
        start = prefix + index * width
        end = min(total - 1, start + width - 1)
        if start <= end:
            ranges.append((index, start, end))
    print(json.dumps({"mode": "segmented", "prefixBytes": prefix, "remainingBytes": remaining, "segments": len(ranges), "totalBytes": total}))

    progress_lock = threading.Lock()
    completed = 0
    began = time.monotonic()

    def fetch(spec: tuple[int, int, int]) -> tuple[int, int, int, Path]:
        nonlocal completed
        index, start, end = spec
        path = partial.parent / f"{args.viewkey}.seg-{start}-{end}"
        expected = end - start + 1
        existing = path.stat().st_size if path.exists() else 0
        if existing > expected:
            path.unlink()
            existing = 0
        for attempt in range(6):
            current = start + existing
            if current > end:
                break
            headers = {
                "User-Agent": download.USER_AGENT,
                "Accept": "video/*,application/octet-stream",
                "Range": f"bytes={current}-{end}",
                "Referer": str(item.get("canonical_url") or ""),
            }
            validator = str(meta.get("etag") or meta.get("lastModified") or "")
            if validator:
                headers["If-Range"] = validator
            try:
                request = urllib.request.Request(str(item["media_url"]), headers=headers)
                with urllib.request.urlopen(request, timeout=60) as response:
                    content_range = download._parse_content_range(response.headers.get("Content-Range"))
                    if response.status != 206 or not content_range:
                        raise download.DownloadError("server did not honor the Range request")
                    if content_range != (current, end, total):
                        raise download.DownloadError("server returned an inconsistent Range")
                    if response.headers.get_content_type() in {"text/html", "application/json"}:
                        raise download.DownloadError("segment response was not media")
                    with path.open("ab") as handle:
                        while True:
                            chunk = response.read(download.CHUNK_SIZE)
                            if not chunk:
                                break
                            handle.write(chunk)
                            existing += len(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
                if existing != expected:
                    raise download.TransientDownloadError(f"incomplete segment: {existing}/{expected}")
                break
            except Exception:
                if attempt >= 5:
                    raise
                time.sleep(min(10, 2 ** attempt))
        with progress_lock:
            completed += expected
            elapsed = max(0.001, time.monotonic() - began)
            print(json.dumps({
                "segment": index + 1,
                "of": len(ranges),
                "completedBytes": completed,
                "remainingBytes": remaining - completed,
                "speedBytesS": round(completed / elapsed),
            }), flush=True)
        return index, start, end, path

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ranges)) as pool:
        parts = sorted(pool.map(fetch, ranges))

    if partial.stat().st_size != prefix:
        raise download.DownloadError("partial changed during segmented download")
    assembled = partial.with_suffix(partial.suffix + ".assembling")
    assembled.unlink(missing_ok=True)
    with assembled.open("wb") as target, partial.open("rb") as source:
        shutil.copyfileobj(source, target, download.CHUNK_SIZE)
        for _index, _start, _end, path in parts:
            with path.open("rb") as segment:
                shutil.copyfileobj(segment, target, download.CHUNK_SIZE)
        target.flush()
        os.fsync(target.fileno())
    if assembled.stat().st_size != total:
        raise download.DownloadError("assembled file size is inconsistent")
    os.replace(assembled, partial)
    for _index, _start, _end, path in parts:
        path.unlink(missing_ok=True)

    history = download.ContentHistory(DATA / "download-content-history.json")
    result = download._finalize_verified_partial(partial, final, item, began, True, history)
    crawler.append_success_keys(DATA / "download-success.txt", [args.viewkey])
    print(json.dumps({
        "status": result["status"],
        "viewkey": args.viewkey,
        "bytes": result.get("bytes"),
        "sha256Verified": bool(result.get("sha256")),
    }), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
