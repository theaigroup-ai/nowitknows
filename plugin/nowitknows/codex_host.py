from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import config as C
from . import hooks, native, notejudge, notify, router, scope, sessions
from .delivery import masked_delivery
from .memory import EntryStore, TopicStore, memory_read_pointer, note_pointer, ownership
from .safety import redact
from .skills import SkillStore, norm_file
from .state import State, atomic_write, count_session, file_lock, session

SKILL_NOTE = "nowitknows skills that may apply; "  # then asks to name the ones not loaded in the session yet


def configuration(env: dict | None = None, *, create: bool = True) -> C.Config:
    source = C.environment(env)
    home = C.home_dir(source)
    codex = (
        Path(C.env("NOWITKNOWS_CODEX_HOME", source) or source.get("CODEX_HOME") or home / ".codex")
        .expanduser()
        .resolve()
    )
    shared = (
        Path(C.env("NOWITKNOWS_SHARED_DATA_DIR", source) or C.env("NOWITKNOWS_DATA_DIR", source) or C.data_dir(source))
        .expanduser()
        .resolve()
    )
    default_data = shared if codex.resolve() == (home / ".codex").resolve() else C.preferred_path(codex, "nowitknows")
    data = Path(C.env("NOWITKNOWS_CODEX_DATA_DIR", source) or default_data).expanduser().resolve()
    if codex.resolve() != (home / ".codex").resolve() and data.resolve() == shared.resolve():
        raise ValueError("Only the default Codex profile can join the shared store")
    skills = Path(C.env("NOWITKNOWS_CODEX_SKILLS_DIR", source) or codex / "skills").expanduser().resolve()
    # Shared project notes live in Claude Code's real config folder. Releases before 0.8.0 pointed this at a stand-in
    # under the data folder, which jobs queued by them still carry.
    claude_config = source.get("CLAUDE_CONFIG_DIR")
    if claude_config and Path(claude_config).expanduser().resolve() == (data / "host").resolve():
        claude_config = None
    mapped = {k: v for k, v in source.items() if not k.startswith("CLAUDE_")}
    # Preserve the shared notes location while stripping Claude-only settings.
    if source.get("CLAUDE_COWORK_MEMORY_PATH_OVERRIDE"):
        mapped["CLAUDE_COWORK_MEMORY_PATH_OVERRIDE"] = source["CLAUDE_COWORK_MEMORY_PATH_OVERRIDE"]
    with contextlib.suppress(OSError, ValueError):  # unreadable now: each model call holds back (saved_guards)
        mapped.update(saved_settings(data))
    mapped.update(
        NOWITKNOWS_HOST="codex",
        NOWITKNOWS_SHARED_DATA_DIR=str(shared),
        NOWITKNOWS_CODEX_HOME=str(codex),
        CODEX_HOME=str(codex),
        NOWITKNOWS_CODEX_DATA_DIR=str(data),
        NOWITKNOWS_CODEX_SKILLS_DIR=str(skills),
        NOWITKNOWS_LEGACY_HOST=C.env("NOWITKNOWS_LEGACY_HOST", source)
        or ("claude" if codex.resolve() == (home / ".codex").resolve() and data == shared else "codex"),
        NOWITKNOWS_DATA_DIR=str(data),
        NOWITKNOWS_SKILLS_DIR=str(skills),
        NOWITKNOWS_CLAUDE_SKILLS_DIR=C.env("NOWITKNOWS_CLAUDE_SKILLS_DIR", source)
        or str(Path(claude_config or home / ".claude") / "skills"),
        NOWITKNOWS_CLAUDE_TRANSCRIPTS_DIR=C.env("NOWITKNOWS_CLAUDE_TRANSCRIPTS_DIR", source)
        or str(Path(claude_config or home / ".claude") / "projects"),
        NOWITKNOWS_TRANSCRIPTS_DIR=str(data / "transcripts"),
        CLAUDE_CONFIG_DIR=claude_config or str(home / ".claude"),
        CLAUDE_CODE_SESSION_ATTENDED="1",
    )
    if not claude_config:  # named here for the notes folder; a claude CLI started from this env must not get it
        mapped[C.CONFIG_DIR_INJECTED_ENV] = "1"
    for name in ("REVIEWER_MODEL", "JUDGE_MODEL"):
        mapped.setdefault("NOWITKNOWS_" + name, C.CODEX_REVIEW_MODEL)
    mapped.setdefault("NOWITKNOWS_CHEAP_MODEL", C.CODEX_MODEL)
    return C.load(mapped, create=create)


# What queued work takes from the settings saved since it was queued: they change what a review reads and where its
# notes go, never what it costs, so they follow the user's latest choice.
CONTENT_SETTINGS = frozenset({"bounded_review_skills", "share_project_notes", "metrics"})
# Queued work must not loosen spending consent, so only stricter limits replace its saved settings.
GUARD_SETTINGS = frozenset({"review", "review_daily_tokens", "allow_extra_usage"})
# The guards while the saved settings cannot be read: no review, no extra usage, the least cap (0 would mean none).
CLOSED_GUARDS = {
    "NOWITKNOWS_REVIEW": "False",
    "NOWITKNOWS_ALLOW_EXTRA_USAGE": "False",
    "NOWITKNOWS_REVIEW_DAILY_TOKENS": "1",
}


def saved_settings(data: Path, keys: frozenset[str] | None = None) -> dict:
    """The settings saved with `configure` in the Codex store `data` (only `keys`, if given), as NOWITKNOWS_<KEY>; none
    when no file is saved, OSError or ValueError when the one saved cannot be read or parsed."""
    files = (data / "codex-settings.json", data / "codex/settings.json", data / "settings.json")
    path = next((p for p in files if p.exists()), None)
    if path is None:
        return {}
    saved = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(saved, dict):
        raise ValueError(f"{path.name} holds no settings object")
    wanted = configurable() if keys is None else configurable() & keys
    return {"NOWITKNOWS_" + key.upper(): str(value) for key, value in saved.items() if key in wanted}


def saved_guards(cfg: C.Config) -> dict:
    """The guards saved in Codex now; while the saved settings cannot be read, closed, with the cause logged."""
    try:
        return saved_settings(cfg.data, GUARD_SETTINGS)
    except (OSError, ValueError) as error:
        _error(cfg, f"saved settings cannot be read, so background learning is held until they can: {error}")
        return dict(CLOSED_GUARDS)


def configurable() -> set[str]:
    return {
        "metrics",
        "review",
        "share_project_notes",
        "review_unattended",
        "allow_extra_usage",
        "reviewer_model",
        "judge_model",
        "cheap_model",
        "memory_interval",
        "skill_interval",
        "review_daily_tokens",
        "router_threshold",
        "archive_max_mb",
        "bounded_review_skills",
        "skill_scope",
    }


def configure(cfg: C.Config, values: dict) -> dict:
    if set(values) - configurable():
        raise ValueError("unknown Codex nowitknows setting")
    current = _read(
        cfg.data / "codex-settings.json", _read(cfg.data / "codex/settings.json", _read(cfg.data / "settings.json", {}))
    )
    current = current if isinstance(current, dict) else {}
    for key, value in values.items():
        default = getattr(cfg, key)
        if type(default) is bool:
            if str(value).lower() not in ("true", "false", "on", "off", "1", "0"):
                raise ValueError("boolean settings use on or off")
            value = str(value).lower() in ("true", "on", "1")
        elif type(default) in (int, float):
            value = type(default)(value)
            if value < 0 or not __import__("math").isfinite(value):
                raise ValueError("numeric settings must be finite and nonnegative")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError("model name is required")
        current[key] = value
    atomic_write(cfg.path("codex-settings.json"), json.dumps(current, indent=2))
    return current


