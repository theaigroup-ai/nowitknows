from __future__ import annotations

import argparse
import contextlib
import json
import ntpath
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from os.path import realpath
from pathlib import Path, PureWindowsPath

if __package__:
    from .build_codex import (
        CATALOG,
        PACKAGE,
        PLUGIN,
        ROOT,
        build,
        digest,
        encoded,
        plan,
        powershell,
        ps_environment,
        ps_quote,
        runtime_python,
        separate,
    )
else:
    from build_codex import (
        CATALOG,
        PACKAGE,
        PLUGIN,
        ROOT,
        build,
        digest,
        encoded,
        plan,
        powershell,
        ps_environment,
        ps_quote,
        runtime_python,
        separate,
    )

sys.path.insert(0, str(ROOT))
from nowitknows import config as C
from nowitknows.state import _replace, file_lock

MARKET = "nowitknows-codex-local"
SELECTOR = f"{PLUGIN}@{MARKET}"
SAFE_ENV = (
    "PATH",
    "HOME",
    "USERPROFILE",
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
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "CODEX_CA_CERTIFICATE",
    "__CF_USER_TEXT_ENCODING",
    # the user's uv settings for getting Python, so the runtime sync honors them; and proxies, which downloads need
    "UV_PYTHON_DOWNLOADS",
    "UV_PYTHON_INSTALL_DIR",
    "UV_PYTHON_INSTALL_MIRROR",
    "UV_PYTHON_PREFERENCE",
    "UV_NATIVE_TLS",
    "UV_CACHE_DIR",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
    "no_proxy",
)


def install_environment(codex_home: Path) -> dict[str, str]:
    return C.dual_env(
        {
            **{k: os.environ[k] for k in SAFE_ENV if k in os.environ},
            "CODEX_HOME": str(codex_home),
            "NOWITKNOWS_CHILD": "1",
        }
    )


def run(command: list[str], cwd: Path, env: dict[str, str]) -> dict:
    step = shlex.join(command)
    # Windows decodes text pipes in reader threads, where UnicodeError can be lost as stdout=None.
    output = {} if C.WINDOWS else {"text": True, "encoding": "utf-8"}
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=C.python_env(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            **output,
        )
        if C.WINDOWS:
            result.stdout = result.stdout.decode("utf-8")
            result.stderr = result.stderr.decode("utf-8")
    except (OSError, UnicodeError) as error:
        if not C.WINDOWS:
            raise
        raise ValueError(f"{step} failed: {error}") from error
    if result.returncode:
        detail = result.stderr.strip() or (result.stdout.strip() if C.WINDOWS else "")
        raise ValueError(f"{step} failed ({result.returncode}): {detail}")
    if command[1:2] == ["sync"] or (C.WINDOWS and command[1:3] == ["-m", "compileall"]):
        return {}
    try:
        value = json.loads(result.stdout)
    except ValueError as error:
        if not C.WINDOWS:
            raise
        detail = "empty stdout" if not result.stdout.strip() else "invalid JSON on stdout"
        raise ValueError(f"{step} failed: {detail}") from error
    if not isinstance(value, dict):
        if C.WINDOWS:
            raise ValueError(f"{step} failed: expected a JSON object")
        raise ValueError("The native CLI returned an unexpected response. Installation stopped.")
    return value


def sync_command(uv: str, package: Path) -> list[str]:
    return [uv, "sync", "--frozen", "--no-dev", "--project", str(package)]


def profile_path(value: Path | None = None) -> Path:
    selected = value if value is not None else Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    if not selected.is_absolute():
        raise ValueError("Select the exact Codex profile with an absolute --codex-home or CODEX_HOME path.")
    return selected.resolve()


