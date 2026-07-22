#!/usr/bin/env python3
"""Crawl public 91porn listing pages and deduplicate video entries.

This tool deliberately uses no authenticated cookies and does not attempt to bypass
VIP, paid, private, or administrator authorization checks.
"""

from __future__ import annotations

import argparse
import csv
import html
import http.cookiejar
import ipaddress
import json
import os
import random
import re
import socket
import sys
import time
import threading
import concurrent.futures
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


DEFAULT_URL = "https://91porn.com/v.php?category=hot&viewtype=basic"
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; PublicVideoIndexer/1.0)"
MAX_HTML_BYTES = 8 * 1024 * 1024
MEDIA_EXTENSIONS = {".mp4", ".m4v", ".webm", ".m3u8"}
VIDEO_FILE_EXTENSIONS = {".mp4", ".m4v", ".webm", ".ts", ".mkv", ".mov", ".avi"}
SIGNATURE_KEYS = {"st", "f", "e", "sig", "signature", "token"}


class CrawlerError(RuntimeError):
    pass


class MediaMismatchError(CrawlerError):
    """The detail page served media that does not belong to the listing item."""

    pass


@dataclass
class Candidate:
    viewkey: str
    href: str
    tracking_class: str = ""
    title: str = ""
    thumbnail_url: str = ""
    duration: str = ""

    def score(self) -> int:
        # The visible listing cards currently use c=llzvq. The page also emits a
        # second, duplicated card set. Prefer the visible set but keep a fallback.
        return (
            (8 if self.tracking_class == "llzvq" else 0)
            + (2 if self.title else 0)
            + (1 if self.thumbnail_url else 0)
            + (1 if self.duration else 0)
        )


@dataclass
class Video:
    viewkey: str
    canonical_url: str
    title: str = ""
    thumbnail_url: str = ""
    duration: str = ""
    source_pages: list[int] = field(default_factory=list)
    media_url: str = ""
    resolved_at: str = ""


class ListingParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.candidates: list[Candidate] = []
        self.current: Candidate | None = None
        self.capture: str | None = None
        self.capture_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "a":
            href = html.unescape(values.get("href", ""))
            try:
                absolute = urllib.parse.urljoin(self.base_url, href)
                parsed = urllib.parse.urlsplit(absolute)
                query = urllib.parse.parse_qs(parsed.query)
                viewkey = (query.get("viewkey") or [""])[0]
            except ValueError:
                return
            if parsed.path.endswith("/view_video.php") and valid_viewkey(viewkey):
                self.current = Candidate(
                    viewkey=viewkey,
                    href=absolute,
                    tracking_class=(query.get("c") or [""])[0],
                )
                self.capture = None
                self.capture_depth = 0
                return

        if self.current is None:
            return
        if tag.lower() == "img" and not self.current.thumbnail_url:
            src = html.unescape(values.get("src", ""))
            self.current.thumbnail_url = urllib.parse.urljoin(self.base_url, src)
        if tag.lower() == "span":
            classes = set(values.get("class", "").split())
            if "video-title" in classes:
                self.capture = "title"
                self.capture_depth = 1
            elif "duration" in classes:
                self.capture = "duration"
                self.capture_depth = 1
            elif self.capture:
                self.capture_depth += 1
        elif self.capture:
            self.capture_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return
        if tag.lower() == "a":
            self.current.title = normalize_text(self.current.title)
            self.current.duration = normalize_text(self.current.duration)
            self.candidates.append(self.current)
            self.current = None
            self.capture = None
            self.capture_depth = 0
            return
        if self.capture:
            self.capture_depth -= 1
            if self.capture_depth <= 0:
                self.capture = None
                self.capture_depth = 0

    def handle_data(self, data: str) -> None:
        if self.current is None or self.capture is None:
            return
        if self.capture == "title":
            self.current.title += data
        elif self.capture == "duration":
            self.current.duration += data