def memory_modes(cfg: C.Config, project: Path) -> native.Modes:
    key = native.project_key(project)
    shared = cfg.project_notes_shared
    chain = native.settings_chain(project, cfg.claude_home) if shared else []
    folder = (
        native.native_memory_dir(cfg.env, project, cfg.claude_home, chain, key)
        if shared
        else cfg.data / "projects" / key / "memory"
    )
    return native.Modes(False, False, folder, folder, key, shared)


def project_notes(cfg: C.Config, modes: native.Modes, prompt: str, cached: dict, append) -> dict:
    delivered, pointers, owns = {}, [], ownership(cfg.data, modes.shared)
    for doc, _ in router.route_request(
        router.topic_index(TopicStore(modes.fallback_dir)), prompt, cfg.router_threshold
    ):
        with contextlib.suppress(OSError, UnicodeError):
            path = Path(doc.path)
            raw, key = path.read_bytes(), "memory:" + str(path.resolve())
            if not owns(path, raw):  # Claude Code's, or changed since nowitknows wrote it: never its body
                line, fingerprint = note_pointer(path, raw, cfg), "pointer:" + hashlib.sha256(raw).hexdigest()
                if line and cached.get(key) != fingerprint:
                    pointers.append((key, fingerprint, line))
                continue
            fingerprint = hashlib.sha256(raw).hexdigest()
            if cached.get(key) == fingerprint:
                append(f"Project memory already supplied in this context: {doc.path}")
            elif append(
                masked_delivery(
                    f"<nowitknows_project_memory>\n{raw.decode('utf-8')}\n</nowitknows_project_memory>", cfg
                )
            ):
                delivered[key] = fingerprint
            else:
                append(f"Relevant project memory: {memory_read_pointer(path.stem, cfg.host)}")
    listed = "\n".join(f"- {line}" for _, _, line in pointers)
    if pointers and append(f"Project notes that may apply; read the ones you need:\n{listed}"):
        delivered.update({key: fingerprint for key, fingerprint, _ in pointers})
    return delivered


def _read(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _error(cfg: C.Config, text: str) -> None:
    with contextlib.suppress(OSError), open(cfg.path("errors.log"), "a", encoding="utf-8") as f:
        f.write(f"{time.ctime()} codex {redact(text)}\n")


def _catalog_path(cfg: C.Config, cwd: Path) -> Path:
    key = hashlib.sha256(str(cwd.resolve()).encode()).hexdigest()
    return cfg.path("native-skills", key + ".json")


def _catalog_stamp(cfg: C.Config, cwd: Path) -> list:
    paths = [Path(cfg.env["CODEX_HOME"]) / "config.toml"]
    paths += [p / ".codex/config.toml" for p in (cwd, *cwd.parents)]
    result = []
    for path in paths:
        with contextlib.suppress(OSError):
            stat = path.stat()
            result.append([str(path.resolve()), stat.st_mtime_ns, stat.st_size])
    return result


def _catalog_source(item: dict) -> str:
    plugin = item.get("pluginId")
    if isinstance(plugin, str) and plugin.strip():
        return "plugin"
    if plugin is not None:
        return "unknown"
    scope = item.get("scope")
    if isinstance(scope, str):
        if scope in ("user", "repo"):
            return "local"
        if scope == "system":
            return "system"
    return "unknown"


def _skill_files(cfg: C.Config, cwd: Path, *, resolved: dict[Path, Path] | None = None) -> list[tuple[Path, str]]:
    catalog = _read(_catalog_path(cfg, cwd), {})
    if not isinstance(catalog, dict) or catalog.get("stamp") != _catalog_stamp(cfg, cwd):
        return []
    entries = catalog.get("skills", [])
    if not isinstance(entries, list):
        return []
    resolved = {} if resolved is None else resolved
    project = native.repo_root(cwd) or cwd
    roots = []
    for current in (cwd, *cwd.parents):
        roots.append(current / ".agents/skills")
        if current == project:
            break
    roots.append(cfg.skills_home)
    found = [path for root in roots for path in router.find_skills(root, resolved=resolved)]
    known_reals = {str(real): real for real in resolved.values()}
    sources, enabled = {}, set()
    for item in entries:
        if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"]:
            path = Path(item["path"])
            real = resolved.get(path)
            if real is None:
                real = known_reals.get(str(path))
                real = resolved[path] = real if real is not None else path.resolve()
            source = _catalog_source(item) if item.get("enabled") is True else "unknown"
            if item.get("enabled") is True:
                enabled.add(real)
            if sources.get(real, "local") == "local":
                sources[real] = source
    found += [Path(item["path"]) for item in entries if isinstance(item, dict) and isinstance(item.get("path"), str)]
    found += router.find_skills(cfg.skills_library, resolved=resolved)
    library = cfg.skills_library.resolve()
    canonical_names = router.canonical_names(cfg, resolved.values())
    home_roots = router.home_skill_roots(cfg)
    enabled_names = {
        path.parent.name for path in found if router.home_skill(path, home_roots) and resolved.get(path) in enabled
    }
    result = {}
    for path in found:
        name = path.parent.name
        real = resolved.get(path)
        if real is None:
            real = resolved[path] = path.resolve()
        canonical = name in canonical_names and real.parent.parent == library
        if name in canonical_names and not canonical and router.home_skill(path, home_roots):
            continue
        if real in enabled or (canonical and name in enabled_names and real not in sources):
            previous = result.get(name)
            if previous is None:
                result[name] = (path, sources.get(real, "local"))
    return list(result.values())


def skill_files(cfg: C.Config, cwd: Path) -> list[Path]:
    return [path for path, _ in _skill_files(cfg, cwd)]


def skill_file(cfg: C.Config, cwd: Path, name: str) -> Path | None:
    """Resolve validated skill_view arguments against the real, enabled catalog paths."""
    store = SkillStore(cfg)
    for skill in skill_files(cfg, cwd):
        with contextlib.suppress(OSError, UnicodeError):
            meta, _ = router.frontmatter(skill.read_text(encoding="utf-8-sig"))
            if (meta.get("name") or skill.parent.name) == name:
                return store.file(name, folder=skill.parent)
    return None


def skill_index(cfg: C.Config, cwd: Path, usage: dict | None = None) -> router.BM25:
    resolved: dict[Path, Path] = {}
    files = _skill_files(cfg, cwd, resolved=resolved)
    return router.skill_index(
        [],
        cfg.claude_home,
        [],
        cfg.data / "skill_triggers.json",
        cfg.path("router", "codex.pkl"),
        usage or {},
        sources={str(path): source for path, source in files},
        files=[(path, "") for path, _ in files],  # the native inventory already established visibility and precedence
        cfg=cfg,
        resolved=resolved,
    )


def _context(event: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}} if text else {}


def _new_context(s: dict) -> None:
    s["codex_context_epoch"] = uuid.uuid4().hex
    s["codex_delivered"] = {}


