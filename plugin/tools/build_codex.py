from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from nowitknows import config as C  # noqa: E402
from nowitknows.state import _replace  # noqa: E402

PLUGIN = "nowitknows-codex"
PACKAGE = Path("plugins") / PLUGIN
CATALOG = Path(".agents/plugins/marketplace.json")


def runtime_python(root: Path) -> Path:
    return root / (".venv/Scripts/python.exe" if C.WINDOWS else ".venv/bin/python")


def ps_quote(value: object) -> str:
    # Outer cmd or PowerShell parsing must not expand the script before the PowerShell child receives it.
    pieces = re.split(r"([$`%])", str(value))
    quoted = [
        f"[char]{ord(part)}" if part in {"$", "`", "%"} else "'" + part.replace("'", "''") + "'" for part in pieces
    ]
    return "(" + " + ".join(quoted) + ")" if len(pieces) > 1 else quoted[0]


def ps_environment(values: dict[str, str]) -> str:
    return (
        "; ".join(
            f"[Environment]::SetEnvironmentVariable({ps_quote(key)}, {ps_quote(value)})"
            for key, value in values.items()
        )
        + "; "
    )


def powershell(script: str) -> list[str]:
    return ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script]


def windows_hook(event: str, runtime: Path | None, home: Path | None) -> str:
    if runtime is None:
        script = ""
        for prefix, name in (("if", "PLUGIN_ROOT"), ("elseif", "CLAUDE_PLUGIN_ROOT")):
            root = f"[Environment]::GetEnvironmentVariable('{name}')"
            script += f"{prefix} ({root}) {{ & ({root} + '/bin/nowitknows.cmd') --codex hook {ps_quote(event)} }} "
    else:
        values = C.dual_env({"CODEX_HOME": str(home), "NOWITKNOWS_CODEX_HOME": str(home), "NOWITKNOWS_HOST": "codex"})
        script = ps_environment(C.python_env(values))
        script += "& " + " ".join(
            ps_quote(arg) for arg in (runtime_python(runtime), runtime / "bin/nowitknows_codex.py", "hook", event)
        )
    return subprocess.list2cmdline(powershell("try { " + script + " } finally { exit 0 }"))


def windows_server(version: str, market: str) -> list[str]:
    script = "$h=$env:CODEX_HOME; if (!$h) { $h=$env:USERPROFILE + '/.codex' }; "
    script += "if ($h.StartsWith('~/')) { $h=$env:USERPROFILE + $h.Substring(1) }; "
    path = ps_quote(f"/plugins/cache/{market}/{PLUGIN}/{version}/bin/nowitknows.cmd")
    script += f"& ($h + {path}) --codex mcp; exit $LASTEXITCODE"
    return powershell(script)


def catalog_server_args(version: str) -> list[str]:
    """The catalog install's server: started in the session folder with no PLUGIN_ROOT, it names the cache."""
    path = f'"$h/plugins/cache/nowitknows/{PLUGIN}/{version}/bin/nowitknows.sh"'
    return [
        "-c",
        'h=${CODEX_HOME:-$HOME/.codex}; case $h in "~/"*) h=$HOME/${h#"~/"} ;; esac; '
        f'f={path}; [ -f "$f" ] || {{ echo "nowitknows: no MCP server at $f; add the catalog as the marketplace'
        f' named nowitknows and reinstall {PLUGIN}" >&2; exit 1; }}; exec /bin/sh "$f" --codex mcp',
    ]


