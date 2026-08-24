#!/usr/bin/env python3
"""Crawl 91porn listing pages and deduplicate video entries.

An optional local session Cookie may be used for the user's normal account access.
VIP, paid, private, and administrator authorization checks are never bypassed.
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
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


DEFAULT_URL = "https://91porn.com/v.php?category=hot&viewtype=basic"
# Cloudflare clearance is bound to the browser profile that issued the session
# cookie. Keep the default aligned with the local Edge session, while allowing
# an explicit override when that browser profile changes.
DEFAULT_USER_AGENT = os.environ.get(
    "CRAWLER_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 "
    "Safari/537.36 Edg/150.0.0.0",
)
MAX_HTML_BYTES = 8 * 1024 * 1024
MEDIA_EXTENSIONS = {".mp4", ".m4v", ".webm", ".m3u8"}
VIDEO_FILE_EXTENSIONS = {".mp4", ".m4v", ".webm", ".ts", ".mkv", ".mov", ".avi"}
SIGNATURE_KEYS = {"st", "f", "e", "sig", "signature", "token"}
AUTH_COOKIE_ENV = "AUTH_COOKIE_FILE"
AUTH_USER_AGENT_ENV = "AUTH_USER_AGENT_FILE"
MAX_AUTH_COOKIE_BYTES = 64 * 1024
MAX_AUTH_USER_AGENT_BYTES = 512
CLOUDFLARE_CHALLENGE_MARKERS = (b"cf-chl-", b"_cf_chl_opt")


class CrawlerError(RuntimeError):
    pass


class CloudflareChallengeError(CrawlerError):
    """A real crawl request was stopped by Cloudflare's browser challenge."""


class MediaMismatchError(CrawlerError):
    """The detail page served media that does not belong to the listing item."""


def is_cloudflare_challenge(body: bytes, headers: object | None = None) -> bool:
    """Identify an actual Cloudflare challenge, not a normal page using CF scripts."""
    if headers is not None:
        try:
            if str(headers.get("cf-mitigated") or "").lower() == "challenge":  # type: ignore[attr-defined]
                return True
        except (AttributeError, TypeError):
            pass
    lowered = body.lower()
    if re.search(rb"<title[^>]*>\s*just a moment(?:\.{3})?\s*</title>", lowered):
        return True
    return any(marker in lowered for marker in CLOUDFLARE_CHALLENGE_MARKERS)


class MediaUnavailableError(CrawlerError):
    """The detail page repeatedly exposed no usable public media source."""

    pass


BLOCKED_MEDIA_FAILURE_KINDS = {"media_mismatch", "media_unavailable"}


