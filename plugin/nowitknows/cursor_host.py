"""Native Cursor host entry points for hooks, MCP, worker jobs, and user commands."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
import time
import traceback
import uuid
from dataclasses import replace
from pathlib import Path

from . import (
    claude,
    cursor_runner,
    cursor_transcript,
    hooks,
    identity,
    modreview,
    native,
    notes_gate,
    notify,
    router,
    scope,
    sessions,
    worker,
)
from . import config as C
from .delivery import masked_delivery
from .memory import EntryStore, TopicStore, note_pointer, ownership
from .safety import redact
from .state import SESSION_TTL_S, State, atomic_write, count_session, file_lock, forget_skill, session, touch_skill

SAFE_ID = re.compile(r"[A-Za-z0-9._-]+")
SKILL_NOTE = "nowitknows skills that may apply; "
EVENT_MAP = {
    "sessionStart": "SessionStart",
    "beforeSubmitPrompt": "UserPromptSubmit",
    "preToolUse": "PreToolUse",
    "postToolUse": "PostToolUse",
    "postToolUseFailure": "PostToolUseFailure",
    "stop": "Stop",
    "sessionEnd": "SessionEnd",
    "preCompact": "PreCompact",
}
CONFIGURABLE = {
    "metrics": "bool",
    "review": "bool",
    "review_unattended": "bool",
    "allow_extra_usage": "bool",
    "os_notify": "bool",
    "review_signals": "bool",
    "share_project_notes": "bool",
    "judge_notes": "bool",
    "bounded_review_skills": "bool",
    "skill_scope": "bool",
    "memory_interval": "int",
    "skill_interval": "int",
    "review_min_gap_s": "int",
    "review_min_digest_chars": "int",
    "review_stop_digest_chars": "int",
    "review_daily_tokens": "int",
    "archive_max_mb": "int",
    "router_threshold": "float",
}
GUARD_SETTINGS = frozenset(("review", "allow_extra_usage", "review_daily_tokens"))
CONTENT_SETTINGS = frozenset(("bounded_review_skills", "share_project_notes", "metrics"))


def _secret_key(key: str | None) -> bool:
    return key is not None and any(part in C.CURSOR_META_SECRET_PARTS for part in re.split(r"[^a-z0-9]+", key.lower()))


def _masked_meta(value, key: str | None = None):
    # A credential-named field is masked whole, so no value form can slip past the pattern matcher.
    if _secret_key(key) and value not in (None, "", [], {}):
        return "[redacted]"
    if isinstance(value, str):
        clean = redact(value)
        if clean == value and key is not None:
            for form in (f"{key}: {value}", f'{key}: "{value}"'):
                if redact(form) != form:
                    return "[redacted]"
        return clean
    if isinstance(value, dict):
        return {k: _masked_meta(v, k if isinstance(k, str) else None) for k, v in value.items()}
    if isinstance(value, list):
        return [_masked_meta(v, key) for v in value]
    return value


def cursor_home(env: dict | None = None) -> Path:
    return C.resolve_cursor_paths(env).profile


def _parse_setting(key: str, value) -> str:
    kind = CONFIGURABLE.get(key)
    if kind is None:
        raise ValueError(f"Unsupported Cursor setting {key!r}")
    if kind == "bool":
        if isinstance(value, bool):
            return "1" if value else "0"
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return "1"
        if text in ("0", "false", "no", "off"):
            return "0"
        raise ValueError(f"{key} must be on/off or true/false")
    if kind == "int":
        if isinstance(value, bool):
            raise ValueError(f"{key} must be a non-negative integer")
        try:
            parsed = int(str(value).strip())
        except ValueError as exc:
            raise ValueError(f"{key} must be a non-negative integer") from exc
        if parsed < 0:
            raise ValueError(f"{key} must be a non-negative integer")
        return str(parsed)
    if kind == "float":
        if isinstance(value, bool):
            raise ValueError(f"{key} must be a finite number")
        try:
            parsed_f = float(str(value).strip())
        except ValueError as exc:
            raise ValueError(f"{key} must be a finite number") from exc
        if not (parsed_f == parsed_f and abs(parsed_f) != float("inf")):
            raise ValueError(f"{key} must be a finite number")
        return str(parsed_f)
    text = str(value).strip()
    if not text:
        raise ValueError(f"{key} must be a non-empty string")
    return text


def _settings_files(data: Path) -> tuple[Path, ...]:
    return (data / "cursor-settings.json", data / "cursor/settings.json", data / "settings.json")


def saved_settings(data: Path, only: frozenset[str] | set[str] | None = None) -> dict[str, str]:
    for path in _settings_files(data):
        if not path.exists():
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("saved settings are not a JSON object")
        out: dict[str, str] = {}
        for k, v in raw.items():
            if k in CONFIGURABLE and (only is None or k in only):
                out[C.ENV_VARS[k]] = _parse_setting(k, v)
        return out
    return {}


def saved_guards(cfg: C.Config) -> dict[str, str]:
    try:
        return saved_settings(cfg.data, GUARD_SETTINGS)
    except (OSError, ValueError) as exc:
        _error(cfg, f"saved settings cannot be read: {exc}")
        return {
            "NOWITKNOWS_REVIEW": "0",
            "NOWITKNOWS_ALLOW_EXTRA_USAGE": "0",
            "NOWITKNOWS_REVIEW_DAILY_TOKENS": "1",
        }


def configuration(env: dict | None = None, *, create: bool = True, profile: Path | None = None) -> C.Config:
    paths = C.resolve_cursor_paths(env, profile=profile)
    saved: dict[str, str] = {}
    with contextlib.suppress(OSError, ValueError):
        saved = saved_settings(paths.data)
    return C.load({**paths.env, **saved}, create=create, cursor_paths=paths)


def configure(cfg: C.Config, updates: dict[str, object]) -> dict:
    path = cfg.data / "cursor-settings.json"
    current: dict = {}
    for candidate in _settings_files(cfg.data):
        if candidate.exists():
            with contextlib.suppress(OSError, ValueError):
                loaded = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    current = dict(loaded)
                    break
    for key, val in updates.items():
        norm = _parse_setting(key, val)
        kind = CONFIGURABLE[key]
        if kind == "bool":
            current[key] = norm == "1"
        elif kind == "int":
            current[key] = int(norm)
        elif kind == "float":
            current[key] = float(norm)
        else:
            current[key] = norm
    atomic_write(path, json.dumps(current, indent=2, sort_keys=True) + "\n")
    return current


def memory_modes(cfg: C.Config, project: Path) -> native.Modes:
    key = native.project_key(project)
    shared = cfg.project_notes_shared
    paths = cfg.cursor_paths
    separate = paths.notes / key / "memory"
    folder = separate
    if shared:
        chain = native.settings_chain(project, paths.claude)
        folder = native.native_memory_dir(paths.env, project, paths.claude, chain, key)
    return native.Modes(False, False, folder, folder, key, shared)


def _error(cfg: C.Config, message: str) -> None:
    with contextlib.suppress(OSError), open(cfg.path("errors.log"), "a", encoding="utf-8") as fh:
        fh.write(f"{time.ctime()} cursor: {redact(message)}\n")


def _safe_id(value: object) -> str | None:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > C.CURSOR_SESSION_ID_MAX
        or text == "empty-state-draft"
        or not SAFE_ID.fullmatch(text)
        or text in (".", "..")
    ):
        return None
    return text


def _session_id(data: dict) -> str | None:
    return _safe_id(data.get("session_id") or data.get("conversation_id"))


def _project(cfg: C.Config, data: dict) -> Path:
    roots = data.get("workspace_roots")
    if isinstance(roots, list):
        for root in roots:
            if isinstance(root, str) and root.strip():
                return Path(root.strip())
    val = data.get("cwd")
    return Path(val.strip()) if isinstance(val, str) and val.strip() else cfg.cursor_paths.project


def _subagent(data: dict) -> bool:
    return bool(data.get("agent_id") or data.get("subagent_id") or data.get("parent_conversation_id"))


def _attended(cfg: C.Config, data: dict) -> bool:
    if _subagent(data) or data.get("is_background_agent") is True:
        return False
    if cfg.env.get("CURSOR_CODE_REMOTE") == "true":
        return False
    return hooks._attended(cfg)


def _cursor_words(text: str) -> str:
    text = text.replace("/nowitknows:setup --cursor", "/nowitknows-setup")
    for value in ("on", "off"):
        text = text.replace(
            f"turn it {value} with /plugin configure nowitknows@nowitknows",
            f"turn it {value} with /nowitknows-setup configure review={value}",
        )
    for cmd in ("setup", "status", "changes", "keep", "report", "doctor"):
        text = text.replace(f"/nowitknows:{cmd}", f"/nowitknows-{cmd}")
    return text


def _resolve_transcript_path(cfg: C.Config, sid: str, project: Path, given: object = None) -> Path | None:
    """Resolve a Cursor session's `.jsonl` (or `.txt` fallback) transcript path."""
    candidates: list[Path] = []
    raw = cfg.cursor_paths.transcript_path(given)
    if raw is not None:
        if raw.suffix == ".txt":
            candidates.append(raw.with_suffix(".jsonl"))
        candidates.append(raw)
    env_path = cfg.cursor_paths.transcript_source
    if env_path is not None:
        ep = env_path
        if ep.suffix == ".txt":
            candidates.append(ep.with_suffix(".jsonl"))
        candidates.append(ep)
    slug = cursor_transcript.slugify_path(project.resolve() if project.exists() else project)
    projects_dir = cfg.cursor_paths.transcript_projects
    search_dirs = [projects_dir / slug / "agent-transcripts"] if slug else []
    if projects_dir.is_dir():
        with contextlib.suppress(OSError):
            for child in sorted(projects_dir.iterdir()):
                tdir = child / "agent-transcripts"
                if tdir not in search_dirs and tdir.is_dir():
                    search_dirs.append(tdir)
    for tdir in search_dirs:
        for ext in ("jsonl", "txt"):
            candidates.append(tdir / sid / f"{sid}.{ext}")
            candidates.append(tdir / f"{sid}.{ext}")
    for candidate in candidates:
        with contextlib.suppress(OSError):
            if candidate.is_file():
                return candidate
    return raw