def encoded(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def separate(source: Path, destination: Path) -> None:
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("Source and output must not overlap. Choose a new output folder outside the source.")


def source_file(source: Path, relative: Path) -> bytes:
    path = source / relative
    if any(p.is_symlink() for p in (path, *path.parents) if p != source and source in p.parents):
        raise ValueError(f"Linked package sources are not supported: {relative}")
    if not path.is_file():
        raise ValueError(f"Required package source is missing: {relative}")
    return path.read_bytes()


def inventory(source: Path) -> dict[Path, bytes]:
    files = [
        Path(".codex-plugin/plugin.json"),
        Path("codex/hooks.json"),
        Path("bin/nowitknows_codex.py"),
        Path("bin/nowitknows.sh"),
        Path("bin/bootstrap.sh"),
        Path("pyproject.toml"),
        Path("uv.lock"),
        Path("LICENSE"),
        Path("THIRD_PARTY_NOTICES.md"),
    ]
    if C.WINDOWS:
        files.append(Path("bin/nowitknows.cmd"))
    for required in ("nowitknows/__init__.py", "nowitknows/codex_host.py", "prompts/skills_protocol.md"):
        source_file(source, Path(required))
    files.extend(p.relative_to(source) for p in (source / "nowitknows").glob("*.py"))
    files.extend(p.relative_to(source) for p in (source / "prompts").rglob("*") if p.is_file())
    files.extend(p.relative_to(source) for p in (source / "codex/skills").glob("*/SKILL.md"))
    excluded = {"__pycache__", "private", "history", "tests", ".git", ".DS_Store"}
    result = {}
    for relative in sorted(set(files)):
        if excluded.intersection(relative.parts) or any(p.startswith(".") for p in relative.parts[1:]):
            continue
        result[relative] = source_file(source, relative)
    expected = {f"nowitknows-{name}" for name in ("setup", "status", "changes", "keep", "report", "doctor")}
    if {p.parent.name for p in result if p.name == "SKILL.md"} != expected:
        raise ValueError("The Codex package requires setup, status, changes, keep, report, and doctor skills.")
    return result


def hook_configuration(originals: dict[Path, bytes]) -> bytes:
    config = ast.parse(originals[Path("nowitknows/config.py")])
    limit = end_timeout = None
    for node in config.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "CODEX_CONTEXT_CHARS" for target in node.targets
        ):
            limit = ast.literal_eval(node.value)
    if type(limit) is not int or limit <= 0:
        raise ValueError("config.py must define a positive CODEX_CONTEXT_CHARS constant.")
    for node in config.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "CODEX_SESSION_END_TIMEOUT_S" for target in node.targets
        ):
            end_timeout = ast.literal_eval(node.value)
    if type(end_timeout) is not int or end_timeout <= 0:
        raise ValueError("config.py must define a positive CODEX_SESSION_END_TIMEOUT_S.")
    hooks = json.loads(originals[Path("codex/hooks.json")])
    for event in ("SessionStart", "UserPromptSubmit"):
        for group in hooks["hooks"][event]:
            for handler in group["hooks"]:
                handler["additionalContextLimit"] = limit
    for group in hooks["hooks"]["SessionEnd"]:
        for handler in group["hooks"]:
            handler["timeout"] = end_timeout
    if C.WINDOWS:
        for groups in hooks["hooks"].values():
            for group in groups:
                for handler in group["hooks"]:
                    handler["timeout"] = max(handler["timeout"], C.CODEX_WINDOWS_HOOK_TIMEOUT_S)
    return encoded(hooks)