@dataclass
class Candidate:
    viewkey: str
    href: str
    tracking_class: str = ""
    title: str = ""
    thumbnail_url: str = ""
    duration: str = ""
    asset_id: str = ""

    def consistent_asset(self) -> bool:
        """Report whether this card's own player id matches the thumbnail it renders.

        The listing repeats every viewkey across several cards and some of those
        cards render a neighbouring video's thumbnail.  Only the card whose
        ``playvthumb_<id>`` overlay agrees with its own ``/thumb/<id>.jpg``
        describes the video the detail page will actually serve, so that card is
        the one worth keeping.
        """
        return bool(self.asset_id) and self.asset_id == media_asset_identifier(self.thumbnail_url)

    def score(self) -> int:
        # Self consistency beats every other hint: the tracking class the site
        # uses for its visible cards changes over time (it was c=llzvq), while a
        # card that contradicts its own player id is always the wrong one.
        return (
            (16 if self.consistent_asset() else 0)
            + (8 if self.tracking_class == "llzvq" else 0)
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
    asset_id: str = ""

    def consistent_asset(self) -> bool:
        """Same self-consistency check as :meth:`Candidate.consistent_asset`."""
        return bool(self.asset_id) and self.asset_id == media_asset_identifier(self.thumbnail_url)


PLAYER_THUMB_ID = re.compile(r"playvthumb_(\d+)")


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
        if not self.current.asset_id:
            overlay = PLAYER_THUMB_ID.search(values.get("id", ""))
            if overlay:
                self.current.asset_id = overlay.group(1)
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
    # Some listing titles are double-escaped (for example, ``&amp;#39;``).
    # HTMLParser decodes the outer layer, so decode the remaining entity before
    # persisting the title.
    return " ".join(html.unescape(value).split())


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
            asset_id=item.asset_id,
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
            # A card that agrees with its own player id wins over one that does
            # not, even when the disagreeing card was seen on an earlier page.
            if video.consistent_asset() and not current.consistent_asset():
                video.source_pages = sorted(set(current.source_pages + video.source_pages))
                for attr in ("title", "thumbnail_url", "duration", "media_url", "resolved_at"):
                    if not getattr(video, attr) and getattr(current, attr):
                        setattr(video, attr, getattr(current, attr))
                merged[video.viewkey] = video
                continue
            current.source_pages = sorted(set(current.source_pages + video.source_pages))
            for attr in ("title", "thumbnail_url", "duration", "media_url", "resolved_at"):
                if not getattr(current, attr) and getattr(video, attr):
                    setattr(current, attr, getattr(video, attr))
            if not current.asset_id and video.asset_id:
                current.asset_id = video.asset_id
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
            asset_id=str(row.get("asset_id") or ""),
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


def append_success_keys(path: Path | None, viewkeys: Iterable[str]) -> None:
    if path is None:
        return
    updated = load_success_keys(path)
    updated.update(viewkey for viewkey in viewkeys if valid_viewkey(viewkey))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(f"{viewkey}\n" for viewkey in sorted(updated)), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def successful_asset_identifiers(
    history: dict[str, Video],
    success_keys: set[str],
) -> set[str]:
    assets = {
        media_asset_identifier(video.thumbnail_url)
        for viewkey, video in history.items()
        if viewkey in success_keys
    }
    assets.discard("")
    return assets


def load_blocked_media_history(path: Path | None) -> dict[str, dict[str, object]]:
    """Load permanently safety-blocked media keyed by the listing viewkey."""
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, dict):
        return {}
    return {
        str(viewkey): dict(value)
        for viewkey, value in items.items()
        if valid_viewkey(str(viewkey)) and isinstance(value, dict)
    }


def load_ignored_media_keys(path: Path | None) -> set[str]:
    """Load explicitly user-ignored media keys without changing the file.

    Ignoring is an opt-in, reversible decision.  This reader deliberately has
    no write/merge side effect, so a resolver or download failure can never
    silently become permanently ignored.
    """
    if path is None or not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return set()
    items = payload.get("items") if isinstance(payload, dict) else None
    if isinstance(items, dict):
        candidates = items.keys()
    elif isinstance(items, list):
        # Accept a simple list as a migration-friendly read-only fallback.
        candidates = (
            item.get("viewkey") if isinstance(item, dict) else item
            for item in items
        )
    else:
        return set()
    return {
        str(viewkey)
        for viewkey in candidates
        if valid_viewkey(str(viewkey))
    }


def write_blocked_media_history(path: Path, items: dict[str, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"version": 1, "items": items}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def prune_blocked_media_history(path: Path | None, completed_keys: set[str]) -> int:
    """Remove obsolete safety blocks for items that later completed."""
    if path is None or not completed_keys:
        return 0
    items = load_blocked_media_history(path)
    remaining = {key: value for key, value in items.items() if key not in completed_keys}
    removed = len(items) - len(remaining)
    if removed:
        write_blocked_media_history(path, remaining)
    return removed


def auth_credential_timestamp(path: Path | None = None) -> str:
    """Return when the stored credential last changed, in run-history format."""
    cookie_path = path or configured_auth_cookie_file()
    if cookie_path is None:
        return ""
    try:
        changed_at = cookie_path.stat().st_mtime
    except OSError:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(changed_at))


