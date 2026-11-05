from __future__ import annotations

import os
import uuid
from pathlib import Path

try:
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:
    from mcp.server.fastmcp import FastMCP as _Server

from . import config as C
from . import native, notejudge, notify, sessions
from .delivery import finish_delivery, screened_delivery
from .memory import EntryStore, TopicStore, apply_operations, note_refusals
from .safety import redact
from .skills import NOT_OURS, SkillStore, norm_file, sha, size_label

server = _Server("nowitknows")
_viewed: dict[str, tuple[str | None, str | None]] = {}
NOT_KEPT = "not saved: nowitknows keeps no skill or memory changes in a remote (cloud or self-hosted runner) session"


def _annotations(readonly: bool = False) -> dict | None:
    if C.env("NOWITKNOWS_HOST", C.environment()) != "codex":
        return None
    return {"readOnlyHint": readonly, "destructiveHint": False, "idempotentHint": readonly, "openWorldHint": False}


SKILL_RULES = (
    "Only nowitknows's learned skills can change: plugin, project, account-synced and user-authored skills are "
    "read-only, so create a learned skill under another name whose description says when to use it instead. Learned "
    "skills are shared by Claude Code and Codex: write steps as shell commands, file paths and plain instructions, and "
    'name a host-specific tool (a host tool name or any mcp__ id) only under a "Platform notes" heading naming Claude '
    "Code or Codex."
)
MEMORY_RULES = "When a store is full, remove or shorten stale entries in that same call. Declarative facts only."


def _writing(rules: str):
    """Claude session prompts omit write rules, so their server adds those rules to the model's tool descriptions."""
    session = (
        C.env("NOWITKNOWS_HOST", C.environment()) != "codex"
        and C.env("NOWITKNOWS_WRITE_MODE", C.environment()) != "stage"
    )

    def register(fn):
        fn.__doc__ = (
            fn.__doc__.replace("{memory_rules}", C.MEMORY_WRITING_RULES)
            .replace("{memory_entry_chars}", str(C.MEMORY_ENTRY_CHARS))
            .replace("{memory_hook_chars}", str(C.MEMORY_HOOK_CHARS - len(C.MEMORY_NOTE_SUFFIX)))
        )
        if fn.__name__ == "skill_manage":
            fn.__doc__ += (
                "\n    Keep precise procedures; omit stories, incidents, dates, repeated rules, "
                "and explanation beyond one clause of why."
            )
        description = f"{fn.__doc__}\n    {rules}" if session else None
        return server.tool(annotations=_annotations(), description=description)(fn)

    return register


def _cfg() -> C.Config:
    return C.for_command()  # Claude Code gives plugin options to hooks only; the notes home must match theirs


def _start(cfg: C.Config) -> Path:
    """The repo's folder where the session began, whose settings choose the notes folder."""
    return Path(C.env("NOWITKNOWS_START", cfg.env) or C.env("NOWITKNOWS_PROJECT", cfg.env) or os.getcwd())


def _modes() -> native.Modes:
    cfg = _cfg()
    project = Path(C.env("NOWITKNOWS_PROJECT", cfg.env) or os.getcwd())
    if pinned := C.env(
        "NOWITKNOWS_MODES", cfg.env
    ):  # a background run: the worker resolved them under the session's env
        return native.current(cfg, native.Modes.from_json(pinned), _start(cfg))  # sharing and folder as they are now
    return native.detect(cfg, project)


def _targets(modes: native.Modes) -> set[str]:
    explicit = C.env("NOWITKNOWS_MEMORY_TARGETS", _cfg().env)
    if explicit is not None:
        return {t for t in explicit.split(",") if t}
    out = {"user", "memory"} if modes.global_tier else set()
    return out | ({"project"} if modes.project_owner == "nowitknows" else set())


def _unavailable(target: str, targets: set[str], modes: native.Modes) -> str:
    if target not in ("user", "memory", "project"):
        return f"no memory target named {target!r} (targets: user, memory, project)"
    if modes.global_tier if target != "project" else modes.project_owner == "nowitknows":
        open_now = ", ".join(sorted(targets)) or "none"
        return (
            f"target {target!r} is not available in this run (nowitknows owns it, but it is outside this run's "
            f"focus; targets open to this run: {open_now})"
        )
    return f"target {target!r} is not available here (Claude Code's native memory owns it)"