def _opened_skill(cfg: C.Config, data: dict, cwd: Path) -> Path | None:
    response = data.get("tool_response")
    result = hooks._json_in(response)
    if (
        not str(data.get("tool_name", "")).endswith("__skill_view")
        or hooks._edit_refused(data)
        or (isinstance(response, dict) and any(response.get(k) for k in ("isError", "is_error", "error")))
        or result.get("ok") is not True
    ):
        return None
    if "tool_input" not in data:  # older native pointer-open events carry only the successful response
        path = result.get("path")
        if not isinstance(path, str) or not isinstance(result.get("content"), str):
            return None
        with contextlib.suppress(OSError, ValueError):
            resolved = Path(path).resolve()
            return next((p for p in skill_files(cfg, cwd) if p.resolve() == resolved), None)
        return None
    args = data.get("tool_input") or {}
    if not isinstance(args, dict):
        return None
    name, fp = args.get("name"), args.get("file_path", "")
    if not isinstance(name, str) or not isinstance(fp, str) or norm_file(fp):
        return None
    return skill_file(cfg, cwd, name)


def _commands(line: str) -> str:
    for command in ("keep", "changes", "report", "status", "setup"):
        line = line.replace(f"/nowitknows:{command}", f"$nowitknows-codex:nowitknows-{command}")
    return line


def _scripted(data: dict) -> bool:
    """A subagent's event (`agent_id`, Codex 0.165's hook input schemas) or a source a job records as unattended."""
    return bool(data.get("agent_id")) or data.get("source") in ("exec", "subagent")


def _rollout_attended(cfg: C.Config, path: object) -> bool | str | None:
    """Whether a person works in the session, from the first line of its own rollout (the hook input's
    `transcript_path`): an interactive originator and no exec or subagent source; "subagent" for a subagent's thread;
    None while that line is unread."""
    if not isinstance(path, str) or not path.endswith(".jsonl"):
        return None
    try:
        home = Path(cfg.env["NOWITKNOWS_CODEX_HOME"]).expanduser().resolve()
        file = Path(path).resolve()
        if not any(file.is_relative_to(home / name) for name in ("sessions", "archived_sessions")):
            return None
        with file.open("rb") as stream:
            line = stream.readline(C.CODEX_META_MAX_BYTES + 1)
        meta = json.loads(line) if line.endswith(b"\n") else None
    except (OSError, ValueError):
        return None
    payload = meta.get("payload") if isinstance(meta, dict) and meta.get("type") == "session_meta" else None
    if not isinstance(payload, dict):
        return None
    source = payload.get("source")
    if isinstance(source, dict) or source == "subagent":
        return "subagent"
    unattended = source in ("exec", "mcp")
    return payload.get("originator") in C.CODEX_ATTENDED_ORIGINATORS and not unattended


def _attended(cfg: C.Config, s: dict, data: dict) -> bool:
    """Automated sessions have no reader, so only attended sessions receive skill-use text and notices."""
    if s.get("codex_attended") is None:
        s["codex_attended"] = _rollout_attended(cfg, data.get("transcript_path"))
    return s["codex_attended"] is True and not _scripted(data)


def _known(st: State, sid: str, data: dict) -> bool:
    return st.read()["sessions"].get(sid, {}).get("codex_attended") is True and not _scripted(data)


def _shows(s: dict, data: dict) -> bool:
    """Showing a notice does not tell the model, so it stays owed until an attended prompt can deliver it."""
    return not data.get("agent_id") and data.get("source") != "subagent" and s.get("codex_attended") != "subagent"


def _shown(st: State, sid: str, data: dict) -> bool:
    return _shows(st.read()["sessions"].get(sid, {}), data)


def _runtimes(cfg: C.Config) -> Path:
    """bin/nowitknows.sh's Codex-first environments: configuration() already set CODEX_HOME the launcher's way."""
    return C.codex_runtime(Path(cfg.env["CODEX_HOME"]))


def setup_finished(cfg: C.Config) -> None:
    """Queue, once, that the background setup of a Codex install from the repo's catalog finished (bin/bootstrap.sh
    leaves the file beside the environment): shown in Codex sessions but a subagent's, told at an attended prompt."""
    with contextlib.suppress(OSError):
        for path in _runtimes(cfg).glob("*/bootstrap.finished"):
            try:
                path.unlink()  # whoever removes it says it, so it is said once
            except FileNotFoundError:
                continue
            line = f"{notify.PREFIX} finished setting up in Codex; if its tools are not running in this session, "
            notify.push(cfg, line + "they start in the next one.", account=True, tell={"kind": "codex-setup-done"})
            return


def _claude_has_nowitknows(claude_home: Path) -> bool:
    """nowitknows in Claude Code, as its plugin registry or plugin cache shows it: file reads only, no claude call."""
    with contextlib.suppress(OSError, ValueError, AttributeError):
        installed = json.loads((claude_home / "plugins/installed_plugins.json").read_text(encoding="utf-8"))
        if any(C.is_ours(str(key).partition("@")[0], "nowitknows") for key in installed.get("plugins") or {}):
            return True
    with contextlib.suppress(OSError):
        return any(path for name in C.aliases("nowitknows") for path in claude_home.glob(f"plugins/cache/*/{name}/*"))
    return False


def claude_later(cfg: C.Config) -> bool:
    """Whether an attended Codex prompt owes the line that Claude Code is there without nowitknows: `claude` on PATH
    and files only; due once per CLAUDE_LATER_DAYS, never once NowItKnows is installed."""
    mark = cfg.path("claude-later")
    try:
        said = mark.read_text(encoding="utf-8").strip()
    except OSError:
        said = ""
    if said == "installed":
        return False
    try:
        last = float(said) if said else 0.0
    except ValueError:  # a damaged mark counts as never told
        last = 0.0
    path = cfg.env.get("PATH")  # the session's own; none means no claude found
    if time.time() - last < C.CLAUDE_LATER_DAYS * 86400 or not path or not shutil.which("claude", path=path):
        return False
    if _claude_has_nowitknows(Path(cfg.env.get("CLAUDE_CONFIG_DIR") or C.home_dir(cfg.env) / ".claude")):
        with contextlib.suppress(OSError):
            atomic_write(mark, "installed\n")
        return False
    return True


def _missed_start(cfg: C.Config, sid: str) -> bool:
    """Whether bin/nowitknows.sh noted that this session started before its environment was ready, taking the note."""
    taken = False
    stale = time.time() - C.START_MISSED_MAX_AGE_S
    with contextlib.suppress(OSError):
        for path in _runtimes(cfg).glob("*/start-missed.*"):
            with contextlib.suppress(FileNotFoundError):
                if path.name == f"start-missed.{sid}":
                    path.unlink()
                    taken = True
                elif path.stat().st_mtime < stale:  # a session that never got a prompt
                    path.unlink()
    return taken


def _joined(start: dict, prompt: dict) -> dict:
    """A replayed start's output folded into its prompt's, the start's context cut to fit CODEX_CONTEXT_CHARS."""
    after = (prompt.get("hookSpecificOutput") or {}).get("additionalContext", "")
    before = (start.get("hookSpecificOutput") or {}).get("additionalContext", "")
    before = before[: max(0, C.CODEX_CONTEXT_CHARS - len(after) - 2)]
    output = {k: v for k, v in prompt.items() if k != "systemMessage"}
    if shown := "\n".join(m for m in (start.get("systemMessage"), prompt.get("systemMessage")) if m):
        output["systemMessage"] = shown
    if text := "\n\n".join(part for part in (before, after) if part):
        output.update(_context("UserPromptSubmit", text))
    return output


