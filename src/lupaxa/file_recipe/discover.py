"""Filesystem discovery that stays inside the requested roots."""

from __future__ import annotations

import os
from pathlib import Path

from lupaxa.file_recipe.errors import FilesystemError, LimitError, RecipeError
from lupaxa.file_recipe.models import FileInfo

_ORDER = {"path", "filename", "relative", "size", "mtime", "ctime"}


def discover(
    roots: list[Path],
    *,
    recursive: bool,
    include_hidden: bool,
    follow_symlinks: bool,
    max_depth: int,
    same_filesystem: bool,
    include_directories: bool,
    includes: list[str],
    excludes: list[str],
    exclude_dirs: list[Path],
    max_files: int,
    max_total_bytes: int,
    order: str,
    stdin_paths: list[Path] | None = None,
) -> tuple[list[FileInfo], list[str]]:
    """Discover files. Symlinks are not followed unless that is explicit."""
    if order not in _ORDER:
        raise RecipeError(f"unsupported order {order!r}")
    if not recursive:
        max_depth = 1
    warnings: list[str] = []
    found: list[FileInfo] = []
    total = 0
    excluded = [path.absolute() for path in exclude_dirs]
    if stdin_paths is not None:
        for path in stdin_paths:
            info = _info(path, path.parent.absolute(), follow_symlinks)
            if info is None:
                raise FilesystemError(f"path not found: {path}")
            _consider(
                info,
                found,
                includes,
                excludes,
                excluded,
                include_hidden,
                include_directories,
                same_filesystem,
                info.root,
                warnings,
            )
            total += info.size
            _limits(found, total, max_files, max_total_bytes)
        return _sort(found, order), warnings
    for root in roots:
        absolute = root.absolute()
        if not absolute.exists():
            raise FilesystemError(f"input not found: {root}")
        root_device = absolute.stat().st_dev
        seen: set[tuple[int, int]] = set()
        _walk(
            absolute,
            absolute,
            root_device,
            depth=1,
            recursive=recursive,
            include_hidden=include_hidden,
            follow_symlinks=follow_symlinks,
            max_depth=max_depth,
            same_filesystem=same_filesystem,
            include_directories=include_directories,
            includes=includes,
            excludes=excludes,
            excluded=excluded,
            found=found,
            seen=seen,
            warnings=warnings,
            total_box=[total],
            max_files=max_files,
            max_total_bytes=max_total_bytes,
        )
        total = sum(item.size for item in found if item.root == str(absolute) or True)
    return _sort(found, order), warnings


def _walk(
    current: Path,
    root: Path,
    root_device: int,
    *,
    depth: int,
    recursive: bool,
    include_hidden: bool,
    follow_symlinks: bool,
    max_depth: int,
    same_filesystem: bool,
    include_directories: bool,
    includes: list[str],
    excludes: list[str],
    excluded: list[Path],
    found: list[FileInfo],
    seen: set[tuple[int, int]],
    warnings: list[str],
    total_box: list[int],
    max_files: int,
    max_total_bytes: int,
) -> None:
    try:
        entries = list(current.iterdir())
    except OSError as exc:
        raise FilesystemError(f"cannot read {current}: {exc}") from exc
    for entry in entries:
        if _hidden(entry) and not include_hidden:
            continue
        if _under_any(entry, excluded):
            continue
        if max_depth and depth > max_depth:
            continue
        is_link = entry.is_symlink()
        if is_link and not follow_symlinks:
            info = _info(entry, root, False)
            if info is not None:
                _consider(
                    info,
                    found,
                    includes,
                    excludes,
                    excluded,
                    include_hidden,
                    include_directories,
                    same_filesystem,
                    str(root),
                    warnings,
                    root_device,
                )
            continue
        if is_link and follow_symlinks:
            try:
                target = entry.resolve()
            except OSError as exc:
                raise FilesystemError(f"cannot resolve symlink {entry}: {exc}") from exc
            if not _inside(root, target):
                raise RecipeError(f"symlink escapes the input root: {entry}")
            try:
                stat = entry.stat()
            except OSError as exc:
                raise FilesystemError(f"cannot stat {entry}: {exc}") from exc
            ident = (stat.st_dev, stat.st_ino)
            if ident in seen:
                raise RecipeError(f"symlink cycle at {entry}")
            seen.add(ident)
        info = _info(entry, root, follow_symlinks)
        if info is None:
            continue
        added = _consider(
            info,
            found,
            includes,
            excludes,
            excluded,
            include_hidden,
            include_directories,
            same_filesystem,
            str(root),
            warnings,
            root_device,
        )
        if added:
            total_box[0] += info.size
            _limits(found, total_box[0], max_files, max_total_bytes)
        descend = max_depth == 0 or depth < max_depth
        if entry.is_dir() and not (is_link and not follow_symlinks) and descend:
            _walk(
                entry if not (is_link and follow_symlinks) else entry.resolve(),
                root,
                root_device,
                depth=depth + 1,
                recursive=recursive,
                include_hidden=include_hidden,
                follow_symlinks=follow_symlinks,
                max_depth=max_depth,
                same_filesystem=same_filesystem,
                include_directories=include_directories,
                includes=includes,
                excludes=excludes,
                excluded=excluded,
                found=found,
                seen=seen,
                warnings=warnings,
                total_box=total_box,
                max_files=max_files,
                max_total_bytes=max_total_bytes,
            )


