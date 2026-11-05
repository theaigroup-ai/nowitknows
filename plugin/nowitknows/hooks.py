from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import claude, identity, native, notify, router, scope
from . import config as C
from .delivery import finish_delivery, masked_delivery
from .memory import EntryStore, TopicStore, memory_read_pointer, note_pointer, ownership
from .safety import _PATTERNS, redact
from .safety import lines as safety_lines
from .skills import SkillStore, norm_file
from .state import State, atomic_write, count_session, forget_skill, log_row, review_factor, session, touch_skill

SLASH = re.compile(r"\s*/([\w.:-]+)")
LAUNCHED = ("Launching skill:", "Loaded skill instructions")
REFUSED = ("denied", "doesn't want to proceed", "rejected", "error", "Unknown skill", "disabled", "not run")
EDITS = ("Write", "Edit", "MultiEdit")
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
BREAKAWAY_FROM_JOB = 0x01000000
ROUTER_PLAIN = "nowitknows router: likely relevant skills; load each that applies with the Skill tool before acting:"
# Automated sessions omit the skill-use line because nobody reads their replies.
SKILL_USE = (
    "When a skill first shapes your work in this session, start that reply with one short line naming it, such as: "
    "Using the <skill> skill."
)


def name_first(names: list[str]) -> str:
    """Asks to name only `names`, the skills not loaded in the session yet: a loaded one is never asked about again."""
    if len(names) == 1:
        return f"name {names[0]} in your reply's first line if you use it"
    return f"name whichever of {', '.join(names)} you use"


def router_header(new: list[str]) -> str:
    return f"{ROUTER_PLAIN.removesuffix(':')}, and {name_first(new)}:" if new else ROUTER_PLAIN


def _attended(cfg: C.Config) -> bool:
    """Someone is working in this session: Claude Code marks it attended; scripted `claude -p` and SDK runs are not.
    Remote Control children run as `claude -p` with attended 0, though a person sends every turn."""
    if cfg.env.get("CLAUDE_CODE_ENVIRONMENT_KIND") == C.REMOTE_CONTROL_KIND:
        return True
    attended = cfg.env.get("CLAUDE_CODE_SESSION_ATTENDED")
    if attended is not None:
        return attended == "1"
    return cfg.env.get("CLAUDE_CODE_ENTRYPOINT") not in C.HEADLESS_ENTRYPOINTS


def surface(cfg: C.Config) -> dict:
    """The session's front end as Claude Code reports it to hooks, kept per session for probes and reports."""
    out = {key: cfg.env.get(var) for key, var in C.SURFACE_ENV.items()}
    return out | {key: bool(cfg.env.get(var)) for key, var in C.SURFACE_FLAGS.items()}


def from_user(data: dict) -> bool:
    """False for a background task's completion notice, which the host delivers as a prompt. Not keyed on `source`:
    its `system` value also covers coordinator relays, which carry the user's task."""
    return not str(data.get("prompt", "")).lstrip().startswith(C.NOTICE_PROMPT)