def discover_binary(explicit: str | None, kind: str) -> str:
    selected = explicit or (C.env("NOWITKNOWS_CODEX_BIN", C.environment()) if kind == "codex" else None)
    if selected:
        found = shutil.which(selected)
        if found is None:
            raise ValueError(f"Selected {kind} binary is unavailable: {selected}")
        return str(Path(found).absolute())
    found = shutil.which(kind)
    if found:
        return str(Path(found).absolute())
    if kind == "codex":
        candidates = [
            folder / "ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
            for folder in (Path("/Applications"), Path.home() / "Applications")
        ]
    else:
        candidates = [
            folder / ("uv.exe" if C.WINDOWS else "uv")
            for folder in (
                Path.home() / ".local/bin",
                Path.home() / ".cargo/bin",
                Path("/opt/homebrew/bin"),
                Path("/usr/local/bin"),
            )
        ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise ValueError(f"Required {kind} was not found. Install it first or select its path with --{kind}-bin.")


def windows_path(path: str | os.PathLike) -> PureWindowsPath:
    # Native realpath expands 8.3 names and resolves links before spelling is normalized.
    value = ntpath.normcase(realpath(path))
    value = "\\\\" + value[8:] if value.startswith("\\\\?\\unc\\") else value.removeprefix("\\\\?\\")
    return PureWindowsPath(value)


def same_path(left, right) -> bool:
    if not C.WINDOWS or not all(isinstance(value, (str, os.PathLike)) for value in (left, right)):
        return left == right
    if not Path(left).is_absolute() or not Path(right).is_absolute():
        return left == right
    return windows_path(left) == windows_path(right)


def unlinked(path: Path, root: Path) -> None:
    linked = (
        any(
            p.is_symlink() and any(same_path(parent, root) for parent in (p, *p.parents)) for p in (path, *path.parents)
        )
        if C.WINDOWS
        else any(p.is_symlink() for p in (path, *path.parents) if p == root or root in p.parents)
    )
    if linked:
        raise ValueError(f"Refusing a linked installer or cache path: {path}")


def atomic_json(path: Path, value: object) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(encoded(value))
        stream.flush()
        os.fsync(stream.fileno())
    _replace(str(temporary), path)


def installer_root(home: Path) -> Path:
    return C.preferred_path(home, "nowitknows-installer")


def release_folder(version: str, identity: str) -> str:
    return f"{version}-{identity[: C.WINDOWS_RELEASE_DIGEST_CHARS] if C.WINDOWS else identity}"


def check_runtime_path(runtime: Path) -> None:
    if not C.WINDOWS:
        return
    deepest = windows_path(runtime / ".venv" / C.WINDOWS_VENV_DEEPEST_PATH)
    length = len(str(deepest).encode("utf-16-le")) // 2
    if length > C.WINDOWS_RUNTIME_PATH_LIMIT:
        raise ValueError(
            f"The runtime folder is too long: {runtime}. Its deepest known path is {length} characters, "
            f"{length - C.WINDOWS_RUNTIME_PATH_LIMIT} over the Windows {C.WINDOWS_RUNTIME_PATH_LIMIT}-character "
            "DLL loading limit. Choose a shorter CODEX_HOME and retry."
        )


@contextlib.contextmanager
def installer_lock(home: Path):
    root = installer_root(home)
    unlinked(root / "install.lock", home)
    root.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(file_lock(root / "install", wait_s=0))
        except TimeoutError as error:
            raise ValueError("Another NowItKnows installer is using this profile. Retry after it finishes.") from error
        yield root


def load_state(home: Path) -> dict:
    path = installer_root(home) / "state.json"
    unlinked(path, home)
    if not path.exists():
        return {"schema_version": 1, "codex_home": str(home), "releases": {}, "active": None}
    state = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != 1
        or not same_path(state.get("codex_home"), str(home))
        or not isinstance(state.get("releases"), dict)
    ):
        raise ValueError(
            "The installer ledger is invalid or belongs to another profile. Existing files were preserved."
        )
    for version, release in state["releases"].items():
        if (
            not isinstance(release, dict)
            or release.get("version") != version
            or not isinstance(release.get("source_sha256"), str)
            or len(release["source_sha256"]) != len(digest(b""))
            or any(char not in "0123456789abcdef" for char in release["source_sha256"])
            or Path(version).name != version
            or version in {".", ".."}
        ):
            raise ValueError("Invalid release identity in installer ledger.")
        runtime = installer_root(home) / "releases" / f"{version}-{release['source_sha256']}" / "runtime"
        if C.WINDOWS and not same_path(release.get("runtime"), str(runtime)):
            runtime = runtime.parent.parent / release_folder(version, release["source_sha256"]) / "runtime"
        if not same_path(release.get("runtime"), str(runtime)) or not Path(release.get("bundle", "")).is_absolute():
            raise ValueError("Invalid release paths in installer ledger.")
        if "codex_bin" in release and (
            not isinstance(release["codex_bin"], str) or not Path(release["codex_bin"]).is_absolute()
        ):
            raise ValueError("Invalid saved Codex executable in installer ledger.")
    return state


def verify(root: Path, hashes: dict[str, str], *, exact: bool = False) -> None:
    unlinked(root, root)
    if exact:
        for path in root.rglob("*"):
            if path.is_symlink() or (
                path.is_file() and path.relative_to(root).as_posix() not in {*hashes, "build-manifest.json"}
            ):
                raise ValueError(f"Unexpected file in immutable release bundle: {path}")
    for name, expected in hashes.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid file path in release receipt.")
        path = root / relative
        unlinked(path, root)
        if not path.is_file() or digest(path.read_bytes()) != expected:
            raise ValueError(f"The release copy differs from its receipt: {path}")


