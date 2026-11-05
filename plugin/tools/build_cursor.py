"""Build a deterministic native Cursor plugin bundle from the shared NowItKnows source tree."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import stat
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path("plugins/nowitknows-cursor")
SDK_FILES = tuple(
    Path("cursor/sdk_runner") / name for name in ("package.json", "package-lock.json", "core.mjs", "runner.mjs")
)
DIRECT_FILES = (
    Path(".cursor-plugin/plugin.json"),
    Path("cursor/mcp.json"),
    Path("cursor/hooks.json"),
    Path("bin/nowitknows_cursor.py"),
    Path("bin/nowitknows.sh"),
    Path("bin/bootstrap.sh"),
    Path("pyproject.toml"),
    Path("uv.lock"),
    Path("LICENSE"),
    Path("THIRD_PARTY_NOTICES.md"),
)
EXECUTABLE_FILES = frozenset((Path("bin/nowitknows.sh"), Path("bin/bootstrap.sh")))
DIRECT_DIRS = (
    Path("cursor/skills"),
    Path("nowitknows"),
    Path("prompts"),
)
FORBIDDEN_NAMES = {"__pycache__", ".DS_Store", ".git", ".venv", ".pytest_cache", ".ruff_cache"}
FORBIDDEN_PARTS = {
    ".claude",
    ".claude-plugin",
    ".codex-plugin",
    "codex",
    "commands",
    "hooks",
    "tests",
    "private",
    "history",
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _python(root: Path) -> Path:
    return root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")


def _check_paths(source: Path, destination: Path) -> tuple[Path, Path]:
    if not source.is_dir():
        raise ValueError(f"Source directory does not exist: {source}")
    raw_source = source.expanduser()
    raw_dest = destination.expanduser()
    if _within(raw_dest, raw_source) or _within(raw_source, raw_dest):
        raise ValueError("Output directory must not overlap the source tree")
    source = raw_source.resolve()
    destination = raw_dest.resolve()
    if _within(destination, source) or _within(source, destination):
        raise ValueError("Output directory must not overlap the source tree")
    if destination.exists():
        raise ValueError(f"Refusing to overwrite existing output directory: {destination}")
    return source, destination


def inventory(source: Path) -> list[Path]:
    files: list[Path] = []
    if (source / "cursor/sdk_runner").exists():
        for relative in SDK_FILES:
            if (source / relative).is_symlink() or not (source / relative).is_file():
                raise ValueError(f"Missing or linked Cursor SDK helper: {relative}")
            files.append(relative)
    for relative in DIRECT_FILES:
        candidate = source / relative
        if candidate.is_symlink():
            raise ValueError(f"Linked package input is not allowed: {relative}")
        if not candidate.is_file():
            raise FileNotFoundError(f"Missing required Cursor package file: {relative}")
        files.append(relative)
    for directory in DIRECT_DIRS:
        base = source / directory
        if base.is_symlink():
            raise ValueError(f"Linked package directory is not allowed: {directory}")
        if not base.is_dir():
            raise FileNotFoundError(f"Missing required Cursor package directory: {directory}")
        for root, dirs, names in os.walk(base, followlinks=False):
            current = Path(root)
            for entry in [*dirs, *names]:
                candidate = current / entry
                if candidate.is_symlink():
                    raise ValueError(f"Linked package input is not allowed: {candidate.relative_to(source)}")
            dirs[:] = sorted(
                d for d in dirs if d not in FORBIDDEN_NAMES and not d.startswith(".") and d not in FORBIDDEN_PARTS
            )
            for name in sorted(names):
                if name in FORBIDDEN_NAMES or name.startswith(".") or name.endswith((".pyc", ".pyo")):
                    continue
                path = current / name
                rel = path.relative_to(source)
                if any(part in FORBIDDEN_PARTS for part in rel.parts):
                    continue
                files.append(rel)
    return sorted(files, key=lambda item: item.as_posix())


def _render_entry(cursor_home: Path | None, runtime_root: Path | None) -> bytes | None:
    if cursor_home is None or runtime_root is None:
        return None
    lines = [
        "from __future__ import annotations",
        "",
        "import os",
        "import sys",
        "from pathlib import Path",
        "",
        "",
        "def main(argv: list[str] | None = None) -> int:",
        "    args = sys.argv[1:] if argv is None else argv",
        '    hook = args[:1] == ["hook"]',
        "    os.environ['NOWITKNOWS_HOST'] = 'cursor'",
        f"    sys.path.insert(0, {str(runtime_root)!r})",
        "    try:",
        "        from nowitknows import config as C",
        f"        os.environ.update(C.resolve_cursor_paths(default_profile=Path({str(cursor_home)!r})).env)",
        "        from nowitknows.cursor_host import main as host_main",
        "",
        "        result = host_main(args)",
        "        return 0 if hook else (result or 0)",
        "    except (Exception, SystemExit):",
        "        if hook:",
        "            return 0",
        "        raise",
        "",
        "",
        'if __name__ == "__main__":',
        "    raise SystemExit(main())",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _load_config(source: Path):
    pkg = f"nowitknows_build_cursor_{uuid.uuid4().hex[:8]}"
    pkg_spec = importlib.util.spec_from_loader(pkg, loader=None, is_package=True)
    if pkg_spec is None:
        raise ValueError("Could not create temporary package spec")
    pkg_mod = importlib.util.module_from_spec(pkg_spec)
    pkg_mod.__path__ = [str(source / "nowitknows")]
    sys.modules[pkg] = pkg_mod
    try:
        for sub in ("legacy", "config"):
            spec = importlib.util.spec_from_file_location(f"{pkg}.{sub}", source / "nowitknows" / f"{sub}.py")
            if spec is None or spec.loader is None:
                raise ValueError(f"Could not load nowitknows/{sub}.py from source")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            setattr(pkg_mod, sub, module)
            spec.loader.exec_module(module)
        return pkg_mod.config
    finally:
        for k in [k for k in sys.modules if k == pkg or k.startswith(f"{pkg}.")]:
            sys.modules.pop(k, None)


def _render_hooks(raw: bytes, runtime_root: Path | None, cursor_home: Path | None) -> bytes:
    hooks = json.loads(raw.decode("utf-8"))
    groups = hooks.get("hooks") if isinstance(hooks, dict) else None
    if not isinstance(groups, dict) or not groups:
        raise ValueError("Cursor hooks.json must define hooks")
    for event, entries in groups.items():
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"Cursor hooks.json event {event!r} must have a handler")
        for handler in entries:
            if not isinstance(handler, dict) or not isinstance(handler.get("command"), str):
                raise ValueError(f"Cursor hooks.json event {event!r} has a malformed command")
            if runtime_root is not None and cursor_home is not None:
                handler["command"] = " ".join(
                    [
                        f"NOWITKNOWS_CURSOR_HOME={shlex.quote(str(cursor_home))}",
                        "NOWITKNOWS_HOST=cursor",
                        shlex.quote(str(_python(runtime_root))),
                        shlex.quote(str(runtime_root / "bin/nowitknows_cursor.py")),
                        "hook",
                        event,
                    ]
                )
    return (json.dumps(hooks, indent=2) + "\n").encode("utf-8")


def _render_mcp(raw: bytes, runtime_root: Path | None, cursor_home: Path | None) -> bytes:
    mcp = json.loads(raw.decode("utf-8"))
    servers = mcp.get("mcpServers") if isinstance(mcp, dict) else None
    if not isinstance(servers, dict) or set(servers) != {"nowitknows"}:
        raise ValueError("Cursor mcp.json must declare only the nowitknows MCP server")
    if runtime_root is not None:
        if not runtime_root.is_absolute():
            raise ValueError("Runtime root must be an absolute path")
        if cursor_home is None or not cursor_home.is_absolute():
            raise ValueError("Cursor profile path must be an explicit absolute path when binding a runtime root")
        servers["nowitknows"] = {
            "command": str(_python(runtime_root)),
            "args": [str(runtime_root / "bin/nowitknows_cursor.py"), "mcp"],
            "env": {
                "CURSOR_CONFIG_DIR": str(cursor_home),
                "NOWITKNOWS_CURSOR_HOME": str(cursor_home),
                "NOWITKNOWS_HOST": "cursor",
            },
        }
    else:
        server = servers["nowitknows"]
        if server.get("command") != "/bin/sh" or server.get("args") != [
            "${CURSOR_PLUGIN_ROOT}/bin/nowitknows.sh",
            "--cursor",
            "mcp",
        ]:
            raise ValueError("Source cursor/mcp.json must launch ${CURSOR_PLUGIN_ROOT}/bin/nowitknows.sh --cursor mcp")
    return (json.dumps(mcp, indent=2) + "\n").encode("utf-8")


def _validate_manifest(raw: bytes) -> dict:
    manifest = json.loads(raw.decode("utf-8"))
    if manifest.get("name") != "nowitknows-cursor":
        raise ValueError("Cursor plugin manifest name must be nowitknows-cursor")
    if manifest.get("hooks") != "./cursor/hooks.json":
        raise ValueError("Cursor plugin manifest must point to ./cursor/hooks.json")
    if manifest.get("skills") != "./cursor/skills/":
        raise ValueError("Cursor plugin manifest must point to ./cursor/skills/")
    if manifest.get("mcpServers") != "./cursor/mcp.json":
        raise ValueError("Cursor plugin manifest must point to ./cursor/mcp.json")
    return manifest


def plan(
    source: Path,
    destination: Path,
    installed_cache: Path | None = None,
    cursor_home: Path | None = None,
    *,
    runtime_root: Path | None = None,
) -> tuple[dict[Path, bytes], dict]:
    source, destination = _check_paths(source, destination)
    _load_config(source)
    selected_runtime = runtime_root if runtime_root is not None else installed_cache
    if selected_runtime is not None:
        selected_runtime = selected_runtime.expanduser()
        if not selected_runtime.is_absolute():
            raise ValueError("Runtime root must be an absolute path")
        if cursor_home is None:
            raise ValueError("Cursor profile path is required when binding a runtime root")
    if cursor_home is not None:
        cursor_home = cursor_home.expanduser()
        if not cursor_home.is_absolute():
            raise ValueError("Cursor profile path must be an absolute path")
        if selected_runtime is None:
            raise ValueError("Runtime or cache path is required when binding a Cursor profile")

    files = inventory(source)
    manifest = _validate_manifest((source / ".cursor-plugin/plugin.json").read_bytes())
    rendered_entry = _render_entry(cursor_home, selected_runtime)

    bundle_files: dict[Path, bytes] = {}
    source_hashes: dict[str, str] = {}
    for relative in files:
        raw = (source / relative).read_bytes()
        source_hashes[relative.as_posix()] = digest(raw)
        if relative == Path("cursor/mcp.json"):
            raw = _render_mcp(raw, selected_runtime, cursor_home)
        elif relative == Path("cursor/hooks.json"):
            raw = _render_hooks(raw, selected_runtime, cursor_home)
        elif relative == Path("bin/nowitknows_cursor.py") and rendered_entry is not None:
            raw = rendered_entry
        elif relative.name == "SKILL.md" and selected_runtime is not None and cursor_home is not None:
            text = raw.decode("utf-8")
            paragraphs = text.split("\n\n")
            if len(paragraphs) > 1 and paragraphs[1].startswith("Use the installed copy of this skill"):
                paragraphs[1] = (
                    f"Use the preserved release runtime at `{selected_runtime}` as the plugin root. "
                    f"Run its `bin/nowitknows_cursor.py` with its `.venv/bin/python`, setting "
                    f"`CURSOR_CONFIG_DIR` and `NOWITKNOWS_CURSOR_HOME` to `{cursor_home}`. "
                    "Pass each argument separately, keep paths intact, and keep the current working folder."
                )
                raw = "\n\n".join(paragraphs).encode("utf-8")
        bundle_files[PACKAGE / relative] = raw

    from nowitknows.installer_source import ARCHIVE, snapshot

    bundle_files[PACKAGE / ARCHIVE] = snapshot(source)
    output_hashes = {path.as_posix(): digest(data) for path, data in sorted(bundle_files.items())}
    summary = {
        "schema_version": 1,
        "name": manifest["name"],
        "version": manifest["version"],
        "package_root": PACKAGE.as_posix(),
        "installed_cache": str(installed_cache) if installed_cache else None,
        "runtime_root": str(selected_runtime) if selected_runtime else None,
        "cursor_home": str(cursor_home) if cursor_home else None,
        "files": output_hashes,
        "sources": source_hashes,
    }
    summary["content_sha256"] = digest(json.dumps(output_hashes, sort_keys=True).encode("utf-8"))
    bundle_files[Path("build-manifest.json")] = (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return bundle_files, summary


def build(
    source: Path,
    destination: Path,
    installed_cache: Path | None = None,
    cursor_home: Path | None = None,
    *,
    runtime_root: Path | None = None,
) -> dict:
    bundle_files, summary = plan(
        source,
        destination,
        installed_cache,
        cursor_home,
        runtime_root=runtime_root,
    )
    destination = destination.expanduser().resolve()
    staging = destination.parent / f".{destination.name}.building-{uuid.uuid4().hex[:12]}"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        for relative, content in bundle_files.items():
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            if relative.is_relative_to(PACKAGE) and relative.relative_to(PACKAGE) in EXECUTABLE_FILES:
                target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--installed-cache", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--cursor-home", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        _, summary = plan(
            args.source,
            args.out,
            args.installed_cache,
            args.cursor_home,
            runtime_root=args.runtime_root,
        )
    else:
        summary = build(
            args.source,
            args.out,
            args.installed_cache,
            args.cursor_home,
            runtime_root=args.runtime_root,
        )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