def _notices(
    cfg: C.Config,
    output: dict,
    event: str = "",
    limit: int = 0,
    attended: bool = False,
    project: str | None = None,
    shows: bool = False,
    extra: tuple[str, ...] = (),
) -> dict:
    """Queued notices as the systemMessage where `shows` (else the queue is left as it is); at a prompt (`event`) in
    a session known to be attended they, and `extra` (fixed lines owed beside them), are also told to the model there,
    once, within `limit` characters."""
    ctx = (output.get("hookSpecificOutput") or {}).get("additionalContext", "")

    def fits(block: str, _shown: list[str]) -> bool:
        return len(ctx) + 2 + len(block) <= limit

    if not shows:
        return output
    try:
        tell = fits if event and attended else None
        lines, block = notify.deliver(
            cfg, tell, wait_s=C.NOTICE_LOCK_WAIT_S, words=_commands, project=project, extra=extra if tell else ()
        )
    except TimeoutError:
        return output
    if lines:
        output["systemMessage"] = "\n".join(
            _commands(re.sub(r"file://\S+", lambda match: "[view](<" + unquote(urlsplit(match[0]).path) + ">)", line))
            for line in notify.compact(lines)
        )
    if block:
        output.update(_context(event, f"{ctx}\n\n{block}" if ctx else block))
    return output


def _protocol(modes: native.Modes | None = None) -> str:
    notes = (
        f"This repo's project notes in {modes.fallback_dir} are shared with Claude Code: read them freely, change them "
        "only with mcp__nowitknows__memory target 'project'. "
        if modes and modes.shared
        else ""
    )
    adapter = (
        "\nCodex adapter: learned skills and NowItKnows memory have their own store, managed with mcp__nowitknows__ "
        "tools; leave Codex's native memory and other plugins' stores unchanged. Learned skills are shared with Claude "
        "Code: write shell commands, file paths and plain steps, and name a host tool (a host tool name or any mcp__ "
        'id) only under a "Platform notes" heading naming Claude Code or Codex. User-authored skills are read-only; '
        f"create a learned skill under another name instead. {notes}"
    )
    protocol = C.prompt("skills_protocol.md").replace(C.session_tools(), "mcp__nowitknows__")
    protocol = protocol.replace(
        "load it with the Skill tool",
        "use its complete text below, or read its SKILL.md file if only a path is supplied",
    )
    return protocol + adapter.rstrip()


def _job_env(cfg: C.Config) -> dict:
    from .modreview import _settings

    return {**_settings(cfg), **{k: cfg.env[k] for k in ("HOME", "USERPROFILE", "PATH") if k in cfg.env}}


def queue(cfg: C.Config, data: dict, reason: str) -> Path:
    record = {
        "id": uuid.uuid4().hex,
        "session": data["session_id"],
        "cwd": data["cwd"],
        "transcript": data.get("transcript_path") or "",
        "turn": data.get("turn_id") or "",
        "source": data.get("source") if data.get("source") in ("subagent", "exec", "mcp") else "",
        "reason": reason,
        "ts": time.time(),
        "env": _job_env(cfg),
    }
    path = cfg.path("codex-jobs", record["id"] + ".json")
    atomic_write(path, json.dumps(record))
    return path


def spawn(cfg: C.Config, data: dict, reason: str) -> None:
    queue(cfg, data, reason)
    if C.env("NOWITKNOWS_DRY_RUN", cfg.env) == "1":
        return
    env = {k: v for k, v in cfg.env.items() if k in ("HOME", "USERPROFILE", "PATH", "SYSTEMROOT", "TMP", "TEMP")}
    env.update(_job_env(cfg), PYTHONPATH=str(C.ROOT), NOWITKNOWS_HOST="codex", NOWITKNOWS_CHILD="1")
    python = getattr(sys, "_base_executable", sys.executable) if sys.platform == "win32" else sys.executable
    argv = [python, str(C.ROOT / "bin" / "nowitknows_codex.py"), "worker"]
    with open(cfg.path("worker.log"), "a", encoding="utf-8") as log:
        args = {"env": env, "cwd": str(C.ROOT), "stdin": subprocess.DEVNULL, "stdout": log, "stderr": log}
        if sys.platform == "win32":
            try:
                subprocess.Popen(argv, **args, creationflags=hooks.DETACHED | hooks.BREAKAWAY_FROM_JOB)
            except OSError:
                subprocess.Popen(argv, **args, creationflags=hooks.DETACHED)
        else:
            subprocess.Popen(argv, **args, start_new_session=True)