def retained_source_digest(receipt: dict) -> str:
    """Windows also recovers releases made before the installer archive joined the source identity."""
    sources = receipt.get("sources")
    if C.WINDOWS and "installer_source_sha256" not in receipt:
        if not isinstance(sources, dict) or not sources:
            raise ValueError("The retained release receipt has no source hashes.")
        return digest(encoded(sources))
    return digest(encoded([sources, receipt.get("installer_source_sha256")]))


def release_identity(release: dict) -> tuple[str, str, Path]:
    bundle = Path(release["bundle"])
    unlinked(bundle / CATALOG, bundle)
    raw = (bundle / CATALOG).read_bytes()
    unlinked(bundle / "build-manifest.json", bundle)
    receipt = json.loads((bundle / "build-manifest.json").read_text(encoding="utf-8"))
    if C.WINDOWS and release.get("source_sha256") != retained_source_digest(receipt):
        raise ValueError("The retained release's full source digest differs from its receipt.")
    if receipt.get("files", {}).get(CATALOG.as_posix()) != digest(raw):
        raise ValueError("The retained catalog differs from its receipt.")
    catalog = json.loads(raw)
    market = catalog.get("name")
    plugins = catalog.get("plugins")
    if (
        not C.is_ours(market, MARKET)
        or not isinstance(plugins, list)
        or len(plugins) != 1
        or not isinstance(plugins[0], dict)
    ):
        raise ValueError("Invalid retained marketplace identity.")
    plugin = plugins[0].get("name")
    if (market, plugin) not in tuple(zip(C.aliases(MARKET), C.aliases(PLUGIN), strict=True)):
        raise ValueError("Invalid retained plugin identity.")
    package = Path("plugins") / plugin
    if plugins[0].get("source") != {"source": "local", "path": "./" + package.as_posix()}:
        raise ValueError("Invalid retained package identity.")
    return market, plugin, package


def package_hashes(receipt: dict) -> dict[str, str]:
    packages = [Path("plugins") / name for name in C.aliases(PLUGIN)]
    present = [package for package in packages if any(Path(name).is_relative_to(package) for name in receipt["files"])]
    if len(present) != 1:
        raise ValueError("Invalid release package in receipt.")
    package = present[0]
    return {
        Path(name).relative_to(package).as_posix(): expected
        for name, expected in receipt["files"].items()
        if Path(name).is_relative_to(package)
    }


def marketplace(codex: str, home: Path, cwd: Path, market: str = MARKET) -> dict | None:
    result = run([codex, "plugin", "marketplace", "list", "--json"], cwd, install_environment(home))
    entries = result.get("marketplaces")
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("Unexpected native marketplace inventory. No marketplace was changed.")
    found = [entry for entry in entries if entry.get("name") == market]
    if len(found) > 1:
        raise ValueError("Ambiguous NowItKnows marketplace sources. No marketplace was changed.")
    entry = found[0] if found else None
    if entry is not None and "marketplaceSource" not in entry:
        # Codex 0.166 lists roots here and source provenance on the installed plugin instead; an older CLI lists both.
        plugin = C.aliases(PLUGIN)[C.aliases(MARKET).index(market)]
        installed = installed_plugin(codex, home, cwd, f"{plugin}@{market}")
        if installed:
            entry = {
                **entry,
                "marketplaceSource": installed.get("marketplaceSource")
                if installed.get("marketplaceName") == market
                else None,
            }
    return entry


def checked_source(entry: dict | None, state: dict, replace_source: Path | None = None) -> Path | None:
    if entry is None:
        return None
    if "marketplaceSource" not in entry:
        root = entry.get("root")
        for release in state["releases"].values():
            if not isinstance(root, str) or not Path(root).is_absolute() or not same_path(root, release["bundle"]):
                continue
            # Registration can survive a crash before any installed plugin supplies provenance.
            market, _, _ = release_identity(release)
            receipt = json.loads((Path(release["bundle"]) / "build-manifest.json").read_text(encoding="utf-8"))
            if entry.get("name") != market or release.get("source_sha256") != retained_source_digest(receipt):
                raise ValueError("The retained marketplace identity differs from its receipt.")
            verify(Path(release["bundle"]), receipt["files"], exact=True)
            return Path(root)
    source = entry.get("marketplaceSource")
    if not isinstance(source, dict):
        raise ValueError("Unexpected native marketplace source. No marketplace was changed.")
    path = Path(source.get("source", ""))
    if (
        source.get("sourceType") != "local"
        or not path.is_absolute()
        or not same_path(path.resolve(), Path(entry.get("root", "")))
    ):
        raise ValueError("The NowItKnows marketplace is not an exact local source. No marketplace was changed.")
    known = {Path(release["bundle"]) for release in state["releases"].values()}
    if not any(same_path(path, bundle) for bundle in known) and (
        replace_source is None or not same_path(path, replace_source.resolve())
    ):
        raise ValueError(
            f"NowItKnows is registered from unmanaged source {path}. To adopt it deliberately, rerun with "
            f"--replace-source {shlex.quote(str(path))}. Close legacy chats for this cache migration."
        )
    return path