def _timestamp_value(value: object) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def block_predates_credential(record: dict[str, object], credential_at: str) -> bool:
    """Report whether a safe block was confirmed before the current credential.

    Such a block deserves exactly one retry: the item may have been unusable only
    because the session was missing.  ``update_blocked_media_history`` refreshes
    ``lastSeenAt`` afterwards, so a still-blocked item converges back to skipped
    instead of being re-fetched on every run.
    """
    credential = _timestamp_value(credential_at)
    confirmed = _timestamp_value(record.get("lastSeenAt") or record.get("firstSeenAt"))
    return bool(credential is not None and (confirmed is None or confirmed < credential))


def matching_blocked_failures(
    videos: Iterable[Video],
    history: dict[str, dict[str, object]],
    *,
    success_keys: set[str] | None = None,
    credential_at: str = "",
) -> list[dict[str, object]]:
    """Return persisted blocks that still match the listing asset identity."""
    failures: list[dict[str, object]] = []
    completed = success_keys or set()
    for video in videos:
        if video.viewkey in completed:
            continue
        record = history.get(video.viewkey)
        if not record:
            continue
        recorded_asset = str(record.get("expectedAsset") or "")
        current_asset = media_asset_identifier(video.thumbnail_url)
        if recorded_asset and current_asset and recorded_asset != current_asset:
            continue
        if credential_at and block_predates_credential(record, credential_at):
            continue
        kind = str(record.get("kind") or "media_mismatch")
        if kind not in BLOCKED_MEDIA_FAILURE_KINDS:
            kind = "media_mismatch"
        failures.append({
            "viewkey": video.viewkey,
            "error": str(record.get("error") or "媒体资源不可用"),
            "kind": kind,
            "persistent": True,
        })
    return failures


def update_blocked_media_history(
    path: Path | None,
    videos: Iterable[Video],
    failures: Iterable[dict[str, object]],
) -> None:
    """Persist confirmed unusable media so later crawls do not retry it."""
    if path is None:
        return
    items = load_blocked_media_history(path)
    videos_by_key = {video.viewkey: video for video in videos}
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    changed = False
    for failure in failures:
        kind = str(failure.get("kind") or "")
        if kind not in BLOCKED_MEDIA_FAILURE_KINDS:
            continue
        viewkey = str(failure.get("viewkey") or "")
        video = videos_by_key.get(viewkey)
        if video is None or not valid_viewkey(viewkey):
            continue
        previous = items.get(viewkey, {})
        items[viewkey] = {
            "expectedAsset": media_asset_identifier(video.thumbnail_url),
            "error": str(failure.get("error") or "媒体资源不可用"),
            "kind": kind,
            "firstSeenAt": str(previous.get("firstSeenAt") or now),
            "lastSeenAt": now,
        }
        changed = True
    if not changed:
        return
    write_blocked_media_history(path, items)


def select_for_processing(
    videos: list[Video],
    history: dict[str, Video],
    existing_keys: set[str] | None,
    success_keys: set[str] | None = None,
    *,
    new_only: bool = False,
    ignored_keys: set[str] | None = None,
) -> tuple[list[Video], int, int, int]:
    processing: list[Video] = []
    new_count = 0
    retry_count = 0
    skipped_count = 0
    ignored = ignored_keys or set()
    for video in videos:
        file_exists = existing_keys is not None and video.viewkey in existing_keys
        previous = history.get(video.viewkey)
        if previous is not None:
            for attr in ("title", "thumbnail_url", "duration", "media_url", "resolved_at"):
                if not getattr(video, attr) and getattr(previous, attr):
                    setattr(video, attr, getattr(previous, attr))

        # This list is explicitly maintained by the user and is reversible by
        # removing an entry.  Never add resolver/download failures here.
        if video.viewkey in ignored:
            skipped_count += 1
            continue

        # The staging directory is a transfer area. Once a video has completed,
        # moving or deleting it is intentional and must not create a retry.
        if success_keys is not None and video.viewkey in success_keys:
            skipped_count += 1
            continue

        # Daily runs can opt into a strict new-only queue.  Previously seen
        # items (including unresolved or missing files) are left for the
        # explicit repair workflow, so a verification page or transient source
        # response cannot cause the same detail URL to be revisited every run.
        if new_only and previous is not None:
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


