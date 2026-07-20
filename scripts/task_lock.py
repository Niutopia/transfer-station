#!/usr/bin/env python3
"""Shared, heartbeat-backed task lock for every crawl/download entry point."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path


STALE_SECONDS = 90
PAUSED_STALE_SECONDS = 24 * 60 * 60


def read_lock(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def lock_is_active(path: Path, *, clean_stale: bool = True) -> bool:
    if not path.exists():
        return False
    payload = read_lock(path)
    state = str(payload.get("state") or "running")
    hostname = str(payload.get("hostname") or "")
    pid = int(payload.get("pid") or 0)
    try:
        age = max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return False
    same_host = hostname == socket.gethostname()
    active = (_pid_alive(pid) and age <= STALE_SECONDS) if same_host else age <= STALE_SECONDS
    if state == "paused":
        active = (_pid_alive(pid) and age <= PAUSED_STALE_SECONDS) if same_host else age <= STALE_SECONDS
    if not active and clean_stale:
        try:
            path.unlink()
        except OSError:
            pass
    return active


def update_lock(path: Path, **changes: object) -> dict[str, object]:
    payload = read_lock(path)
    if not payload:
        return {}
    payload.update(changes)
    payload["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return payload


class TaskLock:
    def __init__(self, path: Path, task: str) -> None:
        self.path = path
        self.task = task
        self.token = uuid.uuid4().hex
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            if self.path.exists() and lock_is_active(self.path):
                return False
            payload = {
                "pid": os.getpid(),
                "pgid": os.getpgrp(),
                "controllable": os.getpid() == os.getpgrp(),
                "hostname": socket.gethostname(),
                "task": self.task,
                "state": "running",
                "token": self.token,
                "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False))
            self.thread = threading.Thread(target=self._heartbeat, daemon=True)
            self.thread.start()
            return True
        return False

    def _heartbeat(self) -> None:
        while not self.stop.wait(20):
            payload = read_lock(self.path)
            if payload.get("token") != self.token:
                return
            try:
                self.path.touch()
            except OSError:
                return

    def release(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)
        payload = read_lock(self.path)
        if payload.get("token") == self.token:
            self.path.unlink(missing_ok=True)

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("another task is already running")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
