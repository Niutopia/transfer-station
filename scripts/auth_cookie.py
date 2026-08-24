#!/usr/bin/env python3
"""Validate and replace the local crawler authentication profile safely."""

from __future__ import annotations

import http.cookiejar
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path


TARGET_URL = "https://91porn.com/index.php"
TARGET_HOSTS = {"91porn.com", "www.91porn.com"}
MAX_COOKIE_BYTES = 64 * 1024
MAX_USER_AGENT_BYTES = 512
MAX_VALIDATION_BYTES = 512 * 1024
COOKIE_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
CHALLENGE_MARKERS = (b"cf-chl-", b"_cf_chl_opt")


class AuthCookieError(ValueError):
    pass


class AuthCookieProbeError(AuthCookieError):
    """The remote probe could not prove whether a locally valid profile works."""


def is_cloudflare_challenge(body: bytes, headers: object | None = None) -> bool:
    """Do not mistake Cloudflare assets embedded in a normal page for a challenge."""
    if headers is not None:
        try:
            if str(headers.get("cf-mitigated") or "").lower() == "challenge":  # type: ignore[attr-defined]
                return True
        except (AttributeError, TypeError):
            pass
    lowered = body.lower()
    if re.search(rb"<title[^>]*>\s*just a moment(?:\.{3})?\s*</title>", lowered):
        return True
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


def normalize_cookie_header(raw_value: object) -> tuple[str, list[tuple[str, str]]]:
    if not isinstance(raw_value, str):
        raise AuthCookieError("Cookie 必须是文本")
    if len(raw_value.encode("utf-8")) > MAX_COOKIE_BYTES:
        raise AuthCookieError("Cookie 内容超过 64 KB 上限")
    raw = raw_value.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    if not raw:
        raise AuthCookieError("Cookie 不能为空")
    if "\r" in raw or "\n" in raw:
        raise AuthCookieError("Cookie Header 不能包含换行")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise AuthCookieError("Cookie Header 不能包含控制字符")

    cookies: list[tuple[str, str]] = []
    seen: set[str] = set()
    for fragment in raw.split(";"):
        fragment = fragment.strip()
        if not fragment:
            continue
        name, separator, value = fragment.partition("=")
        name = name.strip()
        value = value.strip()
        if not separator or not COOKIE_NAME_RE.fullmatch(name):
            raise AuthCookieError("Cookie Header 中存在无效字段")
        if name in seen:
            continue
        seen.add(name)
        cookies.append((name, value))
    if not cookies:
        raise AuthCookieError("Cookie Header 中没有可用字段")
    if "cf_clearance" not in seen:
        raise AuthCookieError("缺少 Cloudflare 验证字段 cf_clearance")
    return "; ".join(f"{name}={value}" for name, value in cookies), cookies


def normalize_user_agent(raw_value: object) -> str:
    if not isinstance(raw_value, str):
        raise AuthCookieError("浏览器标识无效")
    value = raw_value.strip()
    if not value or "\r" in value or "\n" in value:
        raise AuthCookieError("浏览器标识无效")
    if len(value.encode("utf-8")) > MAX_USER_AGENT_BYTES:
        raise AuthCookieError("浏览器标识过长")
    if not value.startswith("Mozilla/5.0"):
        raise AuthCookieError("请使用获取该 Cookie 的浏览器提交")
    return value


def cookie_jar(cookies: list[tuple[str, str]]) -> http.cookiejar.CookieJar:
    jar = http.cookiejar.CookieJar()
    for name, value in cookies:
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


