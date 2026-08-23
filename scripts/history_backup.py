#!/usr/bin/env python3
"""Create and restore small, durable backups of crawler history indexes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_BACKUP_DIR = PROJECT / "history-backups"
BACKUP_FILES = (
    Path("config/daily-sources.json"),
    Path("data/blocked-media.json"),
    Path("data/download-content-history.json"),
    Path("data/download-history.json"),
    Path("data/download-success.txt"),
    Path("data/ignored-media.json"),
    Path("data/run-history.jsonl"),
    Path("data/video-history.json"),
)


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_manifest(project: Path) -> dict[str, object]:
    files: dict[str, dict[str, object]] = {}
    for relative in BACKUP_FILES:
        source = project / relative
        if not source.is_file():
            continue
        files[relative.as_posix()] = {
            "sha256": file_digest(source),
            "bytes": source.stat().st_size,
        }
    return {
        "version": 1,
        "createdAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "files": files,
    }


def manifest_identity(manifest: dict[str, object]) -> str:
    files = manifest.get("files") if isinstance(manifest, dict) else {}
    return hashlib.sha256(
        json.dumps(files, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def read_archive_manifest(path: Path) -> dict[str, object] | None:
    try:
        with zipfile.ZipFile(path) as archive:
            payload = json.loads(archive.read("manifest.json").decode("utf-8"))
    except (OSError, UnicodeError, ValueError, KeyError, zipfile.BadZipFile):
        return None
    return payload if isinstance(payload, dict) else None


def create_backup(
    project: Path = PROJECT,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    *,
    retain: int = 7,
) -> Path | None:
    manifest = backup_manifest(project)
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    latest = backup_dir / "history-latest.zip"
    previous = read_archive_manifest(latest)
    if previous and manifest_identity(previous) == manifest_identity(manifest):
        return latest

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    destination = backup_dir / f"history-{stamp}.zip"
    fd, temporary_name = tempfile.mkstemp(prefix=".history-", suffix=".zip", dir=backup_dir)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
            for relative_name in files:
                archive.write(project / relative_name, relative_name)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        latest_temporary = latest.with_suffix(".zip.tmp")
        shutil.copyfile(destination, latest_temporary)
        os.chmod(latest_temporary, 0o600)
        os.replace(latest_temporary, latest)
    finally:
        temporary.unlink(missing_ok=True)

    archives = sorted(
        (path for path in backup_dir.glob("history-*.zip") if path.name != latest.name),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for old in archives[max(1, retain):]:
        old.unlink(missing_ok=True)
    return latest


def restore_backup(
    project: Path = PROJECT,
    archive_path: Path | None = None,
) -> list[Path]:
    source = archive_path or DEFAULT_BACKUP_DIR / "history-latest.zip"
    allowed = {relative.as_posix(): relative for relative in BACKUP_FILES}
    restored: list[Path] = []
    with zipfile.ZipFile(source) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        files = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(files, dict):
            raise ValueError("历史备份格式无效")
        validated: dict[Path, bytes] = {}
        for relative_name, metadata in files.items():
            relative = allowed.get(str(relative_name))
            if relative is None or not isinstance(metadata, dict):
                raise ValueError("历史备份包含未知文件")
            content = archive.read(relative_name)
            if len(content) != int(metadata.get("bytes") or -1):
                raise ValueError(f"历史备份大小校验失败: {relative_name}")
            if hashlib.sha256(content).hexdigest() != str(metadata.get("sha256") or ""):
                raise ValueError(f"历史备份校验失败: {relative_name}")
            validated[relative] = content
        for relative, content in validated.items():
            destination = project / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".restore.tmp")
            temporary.write_bytes(content)
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
            restored.append(destination)
    return restored


def main() -> int:
    parser = argparse.ArgumentParser(description="备份或恢复抓取历史索引")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--restore-latest", action="store_true")
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    if args.restore_latest or args.archive:
        archive = args.archive or args.backup_dir / "history-latest.zip"
        restored = restore_backup(PROJECT, archive)
        print(f"restored {len(restored)} history files from {archive}")
        return 0
    destination = create_backup(PROJECT, args.backup_dir)
    if destination:
        print(f"backed up history to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