def configured_auth_cookie_file() -> Path | None:
    raw = os.environ.get(AUTH_COOKIE_ENV, "").strip()
    return Path(raw) if raw else Path(__file__).resolve().parents[1] / "data" / "auth-cookie.txt"


def configured_user_agent() -> str:
    raw_path = os.environ.get(AUTH_USER_AGENT_ENV, "").strip()
    path = Path(raw_path) if raw_path else Path(__file__).resolve().parents[1] / "data" / "auth-user-agent.txt"
    try:
        if path.stat().st_size > MAX_AUTH_USER_AGENT_BYTES:
            return DEFAULT_USER_AGENT
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return DEFAULT_USER_AGENT
    if not value or "\r" in value or "\n" in value or not value.startswith("Mozilla/5.0"):
        return DEFAULT_USER_AGENT
    return value


def load_auth_cookie_jar(path: Path | None = None) -> http.cookiejar.CookieJar:
    """Load a raw Cookie header from a local file without exposing it to logs."""
    jar = http.cookiejar.CookieJar()
    cookie_path = path or configured_auth_cookie_file()
    if cookie_path is None:
        return jar
    try:
        if cookie_path.stat().st_size > MAX_AUTH_COOKIE_BYTES:
            raise CrawlerError("登录 Cookie 文件超过 64 KiB")
        raw = cookie_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return jar
    except (OSError, UnicodeError) as exc:
        raise CrawlerError("无法读取登录 Cookie 文件") from exc
    if not raw or raw == "PASTE_NEW_COOKIE_HERE":
        return jar
    for fragment in raw.split(";"):
        name, separator, value = fragment.strip().partition("=")
        if not separator or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            continue
        jar.set_cookie(http.cookiejar.Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=".91porn.com",
            domain_specified=True,
            domain_initial_dot=True,
            path="/",
            path_specified=True,
            secure=True,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={"HttpOnly": None},
            rfc2109=False,
        ))
    return jar


def auth_cookie_configured(path: Path | None = None) -> bool:
    return any(True for _cookie in load_auth_cookie_jar(path))