@_writing(MEMORY_RULES)
def memory(target: str, operations: list[dict]) -> dict:
    """Save facts once: user = identity/preferences; memory = environment/conventions across every project;
    project = this repo (when offered). One atomic batch per target; final size capped.
    user/memory: {action: add|replace|remove, content?, old_text?}. New reviewed memory over {memory_entry_chars} chars
    needs hook (one-line situation, <= {memory_hook_chars} chars); NowItKnows adds suffix. source_exchange=N
    selects supporting [exchange N]. Optional as_of and source='host:session uuid' record known provenance.
    project: {action: write, name, description, type: user|feedback|project|reference, body} or {action: remove, name}.
    Read separately with {action: read}: user/memory block, memory named note, project index or named topic.
    Reads mask values and screen instructions.
    {memory_rules}
    Background tidy: {action: tidy, content: full block, notes?: [{content: exact entry, hook}]}, lossless judged."""
    if _cfg().remote:
        return {"ok": False, "error": NOT_KEPT}
    cfg, modes = _cfg(), _modes()
    targets = _targets(modes)
    if target not in targets:
        return {"ok": False, "error": _unavailable(target, targets, modes)}
    if any(op.get("action") == "read" for op in operations):
        if len(operations) != 1:
            return {"ok": False, "error": "read is one operation, separate from writes"}
        if target == "project":
            store = TopicStore(modes.fallback_dir)
            name = operations[0].get("name", "")
            text = store.text(name) if name else store.index_text(bounded=False)
        else:
            entries = EntryStore(cfg.data / "memory", ledger_dir=cfg.data, host=cfg.host, create=False)
            name = operations[0].get("name", "")
            if name and target == "memory":
                from .global_notes import GlobalNotes

                text = GlobalNotes(entries).read(name)
            elif target == "memory":
                text = entries.render(target, bounded=False)
            else:
                text = entries.render(target)
        if text is None:
            return {"ok": False, "error": "memory not found"}
        return {"ok": True, "content": screened_delivery(text, cfg)}
    source = "background" if cfg.child else "session"
    run_id = C.env("NOWITKNOWS_RUN_ID", cfg.env)
    if cfg.child and target == "project" and C.env("NOWITKNOWS_WRITE_MODE", cfg.env) == "stage":
        results = notejudge.stage(cfg, modes, _start(cfg), operations, run_id, uuid.uuid4().hex)
        note = "queued; an adversarial judge reviews each note before it is saved"
        return {"ok": all(r["ok"] for r in results), "results": results, "note": note}
    result = apply_operations(
        cfg, modes.fallback_dir, target, operations, source, run_id, shared=modes.shared, project=modes.key
    )
    if cfg.child and target == "project":  # a session's model sees the error; nobody watches a background run
        note_refusals(cfg, modes.fallback_dir, operations, result, run_id)
    return result


@server.tool(
    annotations=_annotations(True),
    description="Search locally indexed Claude Code and Codex sessions by keywords or session id. Results can include "
    "up to 3 exchanges per conversation per day. A Codex copy of a "
    "Claude Code chat lists as that chat; its result then carries copy_of, the chat's session id."
    if C.env("NOWITKNOWS_HOST", C.environment()) == "codex"
    else None,
)
def session_search(
    query: str = "",
    limit: int = C.SEARCH_RESULTS,
    sort: str = "",
    session_id: str = "",
    around_uuid: str = "",
    this_project_only: bool = False,
) -> dict:
    """Search past Claude Code, Codex and Cursor sessions. Ranking is keyword overlap; use 6-12 keywords, not a
    question: the distinctive nouns plus the tool names, technical terms, and synonyms the earlier session likely used.
    Returns the best-matching exchanges (default 3, max 10; sort 'newest' or 'oldest'), which can include several
    exchanges of one conversation (up to 3 per day), with a snippet and uuid; a
    Codex copy of a Claude Code chat lists as that chat, and a result shown from the copy carries `copy_of`, the
    chat's session id. `session_id` plus optional `around_uuid` returns the exchanges around that point."""
    cfg = _cfg()
    c = sessions.connect(cfg.path("sessions.db"), cfg.legacy_host)
    try:
        if session_id:
            return {"exchanges": sessions.scroll(c, session_id, around_uuid or None)}
        project = None
        if this_project_only:
            # A background server gets the reviewed session's transcript folder, which
            # still names the project after a `cd` in that session moved the hook's cwd.
            folder = C.env("NOWITKNOWS_TRANSCRIPT_FOLDER", cfg.env)
            cwd = C.env("NOWITKNOWS_PROJECT", cfg.env) or os.getcwd()
            project = native.project_key(Path(cwd))
            if folder:
                row = c.execute(
                    "select repo from sessions where project=? and repo is not null limit 1", (folder,)
                ).fetchone()
                project = row[0] if row else folder
        return {"results": sessions.search(c, query, limit, sort or None, project)}
    finally:
        c.close()


@server.tool(annotations=_annotations(True))
def skill_view(name: str, file_path: str = "") -> dict:
    """Read a skill's SKILL.md or a support file. Required before patching or editing that file."""
    store, fp = _store(), norm_file(file_path)
    try:
        return _skill_view(store, name, fp)
    except (OSError, UnicodeError) as error:
        if store.cfg.host != "codex":
            raise
        return {"ok": False, "error": redact(f"cannot read skill: {error}")}