def skill_files(
    cfg: C.Config,
    project: Path,
    *,
    resolved: dict[Path, Path] | None = None,
    signatures: dict[Path, tuple] | None = None,
) -> list[Path]:
    """Discover project skills first, then managed skills ahead of home-level duplicates."""
    resolved = {} if resolved is None else resolved
    signatures = {} if signatures is None else signatures
    library = cfg.skills_library.resolve()
    home_roots = cfg.cursor_paths.skill_roots
    home_real = {p.resolve() for p in home_roots}
    project_roots = [
        root
        for host in (".cursor", ".agents", ".claude")
        if (root := project / host / "skills").resolve() not in home_real
    ]
    roots = [*project_roots, *home_roots, cfg.skills_library]
    cache_file = cfg.data / "router/cursor-frontmatter.json"
    masked_cache = False
    try:
        saved = json.loads(cache_file.read_text(encoding="utf-8"))
        masked_cache = isinstance(saved, dict) and saved.get("masked") == C.CURSOR_META_MASK_VERSION
        cached = (
            saved.get("files", {}) if isinstance(saved, dict) and saved.get("version") == router.CACHE_VERSION else {}
        )
        if not isinstance(cached, dict):
            cached = {}
    except (OSError, ValueError):
        saved = {}
        cached = {}
    fresh = {}
    seen_real: set[Path] = set()
    link_targets: dict[str, Path] = {}
    candidates = []
    for index, root in enumerate(roots):
        for path in router.find_skills(
            root,
            seen_real,
            resolved=resolved,
            link_targets=link_targets,
            signatures=signatures,
        ):
            candidates.append((index, path, resolved[path]))
    canonical = [real for _, _, real in candidates if real.parent.parent == library]
    personal = cfg.skills_home.resolve()
    ledger_stamp = router._mtime(cfg.data / "skills_ledger.jsonl") if library == personal else None
    ownership_key = [
        router.CACHE_VERSION,
        str(library),
        str(personal),
        cfg.host,
        sorted(p.parent.name for p in canonical),
        list(ledger_stamp or ()),
    ]
    ownership = saved.get("ownership", {}) if isinstance(saved, dict) else {}
    if (
        not isinstance(ownership, dict)
        or ownership.get("key") != ownership_key
        or not isinstance(ownership.get("names"), list)
        or not all(isinstance(name, str) for name in ownership["names"])
    ):
        ownership = {"key": ownership_key, "names": sorted(router.canonical_names(cfg, canonical))}
    managed = set(ownership["names"])
    seen_names: set[str] = set()
    out: list[Path] = []
    for index, path, real in candidates:
        canonical_root = index == len(roots) - 1
        parts = path.parts
        if any(part in ("synced", ".system", "skills-cursor") for part in parts):
            continue
        try:
            stamp = signatures[path] if path in signatures else router._mtime(path)
            if signatures is not None:
                signatures[path] = stamp
            signature = list(stamp or ())
            previous = cached.get(str(path), {})
            previous = previous if isinstance(previous, dict) else {}
            unchanged = previous.get("signature") == signature and isinstance(previous.get("meta"), dict)
            meta = (
                previous["meta"]
                if unchanged
                else router.frontmatter(path.read_text(encoding="utf-8", errors="replace"))[0]
            )
            if not (unchanged and masked_cache):
                meta = _masked_meta(meta)
        except (OSError, KeyError, TypeError):
            continue
        fresh[str(path)] = {"signature": signature, "meta": meta}
        name = str(meta.get("name") or path.parent.name)
        if index >= len(project_roots) and name in managed and real.parent.parent != library:
            continue
        if str(meta.get("disable-model-invocation", "")).lower() == "true":
            if canonical_root or (index >= len(project_roots) and name in managed):
                seen_names.add(name)
            continue
        if name in seen_names:
            continue
        seen_names.add(name)
        out.append(path)
    if fresh != cached or not masked_cache or not isinstance(saved, dict) or saved.get("ownership") != ownership:
        atomic_write(
            cache_file,
            json.dumps(
                {
                    "version": router.CACHE_VERSION,
                    "masked": C.CURSOR_META_MASK_VERSION,
                    "files": fresh,
                    "ownership": ownership,
                }
            ),
        )
    return out


def skill_index(cfg: C.Config, project: Path, usage: dict | None = None) -> router.BM25:
    resolved: dict[Path, Path] = {}
    signatures: dict[Path, tuple] = {}
    files = [(p, "") for p in skill_files(cfg, project, resolved=resolved, signatures=signatures)]
    cache_key = hashlib.sha256("|".join(str(p) for p, _ in files).encode()).hexdigest()[:16]
    cache = cfg.data / "router" / f"cursor-{cache_key}.pkl"
    return router.skill_index(
        [],
        cfg.claude_home,
        [],
        cfg.data / "skill_triggers.json",
        cache,
        usage or {},
        files=files,
        cfg=cfg,
        resolved=resolved,
        signatures=signatures,
    )