class MediaSourceParser(HTMLParser):
    """Collect only media attached to the page's actual video/source elements."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []
        self.posters: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() not in {"source", "video"}:
            return
        values = {key.lower(): value or "" for key, value in attrs}
        src = html.unescape(values.get("src", "")).strip()
        if src and src not in self.urls:
            self.urls.append(src)
        if tag.lower() == "video":
            poster = html.unescape(values.get("poster", "")).strip()
            if poster and poster not in self.posters:
                self.posters.append(poster)


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def valid_viewkey(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{6,64}", value or ""))


def validate_listing_url(raw_url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(raw_url)
    except ValueError as exc:
        raise CrawlerError("列表 URL 无效") from exc
    if parsed.scheme != "https" or parsed.hostname not in {"91porn.com", "www.91porn.com"}:
        raise CrawlerError("只允许抓取 https://91porn.com 的公开列表页")
    if parsed.username is not None or parsed.password is not None:
        raise CrawlerError("URL 不得包含用户名或密码")
    if parsed.path not in {"/v.php", "/index.php"}:
        raise CrawlerError("只允许 v.php 或 index.php 列表页")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def page_url(base_url: str, page: int) -> str:
    parsed = urllib.parse.urlsplit(validate_listing_url(base_url))
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key.lower() != "page"]
    query.append(("page", str(page)))
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), "")
    )


def canonical_video_url(base_url: str, viewkey: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "/view_video.php", urllib.parse.urlencode({"viewkey": viewkey}), "")
    )


def parse_listing(body: str, base_url: str, page: int) -> list[Video]:
    parser = ListingParser(base_url)
    parser.feed(body)
    best: dict[str, Candidate] = {}
    for candidate in parser.candidates:
        previous = best.get(candidate.viewkey)
        if previous is None or candidate.score() > previous.score():
            best[candidate.viewkey] = candidate
    return [
        Video(
            viewkey=item.viewkey,
            canonical_url=canonical_video_url(base_url, item.viewkey),
            title=item.title,
            thumbnail_url=item.thumbnail_url,
            duration=item.duration,
            source_pages=[page],
        )
        for item in best.values()
    ]


def merge_videos(pages: Iterable[list[Video]]) -> list[Video]:
    merged: dict[str, Video] = {}
    for videos in pages:
        for video in videos:
            current = merged.get(video.viewkey)
            if current is None:
                merged[video.viewkey] = video
                continue
            current.source_pages = sorted(set(current.source_pages + video.source_pages))
            for attr in ("title", "thumbnail_url", "duration", "media_url", "resolved_at"):
                if not getattr(current, attr) and getattr(video, attr):
                    setattr(current, attr, getattr(video, attr))
    return list(merged.values())


def load_video_cache(path: Path | None) -> dict[str, Video]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CrawlerError(f"无法读取或解析缓存文件 {path}: {exc}") from exc
    rows = payload.get("videos") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    cache: dict[str, Video] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        viewkey = str(row.get("viewkey") or "")
        if not valid_viewkey(viewkey):
            continue
        source_pages = row.get("source_pages")
        cache[viewkey] = Video(
            viewkey=viewkey,
            canonical_url=str(row.get("canonical_url") or ""),
            title=str(row.get("title") or ""),
            thumbnail_url=str(row.get("thumbnail_url") or ""),
            duration=str(row.get("duration") or ""),
            source_pages=[int(page) for page in source_pages if isinstance(page, int)] if isinstance(source_pages, list) else [],
            media_url=str(row.get("media_url") or ""),
            resolved_at=str(row.get("resolved_at") or ""),
        )
    return cache


def existing_viewkeys(directory: Path | None) -> set[str] | None:
    if directory is None:
        return None
    if not directory.exists():
        return set()
    return {
        path.stem
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in VIDEO_FILE_EXTENSIONS and valid_viewkey(path.stem)
    }


def load_success_keys(path: Path | None) -> set[str]:
    if not path or not path.exists():
        return set()
    try:
        return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
    except (OSError, UnicodeError):
        return set()


def select_for_processing(
    videos: list[Video],
    history: dict[str, Video],
    existing_keys: set[str] | None,
    success_keys: set[str] | None = None,
) -> tuple[list[Video], int, int, int]:
    processing: list[Video] = []
    new_count = 0
    retry_count = 0
    skipped_count = 0
    for video in videos:
        file_exists = existing_keys is not None and video.viewkey in existing_keys
        previous = history.get(video.viewkey)
        if previous is not None:
            for attr in ("title", "thumbnail_url", "duration", "media_url", "resolved_at"):
                if not getattr(video, attr) and getattr(previous, attr):
                    setattr(video, attr, getattr(previous, attr))

        # The staging directory is a transfer area. Once a video has completed,
        # moving or deleting it is intentional and must not create a retry.
        if success_keys is not None and video.viewkey in success_keys:
            skipped_count += 1
            continue

        if not video.media_url:
            processing.append(video)
            if previous is None:
                new_count += 1
            else:
                retry_count += 1
        elif previous is None:
            if existing_keys is not None and video.viewkey in existing_keys:
                skipped_count += 1
                continue
            processing.append(video)
            new_count += 1
        elif existing_keys is not None and video.viewkey not in existing_keys:
            processing.append(video)
            retry_count += 1
        else:
            skipped_count += 1
    return processing, new_count, retry_count, skipped_count


def build_opener() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


class RateLimiter:
    """Thread-safe minimum interval between request starts."""

    def __init__(self, interval: float) -> None:
        self.interval = max(0.0, interval)
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self.interval
        if delay:
            time.sleep(delay)


def fetch_html(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    timeout: float,
    user_agent: str,
    retries: int = 4,
    rate_limiter: RateLimiter | None = None,
) -> str:
    path = urllib.parse.urlsplit(url).path
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"},
        )
        try:
            if rate_limiter:
                rate_limiter.wait()
            with opener.open(request, timeout=timeout) as response:
                final = urllib.parse.urlsplit(response.geturl())
                if final.scheme != "https" or final.hostname not in {"91porn.com", "www.91porn.com"}:
                    raise CrawlerError("列表/详情请求被重定向到非目标站点")
                content_type = response.headers.get_content_type()
                if content_type not in {"text/html", "application/xhtml+xml"}:
                    raise CrawlerError(f"响应不是 HTML: {content_type}")
                raw = response.read(MAX_HTML_BYTES + 1)
                if len(raw) > MAX_HTML_BYTES:
                    raise CrawlerError("HTML 响应超过 8 MiB 限制")
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            response_headers = exc.headers
            exc.close()
            retryable = status_code in {408, 425, 429, 500, 502, 503, 504}
            if not retryable or attempt >= retries:
                raise CrawlerError(f"HTTP {status_code}: {path}") from exc
            retry_after = response_headers.get("Retry-After") if response_headers else None
            try:
                wait_seconds = max(0.0, min(30.0, float(retry_after))) if retry_after else 0.0
            except ValueError:
                wait_seconds = 0.0
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            if attempt >= retries:
                reason = getattr(exc, "reason", exc)
                raise CrawlerError(f"请求失败（已重试 {retries} 次）: {reason}") from exc
            wait_seconds = 0.0
        wait_seconds = wait_seconds or min(20.0, (2 ** attempt) + random.uniform(0.0, 0.5))
        print(f"request retry {attempt + 1}/{retries} in {wait_seconds:.1f}s: {path}", file=sys.stderr)
        time.sleep(wait_seconds)
    raise CrawlerError(f"请求失败: {path}")


def extract_player_media(body: str) -> tuple[list[str], list[str]]:
    decoded_fragments = []
    for encoded in re.findall(r"strencode2?\(\s*[\"']([^\"']+)[\"']", body, re.IGNORECASE):
        try:
            decoded_fragments.append(urllib.parse.unquote(encoded))
        except (UnicodeError, ValueError):
            continue

    def accepted(raw: str) -> str:
        raw = html.unescape(raw).rstrip(");,]")
        try:
            parsed = urllib.parse.urlsplit(raw)
        except ValueError:
            return ""
        extension = Path(parsed.path).suffix.lower()
        if parsed.scheme != "https" or extension not in MEDIA_EXTENSIONS:
            return ""
        if not public_hostname(parsed.hostname or ""):
            return ""
        return raw

    # HTMLParser ignores commented-out tags and script contents. Feeding decoded
    # strencode fragments separately exposes the real player <source> without
    # admitting preroll URLs embedded in JavaScript.
    player_sources: list[str] = []
    player_posters: list[str] = []
    for fragment in [body, *decoded_fragments]:
        parser = MediaSourceParser()
        try:
            parser.feed(fragment)
        except (UnicodeError, ValueError):
            continue
        for raw in parser.urls:
            media_url = accepted(raw)
            if media_url and media_url not in player_sources:
                player_sources.append(media_url)
        for raw in parser.posters:
            poster_url = html.unescape(raw).strip()
            if poster_url and poster_url not in player_posters:
                player_posters.append(poster_url)
    if player_sources:
        return player_sources, player_posters

    # Keep a fallback for older page variants that expose only a raw media URL,
    # but remove HTML comments so stale sample sources cannot win selection.
    uncommented = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    searchable = html.unescape(uncommented + "\n" + "\n".join(decoded_fragments))
    found: list[str] = []
    for raw in re.findall(r"https?://[^\"'<>\\\s]+", searchable):
        media_url = accepted(raw)
        if media_url and media_url not in found:
            found.append(media_url)
    return found, player_posters


def extract_media_urls(body: str) -> list[str]:
    return extract_player_media(body)[0]


def media_asset_identifier(url: str) -> str:
    """Return a stable numeric asset id from a media/thumbnail path when present."""
    try:
        stem = Path(urllib.parse.urlsplit(url).path).stem
    except ValueError:
        return ""
    match = re.fullmatch(r"(\d+)", stem)
    return match.group(1) if match else ""


def validate_player_identity(video: Video, sources: list[str], posters: list[str]) -> None:
    """Reject a detail response that serves a different asset for the requested video."""
    expected = media_asset_identifier(video.thumbnail_url)
    if not expected:
        return
    observed = {
        identifier
        for identifier in (media_asset_identifier(url) for url in [*posters, *sources])
        if identifier
    }
    if observed and expected not in observed:
        raise MediaMismatchError(
            f"详情页媒体与榜单不一致（viewkey={video.viewkey}，期望资源 {expected}，实际 {sorted(observed)[0]}）"
        )


def public_hostname(hostname: str) -> bool:
    if not hostname or hostname.lower() == "localhost":
        return False
    try:
        return not ipaddress.ip_address(hostname).is_private
    except ValueError:
        return True


def choose_media_url(urls: Iterable[str]) -> str:
    def score(raw: str) -> tuple[int, int]:
        parsed = urllib.parse.urlsplit(raw)
        keys = {key.lower() for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)}
        signed = len(keys & SIGNATURE_KEYS)
        video_ext = 1 if Path(parsed.path).suffix.lower() in {".mp4", ".m4v", ".webm"} else 0
        return signed, video_ext

    candidates = list(urls)
    return max(candidates, key=score) if candidates else ""


_progress_lock = threading.RLock()
_progress_path: Path | None = None
_progress_total = 0
_progress_done = 0


def set_progress_target(path: Path | None, total: int) -> None:
    global _progress_path, _progress_total, _progress_done
    with _progress_lock:
        _progress_path = path
        _progress_total = total
        _progress_done = 0
        _write_progress()


def _write_progress() -> None:
    with _progress_lock:
        if _progress_path is None:
            return
        payload = json.dumps({"total": _progress_total, "done": _progress_done, "stage": "resolving"}, ensure_ascii=False)
        tmp = _progress_path.with_suffix(_progress_path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, _progress_path)


def resolve_media(
    opener: urllib.request.OpenerDirector,
    videos: list[Video],
    *,
    timeout: float,
    delay: float,
    user_agent: str,
    concurrency: int = 5,
    retries: int = 4,
    rate_limiter: RateLimiter | None = None,
    continue_on_error: bool = False,
) -> list[dict[str, str]]:
    done_lock = threading.Lock()
    worker_state = threading.local()
    failures: list[dict[str, str]] = []

    def failure_record(video: Video, exc: Exception) -> dict[str, str]:
        return {
            "viewkey": video.viewkey,
            "error": str(exc),
            "kind": "media_mismatch" if isinstance(exc, MediaMismatchError) else "resolve_error",
        }

    def worker_opener() -> urllib.request.OpenerDirector:
        current = getattr(worker_state, "opener", None)
        if current is None:
            current = build_opener()
            worker_state.opener = current
        return current

    def resolve_one(index: int, video: Video) -> None:
        global _progress_done
        try:
            body = fetch_html(
                opener if concurrency <= 1 else worker_opener(),
                video.canonical_url,
                timeout=timeout,
                user_agent=user_agent,
                retries=retries,
                rate_limiter=rate_limiter,
            )
            sources, posters = extract_player_media(body)
            validate_player_identity(video, sources, posters)
            video.media_url = choose_media_url(sources)
            video.resolved_at = time.strftime("%Y-%m-%dT%H:%M:%S%z") if video.media_url else ""
        finally:
            with done_lock:
                _progress_done += 1
                _write_progress()

    if concurrency <= 1 or len(videos) <= 1:
        for index, video in enumerate(videos):
            try:
                resolve_one(index, video)
            except Exception as exc:
                if not continue_on_error:
                    raise
                failures.append(failure_record(video, exc))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(resolve_one, i, video): video for i, video in enumerate(videos)}
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    if not continue_on_error:
                        raise
                    failures.append(failure_record(futures[future], exc))
    return failures


def write_json(path: Path, videos: list[Video], metadata: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"metadata": metadata, "videos": [asdict(video) for video in videos]}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def write_csv(path: Path, videos: list[Video]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["viewkey", "canonical_url", "title", "thumbnail_url", "duration", "source_pages", "media_url", "resolved_at"],
        )
        writer.writeheader()
        for video in videos:
            row = asdict(video)
            row["source_pages"] = ",".join(map(str, video.source_pages))
            writer.writerow(row)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取公开列表页，按 viewkey 去重")
    parser.add_argument("--url", dest="urls", action="append", help="公开列表 URL；可重复传入多个来源")
    parser.add_argument("--pages", type=int, default=2, help="从第 1 页开始抓取的页数（默认 2）")
    parser.add_argument("--delay", type=float, default=1.5, help="请求间隔秒数（默认 1.5）")
    parser.add_argument("--timeout", type=float, default=30.0, help="单请求超时秒数")
    parser.add_argument("--resolve-media", action="store_true", help="逐个访问公开详情页并提取媒体 URL")
    parser.add_argument("--resolve-concurrency", type=int, default=5, help="媒体解析并发数（默认 5，设为 1 回退到串行）")
    parser.add_argument("--listing-concurrency", type=int, default=2, help="列表页并发数（默认 2）")
    parser.add_argument("--retries", type=int, default=4, help="瞬时网络错误重试次数（默认 4）")
    parser.add_argument("--progress", type=Path, help="进度文件路径；解析时写入 done/total JSON")
    parser.add_argument("--history", type=Path, help="跨日历史索引；已见且已下载的视频不再访问详情页")
    parser.add_argument("--success-history", type=Path, help="成功下载的历史名单，用于跳过用户已删文件")
    parser.add_argument("--existing-dir", type=Path, help="已下载视频目录；历史中缺失文件的条目会重新解析")
    parser.add_argument("--new-json", type=Path, help="只输出本次新增或需重试条目的 JSON")
    parser.add_argument("--new-csv", type=Path, help="只输出本次新增或需重试条目的 CSV")
    parser.add_argument("--json", type=Path, default=Path("videos.json"), help="JSON 输出路径")
    parser.add_argument("--csv", type=Path, default=Path("videos.csv"), help="CSV 输出路径")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    args = parser.parse_args(argv)
    if not 1 <= args.pages <= 20:
        parser.error("--pages 必须在 1 到 20 之间")
    if not 0.5 <= args.delay <= 60:
        parser.error("--delay 必须在 0.5 到 60 秒之间")
    if not 1 <= args.timeout <= 120:
        parser.error("--timeout 必须在 1 到 120 秒之间")
    if not 1 <= args.listing_concurrency <= 5:
        parser.error("--listing-concurrency 必须在 1 到 5 之间")
    if not 0 <= args.retries <= 10:
        parser.error("--retries 必须在 0 到 10 之间")
    args.urls = args.urls or [DEFAULT_URL]
    for url in args.urls:
        validate_listing_url(url)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    history = load_video_cache(args.history)
    for viewkey, video in load_video_cache(args.json).items():
        history.setdefault(viewkey, video)
    historical_keys = set(history)
    opener = build_opener()
    request_limiter = RateLimiter(args.delay)
    collected: list[list[Video]] = []
    source_stats_by_index: dict[int, dict[str, object]] = {}
    raw_links = 0
    worker_state = threading.local()

    def listing_worker(source_index: int, base_url: str, page: int):
        current = getattr(worker_state, "opener", None)
        if current is None:
            current = build_opener()
            worker_state.opener = current
        url = page_url(base_url, page)
        body = fetch_html(
            current,
            url,
            timeout=args.timeout,
            user_agent=args.user_agent,
            retries=args.retries,
            rate_limiter=request_limiter,
        )
        listing_parser = ListingParser(base_url)
        listing_parser.feed(body)
        return source_index, base_url, page, len(listing_parser.candidates), parse_listing(body, base_url, page)

    jobs = [(source_index, base_url, page) for source_index, base_url in enumerate(args.urls) for page in range(1, args.pages + 1)]
    page_results = []
    listing_failures: list[dict[str, object]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.listing_concurrency) as pool:
        futures = {pool.submit(listing_worker, *job): job for job in jobs}
        for future in concurrent.futures.as_completed(futures):
            try:
                page_results.append(future.result())
            except Exception as exc:
                source_index, base_url, page = futures[future]
                listing_failures.append({"source": source_index + 1, "page": page, "url": validate_listing_url(base_url), "error": str(exc)})
                print(f"source={source_index + 1}/{len(args.urls)} page={page} failed: {exc}", file=sys.stderr)
    if not page_results:
        raise CrawlerError("所有列表页请求均失败")

    for source_index, base_url, page, page_raw_links, page_videos in sorted(page_results, key=lambda row: (row[0], row[2])):
        collected.append(page_videos)
        raw_links += page_raw_links
        current_stats = source_stats_by_index.setdefault(source_index, {"url": validate_listing_url(base_url), "raw_detail_links": 0, "pages": []})
        current_stats["raw_detail_links"] = int(current_stats["raw_detail_links"]) + page_raw_links
        current_stats["pages"].append(page_videos)
        print(f"source={source_index + 1}/{len(args.urls)} page={page} raw_links={page_raw_links} unique={len(page_videos)}", file=sys.stderr)

    source_stats = []
    for source_index in range(len(args.urls)):
        current_stats = source_stats_by_index.get(source_index, {"url": validate_listing_url(args.urls[source_index]), "raw_detail_links": 0, "pages": []})
        source_stats.append({
            "url": current_stats["url"],
            "raw_detail_links": current_stats["raw_detail_links"],
            "unique_videos": len(merge_videos(current_stats["pages"])),
        })
    videos = merge_videos(collected)
    success_keys = load_success_keys(args.success_history)
    processing, new_count, retry_count, skipped_count = select_for_processing(
        videos,
        history,
        existing_viewkeys(args.existing_dir),
        success_keys,
    )
    resolve_failures: list[dict[str, str]] = []
    if args.resolve_media:
        if args.progress:
            set_progress_target(args.progress, len(processing))
        resolve_failures = resolve_media(
            opener,
            processing,
            timeout=args.timeout,
            delay=args.delay,
            user_agent=args.user_agent,
            concurrency=args.resolve_concurrency,
            retries=args.retries,
            rate_limiter=request_limiter,
            continue_on_error=True,
        )
        if args.progress and args.progress.exists():
            args.progress.unlink(missing_ok=True)
    metadata = {
        "listing_url": validate_listing_url(args.urls[0]),
        "listing_urls": [validate_listing_url(url) for url in args.urls],
        "listing_count": len(args.urls),
        "source_stats": source_stats,
        "pages": args.pages,
        "raw_detail_links": raw_links,
        "unique_videos": len(videos),
        "duplicates_removed": raw_links - len(videos),
        "historical_known_total": len(historical_keys),
        "known_videos_skipped": skipped_count,
        "new_videos": new_count,
        "retry_videos": retry_count,
        "listing_failures": listing_failures,
        "resolve_failures": resolve_failures,
        "detail_pages_requested": len(processing) if args.resolve_media else 0,
        "authenticated": False,
        "media_resolved": bool(args.resolve_media),
    }
    write_json(args.json, videos, metadata)
    write_csv(args.csv, videos)
    if args.new_json:
        write_json(args.new_json, processing, {**metadata, "output_scope": "new_or_retry", "unique_videos": len(processing)})
    if args.new_csv:
        write_csv(args.new_csv, processing)
    if args.history:
        for video in videos:
            history[video.viewkey] = video
        write_json(
            args.history,
            list(history.values()),
            {"archive": True, "total_seen": len(history), "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
        )
    print(json.dumps(metadata, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CrawlerError, OSError, UnicodeError, socket.timeout) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