def _skill_view(store: SkillStore, name: str, fp: str) -> dict:
    path, folder = store.file(name, fp), store.resolved_folder(name)
    if store.cfg.host == "codex" and store.cfg.env.get("CODEX_HOME"):
        from .codex_host import skill_file

        if skill := skill_file(store.cfg, _start(store.cfg), name):
            path = store.file(name, fp, folder=skill.parent)
            folder = str(skill.parent.resolve())
    own_path = store.file(name, fp)
    own = path is not None and own_path is not None and path.resolve() == own_path.resolve()
    text = store.read(name, fp) if own else None
    if not own and path and path.is_file():
        text = path.read_text(encoding="utf-8" if fp else "utf-8-sig")
    if text is None:
        error = f"not found: {name}/{fp or 'SKILL.md'} (user skills folder only)"
        return {"ok": False, "error": redact(error) if store.cfg.host == "codex" else error}
    _viewed[f"{name}/{fp}"] = (folder, sha(text))
    shown_path, content = str(path), text
    if store.cfg.host == "codex":
        shown_path, clean = redact(shown_path), redact(text)
        content = finish_delivery(clean, clean != text or shown_path != str(path), store.cfg)
    return {"ok": True, "path": shown_path, "content": content, "size": size_label(text)}


def _store() -> SkillStore:
    cfg = _cfg()
    staged = C.env("NOWITKNOWS_WRITE_MODE", cfg.env) == "stage"
    return SkillStore(cfg, overlay_run=C.env("NOWITKNOWS_RUN_ID", cfg.env) if staged else None)


@_writing(SKILL_RULES)
def skill_manage(
    action: str,
    name: str,
    content: str = "",
    old_string: str = "",
    new_string: str = "",
    file_path: str = "",
    triggers: list[str] | None = None,
    user_requested: bool = False,
) -> dict:
    """Create or improve a class-level skill in the user's skills folder.
    action: create (full SKILL.md with name/description frontmatter) | patch (old_string -> new_string, unique) |
    edit (full replacement) | write_file (references/, templates/ or scripts/) | archive. No delete.
    patch/edit/write_file over an existing file require skill_view first. On create or patch, pass `triggers`:
    8-12 short, varied example user requests that should load this skill. Set user_requested=true when the user
    explicitly asked for this change (recorded as a user change)."""
    cfg, store = _cfg(), _store()
    if cfg.remote:
        return {"ok": False, "error": NOT_KEPT}
    if action in ("patch", "edit") and norm_file(file_path):
        return {"ok": False, "error": "patch and edit change SKILL.md; to replace a support file use write_file"}
    fp = norm_file(file_path) if action == "write_file" else ""
    change = {
        "action": action,
        "name": name,
        "content": content,
        "old_string": old_string,
        "new_string": new_string,
        "file_path": fp,
    }
    key = f"{name}/{fp}"
    existing = store.read(name, fp)
    needs_view = action in ("patch", "edit") or (action == "write_file" and existing is not None)
    if needs_view and _viewed.get(key) != (store.resolved_folder(name), sha(existing)):
        return {
            "ok": False,
            "error": "read-before-write: call skill_view on this file first (not viewed, or changed since)",
        }
    prepared = store.prepare(change)
    staging = C.env("NOWITKNOWS_WRITE_MODE", cfg.env) == "stage"
    if not prepared.get("ok"):
        if staging and f"SKILL.md over {C.SKILL_BODY_MAX} chars" in prepared.get("errors", []):
            record = store.keep_oversized(change, C.env("NOWITKNOWS_RUN_ID", cfg.env))
            notify.push(cfg, notify.oversized_line(name, record))
        return prepared
    if staging and action == "archive" and name not in store.owned():
        return {"ok": False, "error": NOT_OURS}
    if staging:
        meta = {"run_id": C.env("NOWITKNOWS_RUN_ID", cfg.env), "source": "background", "triggers": triggers or []}
        origin = {
            "project": C.env("NOWITKNOWS_PROJECT_KEY", cfg.env),
            "where": C.env("NOWITKNOWS_PROJECT_NAME", cfg.env, ""),
        }
        cid = store.stage(prepared, {**meta, **origin})
        if action != "archive":
            _viewed[key] = (store.resolved_folder(name), sha(prepared["content"]))
        return {
            "ok": True,
            "staged": cid,
            "note": "queued; an adversarial judge reviews it before it applies",
            "warnings": prepared.get("warnings", []),
        }
    if cfg.host == "claude" and action == "create":  # the server's folder; the post-tool hook pins the session's
        try:
            folder = Path(C.env("NOWITKNOWS_PROJECT", cfg.env) or os.getcwd())
            prepared = {**prepared, "project": native.project_key(folder), "where": native.project_name(folder)}
        except (OSError, RuntimeError, ValueError):  # its folder is gone
            pass
    res = store.apply(prepared, source="user" if user_requested else "session", triggers=triggers)
    if res.get("ok") and action != "archive":
        _viewed[key] = (store.resolved_folder(name), sha(store.read(name, fp)))
    return res


def main() -> None:
    server.run()