def installed_plugin(codex: str, home: Path, cwd: Path, selector: str = SELECTOR) -> dict | None:
    entries = run([codex, "plugin", "list", "--json"], cwd, install_environment(home)).get("installed")
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("Unexpected native plugin inventory. Existing files were preserved.")
    found = [entry for entry in entries if entry.get("pluginId") == selector]
    if len(found) > 1:
        raise ValueError("Ambiguous native NowItKnows installation. Existing files were preserved.")
    return found[0] if found else None


def removal_command(codex: str, home: Path, selector: str) -> list[str]:
    state = load_state(home)
    active = state["releases"].get(state.get("active"))
    if active:
        market, plugin, _ = release_identity(active)
        if selector == f"{plugin}@{market}":
            return [
                sys.executable,
                str(ROOT / "tools/install_codex.py"),
                "--codex-home",
                str(home),
                "--codex-bin",
                codex,
                "--remove",
            ]
    return [codex, "plugin", "remove", selector]


def refuse_other_install(
    codex: str, home: Path, cwd: Path, selector: str = SELECTOR, allowed: tuple[str, ...] = ()
) -> None:
    """Stop before a second nowitknows-codex install (from any other marketplace)."""
    entries = run([codex, "plugin", "list", "--json"], cwd, install_environment(home)).get("installed")
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("Unexpected native plugin inventory. Existing files were preserved.")
    state = load_state(home)
    active = state["releases"].get(state.get("active"))
    if active:
        market, plugin, _ = release_identity(active)
        retained_selector = f"{plugin}@{market}"
        if retained_selector != selector and all(entry.get("pluginId") != retained_selector for entry in entries):
            entries = [*entries, {"pluginId": retained_selector, "installed": True}]
    for entry in entries:
        plugin_id = entry.get("pluginId")
        other = (
            isinstance(plugin_id, str)
            and C.is_ours(plugin_id.partition("@")[0], PLUGIN)
            and plugin_id not in (selector, *allowed)
        )
        if other and entry.get("installed") is not False:
            market = plugin_id.partition("@")[2]
            shared = sorted(
                str(e.get("pluginId"))
                for e in entries
                if e is not entry
                and e.get("installed") is not False
                and isinstance(e.get("pluginId"), str)
                and market
                and e["pluginId"].partition("@")[2] == market
            )
            command = removal_command(codex, home, plugin_id)
            steps = shlex.join(command)
            if "--remove" not in command and shared:
                steps += f" (only that plugin: keep the {market} marketplace, which also provides {', '.join(shared)})"
            elif "--remove" not in command and market:
                steps += " and " + shlex.join(["codex", "plugin", "marketplace", "remove", market])
            raise ValueError(
                f"{plugin_id} is already installed in this profile. A second copy would run two hook sets and two "
                f"nowitknows MCP servers. To migrate to the managed release, run {steps} with this CODEX_HOME, "
                "then rerun the installer. Memory and skills are preserved."
            )


def saved_caches(home: Path, root: Path) -> list[tuple[Path, Path]]:
    journal = root / "cache-recovery.json"
    unlinked(journal, home)
    if not journal.exists():
        return []
    entries = json.loads(journal.read_text(encoding="utf-8"))
    caches = {home / name for name in C.aliases("plugins/cache/nowitknows-codex-local/nowitknows-codex")}
    saved = []
    for destination, backup in entries:
        destination, backup = Path(destination), Path(backup)
        retained = root / "retained-caches"
        within = (
            windows_path(backup).is_relative_to(windows_path(retained))
            if C.WINDOWS
            else backup.is_relative_to(retained)
        )
        if not any(same_path(destination.parent, cache) for cache in caches) or not within:
            raise ValueError("Invalid cache recovery journal. Existing files were preserved.")
        unlinked(backup, home)
        saved.append((destination, backup))
    return saved


def snapshot_caches(home: Path, root: Path) -> list[tuple[Path, Path]]:
    restore_caches(saved_caches(home, root), home)
    retained = root / "retained-caches"
    unlinked(retained, home)
    retained.mkdir(exist_ok=True)
    snapshot = Path(tempfile.mkdtemp(prefix="snapshot-", dir=retained))
    saved = []
    for namespace in C.aliases("plugins/cache/nowitknows-codex-local/nowitknows-codex"):
        cache = home / namespace
        unlinked(cache, home)
        if cache.exists():
            for version in sorted(cache.iterdir()):
                unlinked(version, home)
                if not version.is_dir():
                    raise ValueError(f"Unexpected native cache entry: {version}")
                backup = snapshot / cache.parent.name / version.name
                backup.parent.mkdir(exist_ok=True)
                shutil.copytree(version, backup, symlinks=True)
                saved.append((version, backup))
    atomic_json(root / "cache-recovery.json", [[str(path), str(backup)] for path, backup in saved])
    return saved