def _consider(
    info: FileInfo,
    found: list[FileInfo],
    includes: list[str],
    excludes: list[str],
    excluded: list[Path],
    include_hidden: bool,
    include_directories: bool,
    same_filesystem: bool,
    root: str,
    warnings: list[str],
    root_device: int | None = None,
) -> bool:
    path = Path(info.path)
    if _under_any(path, excluded):
        return False
    if not include_directories and info.is_dir and not info.is_symlink:
        return False
    if same_filesystem and root_device is not None:
        try:
            device = path.stat().st_dev
        except OSError:
            device = root_device
        if device != root_device:
            warnings.append(f"skipped {info.relative} on another filesystem")
            return False
    relative = info.relative
    if excludes and (_match_any(relative, excludes) or _match_any(path.name, excludes)):
        return False
    if includes and not (_match_any(relative, includes) or _match_any(path.name, includes)):
        return False
    if any(existing.path == info.path for existing in found):
        return False
    if info.nlink > 1 and info.is_file:
        warnings.append(f"hard link {info.relative} has {info.nlink} names")
    found.append(info)
    return True


def _info(path: Path, root: Path, follow: bool) -> FileInfo | None:
    try:
        stat = path.stat() if follow else path.lstat()
    except OSError:
        return None
    absolute = path if path.is_absolute() else path.absolute()
    try:
        relative = absolute.relative_to(root.absolute()).as_posix()
    except ValueError:
        relative = absolute.name
    is_link = path.is_symlink()
    is_dir = stat.st_mode & 0o170000 == 0o040000 if not follow else path.is_dir()
    if follow:
        is_dir = path.is_dir()
    return FileInfo(
        path=str(absolute),
        relative=relative,
        root=str(root.absolute()),
        size=int(stat.st_size),
        mtime_ns=int(stat.st_mtime_ns),
        ctime_ns=int(getattr(stat, "st_ctime_ns", int(stat.st_ctime * 1_000_000_000))),
        atime_ns=int(getattr(stat, "st_atime_ns", int(stat.st_atime * 1_000_000_000))),
        mode=int(stat.st_mode),
        uid=int(getattr(stat, "st_uid", 0)),
        nlink=int(stat.st_nlink),
        is_dir=bool(is_dir) and not is_link,
        is_file=path.is_file() if follow else (not is_link and path.is_file()),
        is_symlink=is_link,
    )


def _inside(root: Path, path: Path) -> bool:
    try:
        path.absolute().relative_to(root.absolute())
    except ValueError:
        return False
    return True


def _under_any(path: Path, roots: list[Path]) -> bool:
    absolute = path.absolute()
    for root in roots:
        try:
            absolute.relative_to(root.absolute())
        except ValueError:
            continue
        return True
    return False


def _hidden(path: Path) -> bool:
    return any(part.startswith(".") and part not in {".", ".."} for part in path.parts)


def _match_any(value: str, patterns: list[str]) -> bool:
    import fnmatch

    return any(fnmatch.fnmatch(value, pattern) for pattern in patterns)


def _limits(found: list[FileInfo], total: int, max_files: int, max_total_bytes: int) -> None:
    if max_files and len(found) > max_files:
        raise LimitError(f"file count exceeds max_files ({max_files})")
    if max_total_bytes and total > max_total_bytes:
        raise LimitError(f"total size exceeds max_total_bytes ({max_total_bytes} bytes)")


def _sort(found: list[FileInfo], order: str) -> list[FileInfo]:
    if order == "filename":
        return sorted(found, key=lambda item: (Path(item.path).name.casefold(), item.relative))
    if order == "relative":
        return sorted(found, key=lambda item: item.relative.casefold())
    if order == "size":
        return sorted(found, key=lambda item: (item.size, item.relative))
    if order == "mtime":
        return sorted(found, key=lambda item: (item.mtime_ns, item.relative))
    if order == "ctime":
        return sorted(found, key=lambda item: (item.ctime_ns, item.relative))
    return sorted(found, key=lambda item: item.relative)


def read_stdin_paths(text: str, cwd: Path) -> list[Path]:
    """Parse a newline-separated path list."""
    paths: list[Path] = []
    for line in text.splitlines():
        item = line.strip()
        if not item:
            continue
        path = Path(item)
        paths.append(path if path.is_absolute() else cwd / path)
    if not paths:
        raise RecipeError("stdin did not contain any paths")
    return paths


def stat_device(path: str) -> int:
    """Return the device id, following the path."""
    return os.stat(path).st_dev