def handle(event: str, payload: dict, cfg: C.Config, *, prompt_start: bool = False) -> dict:
    if event == "PreToolUse":
        from .notes_gate import gate

        reason = gate(cfg, payload)
        return (
            {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
            if reason
            else {}
        )
    if cfg.child:
        return {}
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1," + str(C.CODEX_SESSION_ID_MAX) + r"}", sid):
        return {}
    cwd = Path(payload.get("cwd") or os.getcwd())
    data = {**payload, "session_id": sid, "cwd": str(cwd)}
    st = State(cfg.path("state.json"), cfg.host, cfg.legacy_host)
    modes = memory_modes(cfg, cwd)
    if event == "SessionStart":
        from . import identity

        if not prompt_start:
            with contextlib.suppress(Exception):
                refresh_catalog(cfg, str(cwd))
        with st.edit() as state:
            s = session(state, sid)
            _new_context(s)
            s.setdefault("start", str(cwd))  # prompts update `cwd`; the session's repo stays where it started
            modes = memory_modes(cfg, Path(s["start"]))  # also when a resume or compaction starts elsewhere
            s.update(cwd=str(cwd), project=modes.key, in_session=True, modes=json.loads(modes.to_json()))
            attended, shows = _attended(cfg, s, data), _shows(s, data)
            s["surface"] = {"entrypoint": "codex-local", "attended": None}  # review gates read it as Claude's env
            if payload.get("source") not in ("compact", "resume"):
                count_session(state, modes.key, [sid], time.time(), C.PROCESS_MEMORY_DAYS * 86400)
        if not prompt_start:
            with contextlib.suppress(OSError, RuntimeError, ValueError, TimeoutError):  # never blocks memory
                identity.start(cfg, Path(s["start"]), modes)
        protocol = _protocol(modes)
        protocol = protocol if attended else protocol.replace(" " + hooks.SKILL_USE, "")
        entries = EntryStore(cfg.data / "memory", ledger_dir=cfg.data, host=cfg.host)
        entries.seed_user()
        text = protocol + "\n\n" + masked_delivery(entries.snapshot(), cfg)
        head = "\n\nNowItKnows project memory index" + (" (shared with Claude Code):\n" if modes.shared else ":\n")
        # A shared index can hold Claude Code's 200 lines; what does not fit ends with a pointer to the file.
        text += head + hooks._fit_index(TopicStore(modes.fallback_dir), C.INLINE_LIMIT - len(text) - len(head), cfg)
        if modes.shared:
            from . import migrate

            migrate.announce_unshared(cfg, modes.key, native.project_name(Path(s["start"])))
        (queue if prompt_start else spawn)(cfg, data, "start")
        if shows:
            setup_finished(cfg)
            from .setup import announce_version_mismatch

            announce_version_mismatch(cfg)
        return _notices(
            cfg, _context(event, text[: C.INLINE_LIMIT]), attended=attended, shows=shows
        )  # a prompt tells them
    if event == "UserPromptSubmit":
        prompt = str(data.get("prompt") or "")
        if not hooks.from_user(data):
            return _notices(cfg, {}, attended=_known(st, sid, data), shows=_shown(st, sid, data))
        if _missed_start(cfg, sid):
            # Its start ran before nowitknows's environment was ready (bin/nowitknows.sh noted it and exited), so its
            # first prompt that runs carries what that start would have given it
            start = {k: v for k, v in payload.items() if k not in ("prompt", "turn_id")}
            first = handle(
                "SessionStart",
                {**start, "hook_event_name": "SessionStart", "source": "startup"},
                cfg,
                prompt_start=True,
            )
            return _joined(first, handle(event, payload, cfg))
        with st.edit(hook=True) as state:
            s = session(state, sid)
            turn_id = data.get("turn_id")
            if not turn_id or s.get("codex_turn") != turn_id:
                s["turns"] += 1
                s["turns_since_memory"] += 1
                s["codex_turn"] = turn_id
            s["cwd"] = str(cwd)
            usage, turn, start = dict(state["usage"]), s["turns"], Path(s.get("start") or cwd)
            known, attended = set(s.get("skills") or []), _attended(cfg, s, data)
            shows = _shows(s, data)
            if not s.get("codex_context_epoch"):
                _new_context(s)
            if not isinstance(s.get("codex_delivered"), dict):
                _new_context(s)
            epoch, cached = s["codex_context_epoch"], dict(s["codex_delivered"])
        selected = router.route_request(skill_index(cfg, cwd, usage), prompt, cfg.skill_threshold)
        selected, skipped = scope.split(cfg, selected, cwd)  # learned project skills route only in their repos
        parts, activity, size = [], [], 0
        delivered, loaded = {}, []

        def append(text: str) -> bool:
            nonlocal size
            text = masked_delivery(text, cfg)
            length = len(text) + (2 if parts else 0)
            if size + length > C.CODEX_CONTEXT_CHARS:
                return False
            parts.append(text)
            size += length
            return True

        if selected:
            activity.append({"kind": "suggest", "skills": [d.name for d, _ in selected]})
        activity += skipped
        first, named = None, []  # the first skill text this prompt delivers, and the skills it delivers
        for doc, _ in selected:
            try:
                body = Path(doc.path).read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError):
                continue
            key = "skill:" + str(Path(doc.path).resolve())
            fingerprint = hashlib.sha256(body.encode()).hexdigest()
            read = (
                f"use mcp__nowitknows__skill_view(name={json.dumps(doc.name)})"
                if redact(body) != body
                else f"read {doc.path}"
            )
            if cached.get(key) == fingerprint:
                if append(
                    f"Skill {doc.name} was supplied in this context; continue using it. "
                    f"{read[0].upper() + read[1:]} if needed."
                ):
                    activity.append(
                        {
                            "kind": "delivery_reuse",
                            "skill": doc.name,
                            "skill_source": router.trigger_source(Path(doc.path), cfg),
                        }
                    )
                continue
            if not append(
                f"<nowitknows_skill name={json.dumps(doc.name)} path={json.dumps(doc.path)}>\n"
                f"{body}\n</nowitknows_skill>"
            ):
                if append(f"Relevant skill {doc.name}: {read}"):
                    first, named = len(parts) - 1 if first is None else first, [*named, doc.name]
                continue
            first, named = len(parts) - 1 if first is None else first, [*named, doc.name]
            delivered[key] = fingerprint
            source = router.trigger_source(Path(doc.path), cfg)
            loaded.append((doc.name, source))
            activity.append({"kind": "deliver", "skill": doc.name, "delivery": "hook_context", "skill_source": source})
        # Only names that pass the notices' screening can identify a skill the reply used.
        new = [name for name in named if name not in known and notify.screened(name) == name] if attended else []
        note = f"{SKILL_NOTE}{hooks.name_first(new)}:\n" if new else ""
        if note and first is not None and size + len(note) <= C.CODEX_CONTEXT_CHARS:
            parts[first], size = note + parts[first], size + len(note)
        # Notes come from the repo the session started in, where its writes go, also after a `cd` elsewhere.
        home = memory_modes(cfg, start)
        delivered.update(project_notes(cfg, home, prompt, cached, append))
        request = router.request_text(prompt)
        if (
            len(request) <= C.RECALL_REQUEST_MAX_CHARS
            and C.RECALL_REQUEST.search(request)
            and append(
                "Search the earlier work with mcp__nowitknows__session_search before asking the user to repeat it."
            )
        ):
            activity.append({"kind": "recall_hint"})
        if shows:
            setup_finished(cfg)
        from .setup import peer_offers, peers_told

        offers = peer_offers(cfg) if attended and shows else {}
        later = (notify.FIXED["claude-later"],) if attended and shows and claude_later(cfg) else ()
        output = _notices(
            cfg,
            _context(event, "\n\n".join(parts)),
            event,
            C.CODEX_CONTEXT_CHARS,
            attended,
            home.key,
            shows,
            (*later, *offers.values()),
        )
        peers_told(
            cfg,
            {k: _commands(v) for k, v in offers.items()},
            (output.get("hookSpecificOutput") or {}).get("additionalContext", ""),
        )
        if later and later[0] in (output.get("hookSpecificOutput") or {}).get("additionalContext", ""):
            with contextlib.suppress(OSError):  # told: the interval starts now, not at a start with no prompt
                atomic_write(cfg.path("claude-later"), f"{time.time()}\n")
        with st.edit(hook=True) as state:
            s = session(state, sid)
            if s.get("codex_context_epoch") == epoch and isinstance(s.get("codex_delivered"), dict):
                s["codex_delivered"].update(delivered)
                s["codex_delivered"] = dict(list(s["codex_delivered"].items())[-C.CODEX_DELIVERY_HISTORY :])
            for name, source in loaded:
                hooks._loaded(state, s, name, source)
        hooks.log_activity(cfg, sid, turn, activity)
        return output
    if event == "PostToolUse":
        with st.edit() as state:
            s = session(state, sid)
            key = str(data.get("tool_use_id") or "")
            seen = s.setdefault("codex_tools", [])
            if key and key in seen:
                return {}
            if key:
                s["codex_tools"] = [*seen, key][-C.CODEX_TOOL_HISTORY :]
            s["iters_since_skill"] += 1
            turn, attended = s["turns"], s.get("codex_attended") is True and not _scripted(data)
            shows, home = _shows(s, data), (s.get("project"), s.get("start"))
        if path := _opened_skill(cfg, data, cwd):
            name, source = path.parent.name, router.trigger_source(path, cfg)
            repo = native.project_key(cwd)  # where it was loaded, for the scope backfill
            with st.edit() as state:
                hooks._loaded(state, session(state, sid), name, source)
            hooks.log_activity(
                cfg,
                sid,
                turn,
                [{"kind": "load", "skill": name, "delivery": "tool_read", "skill_source": source, "repo": repo}],
            )
            scope.widen(cfg, source, cwd)  # an explicit read, unlike a delivery, adds this repo to its scope
        if str(data.get("tool_name", "")).endswith("__skill_manage"):  # a create: the session's own repo
            made = hooks._json_in(data.get("tool_response"))
            if made.get("ok") is True and made.get("action") == "create" and isinstance(made.get("path"), str):
                scope.pin(cfg, Path(made["path"]), *home)
        if str(data.get("tool_name", "")).endswith("__session_search"):
            result = hooks._json_in(data.get("tool_response"))
            if isinstance(result.get("results") or result.get("exchanges"), list) or "results" in result:
                hooks.log_activity(cfg, sid, turn, [{"kind": "search"}])
        return _notices(cfg, {}, attended=attended, shows=shows)
    if event == "PostCompact":
        with st.edit() as state:
            _new_context(session(state, sid))
        return _context(event, C.prompt("compact.md").replace(C.session_tools(), "mcp__nowitknows__"))
    if event in ("Stop", "PreCompact", "SessionEnd"):
        reason = {"Stop": "stop", "PreCompact": "precompact", "SessionEnd": "end"}[event]
        if event == "PreCompact":
            with st.edit() as state:
                _new_context(session(state, sid))
                session(state, sid)["mod_precompact"] = time.time()
        spawn(cfg, data, reason)
        return _notices(cfg, {}, attended=_known(st, sid, data), shows=_shown(st, sid, data))
    return {}