def restore_missing(source: Path, destination: Path) -> None:
    if destination.is_symlink():
        if source.is_symlink() and (
            os.readlink(source) == os.readlink(destination) or (C.WINDOWS and same_path(source, destination))
        ):
            return
        raise ValueError(f"Cache recovery cannot replace a linked path: {destination}")
    if not destination.exists():
        if source.is_symlink():
            destination.symlink_to(os.readlink(source))
        elif source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination)
    elif source.is_dir() and not source.is_symlink():
        for child in source.iterdir():
            restore_missing(child, destination / child.name)


def restore_caches(saved: list[tuple[Path, Path]], home: Path) -> None:
    for destination, backup in saved:
        unlinked(destination, home)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
                staging = Path(temporary) / "cache"
                shutil.copytree(backup, staging, symlinks=True)
                _replace(str(staging), destination)
        else:
            restore_missing(backup, destination)


def prepare_runtime(release: dict, receipt: dict, uv: str, home: Path, cwd: Path) -> None:
    runtime, bundle = Path(release["runtime"]), Path(release["bundle"])
    check_runtime_path(runtime)
    _, _, package = release_identity(release)
    unlinked(runtime, home)
    if not runtime.exists():
        runtime.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime.parent) as temporary:
            staging = Path(temporary) / "runtime"
            shutil.copytree(bundle / package, staging)
            _replace(str(staging), runtime)
    verify(runtime, package_hashes(receipt))
    python = runtime_python(runtime)
    unlinked(runtime / ".venv", home)
    if release.get("runtime_ready") and python.is_file() and os.access(python, os.X_OK):
        return
    run(sync_command(uv, runtime), cwd, {**install_environment(home), "UV_PROJECT_ENVIRONMENT": str(runtime / ".venv")})
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("uv finished without a working runtime Python. Rerun this installer to retry.")
    if C.WINDOWS:
        run(
            [str(python), "-m", "compileall", "-q", str(runtime / "nowitknows"), str(runtime / "bin")],
            cwd,
            install_environment(home),
        )


def activate(
    release: dict, receipt: dict, state: dict, home: Path, codex: str, cwd: Path, root: Path, previous: Path | None
):
    bundle = Path(release["bundle"])
    market, plugin, _ = release_identity(release)
    selector = f"{plugin}@{market}"
    verify(bundle, receipt["files"], exact=True)
    expected_cache = home / "plugins/cache" / market / plugin / receipt["version"]
    unlinked(expected_cache, home)
    hashes = package_hashes(receipt)
    if expected_cache.exists():
        try:
            verify(expected_cache, hashes)
        except ValueError:
            if release.get("installed"):
                raise
    if same_path(previous, bundle) and state.get("active") == receipt["version"] and expected_cache.is_dir():
        native = installed_plugin(codex, home, cwd, selector)
        if (
            native
            and native.get("version") == receipt["version"]
            and native.get("installed") is True
            and native.get("enabled") is True
        ):
            return "already-installed"
    saved = snapshot_caches(home, root)
    env = install_environment(home)
    if not same_path(previous, bundle):
        if previous is not None:
            run([codex, "plugin", "marketplace", "remove", market, "--json"], cwd, env)
        registered = run([codex, "plugin", "marketplace", "add", str(bundle), "--json"], cwd, env)
        if registered.get("marketplaceName") != market or not same_path(registered.get("installedRoot"), str(bundle)):
            raise ValueError("The marketplace resolved outside the prepared bundle. Installation stopped.")
    try:
        installed = run([codex, "plugin", "add", selector, "--json"], cwd, env)
        if (
            installed.get("pluginId") != selector
            or installed.get("version") != receipt["version"]
            or not same_path(installed.get("installedPath"), str(expected_cache))
        ):
            raise ValueError("The CLI installed a different plugin, version or cache path. Installation stopped.")
        verify(expected_cache, hashes)
        native = installed_plugin(codex, home, cwd, selector)
        if (
            not native
            or native.get("version") != receipt["version"]
            or native.get("installed") is not True
            or native.get("enabled") is not True
        ):
            raise ValueError("The native plugin is not installed and enabled. Check this profile's plugin settings.")
    finally:
        restore_caches(saved, home)
    return "installed"