def _build_start_context(cfg: C.Config, project: Path, modes: native.Modes, attended: bool) -> str:
    protocol = C.prompt("skills_protocol.md")
    if not attended:
        protocol = protocol.replace(" " + hooks.SKILL_USE, "")
    protocol = protocol.replace(
        "load it with the Skill tool and follow it before acting",
        "read its SKILL.md with the Read tool and follow it before acting",
    )
    parts = [protocol]
    if modes.global_tier:
        note = f"{C.session_tools()}memory target 'project' (index below)"
        entries = EntryStore(cfg.data / "memory", ledger_dir=cfg.data, host=cfg.host)
        entries.seed_user()
        snap = masked_delivery(entries.snapshot(), cfg)
        blocks = " (the memory blocks below are shared across projects)" if snap else ""
        parts.append(
            C.prompt("memory_global.md")
            .replace(C.MEMORY_WRITING_RULES, "")
            .strip()
            .replace("{project_note}", note)
            .replace("{blocks}", blocks)
        )
        if snap:
            parts.append(snap)
    if modes.project_owner == "nowitknows":
        head = C.prompt("memory_project.md").replace(C.MEMORY_WRITING_RULES, "").strip()
        room = C.CURSOR_CONTEXT_CHARS - len("\n\n".join(parts)) - len(head) - C.CURSOR_CONTEXT_MIN_BODY_CHARS
        parts.append(
            head
            + "\n"
            + hooks._fit_index(TopicStore(modes.fallback_dir), max(C.CURSOR_CONTEXT_MIN_BODY_CHARS, room), cfg)
        )
    text = "\n\n".join(parts)
    if len(text) > C.CURSOR_CONTEXT_CHARS:
        limit = C.CURSOR_CONTEXT_CHARS - C.CURSOR_CONTEXT_RESERVE_CHARS
        text = text[:limit] + "\n[nowitknows: context truncated; consolidate the memory stores]"
    return text


def _ensure_session_started(
    cfg: C.Config, data: dict, st: State, d: dict, sid: str, project: Path
) -> tuple[dict, native.Modes, str]:
    """Initialize session state if `sessionStart` has not run yet, and return `(s, modes, start_context_if_needed)`."""
    s = session(d, sid)
    attended = _attended(cfg, data)
    if s.get("cursor_started"):
        s["cwd"] = str(project)
        start_dir = Path(s.get("start") or project)
        return s, native.recorded(cfg, s, start_dir), ""
    from .setup import announce_version_mismatch

    announce_version_mismatch(cfg)
    modes = memory_modes(cfg, Path(s.get("start") or project))
    s["cursor_started"] = True
    s["cursor_attended"] = attended
    s["modes"] = {
        "hosted": modes.hosted_memory,
        "auto": modes.auto_memory,
        "native_dir": str(modes.native_dir),
        "fallback_dir": str(modes.fallback_dir),
        "shared": modes.shared,
    }
    s["project"] = modes.key
    s["cwd"] = str(project)
    s.setdefault("start", str(project))
    s["surface"] = {"entrypoint": "cursor", "attended": "1" if attended else "0"}
    if attended and data.get("source") != "compact":
        counted = count_session(d, modes.key, [sid], time.time(), C.PROCESS_MEMORY_DAYS * 86400)
        if counted:
            s["surface"]["counted"] = True
    proj = d["projects"].setdefault(modes.key, {})
    native.reconcile(modes, C.owner(proj), cfg.data, cfg.host)
    proj.update(C.owner_fields(modes.project_owner))
    if modes.shared:
        from . import migrate

        migrate.announce_unshared(cfg, modes.key, native.project_name(project))
    ctx = _build_start_context(cfg, project, modes, attended)
    if s.pop("cursor_compacted", False) or data.get("source") == "compact":
        hint = C.prompt("compact.md").replace(C.session_tools(), "mcp__nowitknows__")
        ctx = ctx[: C.CURSOR_CONTEXT_CHARS - len(hint) - 2] + "\n\n" + hint
    return s, modes, ctx


def queued_environment(cfg: C.Config) -> dict:
    # Prompt hooks only snapshot settings. Authentication belongs to the worker.
    return {
        **modreview._settings(cfg),
        "NOWITKNOWS_REVIEWER_MODEL": cfg.reviewer_model,
        "NOWITKNOWS_JUDGE_MODEL": cfg.judge_model,
        "NOWITKNOWS_CHEAP_MODEL": cfg.cheap_model,
    }


def queue(cfg: C.Config, data: dict, reason: str, **extra) -> str | None:
    sid = _session_id(data)
    if not sid:
        return None
    project = _project(cfg, data)
    transcript = _resolve_transcript_path(cfg, sid, project, data.get("transcript_path"))
    job = {
        "session": sid,
        "cwd": str(project),
        "reason": reason,
        "transcript": str(transcript) if transcript else "",
        "ts": time.time(),
        "env": queued_environment(cfg),
        **extra,
    }
    path = cfg.cursor_paths.jobs / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}-{sid[: C.CURSOR_ROW_ID_CHARS]}.json"
    atomic_write(path, json.dumps(job))
    if C.env("NOWITKNOWS_DRY_RUN", cfg.env) != "1" and C.env("NOWITKNOWS_CURSOR_DEFER_WORKER", cfg.env) != "1":
        _spawn_worker(cfg)
    return str(path)


def _spawn_worker(cfg: C.Config) -> None:
    env = {**cfg.env, "PYTHONPATH": str(C.ROOT), "NOWITKNOWS_HOST": "cursor"}
    hooks._detach(cfg, [sys.executable, str(C.ROOT / "bin/nowitknows_cursor.py"), "worker"], env, "cursor-worker.log")


def session_start(cfg: C.Config, data: dict) -> dict:
    if cfg.remote or _subagent(data):
        return {}
    sid = _session_id(data)
    if not sid:
        return {}
    project = _project(cfg, data)
    st = State(cfg.path("state.json"), cfg.host, cfg.legacy_host)
    with st.edit(hook=True) as d:
        s = session(d, sid)
        if data.get("source") in ("compact", "resume"):
            s.pop("cursor_started", None)
        s, _modes, ctx = _ensure_session_started(cfg, data, st, d, sid, project)
        s["cursor_start_delivered"] = True
        last_activity = max((x.get("seen", 0) for k, x in d["sessions"].items() if k != sid), default=0)
        curate_due = (
            time.time() - d["worker"].get("last_curator", 0) > C.CURATOR_EVERY_DAYS * 86400
            and time.time() - last_activity > C.CURATOR_IDLE_HOURS * 3600
        )
    queue(cfg, data, "start", curate=bool(curate_due), learn=bool(_attended(cfg, data) or cfg.review_unattended))
    return {"additional_context": ctx} if ctx else {}


def before_submit_prompt(cfg: C.Config, data: dict) -> dict:
    sid = _session_id(data)
    if not cfg.remote and sid:
        atomic_write(_prompt_hook_marker(cfg, sid), "1\n")
    return _prompt_context(cfg, data, via="before_submit_prompt")


def _prompt_hook_marker(cfg: C.Config, sid: str) -> Path:
    return cfg.data / "cursor-sessions" / f"{sid}.prompt-hook"


