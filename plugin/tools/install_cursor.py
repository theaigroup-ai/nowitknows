"""Install, update, roll back, or remove the native Cursor package in an explicit Cursor profile."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nowitknows import config as C  # noqa: E402
from tools import build_cursor as B  # noqa: E402

PLUGIN_NAME = "nowitknows-cursor"
STATE_DIR = "nowitknows-installer"
STATE_FILE = "state.json"
BUNDLE_OWNER_FILE = ".nowitknows-cursor-installer.json"
RUNTIME_FILES = ("pyproject.toml", "uv.lock", "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md")
RUNTIME_DIRS = ("nowitknows", "prompts", "bin", ".cursor-plugin", "cursor")
SAFE_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "APPDATA",
    "LOCALAPPDATA",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
    "UV_PYTHON_DOWNLOADS",
    "UV_PYTHON_PREFERENCE",
    "UV_PYTHON_INSTALL_MIRROR",
    "UV_PYPY_INSTALL_MIRROR",
    "UV_INDEX",
    "UV_DEFAULT_INDEX",
    "UV_EXTRA_INDEX_URL",
    "UV_INDEX_URL",
    "UV_FIND_LINKS",
    "UV_NO_CACHE",
    "UV_OFFLINE",
    "UV_NATIVE_TLS",
    "UV_INSECURE_HOST",
    "UV_HTTP_TIMEOUT",
    "UV_CONCURRENT_DOWNLOADS",
    "UV_CONCURRENT_BUILDS",
    "UV_CONCURRENT_INSTALLS",
    "UV_LINK_MODE",
    "UV_COMPILE_BYTECODE",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
    "no_proxy",
    "NETRC",
    "__CF_USER_TEXT_ENCODING",
)
SAFE_RELEASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")


class InstallError(RuntimeError):
    """A bounded local installation failure."""


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _check_profile_data_path(path: Path, cursor_home: Path | None = None) -> None:
    profiles = set(C.resolve_cursor_paths().protected_profiles)
    if cursor_home is not None:
        profiles.add(cursor_home)
    profiles = {profile.resolve() for profile in profiles}
    for candidate in (path.absolute(), path.resolve()):
        for parent in (candidate, *candidate.parents):
            if parent.name in ("projects", "agent-transcripts", "User") and (
                parent.parent.name in (".cursor", ".claude", ".codex", "Cursor", "Claude", "Codex")
                or parent.parent.resolve() in profiles
            ):
                raise InstallError(f"Refusing to modify profile data folder: {path}")


def _remove_tree(path: Path, cursor_home: Path | None = None, *, ignore_errors: bool = False) -> None:
    _check_profile_data_path(path, cursor_home)
    shutil.rmtree(path, ignore_errors=ignore_errors)


def _bundle_owner(bundle: Path, cursor_home: Path) -> dict:
    return {"name": PLUGIN_NAME, "bundle": str(bundle.resolve()), "cursor_home": str(cursor_home.resolve())}


def _check_bundle(bundle: Path, cursor_home: Path) -> None:
    _check_profile_data_path(bundle, cursor_home)
    if bundle.is_symlink():
        raise InstallError(f"Refusing to overwrite symlinked bundle output: {bundle}")
    if not bundle.exists():
        return
    marker = bundle / BUNDLE_OWNER_FILE
    try:
        owned = not marker.is_symlink() and json.loads(marker.read_text(encoding="utf-8")) == _bundle_owner(
            bundle, cursor_home
        )
    except (OSError, ValueError):
        owned = False
    if not owned:
        raise InstallError(f"Refusing to overwrite bundle output without installer ownership: {bundle}")


def _validate_release_id(value: str) -> str:
    text = str(value or "").strip()
    if not SAFE_RELEASE.fullmatch(text) or text in (".", ".."):
        raise InstallError(f"Invalid release identifier: {value!r}")
    return text


def _python(root: Path) -> Path:
    return root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")


def _python_floor(source: Path) -> str:
    for candidate in (source / "pyproject.toml", ROOT / "pyproject.toml"):
        if candidate.is_file():
            return tomllib.loads(candidate.read_text(encoding="utf-8"))["project"]["requires-python"]
    return ">=3.14"


def _package_source(source: Path) -> Path:
    source = source.expanduser().resolve()
    if (source / ".cursor-plugin/plugin.json").is_file():
        return source
    nested = source / B.PACKAGE
    if (nested / ".cursor-plugin/plugin.json").is_file():
        return nested
    raise InstallError(f"Could not find .cursor-plugin/plugin.json in {source}")


def _state_root(cursor_home: Path) -> Path:
    return cursor_home / STATE_DIR


@contextlib.contextmanager
def _installer_lock(cursor_home: Path):
    import fcntl

    root = _state_root(cursor_home)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "install.lock").open("a", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallError("Another NowItKnows installer is using this profile. Retry after it finishes.") from exc
        yield root


def _state_file(cursor_home: Path) -> Path:
    return _state_root(cursor_home) / STATE_FILE


def _local_plugin_path(cursor_home: Path) -> Path:
    return cursor_home / "plugins/local" / PLUGIN_NAME


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:12]}")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def load_state(cursor_home: Path) -> dict:
    path = _state_file(cursor_home)
    if not path.exists():
        return {"schema_version": 1, "active": None, "history": [], "releases": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallError(f"Saved installer state is unreadable: {path}") from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("releases"), dict)
        or not isinstance(data.get("history"), list)
    ):
        raise InstallError(f"Saved installer state is malformed: {path}")
    return data


def save_state(cursor_home: Path, state: dict) -> None:
    _atomic_write(_state_file(cursor_home), json.dumps(state, indent=2, sort_keys=True) + "\n")


def install_environment(cursor_home: Path) -> dict[str, str]:
    env = {k: v for k, v in C.resolve_cursor_paths(profile=cursor_home).env.items() if k in SAFE_ENV}
    env["CURSOR_CONFIG_DIR"] = str(cursor_home)
    env["NOWITKNOWS_CURSOR_HOME"] = str(cursor_home)
    env["NOWITKNOWS_CHILD"] = "1"
    return env


def _run(argv: list[str], env: dict[str, str], cwd: Path, *, timeout: int = 300) -> str:
    try:
        done = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise InstallError(f"Required executable was not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise InstallError(f"Command timed out after {timeout}s: {' '.join(argv)}") from exc
    except OSError as exc:
        raise InstallError(f"Could not run command {argv[0]}: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()
        detail = detail.splitlines()[-1] if detail else f"exit code {done.returncode}"
        raise InstallError(f"Command failed ({' '.join(argv[:3])}): {detail}")
    return done.stdout


def _copy_runtime(source: Path, runtime: Path, cursor_home: Path) -> None:
    if runtime.exists():
        _remove_tree(runtime, cursor_home)
    runtime.mkdir(parents=True, exist_ok=True)
    for name in RUNTIME_FILES:
        candidate = source / name
        if candidate.is_file():
            shutil.copy2(candidate, runtime / name)
        elif name == "README.md":
            (runtime / name).write_text("# NowItKnows Cursor Runtime\n", encoding="utf-8")
        else:
            raise InstallError(f"Missing runtime file: {candidate}")
    for name in RUNTIME_DIRS:
        candidate = source / name
        if not candidate.is_dir():
            raise InstallError(f"Missing runtime directory: {candidate}")
        shutil.copytree(
            candidate,
            runtime / name,
            symlinks=False,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store", "node_modules"),
        )

    from nowitknows.installer_source import ARCHIVE, snapshot

    (runtime / ARCHIVE).write_bytes(snapshot(source))


def _sync_runtime(uv_bin: str, runtime: Path, env: dict[str, str]) -> None:
    _run(
        [uv_bin, "sync", "--frozen", "--python", _python_floor(runtime), "--no-dev", "--project", str(runtime)],
        env,
        runtime,
        timeout=600,
    )
    python = _python(runtime)
    if not python.is_file():
        raise InstallError(f"Runtime Python was not created at {python}")
    try:
        mark = subprocess.run(
            ["cksum"],
            stdin=(runtime / "uv.lock").open("rb"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
        (runtime / ".venv/nowitknows-ready").write_text(mark, encoding="utf-8")
    except (OSError, subprocess.SubprocessError):
        pass


def _stage_bundle(
    source: Path,
    bundle: Path,
    cursor_home: Path,
    runtime: Path,
) -> dict:
    _check_bundle(bundle, cursor_home)
    staging = bundle.parent / f".{bundle.name}.staging-{uuid.uuid4().hex[:12]}"
    try:
        receipt = B.build(
            source,
            staging,
            runtime,
            cursor_home,
            runtime_root=runtime,
        )
        _atomic_write(
            staging / BUNDLE_OWNER_FILE, json.dumps(_bundle_owner(bundle, cursor_home), sort_keys=True) + "\n"
        )
        _check_bundle(bundle, cursor_home)
        if bundle.exists():
            bundle.rename(bundle.with_name(f".{bundle.name}.archive-{uuid.uuid4().hex}"))
        bundle.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(bundle)
        return receipt
    finally:
        _remove_tree(staging, cursor_home, ignore_errors=True)


def _exchange(first: Path, second: Path) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        swap = library.renamex_np
        swap.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = swap(os.fsencode(first), os.fsencode(second), 2)
    elif sys.platform.startswith("linux"):
        swap = library.renameat2
        swap.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = swap(-100, os.fsencode(first), -100, os.fsencode(second), 2)
    else:
        raise InstallError("Atomic Cursor activation requires macOS or Linux")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _deploy_local_plugin(bundle: Path, cursor_home: Path, release: dict | None = None) -> Path:
    package = bundle / B.PACKAGE
    target = _local_plugin_path(cursor_home)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.installing-{uuid.uuid4().hex[:12]}"
    if staging.exists():
        _remove_tree(staging, cursor_home)
    retained = False
    try:
        shutil.copytree(package, staging, symlinks=False)
        if release is not None:
            _verify({**release, "installed_path": str(staging)}, cursor_home)
        if target.exists() or target.is_symlink():
            _exchange(staging, target)
            try:
                if release is not None:
                    _verify(release, cursor_home)
            except BaseException:
                _exchange(staging, target)
                raise
            retained = True
            staging.rename(target.with_name(f".{target.name}.archive-{uuid.uuid4().hex}"))
        else:
            staging.rename(target)
    finally:
        if not retained:
            _remove_tree(staging, cursor_home, ignore_errors=True)
    return target


def _verify(release: dict, cursor_home: Path) -> dict:
    runtime = Path(release["runtime"])
    bundle = Path(release["bundle"])
    installed = Path(release["installed_path"])
    python = _python(runtime)
    if not python.is_file():
        raise InstallError(f"Installed runtime Python is missing: {python}")
    if not (installed / ".cursor-plugin/plugin.json").is_file():
        raise InstallError(f"Installed Cursor plugin manifest is missing: {installed}")
    mcp = json.loads((installed / "cursor/mcp.json").read_text(encoding="utf-8"))
    server = mcp.get("mcpServers", {}).get("nowitknows", {})
    if server.get("command") != str(python):
        raise InstallError("Installed Cursor MCP config does not point to the bound runtime Python")
    hooks = json.loads((installed / "cursor/hooks.json").read_text(encoding="utf-8"))["hooks"]
    for event, entries in hooks.items():
        cmd = entries[0]["command"]
        if str(python) not in cmd or f"hook {event}" not in cmd:
            raise InstallError(f"Installed Cursor hook {event} does not point to the bound runtime")
    return {
        "bundle": str(bundle),
        "installed_path": str(installed),
        "runtime": str(runtime),
        "python": str(python),
    }


def _check_foreign_install(cursor_home: Path, state: dict) -> None:
    target = _local_plugin_path(cursor_home)
    if not target.exists() and not target.is_symlink():
        return
    if target.is_symlink():
        raise InstallError(f"Refusing to overwrite symlinked local plugin at {target}")
    managed_paths = {
        Path(r["installed_path"]).resolve()
        for r in state.get("releases", {}).values()
        if isinstance(r, dict) and isinstance(r.get("installed_path"), str)
    }
    if target.resolve() in managed_paths:
        return
    manifest_path = target / ".cursor-plugin/plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallError(f"Refusing to overwrite unrecognized plugin directory at {target}") from exc
    if manifest.get("name") != PLUGIN_NAME:
        raise InstallError(f"Refusing to overwrite foreign plugin at {target}")


def _validate_paths(source: Path, out: Path | None, cursor_home: Path) -> tuple[Path, Path, Path | None]:
    if not cursor_home.expanduser().is_absolute():
        raise ValueError("Cursor profile path must be an explicit absolute path")
    cursor_home = cursor_home.expanduser().resolve()
    _check_profile_data_path(cursor_home)
    source = _package_source(source)
    if _within(cursor_home, source):
        raise ValueError("Cursor profile path must be separate from the source tree")
    if out is not None:
        if not out.expanduser().is_absolute():
            raise ValueError("Bundle output path must be an explicit absolute path")
        _check_bundle(out.expanduser(), cursor_home)
        out = out.expanduser().resolve()
        if _within(out, source) or _within(source, out) or _within(cursor_home, out):
            raise ValueError("Bundle output path must not overlap the source tree or contain the Cursor profile")
    return source, cursor_home, out


def install(
    source: Path,
    out: Path | None,
    cursor_home: Path,
    uv_bin: str = "uv",
    *,
    dry_run: bool = False,
    sdk: bool = False,
) -> dict:
    source, cursor_home, out = _validate_paths(source, out, cursor_home)
    manifest = json.loads((source / ".cursor-plugin/plugin.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    preview_root = out or (_state_root(cursor_home) / "preview")
    _, summary = B.plan(
        source,
        preview_root / uuid.uuid4().hex,
        _state_root(cursor_home) / "releases" / version / "runtime",
        cursor_home,
    )
    release_id = _validate_release_id(f"{version}-{summary['content_sha256'][:12]}")
    release_root = _state_root(cursor_home) / "releases" / release_id
    runtime = release_root / "runtime"
    bundle = out or (release_root / "bundle")
    installed_path = _local_plugin_path(cursor_home)
    if dry_run:
        return {
            "status": "dry-run",
            "dry_run": True,
            "name": PLUGIN_NAME,
            "version": version,
            "release": release_id,
            "cursor_home": str(cursor_home),
            "bundle": str(bundle),
            "runtime": str(runtime),
            "installed_path": str(installed_path),
            "content_sha256": summary["content_sha256"],
        }

    cursor_home.mkdir(parents=True, exist_ok=True)
    with _installer_lock(cursor_home):
        state = load_state(cursor_home)
        _check_foreign_install(cursor_home, state)
        previous = state.get("active")
        env = install_environment(cursor_home)
        runtime = release_root / "runtimes" / uuid.uuid4().hex

        record = {
            "release": release_id,
            "version": version,
            "content_sha256": summary["content_sha256"],
            "bundle": str(bundle),
            "runtime": str(runtime),
            "installed_path": str(installed_path),
            "runtime_ready": False,
            "installed_at": time.time(),
        }
        from nowitknows import claude, cursor_runner

        try:
            _copy_runtime(source, runtime, cursor_home)
            _sync_runtime(uv_bin, runtime, env)
            try:
                sdk_pause = cursor_runner.install(runtime, env)
            except claude.ClaudeError as exc:
                raise InstallError(
                    f"Cursor SDK setup failed; no new runtime was activated. Any existing runtime remains active. {exc}"
                ) from exc
        except BaseException:
            _remove_tree(runtime, cursor_home, ignore_errors=True)
            raise
        record["sdk"] = True
        record["runtime_ready"] = True

        _stage_bundle(source, bundle, cursor_home, runtime)
        _deploy_local_plugin(bundle, cursor_home, record)
        verified = _verify(record, cursor_home)

        if previous and previous != release_id and previous not in state["history"]:
            state["history"].append(previous)
        state["history"] = [item for item in state["history"] if item != release_id]
        state["releases"][release_id] = record
        state["active"] = release_id
        save_state(cursor_home, state)
        return {
            "status": "installed",
            "dry_run": False,
            "name": PLUGIN_NAME,
            "version": version,
            "release": release_id,
            "previous_release": previous,
            "learning_pause": sdk_pause,
            "cursor_home": str(cursor_home),
            **verified,
        }


def _select_release(state: dict, target: str | None) -> str:
    releases = state.get("releases", {})
    if target:
        if target in releases:
            return target
        matches = [rid for rid, item in releases.items() if item.get("version") == target]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise InstallError(f"Version {target} matches multiple retained releases: {', '.join(sorted(matches))}")
        raise InstallError(f"Release {target} is not retained in this Cursor profile")
    history = [rid for rid in state.get("history", []) if rid in releases and rid != state.get("active")]
    if not history:
        raise InstallError("No previous release is retained for rollback")
    return history[-1]


def rollback(
    cursor_home: Path,
    target: str | None = None,
    uv_bin: str = "uv",
    *,
    dry_run: bool = False,
) -> dict:
    if not cursor_home.expanduser().is_absolute():
        raise ValueError("Cursor profile path must be an explicit absolute path")
    cursor_home = cursor_home.expanduser().resolve()
    state = load_state(cursor_home)
    release_id = _select_release(state, target)
    release = state["releases"][release_id]
    if dry_run:
        return {
            "status": "dry-run",
            "dry_run": True,
            "action": "rollback",
            "release": release_id,
            "version": release["version"],
            "cursor_home": str(cursor_home),
            "bundle": release["bundle"],
            "runtime": release["runtime"],
            "installed_path": release["installed_path"],
        }
    with _installer_lock(cursor_home):
        env = install_environment(cursor_home)
        runtime = Path(release["runtime"])
        bundle = Path(release["bundle"])
        if not _python(runtime).is_file() or not release.get("runtime_ready"):
            _sync_runtime(uv_bin, runtime, env)
            release["runtime_ready"] = True
            save_state(cursor_home, state)
        if not (bundle / B.PACKAGE / ".cursor-plugin/plugin.json").is_file():
            _stage_bundle(runtime, bundle, cursor_home, runtime)
        _deploy_local_plugin(bundle, cursor_home, release)
        verified = _verify(release, cursor_home)
        previous = state.get("active")
        state["history"] = [item for item in state["history"] if item != release_id]
        if previous and previous != release_id:
            state["history"].append(previous)
        state["active"] = release_id
        save_state(cursor_home, state)
        return {
            "status": "rolled_back",
            "dry_run": False,
            "release": release_id,
            "previous_release": previous,
            "version": release["version"],
            "cursor_home": str(cursor_home),
            **verified,
        }


def remove(cursor_home: Path, *, dry_run: bool = False) -> dict:
    if not cursor_home.expanduser().is_absolute():
        raise ValueError("Cursor profile path must be an explicit absolute path")
    cursor_home = cursor_home.expanduser().resolve()
    state = load_state(cursor_home) if _state_file(cursor_home).exists() else {"active": None, "releases": {}}
    active = state.get("active")
    target = _local_plugin_path(cursor_home)
    if dry_run:
        return {
            "status": "dry-run",
            "dry_run": True,
            "action": "remove",
            "active_release": active,
            "cursor_home": str(cursor_home),
            "installed_path": str(target),
        }
    with _installer_lock(cursor_home):
        _check_foreign_install(cursor_home, state)
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            _remove_tree(target, cursor_home)
        if _state_file(cursor_home).exists():
            if active:
                state["history"] = [item for item in state.get("history", []) if item != active]
                state["history"].append(active)
            state["active"] = None
            save_state(cursor_home, state)
        return {
            "status": "removed",
            "dry_run": False,
            "removed_release": active,
            "cursor_home": str(cursor_home),
            "installed_path": str(target),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--cursor-home",
        type=Path,
        default=C.resolve_cursor_paths().profile,
    )
    parser.add_argument("--uv-bin", default=shutil.which("uv") or "uv")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--rollback", nargs="?", const="", metavar="RELEASE")
    group.add_argument("--remove", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--sdk", action="store_true", help="compatibility flag; the pinned Cursor SDK is always installed"
    )
    args = parser.parse_args(argv)
    try:
        if args.remove:
            result = remove(args.cursor_home, dry_run=args.dry_run)
        elif args.rollback is not None:
            result = rollback(
                args.cursor_home,
                args.rollback or None,
                args.uv_bin,
                dry_run=args.dry_run,
            )
        else:
            result = install(
                args.source,
                args.out,
                args.cursor_home,
                args.uv_bin,
                dry_run=args.dry_run,
                sdk=args.sdk,
            )
    except (InstallError, ValueError, FileNotFoundError) as exc:
        sys.stderr.write(f"nowitknows-cursor installer error: {exc}\n")
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
