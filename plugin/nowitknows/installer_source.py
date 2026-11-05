"""Carry deterministic installer inputs with native runtimes, independent of the development checkout."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import zipfile
from pathlib import Path

ARCHIVE = Path("nowitknows/installer-source.zip")
FILES = (
    "pyproject.toml",
    "uv.lock",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "bin/nowitknows.sh",
    "bin/bootstrap.sh",
    "bin/nowitknows_codex.py",
    "bin/nowitknows_cursor.py",
    ".codex-plugin/plugin.json",
    ".codex-plugin/marketplace.json",
    ".cursor-plugin/plugin.json",
    "codex/hooks.json",
    "cursor/hooks.json",
    "cursor/mcp.json",
    "cursor/sdk_runner/package.json",
    "cursor/sdk_runner/package-lock.json",
    "cursor/sdk_runner/core.mjs",
    "cursor/sdk_runner/runner.mjs",
    "tools/build_codex.py",
    "tools/install_codex.py",
    "tools/build_cursor.py",
    "tools/install_cursor.py",
)


def snapshot(source: Path, extra: tuple[str, ...] = ()) -> bytes:
    if (source / ARCHIVE).is_symlink():
        raise ValueError("Linked installer source is not supported")
    if (source / ARCHIVE).is_file():
        return (source / ARCHIVE).read_bytes()
    paths = [Path(name) for name in (*FILES, *extra) if (source / name).is_file()]
    paths.extend(p.relative_to(source) for p in (source / "nowitknows").glob("*.py"))
    paths.extend(p.relative_to(source) for p in (source / "prompts").glob("*.md"))
    for host in ("codex", "cursor"):
        paths.extend(p.relative_to(source) for p in (source / host / "skills").glob("*/SKILL.md"))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in sorted(set(paths)):
            path = source / relative
            if any(p.is_symlink() for p in (path, *path.parents) if p != source and source in p.parents):
                raise ValueError(f"Linked installer source is not supported: {relative}")
            info = zipfile.ZipInfo(relative.as_posix())
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    return output.getvalue()


@contextlib.contextmanager
def materialize(source: Path):
    if not (source / ARCHIVE).is_file():
        yield source
        return
    with tempfile.TemporaryDirectory(prefix="nowitknows-setup-") as temporary:
        root = Path(temporary)
        with zipfile.ZipFile(source / ARCHIVE) as archive:
            archive.extractall(root)
        sys.path.insert(0, str(root))
        try:
            yield root
        finally:
            sys.path.remove(str(root))