def plan(
    source: Path,
    out: Path,
    installed_cache: Path | None = None,
    codex_home: Path | None = None,
    *,
    runtime_root: Path | None = None,
    codex_bin: Path | None = None,
    allow_existing: bool = False,
) -> tuple[dict[Path, bytes], dict]:
    source, out = source.resolve(strict=True), out.resolve()
    separate(source, out)
    separate(ROOT, out)
    if out.exists() and not allow_existing:
        raise ValueError("Output already exists. Choose a new folder; existing data is never replaced.")
    originals = inventory(source)
    catalog_path = Path(".codex-plugin/marketplace.json")
    catalog_bytes = source_file(source, catalog_path)
    catalog = json.loads(catalog_bytes)
    manifest = json.loads(originals[Path(".codex-plugin/plugin.json")])
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", manifest["version"]):
        raise ValueError("The native release requires a safe semantic version.")
    if manifest["name"] != PLUGIN or manifest["hooks"] != "./codex/hooks.json":
        raise ValueError("Codex identity and explicit hook path must match the native package.")
    if manifest["skills"] != "./codex/skills/" or set(manifest["mcpServers"]) != {"nowitknows"}:
        raise ValueError("Codex skills and the nowitknows MCP server must be declared explicitly.")
    server = manifest["mcpServers"]["nowitknows"]
    if (server.get("command"), server.get("args"), server.get("cwd")) != (
        "/bin/sh",
        catalog_server_args(manifest["version"]),
        None,
    ):
        raise ValueError("The catalog MCP server must name this version's cache path (catalog_server_args).")
    if [(p["name"], p["source"]) for p in catalog["plugins"]] != [
        (PLUGIN, {"source": "local", "path": f"./{PACKAGE.as_posix()}"})
    ]:
        raise ValueError("The Codex catalog must reference only the generated native bundle.")
    files = {PACKAGE / name: data for name, data in originals.items()}
    if C.WINDOWS:
        command, *args = windows_server(manifest["version"], catalog["name"])
        server.update(command=command, args=args)
        files[PACKAGE / ".codex-plugin/plugin.json"] = encoded(manifest)
        for relative in originals:
            if relative.name == "SKILL.md":
                files[PACKAGE / relative] = originals[relative].replace(
                    b'/bin/sh "<plugin-root>/bin/nowitknows.sh"', b'& "<plugin-root>/bin/nowitknows.cmd"'
                )
    if runtime_root is not None:
        if installed_cache is not None:
            raise ValueError("Choose a preserved runtime or a legacy cache binding, not both.")
        installed_cache = runtime_root
    if codex_home is not None and installed_cache is None:
        raise ValueError("A Codex profile binding requires an installed cache path.")
    if codex_bin is not None:
        if installed_cache is None or not codex_bin.is_absolute():
            raise ValueError("A saved Codex executable requires an absolute path and runtime binding.")
        entry = Path("bin/nowitknows_codex.py")
        text = originals[entry].decode("utf-8")
        prefix = "from __future__ import annotations\n"
        if not text.startswith(prefix):
            raise ValueError("Unrecognized native entry point for the saved CLI binding.")
        files[PACKAGE / entry] = (
            prefix
            + "\nimport os\n\n"
            + "import sys\nfrom pathlib import Path\n"
            + "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n"
            + "from nowitknows import config as C\n\n"
            + 'if not C.environment().get("NOWITKNOWS_CODEX_BIN"):\n'
            + f'    os.environ["NOWITKNOWS_CODEX_BIN"] = {str(codex_bin)!r}\n'
            + text[len(prefix) :]
        ).encode("utf-8")
    if installed_cache is not None:
        if not installed_cache.is_absolute():
            raise ValueError("The installed cache binding must be an absolute path.")
        if codex_home is None or not codex_home.is_absolute():
            raise ValueError("The installed cache binding requires an absolute Codex profile path.")
        manifest["mcpServers"]["nowitknows"] = {
            "command": str(runtime_python(installed_cache)),
            "args": [str(installed_cache / "bin/nowitknows_codex.py"), "mcp"],
            "env": C.dual_env(
                {
                    "CODEX_HOME": str(codex_home),
                    "NOWITKNOWS_CODEX_HOME": str(codex_home),
                    "NOWITKNOWS_HOST": "codex",
                }
            ),
            "env_vars": [
                "NOWITKNOWS_SHARED_DATA_DIR",
                "NOWITKNOWS_DATA_DIR",
                "NOWITKNOWS_CODEX_DATA_DIR",
                "NOWITKNOWS_CODEX_SKILLS_DIR",
                "NOWITKNOWS_CODEX_BIN",
                *C.LEGACY_CODEX_ENV,
            ],
        }
        files[PACKAGE / ".codex-plugin/plugin.json"] = encoded(manifest)
    if C.WINDOWS:
        # Codex filters the inherited server environment, unlike the Claude launcher.
        server = manifest["mcpServers"]["nowitknows"]
        server["env"] = C.python_env(server.get("env", {}))
        manifest["mcpServers"]["nowitknows"].setdefault("env_vars", []).extend(
            [
                "SYSTEMROOT",
                "SYSTEMDRIVE",
                "WINDIR",
                "COMSPEC",
                "PATHEXT",
                "USERPROFILE",
                "TEMP",
                "TMP",
                "APPDATA",
                "LOCALAPPDATA",
            ]
        )
        files[PACKAGE / ".codex-plugin/plugin.json"] = encoded(manifest)
    files[PACKAGE / "codex/hooks.json"] = hook_configuration(originals)
    if runtime_root is not None:
        hooks = json.loads(files[PACKAGE / "codex/hooks.json"])
        for event, groups in hooks["hooks"].items():
            for group in groups:
                for handler in group["hooks"]:
                    command = shlex.join(
                        [
                            "env",
                            f"CODEX_HOME={codex_home}",
                            f"NOWITKNOWS_CODEX_HOME={codex_home}",
                            "NOWITKNOWS_HOST=codex",
                            f"{C.aliases('NOWITKNOWS_CODEX_HOME')[1]}={codex_home}",
                            f"{C.aliases('NOWITKNOWS_HOST')[1]}=codex",
                            str(runtime_root / ".venv/bin/python"),
                            str(runtime_root / "bin/nowitknows_codex.py"),
                            "hook",
                            event,
                        ]
                    )
                    handler["command"] = (
                        windows_hook(event, runtime_root, codex_home) if C.WINDOWS else f"({command}); exit 0"
                    )
        files[PACKAGE / "codex/hooks.json"] = encoded(hooks)
        for relative in originals:
            if relative.name == "SKILL.md":
                text = originals[relative].decode("utf-8")
                paragraphs = text.split("\n\n")
                if not paragraphs[1].startswith("Use the installed copy of this skill"):
                    raise ValueError(f"Unrecognized native skill runtime instructions: {relative}")
                paragraphs[1] = (
                    f"Use the preserved release runtime at `{runtime_root}` as the plugin root. "
                    f"Run its `bin/nowitknows_codex.py` with its `{runtime_python(Path('.')).as_posix()}`, setting "
                    f"`CODEX_HOME` and `NOWITKNOWS_CODEX_HOME` to `{codex_home}`. "
                    + (
                        f"The entry point defaults to the saved native CLI `{codex_bin}`; "
                        "an explicit `NOWITKNOWS_CODEX_BIN` overrides it. "
                        if codex_bin is not None
                        else ""
                    )
                    + "Pass each argument separately, keep paths intact, and keep the current working folder. "
                    "If Python is unavailable, rerun the release installer to prepare the pinned runtime. "
                    "Do not sync or install into the native cache."
                )
                files[PACKAGE / relative] = "\n\n".join(paragraphs).encode("utf-8")
    elif C.WINDOWS:
        hooks = json.loads(files[PACKAGE / "codex/hooks.json"])
        for event, groups in hooks["hooks"].items():
            for group in groups:
                for handler in group["hooks"]:
                    handler["command"] = windows_hook(event, None, None)
        files[PACKAGE / "codex/hooks.json"] = encoded(hooks)
    from nowitknows.installer_source import ARCHIVE, snapshot

    files[PACKAGE / ARCHIVE] = snapshot(source, ("bin/nowitknows.cmd",) if C.WINDOWS else ())
    files[CATALOG] = catalog_bytes
    hashes = {p.as_posix(): digest(data) for p, data in sorted(files.items())}
    sources = {p.as_posix(): digest(data) for p, data in sorted(originals.items())}
    sources[catalog_path.as_posix()] = digest(catalog_bytes)
    receipt = {
        "schema_version": 1,
        "name": PLUGIN,
        "version": manifest["version"],
        "installer_source_sha256": digest(files[PACKAGE / ARCHIVE]),
        "sources": sources,
        "files": hashes,
        "content_sha256": digest(encoded(hashes)),
    }
    if installed_cache is not None:
        receipt["runtime_root" if runtime_root is not None else "installed_cache"] = str(installed_cache)
        receipt["codex_home"] = str(codex_home)
    if codex_bin is not None:
        receipt["codex_bin"] = str(codex_bin)
    return files, receipt