def instructions(home: Path, cwd: Path, codex: str, release: dict) -> dict:
    runtime = Path(release["runtime"])
    market, plugin, package = (
        release_identity(release) if Path(release["bundle"]).exists() else (MARKET, PLUGIN, PACKAGE)
    )
    entry = runtime / C.aliases("bin/nowitknows_codex.py")[plugin != PLUGIN]
    result = {
        "codex_home": str(home),
        "cwd": str(cwd),
        "bundle": release["bundle"],
        "runtime": str(runtime),
        "version": release["version"],
        "default_codex_bin": release.get("codex_bin"),
        "plugin": f"{plugin}@{market}",
        "hook_review": (
            "Open /hooks in this Codex profile and workspace. Select each of nowitknows-codex's eight definitions, "
            "inspect the command and preserved runtime path, then trust only those exact definitions. "
            "Review again after upgrades when Codex requests it. Restart Codex/the desktop app and open a fresh chat."
        ),
        "hook_definitions": str(Path(release["bundle"]) / package / "codex/hooks.json"),
        "open_codex": shlex.join(["env", f"CODEX_HOME={home}", codex, "-C", str(cwd)]),
        "doctor": shlex.join(
            [
                "env",
                f"CODEX_HOME={home}",
                f"NOWITKNOWS_CODEX_HOME={home}",
                f"{C.aliases('NOWITKNOWS_CODEX_HOME')[1]}={home}",
                str(runtime_python(runtime)),
                str(entry),
                "doctor",
            ]
        ),
        "recovery": (
            "Rerun the same install to resume. --rollback VERSION selects a retained release. --remove preserves data."
        ),
    }
    if C.WINDOWS:
        prefix = ps_environment({"CODEX_HOME": str(home)})
        result["open_codex"] = subprocess.list2cmdline(powershell(prefix + f"& {ps_quote(codex)} -C {ps_quote(cwd)}"))
        prefix += ps_environment({"NOWITKNOWS_CODEX_HOME": str(home)})
        result["doctor"] = subprocess.list2cmdline(
            powershell(prefix + f"& {ps_quote(runtime_python(runtime))} {ps_quote(entry)} doctor")
        )
    return result


def install(
    source: Path,
    out: Path | None,
    codex_home: Path,
    codex: str,
    uv: str,
    dry_run: bool = False,
    *,
    cwd: Path | None = None,
    replace_source: Path | None = None,
) -> dict:
    home = profile_path(codex_home)
    cwd = (cwd or Path.cwd()).resolve(strict=True)
    source = source.resolve(strict=True)
    root = installer_root(home)
    for path in (source, ROOT):
        separate(path, root)
    probe = root / "source-inspection"
    _, original = plan(source, probe, allow_existing=True)
    identity = digest(encoded([original["sources"], original["installer_source_sha256"]]))
    version = original["version"]
    location = root / "releases" / release_folder(version, identity)
    runtime = location / "runtime"
    state = load_state(home)
    existing = state["releases"].get(version)
    if existing and existing["source_sha256"] != identity:
        raise ValueError("This version already identifies different source bytes. Bump the native release version.")
    selected_cli = str(Path(codex).absolute())
    if existing and not same_path(existing.get("codex_bin"), selected_cli):
        raise ValueError(
            "This version already has a different or absent saved CLI binding. "
            "Reuse its selected executable or install a new native release version."
        )
    bundle = out.resolve() if out is not None else Path(existing["bundle"]) if existing else location / "marketplace"
    if existing and not same_path(bundle, Path(existing["bundle"])):
        raise ValueError("This release already has an immutable bundle location. Reuse it or omit --out.")
    if C.WINDOWS and existing:
        # Keep immutable receipt and generated command bytes when only a path's spelling changed.
        home, runtime = Path(state["codex_home"]), Path(existing["runtime"])
        root = installer_root(home)
        bundle, selected_cli = Path(existing["bundle"]), existing["codex_bin"]
    check_runtime_path(runtime)
    separate(bundle, runtime)
    separate(home / "plugins", bundle)
    unlinked(bundle, home)
    files, receipt = plan(
        source, bundle, codex_home=home, runtime_root=runtime, codex_bin=Path(selected_cli), allow_existing=True
    )
    if json.loads(files[CATALOG])["name"] != MARKET:
        raise ValueError("Only the NowItKnows managed marketplace is supported.")
    release = existing or {
        "version": version,
        "source_sha256": identity,
        "bundle": str(bundle),
        "runtime": str(runtime),
        "codex_bin": selected_cli,
        "installed": False,
        "runtime_ready": False,
    }
    if bundle.exists():
        unlinked(bundle / "build-manifest.json", bundle)
        if not existing or (bundle / "build-manifest.json").read_bytes() != encoded(receipt):
            raise ValueError(
                "Output already exists without this release's matching receipt. Existing files were preserved."
            )
        verify(bundle, receipt["files"], exact=True)
    result = {**instructions(home, cwd, codex, release), "dry_run": dry_run, "codex_bin": codex, "uv_bin": uv}
    result["commands"] = [
        [codex, "plugin", "marketplace", "list", "--json"],
        sync_command(uv, runtime),
        [codex, "plugin", "marketplace", "add", str(bundle), "--json"],
        [codex, "plugin", "add", SELECTOR, "--json"],
    ]
    if dry_run:
        result["note"] = (
            "Preview only; no CLI calls or writes. A managed source change also removes only our marketplace first."
        )
        return result
    if home.exists():  # a profile folder that is not there yet holds no other install, and Codex refuses it
        refuse_other_install(codex, home, cwd)  # before the lock creates the installer folder; rechecked under it
    created = not root.exists()
    with installer_lock(home):
        if load_state(home) != state:
            raise ValueError("Installer state changed while preparing. Rerun the installer.")
        try:
            refuse_other_install(codex, home, cwd)
        except ValueError:
            if created:  # only what this run made: the lock, then its folder if nothing else arrived
                if not C.WINDOWS:
                    (root / "install.lock").unlink(missing_ok=True)
                with contextlib.suppress(OSError):
                    root.rmdir()
            raise
        restore_caches(saved_caches(home, root), home)
        previous = checked_source(marketplace(codex, home, cwd), state, replace_source)
        cache = home / "plugins/cache" / MARKET / PLUGIN / version
        unlinked(cache, home)
        if cache.exists():
            try:
                verify(cache, package_hashes(receipt))
            except ValueError:
                if not existing:
                    raise ValueError(
                        "An existing native cache uses this version with different source or runtime bindings. "
                        "Install a new native release version; the existing cache was preserved."
                    ) from None
                if existing.get("installed"):
                    raise
        state["releases"][version] = release
        atomic_json(root / "state.json", state)
        if not bundle.exists():
            build(source, bundle, codex_home=home, runtime_root=runtime, codex_bin=Path(selected_cli))
        prepare_runtime(release, receipt, uv, home, cwd)
        release["runtime_ready"] = True
        atomic_json(root / "state.json", state)
        result["status"] = activate(release, receipt, state, home, codex, cwd, root, previous)
        release["installed"] = True
        state["active"] = version
        state["last_active"] = version
        atomic_json(root / "state.json", state)
    result["installed_path"] = str(cache)
    return result