def build_opener(
    cookie_file: Path | None = None,
    proxy_url: str | None = None,
) -> urllib.request.OpenerDirector:
    """Build a crawler opener with cookies and an explicit network route.

    ``urllib`` installs a default :class:`ProxyHandler` which reads the
    process environment when no handler is supplied.  Pass an explicit empty
    mapping for the normal direct route so a download-only proxy setting cannot
    accidentally change listing/detail requests.  When a proxy is requested,
    use the same HTTP proxy for both HTTP and HTTPS targets (HTTPS is handled
    through CONNECT by ``urllib``).
    """
    proxy = str(proxy_url or "").strip()
    if proxy:
        try:
            parsed = urllib.parse.urlsplit(proxy)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise CrawlerError("代理地址格式无效") from exc
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not hostname
            or parsed.query
            or parsed.fragment
        ):
            raise CrawlerError("代理地址必须是带主机名的 HTTP/HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise CrawlerError("代理地址不能包含用户凭据")
        if port is not None and not 1 <= port <= 65535:
            raise CrawlerError("代理端口无效")
        proxy_mapping = {"http": proxy, "https": proxy}
    else:
        # Do not inherit HTTP(S)_PROXY/ALL_PROXY from the container or host.
        proxy_mapping = {}
    jar = load_auth_cookie_jar(cookie_file)
    return urllib.request.build_opener(
        urllib.request.ProxyHandler(proxy_mapping),
        urllib.request.HTTPCookieProcessor(jar),
    )


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
                if is_cloudflare_challenge(raw, response.headers):
                    raise CloudflareChallengeError(f"Cloudflare Challenge: {path}")
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            response_headers = exc.headers
            # Cloudflare also serves the interstitial as 429/503, and the
            # cf-mitigated header alone is enough to identify it.  Reading the
            # body only for 403 let a challenged 503 look like a retryable
            # upstream error, so no auth signal ever reached the dashboard.
            challenge_status = status_code in {403, 429, 503}
            error_body = exc.read(64 * 1024) if challenge_status else b""
            exc.close()
            if challenge_status and is_cloudflare_challenge(error_body, response_headers):
                raise CloudflareChallengeError(f"Cloudflare Challenge: {path}") from exc
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


def write_progress_state(path: Path | None, payload: dict[str, object]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


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
        write_progress_state(_progress_path, {"total": _progress_total, "done": _progress_done, "stage": "resolving"})


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
    identity_attempts: int | None = None,
) -> list[dict[str, str]]:
    done_lock = threading.Lock()
    worker_state = threading.local()
    failures: list[dict[str, str]] = []

    def failure_record(video: Video, exc: Exception) -> dict[str, str]:
        kind = (
            "auth_challenge" if isinstance(exc, CloudflareChallengeError)
            else "media_mismatch" if isinstance(exc, MediaMismatchError)
            else "media_unavailable" if isinstance(exc, MediaUnavailableError)
            else "resolve_error"
        )
        return {
            "viewkey": video.viewkey,
            "error": str(exc),
            "kind": kind,
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
            # A failed refresh must never leave a previously signed media URL in
            # place; otherwise a blocked mismatch could still reach download.py.
            video.media_url = ""
            video.resolved_at = ""
            # The site occasionally serves an unrelated legacy player while the
            # surrounding detail page is correct. Re-fetch a mismatch before
            # permanently safety-blocking the item.
            check_attempts = max(
                1,
                min(3, identity_attempts if identity_attempts is not None else retries + 1),
            )
            for identity_attempt in range(check_attempts):
                body = fetch_html(
                    opener if concurrency <= 1 else worker_opener(),
                    video.canonical_url,
                    timeout=timeout,
                    user_agent=user_agent,
                    retries=retries,
                    rate_limiter=rate_limiter,
                )
                sources, posters = extract_player_media(body)
                try:
                    validate_player_identity(video, sources, posters)
                except MediaMismatchError:
                    if identity_attempt + 1 >= check_attempts:
                        raise
                    continue
                media_url = choose_media_url(sources)
                if not media_url:
                    if identity_attempt + 1 >= check_attempts:
                        raise MediaUnavailableError(
                            f"详情页未发现可下载媒体（viewkey={video.viewkey}，已复核 {check_attempts} 次）"
                        )
                    continue
                video.media_url = media_url
                video.resolved_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                break
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
        fieldnames = ["viewkey", "canonical_url", "title", "thumbnail_url", "duration", "source_pages", "media_url", "resolved_at"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for video in videos:
            row = asdict(video)
            row["source_pages"] = ",".join(map(str, video.source_pages))
            writer.writerow(row)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def pending_output_metadata(metadata: dict[str, object], processing_count: int) -> dict[str, object]:
    """Describe a narrowed queue without replacing full-crawl totals."""
    return {
        **metadata,
        "output_scope": "new_or_retry",
        "processing_videos": processing_count,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取公开列表页，按 viewkey 去重")
    parser.add_argument("--url", dest="urls", action="append", help="公开列表 URL；可重复传入多个来源")
    parser.add_argument("--pages", type=int, default=2, help="从第 1 页开始抓取的页数（默认 2）")
    parser.add_argument("--delay", type=float, default=1.5, help="请求间隔秒数（默认 1.5）")
    parser.add_argument("--timeout", type=float, default=30.0, help="单请求超时秒数")
    parser.add_argument("--resolve-media", action="store_true", help="逐个访问公开详情页并提取媒体 URL")
    parser.add_argument("--resolve-concurrency", type=int, default=5, help="媒体解析并发数（默认 5，设为 1 回退到串行）")
    parser.add_argument("--media-rechecks", type=int, default=3, help="同一详情页无媒体/身份不一致时的复核次数（默认 3）")
    parser.add_argument("--listing-concurrency", type=int, default=2, help="列表页并发数（默认 2）")
    parser.add_argument("--retries", type=int, default=4, help="瞬时网络错误重试次数（默认 4）")
    parser.add_argument("--progress", type=Path, help="进度文件路径；解析时写入 done/total JSON")
    parser.add_argument("--history", type=Path, help="跨日历史索引；已见且已下载的视频不再访问详情页")
    parser.add_argument("--success-history", type=Path, help="成功下载的历史名单，用于跳过用户已删文件")
    parser.add_argument("--blocked-history", type=Path, help="已确认错误媒体索引；身份未变化时不再访问详情页")
    parser.add_argument("--ignored-history", type=Path, help="用户明确忽略的媒体索引；只读且可手动移除恢复")
    parser.add_argument("--existing-dir", type=Path, help="已下载视频目录；历史中缺失文件的条目会重新解析")
    parser.add_argument("--new-only", action="store_true", help="只解析历史中从未见过的新条目；已见条目交由修复任务处理")
    parser.add_argument("--new-json", type=Path, help="只输出本次新增或需重试条目的 JSON")
    parser.add_argument("--new-csv", type=Path, help="只输出本次新增或需重试条目的 CSV")
    parser.add_argument("--json", type=Path, default=Path("videos.json"), help="JSON 输出路径")
    parser.add_argument("--csv", type=Path, default=Path("videos.csv"), help="CSV 输出路径")
    parser.add_argument("--user-agent", default=configured_user_agent())
    args = parser.parse_args(argv)
    if not 1 <= args.pages <= 20:
        parser.error("--pages 必须在 1 到 20 之间")
    if not 0.5 <= args.delay <= 60:
        parser.error("--delay 必须在 0.5 到 60 秒之间")
    if not 1 <= args.timeout <= 120:
        parser.error("--timeout 必须在 1 到 120 秒之间")
    if not 1 <= args.listing_concurrency <= 5:
        parser.error("--listing-concurrency 必须在 1 到 5 之间")
    if not 1 <= args.resolve_concurrency <= 16:
        parser.error("--resolve-concurrency 必须在 1 到 16 之间")
    if not 1 <= args.media_rechecks <= 3:
        parser.error("--media-rechecks 必须在 1 到 3 之间")
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
    write_progress_state(args.progress, {"stage": "listing", "done": 0, "total": len(jobs), "listingFailures": 0})
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.listing_concurrency) as pool:
        futures = {pool.submit(listing_worker, *job): job for job in jobs}
        for future in concurrent.futures.as_completed(futures):
            try:
                page_results.append(future.result())
            except Exception as exc:
                source_index, base_url, page = futures[future]
                listing_failures.append({
                    "source": source_index + 1,
                    "page": page,
                    "url": validate_listing_url(base_url),
                    "error": str(exc),
                    "kind": "auth_challenge" if isinstance(exc, CloudflareChallengeError) else "request_error",
                })
                print(f"source={source_index + 1}/{len(args.urls)} page={page} failed: {exc}", file=sys.stderr)
            write_progress_state(args.progress, {
                "stage": "listing",
                "done": len(page_results) + len(listing_failures),
                "total": len(jobs),
                "listingFailures": len(listing_failures),
            })
    if not page_results:
        # One challenged page already proves the stored clearance is not working;
        # requiring every listing failure to be a challenge hid the mixed case.
        auth_failure = any(item.get("kind") == "auth_challenge" for item in listing_failures)
        write_progress_state(args.progress, {
            "stage": "failed",
            "phase": "listing",
            "done": len(listing_failures),
            "total": len(jobs),
            "listingFailures": len(listing_failures),
            "authFailure": auth_failure,
            "rawLinks": 0,
            "uniqueVideos": 0,
            "skippedVideos": 0,
            "error": "所有列表页请求均失败",
        })
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
    ignored_keys = load_ignored_media_keys(args.ignored_history)
    ignored_video_keys = {
        video.viewkey for video in videos if video.viewkey in ignored_keys
    }
    # An explicit ignore decision wins over any stale media URL copied from a
    # cache. Keep the inventory row, but never let it re-enter the download
    # queue until its key is removed from ignored-media.json.
    for video in videos:
        if video.viewkey in ignored_keys:
            video.media_url = ""
            video.resolved_at = ""
    prune_blocked_media_history(args.blocked_history, success_keys)
    success_asset_ids = successful_asset_identifiers(history, success_keys)
    asset_duplicate_keys = {
        video.viewkey
        for video in videos
        if video.viewkey not in success_keys
        and media_asset_identifier(video.thumbnail_url) in success_asset_ids
    }
    if asset_duplicate_keys:
        append_success_keys(args.success_history, asset_duplicate_keys)
        success_keys.update(asset_duplicate_keys)
    authenticated = auth_cookie_configured()
    blocked_history = load_blocked_media_history(args.blocked_history)
    # Discarding the whole table whenever a cookie file existed made it
    # write-only: 56 confirmed-unusable pages were re-fetched on every run with
    # no convergence.  Retry only what was confirmed before the current
    # credential, which is the case a new session can actually change.
    persisted_block_failures = matching_blocked_failures(
        videos,
        blocked_history,
        success_keys=success_keys,
        credential_at=auth_credential_timestamp() if authenticated else "",
    )
    persisted_block_keys = {str(item["viewkey"]) for item in persisted_block_failures}
    for video in videos:
        if video.viewkey in persisted_block_keys:
            video.media_url = ""
            video.resolved_at = ""
    processing, new_count, retry_count, skipped_count = select_for_processing(
        [video for video in videos if video.viewkey not in persisted_block_keys],
        history,
        existing_viewkeys(args.existing_dir),
        success_keys,
        new_only=args.new_only,
        ignored_keys=ignored_keys,
    )
    resolve_failures: list[dict[str, object]] = list(persisted_block_failures)
    if args.resolve_media:
        if args.progress:
            set_progress_target(args.progress, len(processing))
        new_resolve_failures = resolve_media(
            opener,
            processing,
            timeout=args.timeout,
            delay=args.delay,
            user_agent=args.user_agent,
            concurrency=args.resolve_concurrency,
            retries=args.retries,
            rate_limiter=request_limiter,
            continue_on_error=True,
            identity_attempts=args.media_rechecks,
        )
        resolve_failures.extend(new_resolve_failures)
        update_blocked_media_history(args.blocked_history, processing, new_resolve_failures)
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
        "ignored_videos": len(ignored_video_keys),
        "asset_duplicates_skipped": len(asset_duplicate_keys),
        "new_videos": new_count,
        "retry_videos": retry_count,
        "listing_failures": listing_failures,
        "resolve_failures": resolve_failures,
        "persisted_blocked_skipped": len(persisted_block_failures),
        "detail_pages_requested": len(processing) if args.resolve_media else 0,
        "authenticated": authenticated,
        "media_resolved": bool(args.resolve_media),
    }
    write_json(args.json, videos, metadata)
    write_csv(args.csv, videos)
    if args.new_json:
        write_json(args.new_json, processing, pending_output_metadata(metadata, len(processing)))
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