def refresh_catalog(cfg: C.Config, cwd: str) -> None:
    from .codex_client import inventory_skills

    path = _catalog_path(cfg, Path(cwd))
    stamp = _catalog_stamp(cfg, Path(cwd))
    # Prompts can read the last verified catalog while the start worker refreshes the same configuration.
    try:
        result = inventory_skills(cfg.env, [cwd])
        catalog = [
            item
            for group in result.get("result", {}).get("data", [])
            for item in group.get("skills", [])
            if type(item.get("enabled")) is bool and isinstance(item.get("path"), str)
        ]
    except Exception:
        atomic_write(path, json.dumps({"stamp": stamp, "skills": []}))
        raise
    atomic_write(path, json.dumps({"stamp": stamp, "skills": catalog}))


def import_records(cfg: C.Config) -> list[tuple[str, str]]:
    home = Path(cfg.env["NOWITKNOWS_CODEX_HOME"]).expanduser()
    claude = Path(cfg.env["NOWITKNOWS_CLAUDE_TRANSCRIPTS_DIR"]).expanduser().resolve()
    sid = r"[A-Za-z0-9_-]{1," + str(C.CODEX_SESSION_ID_MAX) + r"}"
    pairs = []
    for name in C.CODEX_IMPORT_RECORDS:
        if not (home / name).exists():
            continue
        data = _read(home / name)
        records = data.get("records") if isinstance(data, dict) else None
        if not isinstance(records, list):  # half-written or changed format: the threads already known stay listed
            _error(cfg, f"Codex import record {name} could not be read")
            continue
        for record in records:
            record = record if isinstance(record, dict) else {}
            thread = record.get("imported_thread_id", record.get("importedThreadId"))
            source = record.get("source_path", record.get("sourcePath"))
            if not isinstance(thread, str) or not re.fullmatch(sid, thread):
                continue
            original = ""
            if isinstance(source, str) and source.endswith(".jsonl") and re.fullmatch(sid, Path(source).stem):
                with contextlib.suppress(OSError, ValueError, RuntimeError):  # `~nouser/...` raises RuntimeError
                    if Path(source).expanduser().resolve().is_relative_to(claude):
                        original = "claude:" + Path(source).stem
            pairs.append(("codex:" + thread, original))
    return pairs


def index_profile(cfg: C.Config) -> int:
    from . import codex_transcript

    home = Path(cfg.env["NOWITKNOWS_CODEX_HOME"]).expanduser().resolve()
    shared = Path(C.env("NOWITKNOWS_SHARED_DATA_DIR", cfg.env) or C.data_dir(cfg.env)).expanduser().resolve()
    if home.resolve() != (C.home_dir(cfg.env) / ".codex").resolve() and cfg.data.resolve() == shared.resolve():
        raise ValueError("Only the default Codex profile can join the shared store")
    saved = _read(cfg.data / "codex-index.json", {})
    caps = f"{C.HISTORY_LINE_MAX_BYTES}:{C.HISTORY_FILE_MAX_BYTES}".encode()
    version = hashlib.sha256(Path(codex_transcript.__file__).read_bytes() + caps).hexdigest()
    current = isinstance(saved, dict) and saved.get("version") == version
    cache, oversized = (saved.get("files", {}), saved.get("too_large", {})) if current else ({}, {})
    reader, unread, large = sessions.zstd_reader(), 0, {}
    # Oldest first: names start with the time, and a thread's earlier pages must merge before its later ones.
    paths = sorted(
        sessions.transcript_paths([home / "sessions", home / "archived_sessions"]), key=lambda p: (p.name, str(p))
    )
    for path in paths:
        try:
            stat = path.stat()
            fingerprint = list(sessions._fingerprint(stat))
            if cache.get(str(path)) == fingerprint:
                continue
            if oversized.get(str(path)) == fingerprint:  # skipped until it changes
                large[str(path)] = fingerprint
                continue
            if not any(
                path.resolve().is_relative_to((home / name).resolve()) for name in ("sessions", "archived_sessions")
            ):
                continue
            if path.name.endswith(".zst") and not reader:  # counted in status; read on a later pass once one exists
                unread += 1
                continue
            counts: dict = {}
            rows = codex_transcript.convert(path, diagnostics=counts)
            if counts.get("too_large"):
                large[str(path)] = fingerprint
                _error(cfg, f"history indexing skipped {path.name}: over {C.HISTORY_FILE_MAX_BYTES} bytes")
                continue
            if counts.get("read_errors"):  # a part of the file: left uncached, so a later pass reads it whole
                raise OSError(f"Codex history file could not be read completely: {counts.get('read_error', '')}")
            identifiers = {r.get("sessionId") for r in rows if r.get("sessionId")}
            cwd = next((r.get("cwd") for r in rows if r.get("cwd")), "")
            if len(identifiers) != 1 or not cwd:
                continue
            sid = identifiers.pop()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1," + str(C.CODEX_SESSION_ID_MAX) + r"}", sid):
                continue
            target = cfg.transcripts / "codex" / native.project_key(Path(cwd)) / f"{sid}.jsonl"
            codex_transcript.merge_normalized(target, rows, path)
            cache[str(path)] = fingerprint
        except (OSError, ValueError) as error:
            _error(cfg, f"history indexing skipped a file: {error}")
    atomic_write(
        cfg.path("codex-index.json"),
        json.dumps(
            {
                "version": version,
                "files": cache,
                "too_large": large,
                "zstd_reader": reader or None,
                "compressed_unread": unread,
            }
        ),
    )
    connection = sessions.connect(cfg.path("sessions.db"), cfg.legacy_host)
    try:
        sessions.record_imports(connection, import_records(cfg))
        return sessions.ingest(connection, [cfg.transcripts, cfg.data / "archive"])
    finally:
        connection.close()