def validate_auth_profile(cookie_value: object, user_agent_value: object, *, timeout: float = 20.0) -> tuple[str, str, int]:
    normalized_cookie, cookies = normalize_cookie_header(cookie_value)
    user_agent = normalize_user_agent(user_agent_value)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar(cookies)))
    request = urllib.request.Request(
        TARGET_URL,
        headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"},
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != "https" or final.hostname not in TARGET_HOSTS:
                raise AuthCookieProbeError("验证请求被重定向到非目标站点")
            if response.headers.get_content_type() not in {"text/html", "application/xhtml+xml"}:
                raise AuthCookieProbeError("验证响应不是网页")
            body = response.read(MAX_VALIDATION_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read(32 * 1024)
        exc.close()
        if status == 403 and is_cloudflare_challenge(body, exc.headers):
            raise AuthCookieProbeError("Cloudflare 检测请求被拦截") from exc
        raise AuthCookieProbeError(f"Cookie 检测请求失败（HTTP {status}）") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AuthCookieProbeError("暂时无法连接目标站点") from exc
    if len(body) > MAX_VALIDATION_BYTES:
        raise AuthCookieProbeError("验证响应异常")
    if is_cloudflare_challenge(body, response.headers):
        raise AuthCookieProbeError("Cloudflare 检测请求被拦截")
    return normalized_cookie, user_agent, len(cookies)


def _stage_private_text(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value.rstrip("\n") + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        return temporary
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _restore_private_file(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.restore.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(previous)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def replace_auth_profile(cookie_path: Path, user_agent_path: Path, cookie_value: object, user_agent_value: object) -> dict[str, object]:
    normalized_cookie, cookies = normalize_cookie_header(cookie_value)
    user_agent = normalize_user_agent(user_agent_value)
    cookie_count = len(cookies)
    previous_user_agent = user_agent_path.read_bytes() if user_agent_path.exists() else None
    cookie_temporary: Path | None = None
    user_agent_temporary: Path | None = None
    user_agent_replaced = False
    try:
        cookie_temporary = _stage_private_text(cookie_path, normalized_cookie)
        user_agent_temporary = _stage_private_text(user_agent_path, user_agent)
        os.replace(user_agent_temporary, user_agent_path)
        user_agent_replaced = True
        os.replace(cookie_temporary, cookie_path)
    except Exception:
        if user_agent_replaced:
            try:
                _restore_private_file(user_agent_path, previous_user_agent)
            except OSError:
                pass
        raise
    finally:
        if cookie_temporary is not None:
            cookie_temporary.unlink(missing_ok=True)
        if user_agent_temporary is not None:
            user_agent_temporary.unlink(missing_ok=True)
    return auth_cookie_status(cookie_path, user_agent_path, valid=None, cookie_count=cookie_count)


def user_agent_label(user_agent: str) -> str:
    for pattern, label in ((r"Edg/([0-9.]+)", "Edge"), (r"Chrome/([0-9.]+)", "Chrome"), (r"Version/([0-9.]+).*Safari/", "Safari")):
        match = re.search(pattern, user_agent)
        if match:
            return f"{label} {match.group(1).split('.', 1)[0]}"
    return "浏览器身份"


def auth_cookie_status(
    cookie_path: Path,
    user_agent_path: Path,
    *,
    valid: bool | None = None,
    error: str = "",
    cookie_count: int | None = None,
) -> dict[str, object]:
    cookie_value = ""
    user_agent = ""
    try:
        cookie_value = cookie_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        pass
    try:
        user_agent = user_agent_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        pass
    configured = False
    has_clearance = False
    if cookie_value:
        try:
            _, cookies = normalize_cookie_header(cookie_value)
            configured = True
            has_clearance = any(name == "cf_clearance" for name, _ in cookies)
            if cookie_count is None:
                cookie_count = len(cookies)
        except AuthCookieError:
            pass
    updated_at = None
    if cookie_path.exists():
        try:
            updated_at = datetime.fromtimestamp(cookie_path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
        except OSError:
            pass
    return {
        "configured": configured,
        "valid": valid,
        "hasClearance": has_clearance,
        "cookieCount": int(cookie_count or 0),
        "updatedAt": updated_at,
        "browser": user_agent_label(user_agent) if user_agent else None,
        "error": error or None,
    }


def validate_stored_profile(
    cookie_path: Path,
    user_agent_path: Path,
    *,
    latest_manual_crawl: dict[str, object] | None = None,
) -> dict[str, object]:
    status = auth_cookie_status(cookie_path, user_agent_path)
    if not status["configured"]:
        return {**status, "valid": False, "error": "尚未配置可用 Cookie"}
    try:
        user_agent = user_agent_path.read_text(encoding="utf-8")
        normalize_user_agent(user_agent)
    except (OSError, UnicodeError, AuthCookieError) as exc:
        message = str(exc) if isinstance(exc, AuthCookieError) else "Cookie 配置无法读取"
        return auth_cookie_status(cookie_path, user_agent_path, valid=False, error=message)
    if isinstance(latest_manual_crawl, dict):
        # A Cloudflare challenge invalidates the credential no matter how the run
        # was labelled overall: with the listing pages cached a challenged run
        # still exits 0, and keying on resultStatus == "failed" made the whole
        # signal unreachable.
        if latest_manual_crawl.get("authFailure") is True:
            return auth_cookie_status(
                cookie_path,
                user_agent_path,
                valid=False,
                error="上次手动抓取被 Cloudflare 拦截",
            )
        challenges = latest_manual_crawl.get("authChallenges")
        if isinstance(challenges, int) and not isinstance(challenges, bool) and challenges > 0:
            return auth_cookie_status(
                cookie_path,
                user_agent_path,
                valid=False,
                error="上次手动抓取有详情页被 Cloudflare 拦截",
            )
        detail_pages = latest_manual_crawl.get("detailPagesRequested")
        if latest_manual_crawl.get("crawlExitCode") == 0:
            # Only detail pages actually exercise the credential.  A listing-only
            # run proves nothing, so it stays "unknown" rather than "valid".
            if detail_pages is None:
                return auth_cookie_status(cookie_path, user_agent_path, valid=True)
            if isinstance(detail_pages, int) and not isinstance(detail_pages, bool) and detail_pages > 0:
                return auth_cookie_status(cookie_path, user_agent_path, valid=True)
    return auth_cookie_status(cookie_path, user_agent_path, valid=None)
