"""Workdir isolation strategies — hardlink (default), copy (fallback).

A 9.9 GB UE template costs ~7841 directory entries when hardlinked instead
of full copy bytes. Each file's data is shared via the inode; only files the
agent *creates* consume new disk.

Caveat: NTFS hardlinks share inodes, so an in-place rewrite of an existing
file (open-truncate-write-close) would corrupt the source template. We
mitigate this in two ways:
  1. Mark all hardlinked files read-only at clone time (best-effort).
  2. Optional manifest of file hashes (sha256) so post-run validation can
     detect any divergence between workdir and source template.

Most agents ADD files (new C++, new Content/...). For tasks that modify
existing files, prefer copy mode.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import shutil
import stat
import time
from fnmatch import fnmatch


DEFAULT_EXCLUDE_DIRS = frozenset({
    "Binaries", "Intermediate", "Saved", "DerivedDataCache",
    ".git", ".vs", ".claude",
})
DEFAULT_EXCLUDE_GLOBS = ("*.pdb", "*.exe")


@dataclasses.dataclass
class CloneStats:
    files_linked: int = 0
    files_copied: int = 0
    files_skipped: int = 0
    dirs_created: int = 0
    bytes_logical: int = 0   # sum of file sizes (what a copy would cost)
    bytes_physical: int = 0  # actual new bytes on disk (~ 0 for pure hardlink)
    seconds: float = 0.0
    fallback_to_copy: bool = False
    error: str | None = None


def _is_same_volume(a: pathlib.Path, b: pathlib.Path) -> bool:
    """Hardlinks require same volume; on Windows compare drive letters."""
    if os.name == "nt":
        return str(a.resolve())[:2].upper() == str(b.resolve())[:2].upper()
    return a.stat().st_dev == b.stat().st_dev


def _winlong(p: pathlib.Path | str) -> str:
    """Prefix a path with \\?\\ on Windows to bypass MAX_PATH.

    UE asset folders frequently exceed 260 chars; without this prefix
    os.link / shutil.copy2 fail with WinError 3 on those files.
    """
    s = str(p)
    if os.name != "nt":
        return s
    if s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):              # \\server\share UNC
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def hardlink_clone(src: pathlib.Path, dst: pathlib.Path,
                   exclude_dirs: frozenset[str] = DEFAULT_EXCLUDE_DIRS,
                   exclude_globs: tuple[str, ...] = DEFAULT_EXCLUDE_GLOBS,
                   make_readonly: bool = True) -> CloneStats:
    """Clone src -> dst using NTFS/Linux hardlinks where possible.

    Falls back to copy on individual cross-device failures. dst must not
    exist yet (we create it).
    """
    t0 = time.time()
    stats = CloneStats()
    src = src.resolve()
    dst = dst.resolve()

    if dst.exists():
        raise FileExistsError(f"workdir already exists: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)

    if not _is_same_volume(src, dst.parent):
        stats.fallback_to_copy = True

    for root, dirs, files in os.walk(src):
        rel = pathlib.Path(root).relative_to(src)
        # filter excluded dirs in-place so os.walk doesn't recurse into them
        dirs[:] = [d for d in dirs if d not in exclude_dirs]

        target_root = dst / rel
        os.makedirs(_winlong(target_root), exist_ok=True)
        stats.dirs_created += 1

        for fname in files:
            if any(fnmatch(fname, pat) for pat in exclude_globs):
                stats.files_skipped += 1
                continue

            src_file = pathlib.Path(root) / fname
            dst_file = target_root / fname
            src_long = _winlong(src_file)
            dst_long = _winlong(dst_file)
            try:
                size = os.path.getsize(src_long)
            except OSError:
                stats.files_skipped += 1
                continue

            stats.bytes_logical += size

            if stats.fallback_to_copy:
                try:
                    shutil.copy2(src_long, dst_long)
                    stats.files_copied += 1
                    stats.bytes_physical += size
                except OSError as exc:
                    stats.error = f"copy fallback failed for {src_file}: {exc}"
                    return stats
            else:
                try:
                    os.link(src_long, dst_long)
                    stats.files_linked += 1
                    if make_readonly:
                        try:
                            os.chmod(dst_long, stat.S_IREAD)
                        except OSError:
                            pass
                except OSError:
                    try:
                        shutil.copy2(src_long, dst_long)
                        stats.files_copied += 1
                        stats.bytes_physical += size
                    except OSError as exc:
                        stats.error = f"copy fallback failed for {src_file}: {exc}"
                        return stats

    stats.seconds = time.time() - t0
    return stats


def write_manifest(workdir: pathlib.Path, output: pathlib.Path,
                   exclude_globs: tuple[str, ...] = DEFAULT_EXCLUDE_GLOBS) -> int:
    """Write a sha256 manifest of files in `workdir` to `output`. Returns count."""
    entries: dict[str, str] = {}
    for root, _dirs, files in os.walk(workdir):
        for fname in files:
            if any(fnmatch(fname, pat) for pat in exclude_globs):
                continue
            p = pathlib.Path(root) / fname
            rel = p.relative_to(workdir).as_posix()
            try:
                with open(p, "rb") as fh:
                    h = hashlib.sha256()
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                    entries[rel] = h.hexdigest()
            except OSError:
                continue
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(entries, sort_keys=True), encoding="utf-8")
    return len(entries)


def diff_against_manifest(workdir: pathlib.Path, manifest: pathlib.Path
                          ) -> dict[str, list[str]]:
    """Compare current workdir contents against an earlier manifest.

    Returns {added: [...], removed: [...], modified: [...]} (relative posix paths).
    """
    expected = json.loads(manifest.read_text(encoding="utf-8"))
    expected_set = set(expected.keys())
    seen: dict[str, str] = {}
    for root, _dirs, files in os.walk(workdir):
        for fname in files:
            if any(fnmatch(fname, pat) for pat in DEFAULT_EXCLUDE_GLOBS):
                continue
            p = pathlib.Path(root) / fname
            rel = p.relative_to(workdir).as_posix()
            try:
                with open(p, "rb") as fh:
                    h = hashlib.sha256()
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                    seen[rel] = h.hexdigest()
            except OSError:
                continue

    added = sorted(set(seen) - expected_set)
    removed = sorted(expected_set - set(seen))
    modified = sorted(rel for rel in expected_set & set(seen) if seen[rel] != expected[rel])
    return {"added": added, "removed": removed, "modified": modified}