def _prompt_context(cfg: C.Config, data: dict, *, via: str) -> dict:
    if cfg.remote:
        return {}
    prompt = str(data.get("prompt") or "")
    sid = _session_id(data)
    if not sid:
        return {}
    project = _project(cfg, data)
    attended = _attended(cfg, data)
    st = State(cfg.path("state.json"), cfg.host, cfg.legacy_host)
    start_ctx = ""
    need_start_job = False
    with st.edit(hook=True, require_commit=via == "post_tool_use") as d:
        s = session(d, sid)
        if via == "before_submit_prompt":
            s["cursor_prompt_hook_seen"] = True
        elif s.get("cursor_prompt_hook_seen"):
            return {}
        if not hooks.from_user(data):
            return {}
        if via == "post_tool_use":
            fingerprint = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            if not prompt or s.get("cursor_prompt_hash") == fingerprint:
                return {}
            s["cursor_prompt_hash"] = fingerprint
        gen_id = str(data.get("generation_id") or data.get("turn_id") or "")
        if via == "before_submit_prompt" and gen_id and s.get("last_turn_id") == gen_id:
            return {}
        if via == "before_submit_prompt" and gen_id:
            s["last_turn_id"] = gen_id
        was_started = bool(s.get("cursor_started"))
        s, modes, start_ctx = _ensure_session_started(cfg, data, st, d, sid, project)
        if not was_started:
            need_start_job = True
        s["turns"] += 1
        s["turns_since_memory"] += 1
        turn = s["turns"]
        usage = dict(d["usage"])
        loaded = set(s.get("skills") or [])
    if need_start_job:
        queue(cfg, data, "start", curate=False, learn=bool(attended or cfg.review_unattended))

    typed = hooks.SLASH.match(prompt)
    typed_name = typed.group(1) if typed else ""
    events: list[dict] = []
    typed_source = None
    if typed_name and C.SKILL_NAME.fullmatch(typed_name):
        matched_path = next((p for p in skill_files(cfg, project) if p.parent.name == typed_name), None)
        if matched_path:
            typed_source = router.trigger_source(matched_path, cfg)
            with st.edit(hook=True) as d:
                s = session(d, sid)
                hooks._loaded(d, s, typed_name, typed_source)
                s["typed"] = typed_name
                loaded.add(typed_name)
            events.append(
                {
                    "kind": "load",
                    "delivery": "tool_read",
                    "skill": typed_name,
                    "typed": True,
                    "skill_source": typed_source,
                    "repo": modes.key,
                }
            )
            scope.widen(cfg, typed_source, project)

    idx = skill_index(cfg, project, usage)
    if typed_name:
        idx.exclude.add(typed_name)
    routed = [doc for doc, _ in router.route_request(idx, prompt, cfg.skill_threshold)]
    routed, skipped = scope.split(cfg, routed, project)
    events += skipped
    lines: list[str] = []
    if start_ctx:
        lines.append(start_ctx)
    if routed:
        events.append({"kind": "suggest", "skills": [doc.name for doc in routed]})
        new_names = [doc.name for doc in routed if doc.name not in loaded] if attended else []
        header = (
            "nowitknows router: likely relevant skills; "
            "read each SKILL.md that applies with the Read tool before acting"
        )
        if new_names:
            header += f", and {hooks.name_first(new_names)}:"
        else:
            header += ":"
        lines.append(
            "\n".join(
                [
                    header,
                    *(
                        f"- {doc.name} ({doc.path}): "
                        f"{masked_delivery(doc.description, cfg)[: C.NOTE_DESCRIPTION_CHARS]}"
                        for doc in routed
                    ),
                ]
            )
        )
    request = router.request_text(prompt)
    recall_hint = len(request) <= C.RECALL_REQUEST_MAX_CHARS and C.RECALL_REQUEST.search(request) is not None
    if recall_hint:
        events.append({"kind": "recall_hint"})
        lines.append(
            "nowitknows: this may be in a past session; search it with "
            f"{C.session_tools()}session_search (keywords plus likely synonyms) before asking the user."
        )
    if modes.project_owner == "nowitknows":
        mem = router.route_request(
            router.topic_index(TopicStore(modes.fallback_dir)), prompt, cfg.router_threshold, k=3
        )
        found, owns = [], ownership(cfg.data, modes.shared)
        for doc, _ in mem:
            with contextlib.suppress(OSError):
                raw = Path(doc.path).read_bytes()
                line = doc.path if owns(Path(doc.path), raw) else note_pointer(Path(doc.path), raw, cfg)
                found += [masked_delivery(f"- {line}", cfg)] if line else []
        if found:
            lines.append("\n".join(["nowitknows: project memory files likely relevant (Read them):", *found]))

    body = "\n\n".join(lines)
    if attended and not _subagent(data):

        def fits(block: str, _shown: list[str]) -> bool:
            return len(body) + (2 if body else 0) + len(block) <= C.CURSOR_CONTEXT_CHARS

        with contextlib.suppress(TimeoutError, OSError):
            from .setup import peer_offers, peers_told

            offers = peer_offers(cfg)
            _, block = notify.deliver(
                cfg,
                fits,
                wait_s=C.NOTICE_LOCK_WAIT_S,
                words=_cursor_words,
                project=modes.key or None,
                extra=tuple(offers.values()),
            )
            peers_told(cfg, {k: _cursor_words(v) for k, v in offers.items()}, block or "")
            if block:
                body = f"{body}\n\n{block}" if body else block

    if len(body) > C.CURSOR_CONTEXT_CHARS:
        body = body[: C.CURSOR_CONTEXT_CHARS - C.CURSOR_CONTEXT_RESERVE_CHARS] + "\n[nowitknows: context truncated]"
    for doc in routed:
        line = f"- {doc.name} ({doc.path}): {masked_delivery(doc.description, cfg)[: C.NOTE_DESCRIPTION_CHARS]}"
        if line in body:
            events.append(
                {
                    "kind": "deliver",
                    "skill": doc.name,
                    "delivery": "hook_context",
                    "via": via,
                    "ts": time.time(),
                    "skill_source": router.trigger_source(Path(doc.path), cfg),
                    "repo": modes.key,
                }
            )
    hooks.log_activity(cfg, sid, turn, events)
    return {"additional_context": body} if body else {}


def _post_tool_prompt(cfg: C.Config, data: dict, st: State, sid: str, project: Path) -> dict:
    """Print-mode prompt routing is best effort and must never interfere with the tool result."""
    try:
        if _prompt_hook_marker(cfg, sid).exists():
            return {}
        with st.edit(hook=True, require_commit=True) as d:
            if session(d, sid).get("cursor_prompt_hook_seen"):
                return {}
        path = _resolve_transcript_path(cfg, sid, project, data.get("transcript_path"))
        prompt = cursor_transcript.latest_user_message(path) if path is not None else ""
        if prompt:
            return _prompt_context(cfg, {**data, "prompt": prompt}, via="post_tool_use")
    except Exception:
        pass
    return {}


def pre_tool_use(cfg: C.Config, data: dict) -> dict:
    if cfg.remote:
        return {}
    project = _project(cfg, data)
    tool_name = str(data.get("tool_name") or "")
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    norm_tool = "Write" if tool_name in ("Write", "StrReplace", "Edit", "MultiEdit") else tool_name
    norm_input = dict(tool_input)
    if "path" in norm_input and "file_path" not in norm_input:
        norm_input["file_path"] = norm_input["path"]
    payload = {
        **data,
        "cwd": str(project),
        "tool_name": norm_tool,
        "tool_input": norm_input,
    }
    reason = notes_gate.gate(cfg, payload)
    if not reason:
        return {}
    return {"permission": "deny", "user_message": reason, "agent_message": reason}


