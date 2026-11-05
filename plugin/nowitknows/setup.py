from __future__ import annotations

import contextlib
import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import claude, native, notify, skillsync
from . import config as C
from .codex_client import find_codex
from .memory import EntryStore, TopicStore
from .notejudge import waiting
from .report import compressed_history_row, refusals, unshared_row
from .state import State, atomic_write, jsonl_rows

GITHUB = C.GITHUB_REPO
SPARSE = [".claude-plugin", "plugin"]  # the marketplace and the plugin folder: all a clone needs to install


def _latest_version(values) -> str | None:
    versions = [v for v in values if isinstance(v, str) and C.PLUGIN_VERSION.fullmatch(v)]
    return max(versions, key=claude.version_key, default=None)


def _cache_version(home: Path, name: str, manifest: str) -> str | None:
    versions = []
    for alias in C.aliases(name):
        for path in home.glob(f"plugins/cache/*/{alias}/*/{manifest}/plugin.json"):
            with contextlib.suppress(OSError, ValueError, AttributeError):
                identity = json.loads(path.read_text(encoding="utf-8"))
                if C.is_ours(identity.get("name"), name):
                    versions.append(identity.get("version"))
    return _latest_version(versions)


def installed_versions(cfg: C.Config) -> tuple[str | None, str | None]:
    """Claude's registry and Codex's active managed release, else native cache manifests; local files only.
    Retained caches are not active installs when the registry or managed ledger says they were removed."""
    claude_version = None
    try:
        installed = json.loads((cfg.claude_home / "plugins/installed_plugins.json").read_text(encoding="utf-8"))[
            "plugins"
        ]
        claude_version = _latest_version(
            record.get("version")
            for key, records in installed.items()
            if C.is_ours(key.partition("@")[0], "nowitknows") and isinstance(records, list)
            for record in records
            if isinstance(record, dict)
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        claude_version = _cache_version(cfg.claude_home, "nowitknows", ".claude-plugin")
    profile = Path(
        C.env("NOWITKNOWS_CODEX_HOME", cfg.env) or cfg.env.get("CODEX_HOME") or C.home_dir(cfg.env) / ".codex"
    ).expanduser()
    try:
        state = json.loads(
            (C.preferred_path(profile, "nowitknows-installer") / "state.json").read_text(encoding="utf-8")
        )
        active = state.get("active")
        release = state["releases"].get(active, {})
        return claude_version, _latest_version([active]) if release.get("version") == active else None
    except FileNotFoundError:
        pass
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return claude_version, None
    return claude_version, _cache_version(profile, "nowitknows-codex", ".codex-plugin")


def announce_version_mismatch(cfg: C.Config) -> None:
    """Queue each installed version pair once, with a short lock wait so a start never waits on notices."""
    announce_cursor_mismatch(cfg)
    with contextlib.suppress(OSError, ValueError, TimeoutError):
        claude_version, codex_version = installed_versions(cfg)
        if not claude_version or not codex_version or claude_version == codex_version:
            return
        form = {"kind": "version-mismatch", "host": cfg.host, "claude": claude_version, "codex": codex_version}
        if line := notify.told({"tell": form}):
            notify.push(
                cfg,
                line,
                key=f"version-mismatch:{claude_version}:{codex_version}",
                permanent=True,
                tell=form,
                wait_s=C.NOTICE_LOCK_WAIT_S,
            )


def _cursor_version(cfg: C.Config) -> str | None:
    profile = C.resolve_cursor_paths(cfg.env).profile
    state = profile / "nowitknows-installer/state.json"
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
        return _latest_version([data["releases"].get(data.get("active"), {}).get("version")])
    except FileNotFoundError:
        pass
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    manifest = profile / "plugins/local/nowitknows-cursor/.cursor-plugin/plugin.json"
    try:
        identity = json.loads(manifest.read_text(encoding="utf-8"))
        return _latest_version([identity.get("version")]) if identity.get("name") == "nowitknows-cursor" else None
    except FileNotFoundError:
        return _cache_version(profile, "nowitknows-cursor", ".cursor-plugin")
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def announce_cursor_mismatch(cfg: C.Config) -> None:
    with contextlib.suppress(OSError, ValueError, TimeoutError):
        cursor = _cursor_version(cfg)
        if not cursor:
            return
        for peer, version in zip(("claude", "codex"), installed_versions(cfg), strict=True):
            if not version or version == cursor:
                continue
            form = {"kind": "version-mismatch", "host": cfg.host, "cursor": cursor, peer: version, "peer": peer}
            if line := notify.told({"tell": form}):
                notify.push(
                    cfg,
                    line,
                    key=f"version-mismatch:{peer}:{version}:cursor:{cursor}",
                    permanent=True,
                    tell=form,
                    wait_s=C.NOTICE_LOCK_WAIT_S,
                )


def _directory_origin(cfg: C.Config, path: str, installed: dict) -> bool:
    """Accept the registered checkout or its exact registered plugin cache, including older installed versions."""
    if not isinstance(path, str) or not Path(path).is_absolute():
        return False
    try:
        source, root = Path(path).resolve(strict=True), C.ROOT.resolve(strict=True)
        marketplace = json.loads((source / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        entries = [p for p in marketplace.get("plugins", []) if C.is_ours(p.get("name"), "nowitknows")]
        if (
            not C.is_ours(marketplace.get("name"), "nowitknows")
            or len(entries) != 1
            or entries[0].get("source") != "./plugin"
        ):
            return False
        plugin = source / "plugin"
        # A linked plugin folder would extend the checkout's trust to code kept anywhere else
        if plugin.is_symlink() or plugin.resolve(strict=True).parent != source:
            return False
        if plugin.resolve(strict=True) == root:
            return True
        identity = json.loads((plugin / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
        cached = json.loads((root / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
        if not C.is_ours(identity.get("name"), "nowitknows") or not C.is_ours(cached.get("name"), "nowitknows"):
            return False
        for selector, record in (
            (key, record) for key in C.aliases("nowitknows@nowitknows") for record in installed.get(key, [])
        ):
            version = record.get("version")
            location = record.get("installPath")
            if not isinstance(version, str) or version in ("", ".", "..") or Path(version).name != version:
                continue
            namespace = C.aliases("plugins/cache/nowitknows/nowitknows")[selector != "nowitknows@nowitknows"]
            expected = cfg.claude_home / namespace / version
            if (
                isinstance(location, str)
                and Path(location).is_absolute()
                and Path(location).resolve(strict=True) == root == expected.resolve(strict=True)
                and cached.get("version") == version
            ):
                return True
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return False
    return False


def _other_codex_install(cfg: C.Config, codex: str, profile: str) -> str | None:
    """The managed installer refuses a competing marketplace, so detect existing native installs first."""
    if not Path(profile).expanduser().is_dir():  # Codex refuses every plugin command for a missing profile
        return None
    try:
        done = subprocess.run(
            [codex, "plugin", "list", "--json"],
            env={**cfg.env, "CODEX_HOME": profile},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=C.SETUP_TIMEOUT_S,
            check=False,
        )
        entries = json.loads(done.stdout).get("installed") if done.returncode == 0 else None
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        entries = None
    for entry in entries if isinstance(entries, list) else []:
        plugin = entry.get("pluginId") if isinstance(entry, dict) else None
        name, _, _market = plugin.partition("@") if isinstance(plugin, str) else ("", "", "")
        if (
            C.is_ours(name, "nowitknows-codex")
            and plugin != f"nowitknows-codex@{C.CODEX_MANAGED_MARKET}"
            and entry.get("installed") is not False
        ):
            return plugin
    from tools.install_codex import load_state, release_identity

    try:
        state = load_state(Path(profile))
        release = state["releases"].get(state.get("active"))
        if release:
            market, name, _ = release_identity(release)
            if name != "nowitknows-codex":
                return f"{name}@{market}"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def legacy_claude_notices(cfg: C.Config, executable: str = "claude", installed: dict | None = None) -> list[str]:
    if installed is None:
        try:
            installed = json.loads(
                (cfg.claude_home / "plugins/installed_plugins.json").read_text(encoding="utf-8")
            ).get("plugins", {})
        except (OSError, ValueError, AttributeError):
            return []
    return [
        f"found {key}; run {shlex.join([executable, 'plugin', 'uninstall', key])} "
        "before adding NowItKnows. Memory and skills stay in place. "
        "Options set for learnloop do not carry over; set them again under nowitknows with /plugin configure."
        for key in installed
        if C.is_ours(key.partition("@")[0], "nowitknows") and key.partition("@")[0] != "nowitknows"
    ]


def cursor_present(cfg: C.Config) -> bool:
    return C.resolve_cursor_paths(cfg.env).profile.is_dir()


def peer_offers(cfg: C.Config) -> dict[str, str]:
    """Fixed, model-free offers for hosts present without this plugin; spend the interval only after delivery."""
    offers = {}
    profile = C.resolve_cursor_paths(cfg.env).profile
    targets = {
        "cursor": (
            cursor_present(cfg),
            (profile / "plugins/local/nowitknows-cursor/.cursor-plugin/plugin.json").is_file()
            or any(profile.glob("plugins/cache/*/nowitknows-cursor/*/.cursor-plugin/plugin.json")),
        )
    }
    if cfg.host == "cursor":
        from .codex_host import _claude_has_nowitknows

        codex = Path(
            C.env("NOWITKNOWS_CODEX_HOME", cfg.env) or cfg.env.get("CODEX_HOME") or C.home_dir(cfg.env) / ".codex"
        )
        targets.update(
            claude=(
                bool(shutil.which("claude", path=cfg.env.get("PATH", ""))),
                _claude_has_nowitknows(cfg.claude_home),
            ),
            codex=(bool(find_codex(cfg.env)) and codex.is_dir(), any(codex.glob("plugins/cache/*/nowitknows-codex/*"))),
        )
    for host, (present, installed) in targets.items():
        if host == cfg.host or not present or installed:
            continue
        mark = cfg.data / f"{cfg.host}-{host}-later"
        try:
            last = float(mark.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            last = 0
        if time.time() - last >= C.CURSOR_LATER_DAYS * 86400:
            offers[host] = notify.FIXED[f"{host}-later"]
    return offers


def peers_told(cfg: C.Config, offers: dict[str, str], text: str) -> None:
    for host, line in offers.items():
        if line in text:
            atomic_write(cfg.path(f"{cfg.host}-{host}-later"), str(time.time()))


def install(
    cfg: C.Config, *, apply: bool = False, dry_run: bool = False, codex: bool = True, cursor: bool | None = None
) -> int:
    """Install through the hosts' own plugin managers, without inspecting logins or submitting a model turn; where
    Codex is installed, nowitknows-codex too unless `codex` is False (the user declined, `--no-codex`)."""
    from .installer_source import materialize

    with materialize(C.ROOT) as source:
        return _install(cfg, source, apply=apply, dry_run=dry_run, codex=codex, cursor=cursor)


def _install(
    cfg: C.Config, installer_root: Path, *, apply: bool, dry_run: bool, codex: bool, cursor: bool | None
) -> int:
    from .migrate import run as migrate

    commands = []
    claude_cli = shutil.which("claude", path=cfg.env.get("PATH"))
    if executable := claude_cli:

        def read(path: Path) -> dict:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

        known = read(cfg.claude_home / "plugins/known_marketplaces.json")
        installed = read(cfg.claude_home / "plugins/installed_plugins.json").get("plugins", {})
        legacy = legacy_claude_notices(cfg, executable, installed)
        if legacy:
            for notice in legacy:
                print(f"setup refused: {notice}")
            return 1
        source = known.get("nowitknows", {}).get("source", {})
        origin = {k: v for k, v in source.items() if k != "sparsePaths"} if isinstance(source, dict) else source
        registered_repo = next(
            (
                repo
                for repo in C.aliases(GITHUB)
                if origin
                in (
                    {"source": "github", "repo": repo},
                    {"source": "git", "url": f"https://github.com/{repo}.git"},
                    {"source": "git", "url": f"git@github.com:{repo}.git"},
                )
            ),
            None,
        )
        trusted = registered_repo is not None and source.get("sparsePaths", SPARSE) in (SPARSE, SPARSE[::-1])
        if isinstance(source, dict) and set(source) == {"source", "path"} and source["source"] == "directory":
            trusted = _directory_origin(cfg, source["path"], installed)
        foreign = any(
            C.is_ours(key.split("@")[0], "nowitknows") and key != "nowitknows@nowitknows" for key in installed
        )
        if foreign or ("nowitknows" in known and not trusted) or ("nowitknows@nowitknows" in installed and not trusted):
            print(
                "setup refused: foreign or unverifiable nowitknows marketplace/plugin origin; no installation changed"
            )
            if "nowitknows" in known and not trusted:
                remove = shlex.join([executable, "plugin", "marketplace", "remove", "nowitknows"])
                add = shlex.join([executable, "plugin", "marketplace", "add", GITHUB, "--sparse", *SPARSE])
                print(f"To install from {GITHUB}, run {remove}, then {add}, then repeat setup.")
                print("Your skills and memory stay in place.")
            return 1
        register = [executable, "plugin", "marketplace", "add", GITHUB]
        if (
            "nowitknows" not in known
        ):  # CLI_FLOOR, checked below, is past 2.1.284, whose sparse clones work with any git
            register += ["--sparse", *SPARSE]
        commands += [
            [executable, "plugin", "marketplace", "update", "nowitknows"] if "nowitknows" in known else register,
            [
                executable,
                "plugin",
                "update" if "nowitknows@nowitknows" in installed else "install",
                "nowitknows@nowitknows",
            ],
        ]
    executable = find_codex(cfg.env) if codex else None
    if codex and C.env("NOWITKNOWS_CODEX_BIN", cfg.env) and not executable:
        print("setup failed: the explicit NOWITKNOWS_CODEX_BIN does not name an executable")
        return 1
    if not codex:
        print("setup: nowitknows-codex not added, as asked (--no-codex)")
    if executable:
        profile = (
            C.env("NOWITKNOWS_CODEX_HOME", cfg.env) or cfg.env.get("CODEX_HOME") or str(C.home_dir(cfg.env) / ".codex")
        )
        if other := _other_codex_install(cfg, executable, profile):
            if other.partition("@")[0] != "nowitknows-codex":
                from tools.install_codex import removal_command

                removal = shlex.join(removal_command(executable, Path(profile), other))
                print(
                    f"setup: found {other} in Codex; run {removal} "
                    f"with CODEX_HOME={profile} before adding the managed plugin. Memory and skills stay in place."
                )
                return 1
            print(f"setup: {other} is already installed in Codex; left as it is")
            executable = None
        else:
            commands.append(
                [
                    sys.executable,
                    str(installer_root / "tools/install_codex.py"),
                    "--codex-bin",
                    executable,
                    "--codex-home",
                    profile,
                ]
            )
    if cursor is True:
        cursor_profile = (
            C.env("NOWITKNOWS_CURSOR_HOME", cfg.env)
            or cfg.env.get("CURSOR_CONFIG_DIR")
            or str(C.home_dir(cfg.env) / ".cursor")
        )
        commands.append(
            [
                sys.executable,
                str(installer_root / "tools/install_cursor.py"),
                "--cursor-home",
                cursor_profile,
                "--sdk",
            ]
        )
    if cursor is not True:
        for line in peer_offers(cfg).values():
            print(line)
    if not commands:
        print("No claude or codex CLI found on PATH. Install a host CLI, then repeat setup.")
    if claude_cli and not dry_run and (refused := _old_cli(cfg, claude_cli)):
        print(refused)
        return 1
    for command in commands:
        print("setup: " + shlex.join(command), flush=True)
        if not dry_run:
            try:
                result = (  # the claude CLI is stopped gently, and runs without a config folder nowitknows named
                    claude.run_child(
                        command,
                        timeout=C.SETUP_TIMEOUT_S,
                        env=claude.cli_env(cfg.env),
                        cwd=claude.relative_base(cfg),  # a relative config folder resolves where the session's did
                        capture=False,
                    )
                    if command[0] == claude_cli
                    else subprocess.run(
                        command,
                        env=C.python_env(cfg.env),
                        timeout=C.CURSOR_SETUP_TIMEOUT_S
                        if cursor is True and "install_cursor.py" in command[1]
                        else C.SETUP_TIMEOUT_S,
                        check=False,
                    )
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                print(f"setup failed: {error}; repeat setup to resume")
                return 1
            if result.returncode:
                print("setup failed; repeat setup to resume")
                return 1
    print(notes_line(cfg))
    if executable and not dry_run:
        print(
            "Codex: open /hooks and review and trust NowItKnows's exact hook definitions. "
            "Setup does not change hook trust."
        )
    if not dry_run and (cfg.skills_library / ".git").exists():
        skillsync.configure(cfg)
    if result := migrate(cfg):
        return result
    result = migrate(cfg, apply=True) if apply and not dry_run else 0
    if apply and not dry_run and not result:
        from .skills import SkillStore

        SkillStore(cfg).reconcile_shadows(Path.cwd())
    return result


def _old_cli(cfg: C.Config, exe: str) -> str | None:
    """Older CLIs can sign the user out when a short plugin command exits during a login refresh."""
    try:
        version = claude.cli_version(exe, claude.cli_env(cfg.env), claude.relative_base(cfg) or Path.cwd())
    except claude.ClaudeError as error:
        return f"setup failed: {error}; repeat setup to resume"
    if claude.version_key(version) >= claude.version_key(C.CLI_FLOOR):
        return None
    return (
        f"setup refused: your `claude` CLI ({version} at {exe}) is older than {C.CLI_FLOOR}, and on such a build its "
        f"own short commands can sign you out. Update it first, {claude.update_fix(exe)}, then repeat setup. "
        "Nothing was installed."
    )


def notes_line(cfg: C.Config) -> str:
    if cfg.project_notes_shared:
        return (
            "project notes are shared between Claude Code and Codex, in Claude's memory folder for each repo "
            "(opt out: /nowitknows:setup --separate-notes)"
        )
    return "project notes are kept separately by each app (sharing is off)"


def _object(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def separate_notes_for(cfg: C.Config) -> C.Config:
    """Opt hosts out of shared project notes: Claude Code's settings env first, which can refuse, then Codex's and
    Cursor's saved settings (older files' values kept); if saving fails, Claude's is put back."""
    settings = cfg.claude_home / "settings.json"
    before = settings.read_bytes() if settings.exists() else None

    def opt_out(data: dict) -> None:
        env = data.setdefault("env", {})
        if not isinstance(env, dict):
            raise ValueError(f"the env setting in {settings} is not an object")
        env[C.SHARE_NOTES_ENV] = "0"

    _edit_settings(settings, opt_out)
    names = ("codex-settings.json", "codex/settings.json", "settings.json")
    codex = next((s for s in (_object(cfg.data / n) for n in names) if s is not None), {})
    cursor_names = ("cursor-settings.json", "cursor/settings.json", "settings.json")
    cursor = next((s for s in (_object(cfg.data / n) for n in cursor_names) if s is not None), {})
    try:
        atomic_write(cfg.data / names[0], json.dumps({**codex, "share_project_notes": False}, indent=2))
        if cfg.host == "cursor" or (cfg.data / "cursor-settings.json").exists():
            atomic_write(
                cfg.data / "cursor-settings.json", json.dumps({**cursor, "share_project_notes": False}, indent=2)
            )
    except OSError:
        if before is None:
            settings.unlink(missing_ok=True)
        else:
            atomic_write(settings, before)
        raise
    return C.load({**cfg.env, C.SHARE_NOTES_ENV: "0"})


def _unreadable(path: Path) -> str | None:
    """Why a settings file that exists cannot be edited as a JSON object, else None."""
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return f"{path} is not valid JSON ({error})"
    return None if isinstance(value, dict) else f"{path} is not a JSON object"


def _consent(question: str, assume_yes: bool) -> bool:
    return assume_yes or input(f"{question} [y/N] ").strip().lower() == "y"


def _edit_settings(path: Path, change) -> None:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    change(data)
    atomic_write(path, json.dumps(data, indent=2) + "\n")


def run(
    cfg: C.Config,
    retention_days: int,
    git: bool,
    allow_tools: bool,
    assume_yes: bool,
    *,
    dry_run: bool = False,
    separate_notes: bool = False,
    backup_repo: str | None = None,
) -> int:
    """One-time opt-ins a plugin cannot set itself: transcript retention, git history for skills, tool permissions,
    and keeping project notes separate."""
    if dry_run:
        print(
            f"READ-ONLY setup plan: retention_days={retention_days}, git={git}, allow_tools={allow_tools}, "
            f"separate_notes={separate_notes}"
        )
        return 0
    settings = cfg.claude_home / "settings.json"
    if problem := _unreadable(settings):
        print(f"setup changed nothing: {problem}. Fix or remove that file, then run setup again.")
        return 1
    if separate_notes and _consent("Keep project notes separate for each app instead of sharing them?", assume_yes):
        try:
            cfg = separate_notes_for(cfg)
        except (OSError, ValueError) as error:
            print(f"setup changed nothing: project notes stay shared ({error})")
            return 1
        print(f"{notes_line(cfg)} ({settings}, {cfg.data / 'codex-settings.json'})")
        print(
            f"To share again: remove {C.SHARE_NOTES_ENV} from the env block of {settings} (keep the plugin option "
            "share_project_notes on), and in Codex run configure share_project_notes=on."
        )
    current = (json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}).get(
        "cleanupPeriodDays", 30
    )
    if retention_days > current and _consent(
        f"Keep Claude Code transcripts {retention_days} days instead of {current}?", assume_yes
    ):
        _edit_settings(settings, lambda d: d.update(cleanupPeriodDays=retention_days))
        print(f"cleanupPeriodDays: {current} -> {retention_days} ({settings})")
    backup_offered = False
    if git and not (cfg.skills_library / ".git").exists() and _consent(f"git init {cfg.skills_library}?", assume_yes):
        if not cfg.skills_library.exists() or not any(cfg.skills_library.iterdir()):
            skillsync.configure(cfg, backup_repo)
            backup_offered = True
        cfg.skills_library.mkdir(parents=True, exist_ok=True)
        try:
            done = subprocess.run(
                ["git", "-C", str(cfg.skills_library), "init", "-q"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
            )
            failed = (done.stderr.strip() or f"exit {done.returncode}") if done.returncode else ""
        except OSError as e:  # no git on PATH
            failed = str(e)
        if failed:
            print(f"git init failed ({failed}); skill changes are still copied to {cfg.data / 'skill-history'}")
        else:
            print(f"git initialized in {cfg.skills_library}")
    if not backup_offered and ((cfg.skills_library / ".git").exists() or backup_repo is not None):
        skillsync.configure(cfg, backup_repo)
    tools = [C.session_tools() + t for t in ("memory", "session_search", "skill_view", "skill_manage")]
    if allow_tools and _consent(
        "Let nowitknows's memory, skill, and session-search tools run without asking?", assume_yes
    ):

        def allow(d: dict) -> None:
            rules = d.setdefault("permissions", {}).setdefault("allow", [])
            rules += [t for t in tools if t not in rules]

        _edit_settings(settings, allow)
        print(f"allowed {len(tools)} nowitknows tools ({settings})")
    from .skills import SkillStore

    SkillStore(cfg).reconcile_shadows(Path.cwd())
    print(status(cfg, Path.cwd()))
    return 0


def status(cfg: C.Config, project: Path) -> str:
    from .skills import SkillStore

    modes = native.detect(cfg, project)
    mem = EntryStore(cfg.data / "memory")
    st = State(cfg.data / "state.json", cfg.host, cfg.legacy_host).read()
    week = time.time() - C.WEEK_S
    runs = jsonl_rows(cfg.data / "usage.jsonl")
    recent = [r for r in runs if r.get("ts", 0) > week]
    reviews = sum(
        1 for r in jsonl_rows(cfg.data / "runs.jsonl") if r.get("ts", 0) > week
    )  # usage has one per model call
    by_model: dict[str, int] = {}
    for r in recent:
        old = sum(r.get(k, 0) for k in ("input", "cache_write", "cache_read", "output"))  # records before models
        models = r["models"] if isinstance(r.get("models"), dict) else {r["model"]: old}
        for m, n in models.items():
            family = next((f for f in ("opus", "sonnet", "haiku", "fable") if f in m), m)
            by_model[family] = by_model.get(family, 0) + n
    tokens = ", ".join(f"{m} {n:,}" for m, n in sorted(by_model.items(), key=lambda kv: -kv[1]))
    remote = claude.runner_session(cfg)
    in_session = st["sessions"].get(cfg.env.get("CLAUDE_CODE_SESSION_ID", ""), {}).get("in_session")
    if cfg.host == "cursor":
        from .cursor_host import doctor

        report = doctor(cfg)
        uses = "your Cursor account for all background roles"
        if not cfg.review:
            uses = "nothing (background learning is off)"
        elif report["learning_message"]:
            uses = report["learning_message"]
        extra = (
            "Cursor account spending settings decide whether calls can bill past the plan; "
            "allow_extra_usage does not enforce this"
        )
    elif in_session and not remote:
        uses = (
            "current Claude Code session, subscription windows checked before each call; end reviews use CLI or queue"
        )
        extra = (
            "allowed past subscription limits (allow_extra_usage is on)"
            if cfg.allow_extra_usage
            else "calls skipped at 100% of any subscription window (allow_extra_usage is off)"
        )
    else:
        ok, auth = claude.auth_check(cfg)
        idle = "none in this session" if remote else "nothing, paused"
        uses = f"your Claude subscription ({auth})" if ok else f"{idle}: {auth}"
        extra = (
            "not used here (no background runs in this session)"
            if remote
            else extra_usage_row(cfg, runs=ok and cfg.review, setup_token=ok and auth.startswith("setup-token"))
        )
    applied = sum(1 for e in jsonl_rows(cfg.data / "skills_ledger.jsonl") if e.get("ts", 0) > week and e.get("verdict"))
    rejected = sum(  # judge rejections only: a change that expired unjudged is not one
        1
        for r in jsonl_rows(cfg.data / "rejected.jsonl")
        if r.get("ts", 0) > week
        and r.get("reason") not in ("expired", "over_limit", "support write refused", "support write rejected")
    )
    staged = (len(list((cfg.data / "staged").glob("*.json"))) if (cfg.data / "staged").exists() else 0) + waiting(cfg)
    archive = (
        sum(p.stat().st_size for p in (cfg.data / "archive").rglob("*.gz")) if (cfg.data / "archive").exists() else 0
    )
    used = {t: f"{mem.size(mem.entries(t))}/{mem.limits[t]}" for t in ("user", "memory")}
    global_tier = f"user {used['user']}, memory {used['memory']}"
    snapshot = (
        time.strftime("%Y-%m-%d %H:%M", time.localtime(cfg.options_snapshot_at))
        if cfg.options_snapshot_at is not None
        else None
    )
    rows = [
        ("hosted account memory", modes.hosted_memory),
        ("native auto-memory", modes.auto_memory),
        *(
            [
                (
                    "plugin settings",
                    f"as of your last prompt, {snapshot}; a change made since then shows after your next prompt",
                )
            ]
            if cfg.options_snapshot_at is not None
            else []
        ),
        ("shared project notes", "on (Claude Code and Codex)" if modes.shared else "off"),
        (
            "notes home",
            f"{modes.fallback_dir} (shared)"
            if modes.shared
            else "hosted project memory (nowitknows defers)"
            if modes.hosted_memory
            else f"{modes.fallback_dir} (nowitknows's, while native auto-memory is off)",
        ),
        ("project memory owner", f"{modes.project_owner} ({modes.project_dir})"),
        ("global tier", global_tier if modes.global_tier else "off (native account memory)"),
        ("project memories", len(TopicStore(modes.project_dir).topics())),
        ("project notes not saved last 7d", refusals(cfg, week)),
        *([("older project notes not shared yet", unshared_row(cfg, modes.key))] if modes.shared else []),
        *skillsync.rows(cfg),
        ("skills used (all time)", sum(st["usage"].values())),
        *([("skills without links", ", ".join(missing))] if (missing := SkillStore(cfg).unlinked()) else []),
        ("background learning", learning_row(cfg)),
        ("background tokens today", budget_row(cfg)),
        ("background runs use", uses),
        ("extra usage", extra),
        ("staged changes", staged),
        (
            "background runs last 7d",
            f"{_count(reviews, 'review')}, {_count(len(recent), 'model run')}"
            + (f", tokens processed: {tokens}" if tokens else ""),
        ),
        ("judged skill changes last 7d", f"{applied} applied, {rejected} rejected"),
        ("skills needing attention", attention_row(cfg)),
        ("sessions last 7d by surface", surfaces_row(st, week)),
        ("transcript archive", f"{archive / 1_048_576:.1f} MB of {cfg.archive_max_mb} MB"),
        *(
            [("compressed Codex history", history)]
            if (history := compressed_history_row(cfg.data, unread_only=True))
            else []
        ),
    ]
    from .report import tokens as count_tokens
    from .router import skill_shadows

    rows.extend(
        (
            f"shadowed skill copy {shadow['name']}",
            f"{shadow['path']}; using {shadow['canonical']}; foreign copy left untouched",
        )
        for shadow in skill_shadows(cfg, project)
    )

    for host in ("claude", "codex", "cursor"):
        tokens = sum(sum(count_tokens(r).values()) for r in recent if r.get("host", "claude") == host)
        count = sum(
            r.get("host", "claude") == host and r.get("ts", 0) > week for r in jsonl_rows(cfg.data / "runs.jsonl")
        )
        rows.append((f"{host} last 7d", f"{count} reviews, {tokens:,} tokens"))
    return "\n".join(f"{k}: {v}" for k, v in rows)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def extra_usage_row(cfg: C.Config, runs: bool, setup_token: bool) -> str:
    try:
        flag = None if setup_token else claude.extra_usage_flag(cfg)
    except claude.ClaudeError as e:
        return f"unknown: {e}"
    if flag is None:
        unseen = "cannot be seen for this login (Claude Code caches no account details for it)"
        if not cfg.allow_extra_usage:
            return f"{unseen}; background learning pauses (allow_extra_usage is off)"
        return f"{unseen}; if it is on, background runs past your plan's limits are billed"
    if not flag:
        return "off, as of Claude Code's last account refresh"
    if not cfg.allow_extra_usage:
        return "on; background learning pauses while it is on (allow_extra_usage is off)"
    if runs:
        return "on; background runs past your plan's limits are billed (allow_extra_usage off pauses them instead)"
    return "on (background runs are paused or turned off)"


def budget_row(cfg: C.Config) -> str:
    from .worker import tokens_today

    used = tokens_today(cfg)
    return f"{used:,}" + (
        f" of {cfg.review_daily_tokens:,} a day" if cfg.review_daily_tokens > 0 else " (no daily limit)"
    )


def learning_row(cfg: C.Config) -> str:
    if cfg.host == "cursor" and cfg.review:
        from .cursor_host import doctor

        report = doctor(cfg)
        if report["learning"] == "paused":
            return "paused; " + report["learning_message"]
    if not cfg.review:
        return "off (turn it on with /plugin configure nowitknows@nowitknows)"
    scripted = "scripted claude -p and SDK sessions too" if cfg.review_unattended else "not scripted claude -p runs"
    return f"on in the sessions you work in, {scripted} (turn it off with /plugin configure nowitknows@nowitknows)"


def surfaces_row(st: dict, since: float) -> str:
    """Sessions active since `since`, by entrypoint plus any environment kind (`+bridge` under Remote Control), and
    how many counted as working sessions."""
    seen: dict[str, list[int]] = {}
    for s in st.get("sessions", {}).values():
        sf = s.get("surface") if isinstance(s.get("surface"), dict) else None
        if sf and s.get("seen", 0) > since:
            label = "+".join(str(v) for v in (sf.get("entrypoint") or "unknown", sf.get("environment_kind")) if v)
            row = seen.setdefault(label, [0, 0])
            row[0] += 1
            row[1] += bool(sf.get("counted"))
    return ", ".join(f"{e} {n} ({c} working)" for e, (n, c) in sorted(seen.items())) or "none recorded"


def attention_row(cfg: C.Config) -> str:
    from .report import needs_attention

    flagged = needs_attention(cfg)
    if not flagged:
        return "none"
    rule = f"changed after {C.ATTENTION_CHANGES} of their last {C.ATTENTION_LOADS} reviewed loads"
    return f"{', '.join(flagged)} ({rule}; /nowitknows:report has details)"


def changes(cfg: C.Config, n: int) -> str:
    out = []
    for path, kind in ((cfg.data / "skills_ledger.jsonl", "skill"), (cfg.data / "memory_ledger.jsonl", "memory")):
        out += [{**e, "kind": kind} for e in jsonl_rows(path) if e.get("action") != "scope"]  # scopes: listed below
    rejected = sorted((cfg.data / "rejected").glob("*.md")) if (cfg.data / "rejected").exists() else []
    out += [
        {"ts": p.stat().st_mtime, "kind": "rejected", "action": "rejected", "name": p.stem, "path": str(p)}
        for p in rejected
    ]
    order = {id(e): i for i, e in enumerate(out)}
    out.sort(key=lambda e: (e["ts"], order[id(e)]), reverse=True)
    lines = []
    for e in out[:n]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["ts"]))
        label = e.get("name") or e.get("target") or ""
        verdict = f" · judge {e['verdict']}" if e.get("verdict") else ""
        # A project note carries its writer as metadata.source; a removal names the app that removed it.
        note = f" · nowitknows-{e.get('host') or 'claude'}" if e.get("store") == "project" else ""
        backup = f" · [backup]({notify.link(e['backup'])})" if e.get("backup") else ""
        tidy = ""
        if e["kind"] == "memory" and e["action"] == "tidy":
            tidy = f" · {len(e['before'].strip())} to {len(e['after'].strip())} characters, nothing dropped"
        lines.append(
            f"- {when} · {e['kind']} {e['action']} [{label}]({notify.link(e['path'])}){verdict}"
            f" · {e.get('source', '')}{note}{backup}{tidy}"
        )
    from .scope import listing

    return "\n".join([*lines, *(listing(cfg, n) if lines else [])]) or "No nowitknows changes yet."