def _process_job(cfg: C.Config, job: dict) -> dict:
    from . import codex_transcript, modreview, worker
    from .codex_client import Client

    if cfg.child:
        return {"review": "NowItKnows child"}
    sid, cwd, reason = job["session"], job["cwd"], job["reason"]
    SkillStore(cfg).reconcile_shadows(Path(cwd))
    imports = import_records(cfg)
    with State(cfg.path("state.json"), cfg.host, cfg.legacy_host).edit() as state:
        s = session(state, sid)
        modes = memory_modes(cfg, Path(cwd))
        s.setdefault("project", modes.key)
        s.setdefault("modes", json.loads(modes.to_json()))
        s.setdefault("start", cwd)
        s["in_session"] = True
        if any(thread == f"codex:{sid}" for thread, _ in imports):
            s["imported"] = True  # its copied turns are left out of reviews even where their importer page is gone
    source = Path(job["transcript"]) if job.get("transcript") else None
    transcript = cfg.transcripts / "codex" / modes.key / f"{sid}.jsonl"
    if source and source.is_file():
        home = Path(cfg.env["NOWITKNOWS_CODEX_HOME"]).expanduser().resolve().resolve()
        if not any(source.resolve().is_relative_to(home / name) for name in ("sessions", "archived_sessions")):
            raise ValueError("transcript is outside this Codex profile")
        codex_transcript.normalize(source, transcript, cwd, expected_session=sid)
    origin, metadata = "", {}
    if transcript.is_file():
        with transcript.open(encoding="utf-8") as stream:
            first = json.loads(stream.readline() or "{}")
        metadata = first.get("codex", {}).get("session", {})
        origin = metadata.get("source", "")
    unattended = (
        origin in ("exec", "subagent", "mcp")
        or isinstance(origin, dict)
        or job.get("source") in ("exec", "subagent", "mcp")
    )
    development = (
        not cfg.review_unattended
        and origin == "exec"
        and job.get("source") not in ("subagent", "mcp")
        and any(x.get("file_change") for x in sessions.exchanges_since(transcript, s.get("reviewed_uuid"))[0])
        and native.repo_root(Path(s["start"])) is not None
    )
    with State(cfg.path("state.json"), cfg.host, cfg.legacy_host).edit() as state:
        session(state, sid)["codex_dev"] = development
    connection = sessions.connect(cfg.path("sessions.db"), cfg.legacy_host)
    try:
        sessions.record_imports(connection, imports)
        indexed = sessions.ingest(connection, [cfg.transcripts, cfg.data / "archive"])
    finally:
        connection.close()
    if reason == "start":
        from . import identity

        with contextlib.suppress(Exception):
            refresh_catalog(cfg, cwd)
        try:
            start = Path(s["start"])
            identity.start(cfg, start, memory_modes(cfg, start))
        except Exception as error:
            _error(cfg, f"repository identity: {error}")
        added = index_profile(cfg)
        try:
            scope.backfill(cfg, Path(s["start"]))  # model-free: scopes learned skills that have no scope yet
        except Exception as error:
            _error(cfg, f"skill scope backfill: {error}")
        if cfg.review and (not unattended or cfg.review_unattended):
            with contextlib.suppress(Exception):
                worker.backfill_triggers(cfg, Path(cwd))
        return {"indexed": indexed + added}
    if reason == "end" and transcript.is_file():
        sessions.archive(transcript, cfg.data / "archive", cfg.archive_max_mb)
    if not cfg.review:
        return {"indexed": indexed, "review": "disabled"}
    if unattended and not cfg.review_unattended and not (development and reason == "end"):
        return {"indexed": indexed, "review": "unattended session"}
    if reason in ("end", "precompact") and transcript.is_file():
        modreview.enqueue(cfg, {"session": sid, "transcript": str(transcript), "cwd": cwd, "reason": reason})
    step = modreview.plan(cfg, sid, str(transcript) if transcript.is_file() else "", cwd)
    calls = 0
    with contextlib.ExitStack() as connections:
        client, policy = None, None
        while step.get("action") == "model" and calls < C.CODEX_REVIEW_STEPS:
            model_used = step["model"]
            try:
                if client is None or policy != step["allow_extra_usage"]:
                    connections.close()
                    allowed = step["allow_extra_usage"]
                    guard = worker.spending_guard(cfg, allowed)
                    client = connections.enter_context(Client(cfg.env, cwd, allow_extra_usage=allowed, guard=guard))
                    policy = step["allow_extra_usage"]
                result = client.complete(step["system"], step["prompt"], None, model_used)
            except Exception as error:
                _error(cfg, str(error))
                if getattr(error, "started", False):
                    modreview.fail(cfg, step["step"], {"error": str(error), "usage": getattr(error, "usage", {})})
                else:
                    modreview.release(cfg, sid)
                with contextlib.suppress(OSError):
                    atomic_write(
                        cfg.path("codex-status.json"),
                        json.dumps(
                            {
                                "state": "paused",
                                "updated": time.time(),
                                "reason": redact(str(error)),
                                "admission": getattr(error, "status", {}),
                            }
                        ),
                    )
                notify.push(
                    cfg,
                    f"NowItKnows · Codex learning paused: {redact(str(error))[: C.TOOL_ERROR_CHARS]}",
                    key="codex-model",
                    account=True,
                    tell={"kind": "codex-paused"},
                )
                return {"indexed": indexed, "calls": calls, "paused": str(error)}
            step = modreview.answer(cfg, step["step"], {"text": result["text"], "usage": result["usage"]})
            calls += 1
            with contextlib.suppress(OSError):
                atomic_write(
                    cfg.path("codex-status.json"),
                    json.dumps(
                        {
                            "state": "ready",
                            "updated": time.time(),
                            "admission": result.get("admission", {}),
                            "model": model_used,
                        }
                    ),
                )
    with contextlib.suppress(Exception):
        refresh_catalog(cfg, cwd)
    last = State(cfg.path("state.json"), cfg.host, cfg.legacy_host).read()["worker"].get("last_curator", 0)
    if time.time() - last > C.CURATOR_EVERY_DAYS * 86400:
        worker.curate(cfg)
    return {"indexed": indexed, "calls": calls, "action": step.get("action")}


def settle_legacy(cfg: C.Config, legacy: Path) -> None:
    """Run one model-free worker pass in the legacy store; unanswerable work stays there."""
    old = configuration(
        {
            **cfg.env,
            "NOWITKNOWS_CODEX_DATA_DIR": str(legacy),
            "NOWITKNOWS_LEGACY_HOST": "codex",
            "NOWITKNOWS_LEGACY_STORE": str(legacy),
        }
    )
    run_worker(old, settle_only=True)


def run_worker(cfg: C.Config, *, settle_only: bool = False) -> dict:
    from . import skillsync, worker

    results = []
    metrics_jobs = []
    if (
        settle_only
        and not any(
            not _read(p).get("answered") and (cfg.data / "mod/answers" / p.name).exists()
            for p in (cfg.data / "mod/steps").glob("*.json")
        )
        and not any(
            not _read(p).get("done") and _read(p).get("host") == cfg.host
            for p in (cfg.data / "paid-replies").glob("*.json")
        )
    ):
        return {"settled": True}
    try:
        with file_lock(cfg.path("codex-worker"), wait_s=0 if settle_only else C.SQLITE_TIMEOUT_S):
            if settle_only:
                from . import modreview

                with file_lock(cfg.path("worker"), wait_s=0):
                    from . import paid

                    paid.recover(cfg)
                    owners = {_read(p).get("flow", {}).get("owner") for p in (cfg.data / "mod/steps").glob("*.json")}
                    for owner in sorted(o for o in owners if isinstance(o, str)):
                        modreview.resume(cfg, owner)
                return {"settled": True}
            done = 0
            while done < C.CODEX_JOB_LIMIT:
                pending = sorted((cfg.data / "codex-jobs").glob("*.json"), key=lambda p: p.stat().st_mtime)
                if not pending:
                    break
                path = pending[0]
                done += 1
                job = _read(path)
                if not isinstance(job, dict) or not all(
                    isinstance(job.get(k), str) for k in ("session", "cwd", "reason")
                ):
                    path.replace(path.with_suffix(".bad"))
                    continue
                saved = worker._queued_cfg(cfg, job.get("env", {}))  # its own settings, never looser guards
                try:
                    metrics_jobs.append((saved, job))
                    result = _process_job(saved, job)
                    results.append(result)
                    path.replace(path.with_suffix(".done"))
                except Exception as error:
                    _error(saved, str(error))
                    results.append({"error": str(error)})
                    path.replace(path.with_suffix(".failed"))
    except TimeoutError:
        return {"queued": True}
    from .session_metrics import after_job

    for selected, item in metrics_jobs:
        after_job(selected, item)
    try:
        skillsync.sync(cfg)
    except Exception as error:
        _error(cfg, f"skill backup skipped: {type(error).__name__}")
    return {"jobs": results}