def recover(home: Path, codex: str, uv: str, cwd: Path, version: str | None, remove: bool, dry_run: bool) -> dict:
    state = load_state(home)
    if remove:
        identities = {release_identity(release)[:2] for release in state["releases"].values()} or {(MARKET, PLUGIN)}
        commands = [
            command
            for market, plugin in sorted(identities)
            for command in (
                [codex, "plugin", "remove", f"{plugin}@{market}", "--json"],
                [codex, "plugin", "marketplace", "remove", market, "--json"],
            )
        ]
        if not dry_run:
            with installer_lock(home) as root:
                if load_state(home) != state:
                    raise ValueError("Installer state changed while preparing. Rerun the installer.")
                targets = []
                for market, plugin in sorted(identities):
                    registered = marketplace(codex, home, cwd, market)
                    checked_source(registered, state)
                    native = installed_plugin(codex, home, cwd, f"{plugin}@{market}")
                    targets.append((market, plugin, registered, native))
                saved = snapshot_caches(home, root)
                try:
                    for market, plugin, registered, native in targets:
                        if native and native.get("installed") is True:
                            run(
                                [codex, "plugin", "remove", f"{plugin}@{market}", "--json"],
                                cwd,
                                install_environment(home),
                            )
                        if registered is not None:
                            run(
                                [codex, "plugin", "marketplace", "remove", market, "--json"],
                                cwd,
                                install_environment(home),
                            )
                finally:
                    restore_caches(saved, home)
                if state.get("active"):
                    state["last_active"] = state["active"]
                state["active"] = None
                atomic_json(root / "state.json", state)
        return {
            "status": "removed" if not dry_run else "preview",
            "commands": commands,
            "preserved": "All release runtimes, retained caches, memory, skills and installer ledger.",
        }
    release = state["releases"].get(version)
    if not release:
        raise ValueError(f"No retained release {version}. Available: {', '.join(state['releases']) or 'none'}")
    check_runtime_path(Path(release["runtime"]))
    bundle = Path(release["bundle"])
    market, plugin, _ = release_identity(release)
    selector = f"{plugin}@{market}"
    unlinked(bundle / "build-manifest.json", bundle)
    receipt = json.loads((bundle / "build-manifest.json").read_text(encoding="utf-8"))
    verify(bundle, receipt["files"], exact=True)
    if not same_path(receipt.get("codex_home"), str(home)) or not same_path(
        receipt.get("runtime_root"), release["runtime"]
    ):
        raise ValueError("The retained release belongs to another runtime or profile.")
    if not same_path(receipt.get("codex_bin"), release.get("codex_bin")):
        raise ValueError("The retained release has a different saved Codex executable.")
    result = {**instructions(home, cwd, codex, release), "dry_run": dry_run, "codex_bin": codex, "uv_bin": uv}
    current = state["releases"].get(state.get("active") or state.get("last_active"))
    current_market, current_plugin, _ = release_identity(current) if current else (market, plugin, PACKAGE)
    current_selector = f"{current_plugin}@{current_market}"
    transition = current_selector != selector
    allowed = (current_selector,) if transition else ()
    if not dry_run:
        refuse_other_install(codex, home, cwd, selector, allowed)
        with installer_lock(home) as root:
            if load_state(home) != state:
                raise ValueError("Installer state changed while preparing. Rerun the installer.")
            refuse_other_install(codex, home, cwd, selector, allowed)
            restore_caches(saved_caches(home, root), home)
            previous = checked_source(marketplace(codex, home, cwd, market), state)
            prepare_runtime(release, receipt, uv, home, cwd)
            release["runtime_ready"] = True  # Reuse is safe only after dependency preparation succeeds.
            if transition:
                registered = marketplace(codex, home, cwd, current_market)
                checked_source(registered, state)
                native = installed_plugin(codex, home, cwd, current_selector)
                saved = snapshot_caches(home, root)
                try:
                    if native and native.get("installed") is True:
                        run([codex, "plugin", "remove", current_selector, "--json"], cwd, install_environment(home))
                    if registered is not None:
                        run(
                            [codex, "plugin", "marketplace", "remove", current_market, "--json"],
                            cwd,
                            install_environment(home),
                        )
                finally:
                    restore_caches(saved, home)
                state["active"] = None
                atomic_json(root / "state.json", state)
            result["status"] = activate(release, receipt, state, home, codex, cwd, root, previous)
            state["active"] = version
            state["last_active"] = version
            atomic_json(root / "state.json", state)
    return result