def _extract_tool_arg(tool_input: dict) -> str:
    from .outcome import command_text

    for key in ("command", "cmd", "file_path", "path", "pattern", "glob_pattern", "url", "query"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            return (command_text(val) if key in ("command", "cmd") else redact(val.strip()))[: C.TOOL_ARG_CHARS]
    return ""


def outcome_identity(cfg: C.Config, data: dict, sid: str) -> dict:
    call_id = str(data.get("tool_use_id") or data.get("tool_call_id") or "")
    offset = None
    path = _resolve_transcript_path(cfg, sid, _project(cfg, data), data.get("transcript_path"))
    if path is not None:
        with contextlib.suppress(OSError):
            offset = path.stat().st_size
    return {"call_id": call_id, "source_offset": offset}


def _record_tool_outcome(
    s: dict,
    tool_name: str,
    tool_input: dict,
    status: str,
    text: object = "",
    *,
    call_id: str = "",
    source_offset: int | None = None,
) -> None:
    from .outcome import COMMAND_TOOLS, output_text, success_output_allowed
    from .safety import secret_output

    norm_name, identity_input = cursor_transcript._normalize_tool_call(tool_name, tool_input, identity=True)
    background = cursor_transcript._shell_background(norm_name, tool_input)
    if background and status == "ok":
        status = "unknown"
    masked = _masked_outcome(text)
    outcome_text = masked if isinstance(masked, str) else json.dumps(masked, ensure_ascii=False)
    exit_match = re.match(r"(?:Error: )?Exit code (-?\d+)\b", outcome_text)
    command = tool_input.get("command", tool_input.get("cmd", ""))
    safe_tail = (
        output_text(outcome_text, command)
        if text
        and norm_name in COMMAND_TOOLS
        and isinstance(command, str)
        and success_output_allowed(command, s.get("cwd", s.get("start")))
        else ""
    )
    if isinstance(command, str) and secret_output(command):
        outcome_text = "[redacted]"
    outcomes = s.setdefault("cursor_tool_outcomes", [])
    outcomes.append(
        {
            "call_id": call_id,
            "source_offset": source_offset,
            "identity_missing": not call_id and source_offset is None,
            "tool": norm_name,
            "arg": _extract_tool_arg(tool_input),
            "input_key": cursor_transcript._input_key(identity_input),
            "identity_key": cursor_transcript._identity_key(tool_name, tool_input, hook=True),
            "identity_masked": cursor_transcript._identity_masked(tool_name, tool_input),
            "status": status,
            **({"background": True} if background else {}),
            "text": (safe_tail if status in ("ok", "unknown") else redact(outcome_text))[: C.TOOL_ERROR_CHARS]
            if text
            else "",
            "output_tail": safe_tail,
            **({"exit_code": int(exit_match[1])} if exit_match else {}),
            "ts": round(time.time(), 3),
        }
    )
    del outcomes[: -C.CURSOR_TOOL_HISTORY]


def _masked_outcome(value, key: str | None = None):
    if _secret_key(key) and value not in (None, "", [], {}):
        return "[redacted]"
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return _masked_meta(value, key)
        if isinstance(decoded, (dict, list, str)) and decoded != value:
            return _masked_outcome(decoded, key)
        return _masked_meta(value, key)
    if isinstance(value, dict):
        return {redact(k): _masked_outcome(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_masked_outcome(v, key) for v in value]
    return value


def post_tool_use(cfg: C.Config, data: dict) -> dict:
    if cfg.remote:
        return {}
    sid = _session_id(data)
    if not sid:
        return {}
    project = _project(cfg, data)
    tool_name = str(data.get("tool_name") or "")
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    tool_output = data.get("tool_output", data.get("tool_response"))
    use_id = str(data.get("tool_use_id") or "")
    st = State(cfg.path("state.json"), cfg.host, cfg.legacy_host)
    events: list[dict] = []
    loads: list[dict] = []
    made: list[tuple[Path, str | None, str | None]] = []

    with st.edit(hook=True) as d:
        s = session(d, sid)
        seen_ids = s.setdefault("cursor_tool_ids", [])
        use_key = [use_id, tool_name]
        if use_id and use_key in seen_ids:
            return {}
        if use_id:
            seen_ids.append(use_key)
            s["cursor_tool_ids"] = seen_ids
        turn = s["turns"]
        repo = s.get("project") or native.project_key(project)
        wrote_skill = False

        # Detect skill reads via Read or skill_view
        file_path = str(tool_input.get("file_path") or tool_input.get("path") or "")
        is_read_err = isinstance(tool_output, dict) and any(
            tool_output.get(k) for k in ("isError", "is_error", "error")
        )
        if tool_name in ("Read", "ReadFile") and not is_read_err and Path(file_path).name == "SKILL.md":
            with contextlib.suppress(OSError, RuntimeError):
                p = Path(file_path)
                if not p.is_absolute():
                    p = project / p
                resolved = p.resolve()
                matched_skill = next((sp for sp in skill_files(cfg, project) if sp.resolve() == resolved), None)
                if matched_skill is not None or (
                    p.is_file()
                    and C.SKILL_NAME.fullmatch(p.parent.name)
                    and not any(part in ("synced", ".system", "skills-cursor") for part in p.parts)
                ):
                    target_skill = matched_skill or p
                    skill_name = target_skill.parent.name
                    source = router.trigger_source(target_skill, cfg)
                    hooks._loaded(d, s, skill_name, source)
                    s["cursor_evidence_ts"] = time.time()
                    loads.append(source)
                    events.append(
                        {
                            "kind": "load",
                            "delivery": "tool_read",
                            "skill": skill_name,
                            "skill_source": source,
                            "repo": repo,
                        }
                    )

        norm_tool_name, _ = cursor_transcript._normalize_tool_call(tool_name, tool_input)
        bare = C.tool_name(norm_tool_name) or C.tool_name(tool_name.removeprefix("MCP:"))
        res = hooks._json_in(tool_output)
        if bare == "skill_view" and not is_read_err and res.get("ok") is True and isinstance(res.get("path"), str):
            p = Path(res["path"])
            if p.name == "SKILL.md":
                skill_name = p.parent.name
                source = router.trigger_source(p, cfg)
                hooks._loaded(d, s, skill_name, source)
                s["cursor_evidence_ts"] = time.time()
                loads.append(source)
                events.append(
                    {
                        "kind": "load",
                        "delivery": "tool_read",
                        "skill": skill_name,
                        "skill_source": source,
                        "repo": repo,
                    }
                )
        elif bare == "skill_manage":
            if res.get("ok") and res.get("path"):
                wrote_skill = True
                if res.get("action") == "archive":
                    forget_skill(d, res.get("name"))
                else:
                    touch_skill(d, res.get("name"), repo)
                if res.get("action") == "create" and isinstance(res["path"], str):
                    made.append((Path(res["path"]), repo, s.get("start")))
        elif bare == "session_search" and (
            isinstance(res.get("results"), list) or isinstance(res.get("exchanges"), list)
        ):
            events.append({"kind": "search"})
        elif tool_name in ("Write", "StrReplace", "Edit", "MultiEdit") and (edited := hooks._skill_of(cfg, file_path)):
            touch_skill(d, edited, repo)

        outcome_result = {**res, **tool_output} if isinstance(tool_output, dict) else res
        is_err = False
        error_evidence = None
        if bare in ("skill_view", "skill_manage") and outcome_result.get("ok") is False:
            is_err = True
            error_evidence = outcome_result.get("error") or outcome_result.get("errors") or "ok: false"
        elif outcome_result.get("exitCode") not in (None, 0):
            is_err = True
            error_evidence = outcome_result.get("output") or f"exit code {outcome_result.get('exitCode')}"
        elif is_read_err or any(outcome_result.get(k) for k in ("isError", "is_error", "error")):
            is_err = True
            error_evidence = outcome_result.get("error") or outcome_result.get("output") or tool_output
        captured = _masked_outcome(error_evidence if error_evidence is not None else tool_output)
        outcome_text = (
            ""
            if captured in (None, "", {}, [])
            else (captured if isinstance(captured, str) else json.dumps(captured, ensure_ascii=False))
        )
        status = "failed" if is_err else "ok"
        if norm_tool_name in ("Shell", "Bash") and not is_err and outcome_result.get("exitCode") != 0:
            status = "unknown"
        _record_tool_outcome(
            s,
            tool_name,
            tool_input,
            status,
            outcome_text,
            **outcome_identity(cfg, data, sid),
        )

        if wrote_skill:
            s["iters_since_skill"] = 0
        elif not _subagent(data):
            s["iters_since_skill"] += 1

    hooks.log_activity(cfg, sid, turn, events)
    for source in loads:
        scope.widen(cfg, source, project)
    for path, proj_key, start in made:
        scope.pin(cfg, path, proj_key, start)
    return _post_tool_prompt(cfg, data, st, sid, project)


def post_tool_use_failure(cfg: C.Config, data: dict) -> dict:
    if cfg.remote:
        return {}
    sid = _session_id(data)
    if not sid:
        return {}
    tool_name = str(data.get("tool_name") or "")
    tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    failure_type = str(data.get("failure_type") or "error")
    error_msg = data.get("error_message") or failure_type
    status = "denied" if failure_type == "permission_denied" else "failed"
    if data.get("is_interrupt"):
        status = "unknown"
    st = State(cfg.path("state.json"), cfg.host, cfg.legacy_host)
    with st.edit(hook=True) as d:
        s = session(d, sid)
        _record_tool_outcome(s, tool_name, tool_input, status, error_msg, **outcome_identity(cfg, data, sid))
        if not _subagent(data):
            s["iters_since_skill"] += 1
    return {}


def stop(cfg: C.Config, data: dict) -> dict:
    if cfg.remote or data.get("stop_hook_active") or _subagent(data):
        return {}
    sid = _session_id(data)
    if not sid:
        return {}
    if (_attended(cfg, data) or cfg.review_unattended) and hooks._due(cfg, {**data, "session_id": sid}):
        queue(cfg, data, "stop")
    return {}


def pre_compact(cfg: C.Config, data: dict) -> dict:
    if cfg.remote or _subagent(data):
        return {}
    sid = _session_id(data)
    if not sid:
        return {}
    st = State(cfg.path("state.json"), cfg.host, cfg.legacy_host)
    with st.edit(hook=True) as d:
        s = session(d, sid)
        s.pop("cursor_started", None)
        s.pop("cursor_start_delivered", None)
        s.pop("last_turn_id", None)
        s["cursor_compacted"] = True
        s["mod_precompact"] = time.time()
    if cfg.review and (_attended(cfg, data) or cfg.review_unattended):
        queue(cfg, data, "precompact")
    return {}


def session_end(cfg: C.Config, data: dict) -> dict:
    if cfg.remote or _subagent(data):
        return {}
    sid = _session_id(data)
    if not sid:
        return {}
    queue(cfg, data, "end", learn=bool(_attended(cfg, data) or cfg.review_unattended))
    return {}


HANDLERS = {
    "SessionStart": session_start,
    "UserPromptSubmit": before_submit_prompt,
    "PreToolUse": pre_tool_use,
    "PostToolUse": post_tool_use,
    "PostToolUseFailure": post_tool_use_failure,
    "Stop": stop,
    "PreCompact": pre_compact,
    "SessionEnd": session_end,
}


def handle(event: str, data: dict, cfg: C.Config | None = None) -> dict:
    cfg = cfg or configuration()
    canonical = EVENT_MAP.get(event, event)
    if cfg.child or canonical not in HANDLERS:
        return {}
    return HANDLERS[canonical](cfg, data) or {}


def select_runner(cfg: C.Config, *, check_auth: bool = True, role: str = "reviewer") -> tuple[str | None, str]:
    """Check only the official SDK login, without starting a model turn."""
    from . import cursor_runner

    ok, detail = cursor_runner.check(cfg) if check_auth else (True, "Cursor SDK login not checked")
    return ("cursor" if ok else None), detail


def _normalize_session_transcript(cfg: C.Config, sid: str, source: Path | None, cwd: str) -> Path | None:
    if source is None or not source.exists():
        return None
    st = State(cfg.path("state.json"), cfg.host, cfg.legacy_host).read()
    outcomes = st.get("sessions", {}).get(sid, {}).get("cursor_tool_outcomes") or []
    proj_slug = cursor_transcript.project_key(cwd) or "cursor-project"
    target = cfg.transcripts / proj_slug / f"{sid}.jsonl"
    try:
        return cursor_transcript.normalize(
            source,
            target,
            cwd,
            expected_session=sid,
            outcomes=outcomes,
        )
    except (OSError, ValueError) as exc:
        _error(cfg, f"transcript normalize failed for {sid}: {exc}")
        return None


def index_profile(cfg: C.Config) -> int:
    """Normalize and index Cursor transcripts under `~/.cursor/projects/*/agent-transcripts/`."""
    projects_dir = cfg.cursor_paths.transcript_projects
    st = State(cfg.path("state.json"), cfg.host, cfg.legacy_host).read()
    sessions_state = st.get("sessions", {})
    if projects_dir.is_dir():
        proj_root = projects_dir.resolve()
        for tdir in sorted(projects_dir.glob("*/agent-transcripts")):
            candidates = [*sorted(tdir.rglob("*.jsonl")), *sorted(tdir.rglob("*.txt"))]
            seen_sids: set[str] = set()
            for path in candidates:
                if "subagents" in path.parts or path.is_symlink() or not path.is_file():
                    continue
                with contextlib.suppress(OSError, RuntimeError):
                    if not path.resolve().is_relative_to(proj_root):
                        continue
                sid = _safe_id(cursor_transcript._session_id_from_path(path))
                if not sid or sid in seen_sids:
                    continue
                seen_sids.add(sid)
                rec = sessions_state.get(sid, {})
                cwd = str(rec.get("start") or rec.get("cwd") or "")
                raw_folder = cursor_transcript.project_key(cwd) if cwd else tdir.parent.name
                folder = _safe_id(raw_folder) or "cursor-project"
                target = cfg.transcripts / folder / f"{sid}.jsonl"
                try:
                    if target.exists() and target.stat().st_mtime >= path.stat().st_mtime:
                        continue
                    cursor_transcript.normalize(
                        path,
                        target,
                        cwd or None,
                        expected_session=sid,
                        outcomes=rec.get("cursor_tool_outcomes") or [],
                    )
                except (OSError, ValueError):
                    continue
    conn = sessions.connect(cfg.path("sessions.db"), cfg.legacy_host)
    try:
        return sessions.ingest(conn, [cfg.transcripts, cfg.data / "archive"])
    finally:
        conn.close()


def _pause_notice(cfg: C.Config, detail: str) -> None:
    notify.push(cfg, f"{notify.PREFIX} {detail}", key="cursor-model", account=True, tell={"kind": "cursor-paused"})


def _run_cursor_steps(cfg: C.Config, sid: str, normalized: Path | None, cwd: str, reason: str) -> dict:
    from . import model, paid

    if reason in ("end", "precompact") and normalized and normalized.is_file():
        modreview.enqueue(cfg, {"session": sid, "transcript": str(normalized), "cwd": cwd, "reason": reason})
    transcript_arg = str(normalized) if normalized and normalized.is_file() and reason != "start" else ""
    step = modreview.plan(cfg, sid, transcript_arg, cwd)
    calls = 0
    while step.get("action") == "model" and calls < C.CODEX_REVIEW_STEPS:
        stored = modreview._read(cfg.path("mod", "steps", step["step"] + ".json"))
        request = dict(stored["request"])
        if stored["kind"] == "reviewer":
            request["schema"] = modreview.REVIEW_SCHEMA
        saved = worker._queued_cfg(cfg, stored["env"])
        try:
            with paid.capture(saved, {"kind": "module", "step": step["step"]}) as receipt:
                response = model.run(saved, **request)
                text = (
                    json.dumps(response["structured_output"])
                    if isinstance(response.get("structured_output"), dict)
                    else response["result"]
                )
                step = modreview.answer(saved, step["step"], {"text": text, "usage": response["usage"]})
                paid.complete(saved, receipt)
        except claude.ClaudeError as exc:
            if isinstance(exc, claude.AuthError):
                modreview.release(cfg, sid)
            else:
                modreview.fail(
                    saved, step["step"], {"error": str(exc), "paid_reply": getattr(exc, "paid_reply", False)}
                )
                if stored["kind"] != "reviewer":
                    with State(saved.path("state.json"), saved.host, saved.legacy_host).edit() as state:
                        w = state["worker"]
                        w["failures"] = w.get("failures", 0) + 1
                        w["backoff_until"] = time.time() + min(
                            C.BACKOFF_MAX_S, C.BACKOFF_BASE_S * 2 ** (w["failures"] - 1)
                        )
            detail = redact(str(exc))
            if isinstance(exc, cursor_runner.CursorTimeout):
                return {"calls": calls, "runner": "cursor", "action": "done", "retry": detail}
            _pause_notice(cfg, detail)
            return {"calls": calls, "runner": "cursor", "paused": detail}
        calls += 1
    return {"calls": calls, "runner": "cursor", "action": step.get("action")}


def _process_job(cfg: C.Config, job_args: dict) -> dict:
    from .skills import SkillStore

    reason = str(job_args.get("reason") or "stop")
    sid = str(job_args.get("session") or "")
    cwd = str(job_args.get("cwd") or ".")
    SkillStore(cfg).reconcile_shadows(Path(cwd))
    raw_transcript = Path(job_args["transcript"]) if job_args.get("transcript") else None
    if raw_transcript is None and sid:
        raw_transcript = _resolve_transcript_path(cfg, sid, Path(cwd))
    normalized = _normalize_session_transcript(cfg, sid, raw_transcript, cwd) if sid and reason != "start" else None

    result: dict = {"reason": reason, "session": sid}
    if reason == "end" and normalized and normalized.exists():
        result["archive"] = str(sessions.archive(normalized, cfg.data / "archive", cfg.archive_max_mb))
        with State(cfg.path("state.json"), cfg.host, cfg.legacy_host).edit() as state:
            session(state, sid).pop("cursor_tool_outcomes", None)
    if reason in ("start", "end"):
        result["indexed"] = index_profile(cfg)
        state = State(cfg.path("state.json"), cfg.host, cfg.legacy_host).read()
        stale = time.time() - SESSION_TTL_S
        for marker in (cfg.data / "cursor-sessions").glob("*.prompt-hook"):
            with contextlib.suppress(OSError):
                sid_seen = state["sessions"].get(marker.name.removesuffix(".prompt-hook"), {}).get("seen", 0)
                if max(marker.stat().st_mtime, sid_seen) < stale:
                    marker.unlink()
    if reason == "start":
        from .skills import SkillStore

        SkillStore(cfg).register_cursor_profile()
        with contextlib.suppress(OSError, RuntimeError, ValueError, TimeoutError):
            identity.start(cfg, Path(cwd), native.detect(cfg, Path(cwd)))
        result["scope"] = scope.backfill(cfg, Path(cwd))
    learn = worker.guarded(cfg).review and bool(job_args.get("learn", True))
    if not learn:
        result["review"] = {"ran": False, "reason": "unattended or review off"}
        return result

    from . import cursor_models

    if reason in ("end", "precompact") and normalized and normalized.is_file():
        modreview.enqueue(cfg, {"session": sid, "transcript": str(normalized), "cwd": cwd, "reason": reason})
    state = State(cfg.path("state.json"), cfg.host, cfg.legacy_host).read()
    if time.time() < state["worker"].get("backoff_until", 0):
        return {**result, "calls": 0, "runner": "cursor", "action": "done"}
    cursor_models.refresh(cfg)
    runner, detail = select_runner(cfg, role="judge")
    if runner is None:
        detail = cursor_runner.pause_line("login")
        cursor_runner._status(cfg, reason, cursor_models.target(cfg), "paused", "login")
        _pause_notice(cfg, detail)
        result["review"] = {"ran": False, "reason": detail}
        return result

    if job_args.get("curate"):
        result["curator"] = worker.curate(cfg)
    result.update(_run_cursor_steps(cfg, sid, normalized, cwd, reason))
    if (
        reason in ("start", "end")
        and job_args.get("triggers", True)
        and not (result.get("paused") or result.get("retry"))
    ):
        try:
            result["triggers"] = worker.backfill_triggers(cfg, Path(cwd))
        except cursor_runner.CursorTimeout as exc:
            with State(cfg.path("state.json"), cfg.host, cfg.legacy_host).edit() as state:
                w = state["worker"]
                w["failures"] = w.get("failures", 0) + 1
                w["backoff_until"] = time.time() + min(C.BACKOFF_MAX_S, C.BACKOFF_BASE_S * 2 ** (w["failures"] - 1))
            result["retry"] = redact(str(exc))
        except claude.ClaudeError as exc:
            result["error"] = redact(str(exc))

    return result


def run_worker(cfg: C.Config | None = None) -> dict:
    from . import skillsync

    cfg = cfg or configuration()
    jobs_dir = cfg.cursor_paths.jobs
    results: list[dict] = []
    metrics_jobs = []
    try:
        with file_lock(cfg.path("cursor-worker"), wait_s=C.SQLITE_TIMEOUT_S):
            done = 0
            while done < C.CURSOR_JOB_LIMIT:
                queued = sorted(jobs_dir.glob("*.json")) if jobs_dir.exists() else []
                if not queued:
                    break
                path = queued[0]
                done += 1
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(item, dict):
                        raise ValueError("job is not a JSON object")
                except (OSError, ValueError):
                    with contextlib.suppress(OSError):
                        path.replace(path.with_suffix(".bad"))
                    continue
                queued_cfg = worker._queued_cfg(cfg, item.get("env", {})) if isinstance(item.get("env"), dict) else cfg
                job_cfg = replace(queued_cfg, env={**queued_cfg.env, "NOWITKNOWS_CHILD": "0"})
                try:
                    metrics_jobs.append((job_cfg, item))
                    results.append(_process_job(job_cfg, item))
                    with contextlib.suppress(OSError):
                        path.unlink(missing_ok=True)
                except Exception as exc:
                    _error(job_cfg, f"worker job failed: {exc}")
                    results.append({"error": redact(str(exc))})
                    with contextlib.suppress(OSError):
                        path.replace(path.with_suffix(".failed"))
    except TimeoutError:
        return {"busy": True, "jobs": results}
    from .session_metrics import after_job

    for selected, item in metrics_jobs:
        after_job(selected, item)
    try:
        skillsync.sync(cfg)
    except Exception as error:
        _error(cfg, f"skill backup skipped: {type(error).__name__}")
    return {"jobs": results}


def doctor(cfg: C.Config) -> dict:
    from .skills import SkillStore

    runner, detail = select_runner(cfg, role="judge")
    status_path = cfg.data / "cursor-status.json"
    last_status = None
    if status_path.exists():
        with contextlib.suppress(OSError, ValueError):
            last_status = json.loads(status_path.read_text(encoding="utf-8"))
    if not isinstance(last_status, dict) or last_status.get("version") != cursor_runner.status_version():
        last_status = {}
    kind = last_status.get("reason_kind")
    if kind not in ("", "login", "call"):
        last_status = {}
    elif last_status.get("state") == "paused":
        last_status["reason"] = cursor_runner.pause_line(kind)
    if runner and kind == "login":
        cursor_runner._status(cfg, "login-check", last_status.get("model", ""), "ready")
        last_status = {"state": "ready"}
    line = cursor_runner.pause_line("call") if last_status.get("state") == "paused" and kind == "call" else ""
    if runner is None:
        line = cursor_runner.pause_line("login")
    if not cfg.review:
        line = ""
    account_check = {"state": "ready" if runner else "paused", "runner": runner, "detail": detail}
    review_transport = {"state": "ready" if runner else "skipped", "runner": runner}
    return {
        "host": "cursor",
        "cursor_home": str(cfg.cursor_paths.profile),
        "data_dir": str(cfg.data),
        "skills_home": str(cfg.skills_home),
        "skills_library": str(cfg.skills_library),
        **({"skills_without_links": missing} if (missing := SkillStore(cfg).unlinked()) else {}),
        "account_check": account_check,
        "sdk_check": account_check,
        "review_transport": review_transport,
        "background_runner": runner,
        "background_detail": detail,
        "learning": "off" if not cfg.review else "paused" if line else "ready" if runner else "off",
        "learning_message": line,
        "setup_command": "/nowitknows-setup" if line else "",
        "learning_status": last_status or {"state": "idle"},
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="nowitknows-cursor")
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hook")
    h.add_argument("event")
    sub.add_parser("worker")
    sub.add_parser("mcp")
    sub.add_parser("doctor")
    sub.add_parser("status")
    s = sub.add_parser("setup")
    s.add_argument("--git", action="store_true")
    s.add_argument("--cursor-sdk", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--backup-repo", metavar="OWNER/NAME")
    s.add_argument("--separate-notes", action="store_true")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--apply", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("configure")
    c.add_argument("pairs", nargs="*")
    sub.add_parser("migrate").add_argument("--apply", action="store_true")
    ch = sub.add_parser("changes")
    ch.add_argument("--n", type=int, default=C.CHANGES_SHOWN)
    sub.add_parser("index")
    sub.add_parser("index-SessionStart")
    sub.add_parser("index-SessionEnd")
    r = sub.add_parser("report")
    r.add_argument("--weeks", type=int, default=C.REPORT_WEEKS)
    r.add_argument("--json", action="store_true")
    sub.add_parser("ui").add_argument("--open", action="store_true")
    k = sub.add_parser("keep")
    k.add_argument("name")

    ns = p.parse_args(args)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if ns.cmd == "hook":
        cfg = None
        raw = ""
        out: dict = {}
        try:
            raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
            cfg = configuration()
            payload = json.loads(raw) if raw.strip() else {}
            out = handle(ns.event, payload if isinstance(payload, dict) else {}, cfg)
            if C.env("NOWITKNOWS_DEBUG", cfg.env) == "1":
                with open(cfg.path("debug.jsonl"), "a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(
                            {
                                "event": EVENT_MAP.get(ns.event, "unknown"),
                                "ts": time.time(),
                                "session_id": _session_id(payload) if isinstance(payload, dict) else None,
                                "generation_id": _safe_id(payload.get("generation_id"))
                                if isinstance(payload, dict)
                                else None,
                                "input_bytes": len(raw.encode("utf-8")),
                                "output_bytes": len(json.dumps(out).encode("utf-8")),
                            }
                        )
                        + "\n"
                    )
        except Exception:
            if cfg is not None:
                _error(cfg, f"hook {ns.event}\n{traceback.format_exc()}")
            out = {}
        sys.stdout.write(json.dumps(out))
        return 0

    if ns.cmd == "mcp":
        os.environ["NOWITKNOWS_HOST"] = "cursor"
        from .mcp_server import main as serve

        serve()
        return 0

    if ns.cmd == "worker":
        cfg = configuration()
        print(json.dumps(run_worker(cfg), default=str))
        return 0

    if ns.cmd == "doctor":
        cfg = configuration(create=False)
        print(json.dumps(doctor(cfg), indent=2, sort_keys=True))
        return 0

    if ns.cmd == "configure":
        cfg = configuration()
        updates = {}
        for pair in ns.pairs:
            if "=" not in pair:
                raise ValueError(f"Expected key=value, got {pair!r}")
            k_name, _, val = pair.partition("=")
            updates[k_name.strip()] = val.strip()
        saved = configure(cfg, updates)
        print(
            json.dumps(
                {
                    "ok": True,
                    "settings": {k: v for k, v in saved.items() if k in CONFIGURABLE},
                    "ignored": {k: v for k, v in saved.items() if k not in CONFIGURABLE},
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if ns.cmd == "status":
        from .setup import status

        cfg = configuration(create=False)
        print(_cursor_words(status(cfg, Path.cwd())))
        from . import cursor_models

        print(f"Cursor background model: {cursor_models.target(cfg)} (all roles)")
        with contextlib.suppress(OSError, ValueError):
            saved = json.loads((cfg.data / "cursor-status.json").read_text(encoding="utf-8"))
            if isinstance(saved, dict) and saved.get("version") == cursor_runner.status_version():
                print(f"last Cursor learning: {saved.get('state', 'unknown')}")
        return 0

    if ns.cmd == "changes":
        from .setup import changes

        cfg = configuration(create=False)
        print(_cursor_words(changes(cfg, ns.n)))
        return 0

    if ns.cmd == "keep":
        from .skills import SkillStore

        cfg = configuration()
        name = ns.name.strip()
        res = SkillStore(cfg).keep(name)
        if not res["ok"]:
            print(f"nowitknows: {res['error']}")
            return 1
        verb = "restored" if res["action"] == "restore" else "kept"
        link = notify.link(res["path"])
        print(f"{notify.PREFIX} {verb} skill {name}; it is yours now, and nowitknows never archives it {link}")
        return 0

    if ns.cmd == "report":
        from .report import render
        from .telemetry import summary

        cfg = configuration(create=False)
        if ns.json:
            print(json.dumps(summary(cfg), indent=2, sort_keys=True))
        else:
            print(_cursor_words(render(cfg, ns.weeks)))
        return 0

    if ns.cmd == "ui":
        from .ui import run as run_ui

        return run_ui(configuration(create=False), open_browser=ns.open)

    if ns.cmd in ("index", "index-SessionStart", "index-SessionEnd"):
        cfg = configuration()
        print(index_profile(cfg))
        return 0

    if ns.cmd == "migrate":
        from .migrate import run as run_migrate

        return run_migrate(configuration(create=False), apply=ns.apply)

    if ns.cmd == "setup":
        from .setup import run as run_setup

        cfg = configuration(create=not ns.dry_run)
        print("SDK runs follow your account Privacy Mode, the same as the IDE.")
        if ns.git or ns.separate_notes or ns.backup_repo:
            return run_setup(
                cfg,
                retention_days=0,
                git=ns.git,
                allow_tools=False,
                assume_yes=ns.yes,
                dry_run=ns.dry_run,
                separate_notes=ns.separate_notes,
                backup_repo=ns.backup_repo,
            )
        from .setup import install

        return install(cfg, apply=ns.apply, dry_run=ns.dry_run, cursor=True)

    return 0