def _project(cfg: C.Config, data: dict) -> Path:
    return Path(data.get("cwd") or cfg.env.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def spawn(cfg: C.Config, args: list[str]) -> None:
    if C.env("NOWITKNOWS_DRY_RUN", cfg.env) == "1":
        with open(cfg.path("spawned.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(args) + "\n")
        return
    env = {**cfg.env, "PYTHONPATH": str(C.ROOT)}
    if exe := claude.find_claude(cfg.env):  # resolve now: the session's process may be gone when the worker runs
        env["NOWITKNOWS_CLAUDE_BIN"] = exe
    python = sys.executable
    if sys.platform == "win32":
        # A venv's python.exe is a launcher that starts the interpreter without our creation flags, so the worker
        # would get a console window; the worker needs only the standard library and PYTHONPATH.
        base = getattr(sys, "_base_executable", "")
        python = base if base and os.path.isfile(base) else python
    _detach(cfg, [python, "-m", "nowitknows", *args], env, "worker.log")


def _detach(cfg: C.Config, argv: list[str], env: dict, log_name: str) -> int:
    """Start argv outside Claude Code's process group or job; its pid, 0 when unknown."""
    with open(cfg.path(log_name), "a", encoding="utf-8") as log:
        launch = {"cwd": str(C.ROOT), "env": env, "stdin": subprocess.DEVNULL, "stdout": log, "stderr": log}
        if sys.platform != "win32":
            started = subprocess.Popen(argv, **launch, start_new_session=True)
        else:
            started = _windows_detach(argv, launch)
    return getattr(started, "pid", 0)


def _windows_detach(argv: list[str], launch: dict, flags: int = 0):
    """Try to leave the host's job; some jobs forbid breakaway."""
    try:
        return subprocess.Popen(argv, **launch, creationflags=DETACHED | BREAKAWAY_FROM_JOB | flags)
    except OSError:
        return subprocess.Popen(argv, **launch, creationflags=DETACHED | flags)


def setup_finished(cfg: C.Config) -> None:
    """Say once, in a session someone works in, that a background setup finished (bin/bootstrap.sh leaves the file; an
    MCP start then removes it, as its tools start); unsaid once no new session skips a server that failed before it."""
    if cfg.remote:
        return
    path = cfg.path("bootstrap.finished")
    with contextlib.suppress(OSError, TimeoutError):  # the notice is a courtesy; a hook never fails on it
        age = time.time() - path.stat().st_mtime
        if age < C.SETUP_NOTICE_SETTLE_S or (age < C.MCP_RETRY_MINUTES * 60 and not _attended(cfg)):
            return  # a server waiting for this setup removes it first; a session nobody works in leaves it
        path.unlink()  # whoever removes it says it, so it is said once
        if age < C.MCP_RETRY_MINUTES * 60:
            notify.push(cfg, f"{notify.PREFIX} finished setting up; reconnect nowitknows in /mcp to use its tools.")


def codex_later(cfg: C.Config) -> bool:
    """Whether an attended Claude Code session owes the line that Codex is there without nowitknows in its profile:
    file checks only (codex_client.find_codex's), no codex call; due once per CODEX_LATER_DAYS after it was last told
    (`codex_told` stamps it), and never again once nowitknows-codex is installed."""
    if cfg.host != "claude" or cfg.remote or not _attended(cfg):
        return False
    mark = cfg.path("codex-later")
    try:
        said = mark.read_text(encoding="utf-8").strip()
    except OSError:
        said = ""
    with contextlib.suppress(OSError, ValueError):  # the notice is a courtesy; a hook never fails on it
        env = cfg.env
        profile = Path(C.env("NOWITKNOWS_CODEX_HOME", env) or env.get("CODEX_HOME") or C.home_dir(env) / ".codex")
        if said == "installed":
            return False
        # install_codex.py's cache: <profile>/plugins/cache/<marketplace>/nowitknows-codex/<version>, from any market
        if any(
            path
            for name in C.aliases("nowitknows-codex")
            for path in profile.expanduser().glob(f"plugins/cache/*/{name}/*")
        ):
            atomic_write(mark, "installed\n")
            return False
        from .codex_client import find_codex

        if not find_codex(env) or not profile.expanduser().is_dir():  # a profile shows Codex has been used
            return False
        try:
            last = float(said) if said else 0.0
        except ValueError:  # a damaged mark counts as never told
            last = 0.0
        return time.time() - last >= C.CODEX_LATER_DAYS * 86400
    return False


def codex_told(cfg: C.Config) -> None:
    """Stamp the Codex-later line as told, once a prompt has told it, so a start with no prompt spends no interval."""
    with contextlib.suppress(OSError):
        atomic_write(cfg.path("codex-later"), f"{time.time()}\n")


def _shell_owed(cfg: C.Config, sid: str) -> list[str]:
    """The setup kind bin/nowitknows.sh left owed in this attended session (`setup-owed.<sid>`), taken once the
    environment runs: only a start that failed is still true then; what it said before it was ready is not."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
        return []
    path = cfg.path(f"setup-owed.{sid}")
    try:
        kinds = path.read_text(encoding="utf-8").split()
        path.unlink()
    except OSError:
        return []
    return [kind for kind in kinds if kind == "start-failed"]


def worker_args(data: dict, reason: str, **extra) -> list[str]:
    args = [
        "worker",
        "--reason",
        reason,
        "--session",
        data.get("session_id") or "",
        "--transcript",
        data.get("transcript_path") or "",
        "--cwd",
        data.get("cwd") or "",
    ]
    for k, v in extra.items():
        args += [f"--{k}", str(v)]
    return args


def _with_notices(
    cfg: C.Config,
    out: dict,
    event: str = "",
    project: str | None = None,
    owed: tuple[str | None, ...] = (),
) -> tuple[dict, bool]:
    """Queued notices as the systemMessage in a session someone works in (a script's leaves the queue as it is); at a
    prompt (`event`) they and `owed` are told to the model once, when all fits; True when told."""
    setup_finished(cfg)
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext", "")

    def fits(block: str, shown: list[str]) -> bool:
        return len(ctx) + 2 + len(block) + len("\n".join(shown)) <= C.INLINE_LIMIT

    attended = _attended(cfg)
    tell, lines, block = fits if event and attended else None, [], ""
    try:
        if attended:
            lines, block = notify.deliver(cfg, tell, wait_s=C.NOTICE_LOCK_WAIT_S, project=project, extra=owed)
    except TimeoutError:  # the queue waits for the next hook; the owed setup lines need no queue
        said = notify.block(list(owed)) if tell and owed else ""
        block = said if said and fits(said, []) else ""
    lines = notify.compact(lines)
    if lines:
        out["systemMessage"] = "\n".join(lines)
    if lines and cfg.os_notify:
        out["terminalSequence"] = notify.osc9(lines)
    if block:
        out["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": f"{ctx}\n\n{block}" if ctx else block}
    return out, bool(block)


def log_activity(cfg: C.Config, sid: str, turn: int, rows: list[dict]) -> None:
    """Local field telemetry: router suggestions, recall hints, skill loads, and searches per session turn."""
    if not rows:
        return
    now = round(time.time(), 3)
    try:
        for row in rows:
            log_row(
                cfg, "activity.jsonl", {"ts": now, "session": sid, "turn": turn, **row}, wait_s=C.ACTIVITY_LOCK_WAIT_S
            )
    except (OSError, TimeoutError) as e:  # telemetry never costs a hook its router lines or write notices
        with contextlib.suppress(OSError), open(cfg.path("errors.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.ctime()} activity.jsonl not written: {e}\n")


def save_options(cfg: C.Config) -> None:
    """Keep the options this session's hooks see for commands; a session that exports none (--plugin-dir) keeps the
    saved ones."""
    path, options = cfg.path(C.OPTIONS_FILE), C.plugin_options(cfg.env)
    if not options:
        return
    try:
        if json.loads(path.read_text(encoding="utf-8")) == options:
            os.utime(path, None)
            return
    except (OSError, ValueError):
        pass
    atomic_write(path, json.dumps(options, sort_keys=True))


def _protocol(cfg: C.Config, name: str) -> str:
    text = C.prompt(name).replace("\n" + C.MEMORY_WRITING_RULES, "")
    return text if _attended(cfg) else text.replace(" " + SKILL_USE, "")


def session_start(cfg: C.Config, data: dict) -> dict:
    project = _project(cfg, data)
    if cfg.remote:  # a cloud VM: nothing written there outlives the session
        ctx = _protocol(cfg, "skills_protocol_remote.md")
        if data.get("source") == "compact":
            ctx += "\n\n" + C.prompt("compact.md")
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}
    from .setup import announce_version_mismatch, legacy_claude_notices

    if _attended(cfg):
        announce_version_mismatch(cfg)
    for notice in legacy_claude_notices(cfg):
        notify.push(cfg, f"{notify.PREFIX} {notice}", key="legacy-install:" + notice, permanent=True)
    parts = [_protocol(cfg, "skills_protocol.md")]
    if data.get("source") == "compact":
        parts.append(C.prompt("compact.md"))
    save_options(cfg)
    modes = native.detect(cfg, project)

    with contextlib.suppress(OSError, RuntimeError, ValueError, TimeoutError):  # identity never blocks memory
        identity.start(cfg, project, modes)
    st = State(cfg.path("state.json"), cfg.host, cfg.legacy_host)
    with st.edit() as d:
        s = session(d, data.get("session_id", "unknown"))
        s["pid"] = cfg.env.get("CLAUDE_PID")  # the process running the chat now: the migration fence reads it
        s["in_session"] = C.env("NOWITKNOWS_IN_SESSION", cfg.env) == data.get("session_id", "unknown")
        s["modes"] = {
            "hosted": modes.hosted_memory,
            "auto": modes.auto_memory,
            "native_dir": str(modes.native_dir),
            "fallback_dir": str(modes.fallback_dir),
            "shared": modes.shared,
        }
        proj = d["projects"].setdefault(modes.key, {})  # memory owner per repo: repos may share one memory folder
        s["project"] = modes.key
        s["cwd"] = str(project)
        s.setdefault("start", str(project))  # where the session started; its later folders may be other repos
        pending = bool(s.get("setup_owed"))
        old = s.get("surface") if isinstance(s.get("surface"), dict) else {}
        counted = bool(old.get("counted"))  # a compact or resume start keeps the first answer
        s["surface"] = surface(cfg)  # which front end ran it: the desktop app, a Remote Control thread, a script
        if data.get("source") != "compact" and _attended(cfg):  # skills age by working sessions in their repo
            process = [cfg.env.get("CLAUDE_PID") or data.get("session_id", "unknown")]
            if cfg.env.get("CLAUDE_CODE_ENVIRONMENT_KIND") == C.REMOTE_CONTROL_KIND:
                process.append(f"bridge:{data.get('session_id') or 'unknown'}")  # idle children respawn, new pid
            counted = count_session(d, modes.key, process, time.time(), C.PROCESS_MEMORY_DAYS * 86400) or counted
        if counted:
            s["surface"]["counted"] = True
        migrated = native.reconcile(modes, C.owner(proj), cfg.data, cfg.host)
        proj.update(C.owner_fields(modes.project_owner))
        last_activity = max(
            (x.get("seen", 0) for k, x in d["sessions"].items() if k != data.get("session_id")), default=0
        )
        curate_due = (
            time.time() - d["worker"].get("last_curator", 0) > C.CURATOR_EVERY_DAYS * 86400
            and time.time() - last_activity > C.CURATOR_IDLE_HOURS * 3600
        )
    if migrated and migrated["copied"]:
        notify.push(
            cfg,
            f"{notify.PREFIX} moved {migrated['copied']} project memory files ({migrated['direction']}) "
            f"{notify.link(modes.project_dir)}",
        )
    if modes.global_tier:
        note = (
            "Claude Code's memory for this repo"
            if modes.project_owner == "native"
            else f"{C.session_tools()}memory target 'project' (index below)"
        )
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
        room = C.INLINE_LIMIT - len("\n\n".join(parts)) - len(head) - 200
        parts.append(head + "\n" + _fit_index(TopicStore(modes.fallback_dir), room, cfg))
    if modes.shared:
        from . import migrate

        migrate.announce_unshared(cfg, modes.key, native.project_name(project))
    spawn(cfg, worker_args(data, "start", curate=int(curate_due), learn=int(_learns(cfg))))
    later = codex_later(cfg)
    if _attended(cfg) and (pending or later):  # a new start's setup state supersedes what an older one owed
        with st.edit() as d:
            session(d, data.get("session_id", "unknown"))["setup_owed"] = ["codex-later"] if later else []
    if re.fullmatch(r"[A-Za-z0-9_-]+", sid := str(data.get("session_id") or "")):
        cfg.path(f"setup-owed.{sid}").unlink(missing_ok=True)  # a failed start this session's shell owes comes after
    context = {"hookEventName": "SessionStart", "additionalContext": "\n\n".join(parts)}
    out = _with_notices(cfg, {"hookSpecificOutput": context})[0]
    ctx = out["hookSpecificOutput"]["additionalContext"]
    room = C.INLINE_LIMIT - len(out.get("systemMessage", ""))
    if len(ctx) > room:
        ctx = ctx[: room - 80] + "\n[nowitknows: context truncated; consolidate the memory stores]"
    return {**out, "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}


def _fit_index(store: TopicStore, room: int, cfg: C.Config | None = None) -> str:
    """Mask before truncation so a size limit cannot hide part of a secret from screening."""
    original = store.index_text(bounded=False)
    clean = redact(original)
    masked = clean != original
    text = "\n".join(safety_lines(clean.removesuffix("\n"))[: C.INDEX_MAX_LINES])
    text = text.encode()[: C.INDEX_MAX_BYTES].decode(errors="ignore")
    found = safety_lines(text) if text else []
    # Values are already masked. The legacy credential scan can retry every suffix of a long keyword run;
    # keep all the other checks, including the same instruction and hidden-character screening, per line.
    checks = [rx for label, rx in _PATTERNS if label != "a secret-shaped value"]
    lines = [line for line in found if not any(rx.search(line) for rx in checks)]
    if not found:
        return "(no project memories yet)"
    withheld = f"({len(found) - len(lines)} index lines withheld: they look like secrets or instructions)"
    room -= (len(withheld) + 1) if len(lines) < len(found) else 0
    room -= len(C.DELIVERY_MASK_NOTE) + 1 if masked else 0
    kept, used = [], 0
    pointer = memory_read_pointer(host=cfg.host if cfg else None) if masked else f"Read {store.index}"
    more = len(f"({len(lines)} more: {pointer})") + 1  # the pointer to the rest counts against the room
    for line in lines:
        if used + len(line) + 1 + more > room:
            kept.append(f"({len(lines) - len(kept)} more: {pointer})")
            break
        kept.append(line)
        used += len(line) + 1
    return finish_delivery("\n".join([*kept, *([withheld] if len(lines) < len(found) else [])]), masked, cfg)


def user_prompt_submit(cfg: C.Config, data: dict) -> dict:
    save_options(cfg)  # a /plugin configure mid-session reaches commands at the next prompt
    project = _project(cfg, data)
    st = State(cfg.path("state.json"), cfg.host, cfg.legacy_host)
    dirs = [cfg.skills_home, project / ".claude" / "skills"]
    prompt = data.get("prompt", "")
    if not from_user(data):  # no routing, and no turn toward a memory review
        if cfg.remote:
            return {}
        with st.edit(hook=True) as d:
            s = session(d, data.get("session_id", "unknown"))
            s["turns"] += 1  # what the model does about the notice is activity of its own turn
            s["typed"] = None
        return _with_notices(cfg, {})[0]
    typed = SLASH.match(prompt)
    name = typed.group(1) if typed else ""
    usage: dict = {}
    sid, turn, events, loaded, owed, typed_source = data.get("session_id", "unknown"), 0, [], set(), (), None
    if not cfg.remote:
        with st.edit(hook=True) as d:
            s = session(d, sid)
            pending = s.get("setup_owed")  # the kinds of setup notices its start showed, told in fixed words
            if _attended(cfg):
                pending = [*(pending if isinstance(pending, list) else [])]
                pending += [kind for kind in _shell_owed(cfg, sid) if kind not in pending]
                s["setup_owed"] = pending
            owed = tuple(notify.told({"tell": {"kind": k}}) for k in pending) if isinstance(pending, list) else ()
            s["turns"] += 1
            s["turns_since_memory"] += 1
            s["typed"] = None
            turn = s["turns"]
            path = next(
                (
                    root / name / "SKILL.md"
                    for root in dirs
                    if C.SKILL_NAME.fullmatch(name) and (root / name / "SKILL.md").is_file()
                ),
                None,
            )
            if path:
                source = typed_source = router.trigger_source(path, cfg)
                _loaded(d, s, name, source)  # typed skills bypass the Skill tool
                s["typed"] = name
                repo = native.project_key(project)  # where it was loaded, for the scope backfill
                events.append({"kind": "load", "skill": name, "typed": True, "skill_source": source, "repo": repo})
            usage, loaded = dict(d["usage"]), set(s.get("skills") or [])
            modes = native.recorded(cfg, s, project)
    chain = native.settings_chain(project, cfg.claude_home)
    cache = cfg.data / "router" / (hashlib.sha256("|".join(map(str, dirs)).encode()).hexdigest()[:16] + ".pkl")
    idx = router.skill_index(dirs, cfg.claude_home, chain, cfg.data / "skill_triggers.json", cache, usage, cfg=cfg)
    if name:
        idx.exclude.add(name)  # a typed skill is already in the prompt; suggesting it loads it twice
    routed = [doc for doc, _ in router.route_request(idx, prompt, cfg.skill_threshold)]
    routed, skipped = scope.split(cfg, routed, project)  # learned project skills route only in their repos
    blocked = set()
    for doc in routed:
        visible = cfg.skills_home / doc.name
        if not cfg.remote and Path(doc.path).resolve() == (cfg.skills_library / doc.name / "SKILL.md").resolve():
            try:
                working = (visible / "SKILL.md").is_file() and visible.resolve() == (
                    cfg.skills_library / doc.name
                ).resolve()
            except (OSError, RuntimeError):
                working = False
            if not working:
                blocked.add(doc.name)
    lines = [
        f"- {doc.name}: {doc.description[: C.NOTE_DESCRIPTION_CHARS]}"
        + (
            f"; load Nik's copy with {C.session_tools()}skill_view(name={json.dumps(doc.name)})"
            if doc.name in blocked
            else ""
        )
        for doc in routed
    ]
    if routed:
        events.append({"kind": "suggest", "skills": [doc.name for doc in routed]})
    events += skipped
    request = router.request_text(prompt)
    recall_hint = (
        not cfg.remote and len(request) <= C.RECALL_REQUEST_MAX_CHARS and C.RECALL_REQUEST.search(request) is not None
    )
    if not cfg.remote:
        if recall_hint:
            events.append({"kind": "recall_hint"})
        log_activity(cfg, sid, turn, events)
        scope.widen(cfg, typed_source, project)  # a typed skill is loaded here, so it routes here from now on
    if lines:
        new = [doc.name for doc in routed if doc.name not in loaded] if _attended(cfg) and not cfg.remote else []
        header = router_header(new)
        if blocked:
            header = header.replace("with the Skill tool", "with the indicated tool (otherwise the Skill tool)")
        lines = [header, *lines]
    if recall_hint:
        lines.append(
            "nowitknows: this may be in a past session; search it with "
            f"{C.session_tools()}session_search (keywords plus likely synonyms) before asking the user."
        )
    if not cfg.remote and modes.project_owner == "nowitknows":
        mem = router.route_request(
            router.topic_index(TopicStore(modes.fallback_dir)), prompt, cfg.router_threshold, k=3
        )
        found, owns = [], ownership(cfg.data, modes.shared)
        for d, _ in mem:
            with contextlib.suppress(OSError):
                raw = Path(d.path).read_bytes()  # a note nowitknows does not own is described, as on Codex
                line = d.path if owns(Path(d.path), raw) else note_pointer(Path(d.path), raw, cfg)
                found += [f"- {line}"] if line else []
        if found:
            lines += ["nowitknows: project memory files likely relevant (Read them):", *found]
    out: dict = {}
    if lines:
        out["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "additionalContext": "\n".join(lines)}
    if cfg.remote:
        return out
    from .setup import peer_offers, peers_told

    offers = peer_offers(cfg) if _attended(cfg) else {}
    out, said = _with_notices(cfg, out, "UserPromptSubmit", project=modes.key or None, owed=(*owed, *offers.values()))
    peers_told(cfg, offers, (out.get("hookSpecificOutput") or {}).get("additionalContext", ""))
    if said and owed:
        with st.edit(hook=True) as d:
            session(d, sid).pop("setup_owed", None)
        if isinstance(pending, list) and "codex-later" in pending:
            codex_told(cfg)
    return out


def _json_in(resp) -> dict:
    """The tool's JSON result from any MCP response shape: dict, structuredContent, content blocks, or text."""
    if isinstance(resp, dict):
        if "ok" in resp:
            return resp
        if isinstance(resp.get("structuredContent"), dict):
            return _json_in(resp["structuredContent"])
        return _json_in(resp.get("content") or resp.get("result") or "")
    if isinstance(resp, list):
        for block in resp:
            found = _json_in(block.get("text", "") if isinstance(block, dict) else block)
            if found:
                return found
        return {}
    text = str(resp or "")
    start, end = text.find("{"), text.rfind("}")
    try:
        found = json.loads(text[start : end + 1]) if start >= 0 else {}
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _skill_ran(call: dict) -> bool:
    """Claude Code's launch text, or any other answer with no sign of a refusal (unknown wording counts as a load,
    which can only keep a skill longer)."""
    resp = call.get("tool_response")
    if isinstance(resp, dict):
        return not (resp.get("is_error") or resp.get("error"))
    text = resp if isinstance(resp, str) else json.dumps(resp)
    return text.lstrip().startswith(LAUNCHED) or not any(r in text for r in REFUSED)


def _edit_refused(call: dict) -> bool:
    """Edits that ran answer with a structured result; refusals and failures answer with text."""
    resp = call.get("tool_response")
    return isinstance(resp, str) and any(r in resp for r in REFUSED)


def _searched(call: dict) -> bool:
    """A session_search that ran answers with results or exchanges; a denied, rejected, or failed call with text."""
    res = _json_in(call.get("tool_response"))
    return not _edit_refused(call) and (isinstance(res.get("results"), list) or isinstance(res.get("exchanges"), list))


def _skill_of(cfg: C.Config, file_path: str | None) -> str | None:
    """The user skill a file belongs to, or None."""
    try:
        path = Path(file_path or "").resolve()
        root = cfg.skills_library if path.is_relative_to(cfg.skills_library.resolve()) else cfg.skills_home
        rel = path.relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return rel.parts[0] if len(rel.parts) > 1 else None


def _loaded(d: dict, s: dict, skill: str, source: dict | None = None) -> None:
    d["usage"][skill] = d["usage"].get(skill, 0) + 1
    d["usage_last"][skill] = time.time()
    s["skills"] = [*(x for x in s["skills"] if x != skill), skill][-C.SESSION_SKILLS :]  # distinct, most recent last
    sources = s.setdefault("skill_sources", {})
    if source and source not in sources.setdefault(skill, []):
        sources[skill].append(source)
    s["skill_sources"] = {name: values for name, values in sources.items() if name in s["skills"]}
    touch_skill(d, skill, s.get("project"))


def post_tool_batch(cfg: C.Config, data: dict) -> dict:
    if cfg.remote:
        return {}
    calls = data.get("tool_calls") or []
    notices, events, loads, made = [], [], [], []
    with State(cfg.path("state.json"), cfg.host, cfg.legacy_host).edit() as d:
        s = session(d, data.get("session_id", "unknown"))
        turn = s["turns"]
        wrote_skill = False
        for call in calls:
            name = call.get("tool_name", "")
            if name == "Skill":
                skill = (call.get("tool_input") or {}).get("skill")
                again = bool(skill) and skill == s.get("typed")
                if again:
                    s["typed"] = None  # the model re-invoked the skill the user typed: Claude Code says it is loaded
                if skill and _skill_ran(call):
                    project = _project(cfg, data)
                    files = router.skill_files(
                        [cfg.skills_home, project / ".claude/skills"],
                        cfg.claude_home,
                        native.settings_chain(project, cfg.claude_home),
                    )
                    source = next(
                        (router.trigger_source(p, cfg) for p, prefix in files if prefix + p.parent.name == skill), None
                    )
                    loads.append(source)  # a re-call too: the turn may have moved to another repo
                    repo = native.project_key(project)  # where it was loaded, for the scope backfill
                    if not again:
                        _loaded(d, s, skill, source)
                        events.append({"kind": "load", "skill": skill, "skill_source": source, "repo": repo})
                    else:  # counted when typed; its repo is kept for the scope backfill
                        events.append({"kind": "scope_load", "skill": skill, "skill_source": source, "repo": repo})
            elif name in (C.session_tools() + "skill_view", C.CHILD_TOOLS + "skill_view"):
                args = call.get("tool_input") or {}
                skill = args.get("name")
                res = _json_in(call.get("tool_response"))
                if res.get("ok") and skill and not norm_file(args.get("file_path")):
                    path = SkillStore(cfg).file(skill)
                    if path is not None:
                        source = router.trigger_source(path, cfg)
                        _loaded(d, s, skill, source)
                        loads.append(source)
                        events.append(
                            {
                                "kind": "load",
                                "skill": skill,
                                "skill_source": source,
                                "repo": native.project_key(_project(cfg, data)),
                            }
                        )
            elif name in EDITS and (edited := _skill_of(cfg, (call.get("tool_input") or {}).get("file_path"))):
                if not _edit_refused(call):
                    touch_skill(d, edited, s.get("project"))  # Claude edited a skill file directly
            elif name in (C.session_tools() + "skill_manage", C.CHILD_TOOLS + "skill_manage"):
                res = _json_in(call.get("tool_response"))
                if res.get("ok") and res.get("path"):
                    wrote_skill = True
                    if res.get("action") == "archive":
                        forget_skill(d, res.get("name"))
                    else:
                        touch_skill(d, res.get("name"), s.get("project"))
                    if res.get("action") == "create" and isinstance(res["path"], str):  # pinned below
                        made.append((Path(res["path"]), s.get("project"), s.get("start")))
                    notices.append(notify.skill_line(res.get("action", ""), res.get("name", ""), res["path"]))
            elif name in (C.session_tools() + "memory", C.CHILD_TOOLS + "memory"):
                res = _json_in(call.get("tool_response"))
                done = [r for r in res.get("results") or [] if r.get("ok") and r.get("path")]
                if res.get("changes") and res.get("path"):
                    notices.append(notify.memory_line({"changed": res["changes"]}, res["path"]))
                elif done:
                    notices.append(notify.memory_line({"changed": len(done)}, done[-1]["path"]))
        if wrote_skill:
            s["iters_since_skill"] = 0
        elif not data.get("agent_id"):  # main-loop iterations only: the digest omits subagent work
            s["iters_since_skill"] += 1
    searches = sum(c.get("tool_name") == C.session_tools() + "session_search" and _searched(c) for c in calls)
    log_activity(cfg, data.get("session_id", "unknown"), turn, events + [{"kind": "search"}] * searches)
    for source in loads:  # a skill loaded here routes here from now on
        scope.widen(cfg, source, _project(cfg, data))
    for path, project, start in made:
        scope.pin(cfg, path, project, start)
    return {"systemMessage": "\n".join(notices)} if notices else {}


def pre_tool_use(cfg: C.Config, data: dict) -> dict:
    if cfg.remote:
        return {}
    s = State(cfg.path("state.json"), cfg.host, cfg.legacy_host).read()["sessions"].get(data.get("session_id", ""), {})
    tool, project = data.get("tool_name", ""), _project(cfg, data)
    checked_in = None
    if tool in native.GATED_TOOLS:
        checked_in = native.checked_in_folder(cfg.env, native.settings_chain(project, cfg.claude_home))
    reason = native.gate(tool, data.get("tool_input") or {}, native.recorded(cfg, s, project), checked_in)
    if not reason:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _learns(cfg: C.Config) -> bool:
    """Background learning spends the user's plan, so it follows the sessions someone works in, not scripted runs.
    The review option itself is the worker's to apply."""
    return cfg.review_unattended or _attended(cfg)


def _in_session(cfg: C.Config, data: dict) -> bool:
    s = State(cfg.path("state.json"), cfg.host, cfg.legacy_host).read()["sessions"].get(data.get("session_id", ""), {})
    return bool(s.get("in_session"))


def _due(cfg: C.Config, data: dict) -> bool:
    st = State(cfg.path("state.json"), cfg.host, cfg.legacy_host).read()
    s = st["sessions"].get(data.get("session_id", ""), {})
    if not cfg.review or time.time() < st["worker"].get("backoff_until", 0):
        return False
    # a review sees a slice worth its fixed cost; a check that found too little, or no budget, also waits the gap
    if time.time() - max(s.get("last_review", 0), s.get("last_check", 0)) < cfg.review_min_gap_s:
        return False
    k = review_factor(st["worker"])
    memory_ok = not (s.get("modes") or {}).get("hosted", False)
    return (memory_ok and s.get("turns_since_memory", 0) >= cfg.memory_interval * k) or s.get(
        "iters_since_skill", 0
    ) >= cfg.skill_interval * k


def stop(cfg: C.Config, data: dict) -> dict:
    if cfg.remote or data.get("stop_hook_active"):
        return {}
    save_options(cfg)
    if _learns(cfg) and not _in_session(cfg, data) and _due(cfg, data):  # _due also checks the review option
        spawn(cfg, worker_args(data, "stop"))
    return _with_notices(cfg, {})[0]


def pre_compact(cfg: C.Config, data: dict) -> dict:
    if not cfg.remote and cfg.review and _learns(cfg):
        if _in_session(cfg, data):
            with State(cfg.path("state.json"), cfg.host, cfg.legacy_host).edit() as state:
                session(state, data.get("session_id", "unknown"))["mod_precompact"] = time.time()
        else:
            spawn(cfg, worker_args(data, "precompact"))
    return {}


def session_end(cfg: C.Config, data: dict) -> dict:
    if not cfg.remote:  # a scripted session is still archived and indexed for search
        spawn(cfg, worker_args(data, "end", learn=int(_learns(cfg))))
    return {}


def instructions_loaded(cfg: C.Config, data: dict) -> dict:
    if cfg.remote or not data.get("file_path"):
        return {}
    with State(cfg.path("state.json"), cfg.host, cfg.legacy_host).edit() as d:
        s = session(d, data.get("session_id", "unknown"))
        if data["file_path"] not in s["instructions"]:
            s["instructions"] = (s["instructions"] + [data["file_path"]])[-30:]
    return {}


HANDLERS = {
    "SessionStart": session_start,
    "UserPromptSubmit": user_prompt_submit,
    "PostToolBatch": post_tool_batch,
    "PreToolUse": pre_tool_use,
    "Stop": stop,
    "PreCompact": pre_compact,
    "SessionEnd": session_end,
    "InstructionsLoaded": instructions_loaded,
}


def handle(event: str, data: dict, cfg: C.Config | None = None) -> dict:
    cfg = cfg or C.get()
    if cfg.child or event not in HANDLERS:
        return {}
    return HANDLERS[event](cfg, data) or {}