def build(
    source: Path,
    out: Path,
    installed_cache: Path | None = None,
    codex_home: Path | None = None,
    *,
    runtime_root: Path | None = None,
    codex_bin: Path | None = None,
) -> dict:
    source, out = source.resolve(strict=True), out.resolve()
    files, receipt = plan(source, out, installed_cache, codex_home, runtime_root=runtime_root, codex_bin=codex_bin)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".nowitknows-codex-", dir=out.parent) as temporary:
        staging = Path(temporary) / "bundle"
        for relative, data in files.items():
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o755 if path.suffix == ".sh" else 0o644)  # an unbound package's server is bin/nowitknows.sh
        (staging / "build-manifest.json").write_bytes(encoded(receipt))
        if out.exists():
            raise ValueError("Output appeared during the build. Existing data was preserved.")
        if C.WINDOWS:
            _replace(str(staging), out)
        else:
            os.rename(staging, out)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a separate nowitknows Codex marketplace from shared source.")
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--installed-cache", type=Path)
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--codex-bin", type=Path, help="Absolute default native CLI for the bound runtime.")
    args = parser.parse_args(argv)
    try:
        receipt = build(args.source, args.out, args.installed_cache, args.codex_home, codex_bin=args.codex_bin)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Build stopped: {error}\n")
    print(f"Built {PLUGIN} {receipt['version']}: {args.out.resolve()}")
    print(f"Source receipt: {args.out.resolve() / 'build-manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