RETAINED = "Retained releases and cache snapshots are kept in the selected profile. Rerun to recover or retry.\n"


def kept(home: Path | None) -> bool:
    """Whether a stopped run leaves anything to recover: a retained release or a cache snapshot."""
    root = installer_root(home) if home else None
    with contextlib.suppress(OSError):
        return bool(root and ((root / "cache-recovery.json").exists() or any((root / "releases").iterdir())))
    return False


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if C.WINDOWS and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Install, update or recover the local NowItKnows Codex release.")
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument(
        "--out", type=Path, help="Optional immutable marketplace location; normally selected automatically."
    )
    parser.add_argument("--codex-home", type=Path, help="Defaults to CODEX_HOME, otherwise ~/.codex.")
    parser.add_argument(
        "--codex-bin",
        help="Existing CLI; defaults to NOWITKNOWS_CODEX_BIN, the saved release CLI, PATH, then ChatGPT.app.",
    )
    parser.add_argument("--uv-bin", help="Existing uv; defaults to PATH, then standard standalone locations.")
    parser.add_argument(
        "--replace-source", type=Path, help="Deliberately adopt this exact existing local marketplace source."
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--rollback", metavar="VERSION")
    actions.add_argument("--remove", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    home = None
    try:
        home = profile_path(args.codex_home)
        state = load_state(home)
        active = state.get("active") or state.get("last_active")
        version = args.rollback or active
        if not args.rollback and not args.remove:
            version = json.loads((args.source / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))["version"]
        saved = state["releases"].get(version, {}).get("codex_bin")
        if saved is None:
            saved = state["releases"].get(active, {}).get("codex_bin")
        selected = args.codex_bin or C.env("NOWITKNOWS_CODEX_BIN", C.environment()) or saved
        codex = (selected or "codex") if args.dry_run else discover_binary(selected, "codex")
        uv = (args.uv_bin or "uv") if args.dry_run or args.remove else discover_binary(args.uv_bin, "uv")
        if not args.dry_run:
            print(f"NowItKnows profile: {home}\nWorkspace: {Path.cwd()}\nCodex CLI: {codex}\nuv: {uv}", file=sys.stderr)
        if args.rollback or args.remove:
            result = recover(home, codex, uv, Path.cwd(), args.rollback, args.remove, args.dry_run)
        else:
            result = install(
                args.source,
                args.out,
                home,
                codex,
                uv,
                args.dry_run,
                replace_source=args.replace_source,
            )
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Install stopped: {error}\n" + (RETAINED if kept(home) else ""))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