def _unshared(cfg: C.Config) -> str | None:
    """None when sharing is off; else what setup.unshared_row says of the repo Codex was started in."""
    if not cfg.project_notes_shared:
        return None
    from .report import unshared_row

    return unshared_row(cfg, native.project_key(Path(C.env("NOWITKNOWS_PROJECT", cfg.env) or os.getcwd())))


def status(cfg: C.Config) -> dict:
    from . import skillsync
    from .report import compressed_history_row, host_totals, refusals
    from .worker import tokens_today

    return {
        "host": "codex",
        "data": str(cfg.data),
        "skills": str(cfg.skills_home),
        "skills_library": str(cfg.skills_library),
        **({"skills_without_links": missing} if (missing := SkillStore(cfg).unlinked()) else {}),
        "shadowed_skill_copies": router.skill_shadows(cfg, Path(C.env("NOWITKNOWS_PROJECT", cfg.env) or os.getcwd())),
        "skill_backup": dict(skillsync.rows(cfg)),
        "hosts_last_7d": host_totals(cfg, time.time() - C.WEEK_S),
        "native_memory": "separate; never modified",
        "share_project_notes": cfg.project_notes_shared,
        "skill_scope": (
            "a learned project skill is delivered only in the repos it was learned or read with skill_view in "
            "(opt out: configure skill_scope=off)"
            if cfg.skill_scope
            else "every learned skill is delivered in every repo (configure skill_scope=on scopes project skills)"
        ),
        "project_notes": (
            "project notes are shared between Claude Code and Codex, in Claude's memory folder for each repo "
            "(opt out: configure share_project_notes=off)"
            if cfg.project_notes_shared
            else "project notes stay in nowitknows's own store (sharing is off or this profile is isolated)"
        ),
        "notes_home": str(memory_modes(cfg, Path(C.env("NOWITKNOWS_PROJECT", cfg.env) or os.getcwd())).fallback_dir),
        "notes_not_saved_last_7d": refusals(cfg, time.time() - C.WEEK_S),
        "older_notes_not_shared": _unshared(cfg),
        "review": cfg.review,
        "model": cfg.reviewer_model,
        "judge": cfg.judge_model,
        "allow_extra_usage": cfg.allow_extra_usage,
        "transport": "own ChatGPT-authenticated Codex CLI; separate review sessions",
        "pending": len(list((cfg.data / "codex-jobs").glob("*.json"))),
        "staged": len(SkillStore(cfg).staged_changes()) + notejudge.waiting(cfg),
        "daily_token_limit": cfg.review_daily_tokens,
        "tokens_today": tokens_today(cfg),
        "learning_status": _read(cfg.data / "codex-status.json", {"state": "not yet checked"}),
        "compressed_history": compressed_history_row(cfg.data) or "not indexed yet",
    }


def doctor(cfg: C.Config) -> dict:
    from .codex_client import Client, allowance

    output = status(cfg)
    output["review_transport"] = {"state": "skipped", "reason": "Account check did not pass"}
    try:
        with Client(cfg.env, Path.cwd(), allow_extra_usage=cfg.allow_extra_usage) as client:
            account = client.call("account/read", {"refreshToken": False})["result"]
            limits = client.call("account/rateLimits/read", {})["result"]
            output["account_check"] = allowance(account, limits, cfg.reviewer_model, cfg.allow_extra_usage)
            if output["account_check"]["admit"]:
                try:
                    output["review_transport"] = client.check_review(cfg.reviewer_model)
                except Exception as error:
                    output["review_transport"] = {"state": "paused", "reason": redact(str(error))}
    except Exception as error:
        output["account_check"] = {"state": "paused", "reason": redact(str(error))}
    return output


def main(argv: list[str] | None = None) -> int:
    os.environ["NOWITKNOWS_HOST"] = "codex"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="nowitknows-codex")
    subs = parser.add_subparsers(dest="operation", required=True)
    subs.add_parser("hook").add_argument("event", nargs="?")
    for name in ("worker", "mcp", "status", "doctor", "changes", "report", "index"):
        subs.add_parser(name)
    setup_parser = subs.add_parser("setup")
    setup_parser.add_argument("--dry-run", action="store_true")
    setup_parser.add_argument("--apply", action="store_true")
    setup_parser.add_argument("--cursor", action="store_true")
    setup_parser.add_argument("--backup-repo", metavar="OWNER/NAME")
    subs.add_parser("ui", help="serve a read-only local snapshot").add_argument("--open", action="store_true")
    subs.add_parser("keep").add_argument("name")
    subs.add_parser("migrate").add_argument("--apply", action="store_true")
    settings = subs.add_parser("configure")
    settings.add_argument("settings", nargs="+", metavar="key=value")
    args = parser.parse_args(argv)
    cfg = configuration(create=args.operation not in ("migrate", "ui") and not getattr(args, "dry_run", False))
    if args.operation == "hook":
        output = {}
        try:
            payload = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
            output = handle(args.event or payload.get("hook_event_name", ""), payload, cfg)
        except Exception as error:
            _error(cfg, str(error))
        print(json.dumps(output))
        return 0
    if args.operation == "mcp":
        from .mcp_server import main as serve

        serve()
        return 0
    if args.operation == "worker":
        print(json.dumps(run_worker(configuration({**cfg.env, "NOWITKNOWS_CHILD": "0"}))))
    elif args.operation in ("status", "setup"):
        from .setup import install, peer_offers

        if args.operation == "setup" and args.cursor:
            return install(cfg, apply=args.apply, dry_run=args.dry_run, cursor=True)
        if args.operation == "setup" and not args.dry_run:
            SkillStore(cfg).reconcile_shadows(Path.cwd())
            from . import skillsync

            if (cfg.skills_library / ".git").exists():
                skillsync.configure(cfg, args.backup_repo)
            elif args.backup_repo is not None:
                print("Skill backup needs library git history; enable it and run setup in a terminal")
        print(json.dumps(status(cfg), indent=2))
        if args.operation == "setup":
            for line in peer_offers(cfg).values():
                print(_commands(line))
    elif args.operation == "doctor":
        print(json.dumps(doctor(cfg), indent=2))
    elif args.operation == "configure":
        if any("=" not in value for value in args.settings):
            parser.error("settings use key=value")
        print(json.dumps(configure(cfg, dict(value.split("=", 1) for value in args.settings)), indent=2))
    elif args.operation == "keep":
        print(json.dumps(SkillStore(cfg).keep(args.name)))
    elif args.operation == "report":
        from .report import render

        print(render(cfg))
    elif args.operation == "ui":
        from .ui import run

        return run(cfg, open_browser=args.open)
    elif args.operation == "changes":
        from .setup import changes

        print(changes(cfg, C.CHANGES_SHOWN))
    elif args.operation == "index":
        print(json.dumps({"indexed": index_profile(cfg)}))
    elif args.operation == "migrate":
        from .migrate import run as migrate

        return migrate(C.for_command(create=False), apply=args.apply)
    return 0
