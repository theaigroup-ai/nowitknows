from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from . import config as C
from .safety import redact, scan
from .state import atomic_write, durable_write, file_lock, log_row

PREFIX = "NowItKnows ·"
TELL = "nowitknows notices; in this reply, tell the user in one short sentence:"
ACCOUNT = (
    f"{PREFIX} background learning reached today's",
    f"{PREFIX} background learning paused:",
    f"{PREFIX} Codex learning paused:",
)
# A notice shown to the user but not yet told to the model is kept as an account notice of this host, which no build
# runs as, so builds before 0.9.0 sharing the queue neither show it again nor drop it; `for` names who may tell it.
OWED = "model"
SKILL_VERBS = {
    "create": "created",
    "patch": "updated",
    "edit": "updated",
    "write_file": "updated",
    "archive": "archived",
}
SKILL_POINTER = "; /nowitknows:changes lists it"  # ends a told skill line; Codex's hook renames the command
CODEX_CHANGES = "$nowitknows-codex:nowitknows-changes"  # that name in Codex, the longer of the two
MEMORY_VERBS = {"added": "saved", "updated": "updated", "changed": "changed", "removed": "removed"}
NOTE_FILE = re.compile(r"[A-Za-z0-9_-]{1,64}\.md")
WHERE = re.compile(r"[\w.-]{1,40}")
# What the model is told for a notice of a fixed kind, whatever its line says: never a reason, an error or a path;
# `{where}` takes the notice's project folder
FIXED = {
    "repo-notes": "nowitknows found this repository's project notes at another path; "
    "its notice gives the folders and manual copy steps",
    "skill-backup-failed": "nowitknows could not back up your learned skills; learning continues and "
    "/nowitknows:status says why",
    "budget": "nowitknows's background learning reached today's token limit and resumes tomorrow",
    "paused": "nowitknows's background learning is paused; /nowitknows:status says why",
    "cursor-paused": "nowitknows's Cursor learning is paused; /nowitknows:status says why",
    "codex-paused": "nowitknows's Codex learning is paused; /nowitknows:status says why",
    "unshared": "nowitknows has older project notes{where} outside the shared notes folder; /nowitknows:setup previews "
    "a move",
    "uv-path": "nowitknows's uv is not on Claude Code's PATH; nowitknows's setup notice names the folder to add",
    "uv-shim": "nowitknows found uv only as a pyenv shim here, so its tools may not start; a standalone uv fixes it",
    "setup-running": "nowitknows is installing what its tools need in the background; they start once it finishes",
    "setup-done": "nowitknows has finished setting up; reconnecting nowitknows in /mcp starts its tools",
    "setup-failed": "nowitknows could not install what its tools need; its setup notice says how to finish",
    "uv-missing": "nowitknows needs uv, and none was found; its setup notice gives the command that installs it",
    "uv-old": "nowitknows needs a newer uv to install Python 3.14; its setup notice says how to update it",
    "start-failed": "nowitknows could not start; /nowitknows:status shows the error",
    "cursor-later": "nowitknows isn't set up in Cursor; run /nowitknows:setup --cursor to add it",
    "codex-later": "Codex is installed but nowitknows isn't set up there; run /nowitknows:setup to add it",
    # a Codex install from the repo's catalog: the server that waited may or may not have started this session
    "codex-setup-done": "nowitknows has finished setting up in Codex; if its tools are not running in this session, "
    "they start in the next one",
    # told in Codex; the user's yes runs it. `;`: a marketplace already registered fails the add, not the install
    "claude-later": f"Claude Code lacks NowItKnows; to add it, run: {C.CLAUDE_INSTALL}",
}
# Older builds queued uv-path and uv-shim text, so keep those spellings.
# First match wins, so uv-old precedes needs Python.
SETUP = (
    ("Codex is installed but nowitknows isn't set up there", "codex-later"),
    ("is installed but needs uv", "uv-missing"),
    ("needs Python 3.14 or later, which", "uv-old"),
    ("could not start", "start-failed"),
    ("could not set up", "setup-failed"),
    ("has just set up", "setup-done"),
    ("finished setting up", "setup-done"),
    ("is using ", "uv-path"),
    ("uv starts only from nowitknows's folder", "uv-shim"),
    ("could not install", "setup-failed"),
    ("needs Python", "setup-failed"),
    ("'s background setup stopped twice", "setup-failed"),
    ("has just installed", "setup-done"),
    ("is installing", "setup-running"),
    ("is still installing", "setup-running"),
    ("is setting up", "setup-running"),
    ("is still setting up", "setup-running"),
    ("'s last setup stopped", "setup-running"),
)


def link(path: str | Path) -> str:
    try:
        return Path(path).resolve().as_uri()
    except ValueError:
        return str(path)


def budget_line(used: int, budget: int) -> str:
    return (
        f"background learning paused until tomorrow: today's {used:,} of {budget:,} token budget is used; "
        "NOWITKNOWS_REVIEW_DAILY_TOKENS changes it, 0 removes it"
    )


def skill_line(action: str, name: str, path: str, verdict: str | None = None, *, base: str | None = None) -> str:
    verb = {
        "create": "created skill",
        "patch": "updated skill",
        "edit": "rewrote skill",
        "write_file": "updated a file in skill" if base is not None else "added a file to skill",
        "archive": "archived skill",
    }.get(action, f"{action} skill")
    judged = f" (judge: {verdict})" if verdict else ""
    return f"{PREFIX} {verb} {name}{judged} {link(path)}"


def rejected_line(name: str, n_findings: int, path: Path) -> str:
    return f"{PREFIX} dropped a proposed change to skill {name} ({n_findings} blocking findings) {link(path)}"


def oversized_line(name: str, path: Path) -> str:
    return (
        f"{PREFIX} refused a proposed change to skill {name}: SKILL.md would pass its size ceiling; kept {link(path)}"
    )


def refused_line(name: str, reason: str, index: Path, limit: str | None = None) -> str:
    if limit is None:
        why = reason
    elif limit == "changing":
        why = "MEMORY.md kept changing while nowitknows edited it"
    else:
        why = f"nowitknows's share of MEMORY.md is full ({limit}); remove or shorten stale project notes to make room"
    return f"{PREFIX} project note {name} not saved: {why} {link(index)}"


def memory_line(counts: dict, path: str) -> str:
    parts = [f"{v} {k}" for k, v in counts.items() if v]
    return f"{PREFIX} memory: {', '.join(parts)} {link(path)}"


def _queue(q: Path) -> dict:
    """The queue, read under its lock. An unreadable one (a crash mid-write) is kept aside as `*.bad`, and a notice
    that says so takes its place."""
    try:
        state = json.loads(q.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError:  # also undecodable bytes: UnicodeDecodeError is a ValueError
        state = None
    if isinstance(state, dict):
        return state
    bad = q.with_name(f"{q.stem}-{uuid.uuid4().hex[:8]}.bad")
    q.replace(bad)
    line = f"{PREFIX} unreadable notices were set aside; /nowitknows:changes lists every change {link(bad)}"
    return {"items": [{"ts": time.time(), "line": line}]}


def push(
    cfg: C.Config,
    line: str,
    key: str | None = None,
    *,
    permanent: bool = False,
    account: bool = False,
    tell: dict | None = None,
    project: tuple[str, str] | None = None,
    day: str | None = None,
    wait_s: float = C.SQLITE_TIMEOUT_S,
) -> bool:
    """Notices retain a fixed model-facing form so user-visible errors and paths cannot become instructions."""
    q = cfg.path("notifications.json")
    if key and account:
        key = f"{cfg.host}:{key}"
    with file_lock(q, wait_s=wait_s):
        state = _queue(q)
        items, seen = state.get("items", []), state.get("seen", {})
        delivered = state.get("permanent", [])
        if key in delivered:
            return False
        now = time.time()
        if key and now - seen.get(key, 0) < C.NOTICE_REPEAT_S:
            return False
        if key:
            seen[key] = now
            if permanent:
                delivered.append(key)
        items.append(
            {
                "ts": now,
                "line": line,
                "host": cfg.host,
                "audience": "account" if account else "shared",
                **({"permanent": key} if permanent else {}),
                **({"tell": tell} if tell else {}),
                **({"project": project[0], "where": project[1]} if project else {}),
                **({"day": day} if day else {}),
            }
        )
        seen = {k: t for k, t in seen.items() if now - t < C.NOTICE_REPEAT_S}
        kept = [item for i, item in enumerate(items) if item.get("permanent") or i >= len(items) - 50]
        (durable_write if permanent else atomic_write)(
            q, json.dumps({"items": kept, "seen": seen, "permanent": delivered})
        )
    return True


def _account(item: dict) -> bool:
    return item.get("audience") == "account" or ("audience" not in item and item["line"].startswith(ACCOUNT))


def _owed(cfg: C.Config, item: dict) -> dict:
    target = item.get("host", cfg.legacy_host) if _account(item) else "*"
    return {**item, "host": OWED, "audience": "account", "for": target}


def skill_form(action: str, name: str) -> dict:
    return {"kind": "skill", "verb": SKILL_VERBS.get(action), "name": name}


def memory_form(counts: dict, path: str | Path) -> dict:
    return {"kind": "memory", "counts": counts, "file": Path(path).name}


def tidy(cfg: C.Config, row: dict) -> None:
    form = {
        "kind": "tidy",
        "target": row["target"],
        "before": len(row["before"].strip()),
        "after": len(row["after"].strip()),
    }
    clause = told({"tell": form})
    if clause:
        push(
            cfg,
            f"{PREFIX} {clause.removeprefix('nowitknows ')} {link(row['path'])}",
            key="memory-tidy:" + row["write_id"],
            permanent=True,
            tell=form,
        )


def setup_kind(line: str) -> str | None:
    rest = line.removeprefix(PREFIX).removeprefix("nowitknows").lstrip()
    return next((kind for opening, kind in SETUP if rest.startswith(opening)), None)


def told(item: dict) -> str | None:
    """The clause the model may be told for a queued notice, built from its kind and checked fields only, never from
    its text; None for a notice without such a form."""
    form = item.get("tell") if isinstance(item.get("tell"), dict) else {"kind": setup_kind(str(item.get("line")))}
    kind, where = form.get("kind"), item.get("where")
    where = where if isinstance(where, str) and WHERE.fullmatch(where) else ""
    work = f" from your work in {where}" if where else ""
    if kind == "version-mismatch":
        if form.get("peer") in ("claude", "codex"):
            peer = form["peer"]
            cursor, version = form.get("cursor"), form.get(peer)
            if all(isinstance(v, str) and C.PLUGIN_VERSION.fullmatch(v) for v in (cursor, version)):
                label = "Claude Code" if peer == "claude" else "Codex"
                return (
                    f"NowItKnows for Cursor is {cursor}, {label}'s is {version}: "
                    "/nowitknows:setup --cursor updates these editions."
                )
            return None
        codex, claude = form.get("codex"), form.get("claude")
        if all(isinstance(v, str) and C.PLUGIN_VERSION.fullmatch(v) for v in (codex, claude)):
            if form.get("host") == "claude":
                return f"NowItKnows for Codex is {codex}, Claude Code's is {claude}: /nowitknows:setup updates both."
            if form.get("host") == "codex":
                return f"NowItKnows for Claude Code is {claude}, Codex's is {codex}: /nowitknows:setup updates both."
    elif kind in ("skill-backup-conflict", "skill-backup-archive"):
        names = form.get("names")
        if isinstance(names, list) and names and all(isinstance(n, str) and C.SKILL_NAME.fullmatch(n) for n in names):
            if kind == "skill-backup-archive":
                return (
                    f"nowitknows skills archived on another machine: {', '.join(names)}; "
                    "use /nowitknows:keep to restore them"
                )
            return (
                f"nowitknows kept both versions of conflicting skills: {', '.join(names)}; "
                "/nowitknows:status shows the backup"
            )
    elif kind == "skill":
        verb, name = form.get("verb"), form.get("name")
        if verb in ("created", "updated", "archived") and isinstance(name, str) and C.SKILL_NAME.fullmatch(name):
            said = f"nowitknows {verb} the skill {name}"
            # The pointer outlives the cap: the project clause goes first, sized for Codex's longer command name
            room = C.NOTICE_TOLD_CHARS - len(SKILL_POINTER) - len(CODEX_CHANGES) + len("/nowitknows:changes")
            return f"{said}{work if len(said) + len(work) <= room else ''}{SKILL_POINTER}"
    elif kind == "tidy":
        target, before, after = form.get("target"), form.get("before"), form.get("after")
        if target in ("user", "memory") and type(before) is int and type(after) is int and 0 < after < before:
            block = "profile" if target == "user" else "global"
            return (
                f"nowitknows tightened your {block} memory ({before} to {after} characters, nothing dropped)"
                f"{SKILL_POINTER}"
            )
    elif kind == "memory":
        counts = form.get("counts") if isinstance(form.get("counts"), dict) else {}
        done = [
            (MEMORY_VERBS[verb], n) for verb, n in counts.items() if verb in MEMORY_VERBS and type(n) is int and n > 0
        ]
        if done:
            parts = [f"{verb} {n}" for verb, n in done]
            parts[0] += " memory note" if done[0][1] == 1 else " memory notes"
            said = parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"
            file = form.get("file")
            said += f" in {file}" if isinstance(file, str) and NOTE_FILE.fullmatch(file) else ""
            return f"nowitknows {said}{work}"
    elif kind == "scoped":  # the scope backfill (scope.py): how many learned skills now route only in their repos
        n = form.get("count")
        if type(n) is int and n > 0:
            skills, own, them = ("skill", "its", "it") if n == 1 else ("skills", "their", "them")
            said = f"nowitknows now suggests {n} learned project {skills} only in {own} own repos"
            return f"{said}; /nowitknows:changes lists {them}"
    elif kind == "budget" and type(form.get("used")) is int and type(form.get("budget")) is int:
        used, budget = form["used"], form["budget"]
        if used >= 0 and budget > 0:
            return budget_line(used, budget)
    elif isinstance(kind, str) and kind in FIXED:
        return FIXED[kind].replace("{where}", f" for {where}" if where else "")
    return None


def screened(line: str) -> str | None:
    """A told line on one line and within NOTICE_TOLD_CHARS, or None when it looks like a secret or an instruction,
    as the session-start index screens its lines."""
    line = " ".join(line.split())
    if redact(line) != line or scan(line):
        return None
    line = line[: C.NOTICE_TOLD_CHARS]
    return line if redact(line) == line and not scan(line) else None


def block(
    lines: list[str | None],
    words: Callable[[str], str] = lambda line: line,
    *,
    texts: tuple[str | None, ...] = (),
) -> str:
    """Tell fixed clauses and, for at most NOTICE_TELL_OTHERS unformed notices, their screened texts. Withheld and
    overflow lines are counted; identical told lines keep only their latest position."""
    forms = [screened(words(line)) if line else None for line in lines]
    few = forms.count(None) <= C.NOTICE_TELL_OTHERS
    said, others = [], 0
    for i, safe in enumerate(forms):
        if lines[i] is None and few and i < len(texts) and texts[i]:
            safe = screened(words(texts[i]))
        if safe:
            said.append(safe)
        else:
            others += 1
    said = list(reversed(dict.fromkeys(reversed(said))))
    if others or len(said) > C.NOTICE_MAX_LINES:
        keep = said[-(C.NOTICE_MAX_LINES - 1) :]
        others += len(said) - len(keep)
        said = [
            *keep,
            words(
                f"nowitknows has {others} other notice{'s' if others != 1 else ''}; "
                "/nowitknows:changes lists the changes"
            ),
        ]
    return "\n".join([TELL, *(f"- {line}" for line in said)])


def _here(item: dict, project: str | None, now: float) -> bool:
    """Whether a notice may be told in a session of `project`: its own project's sessions first, others once it has
    waited NOTICE_PROJECT_WAIT_S."""
    origin = item.get("project")
    return project is None or not origin or origin == project or now - item.get("ts", 0) >= C.NOTICE_PROJECT_WAIT_S


def deliver(
    cfg: C.Config,
    fits: Callable[[str, list[str]], bool] | None = None,
    *,
    wait_s: float = C.SQLITE_TIMEOUT_S,
    words: Callable[[str], str] = lambda line: line,
    project: str | None = None,
    extra: tuple[str | None, ...] = (),
) -> tuple[list[str], str]:
    """A notice can be shown before it reaches the model, so those deliveries are tracked separately."""
    q = cfg.data / "notifications.json"
    if not q.exists():
        said = block(list(extra), words) if fits and extra else ""
        return [], said if said and fits(said, []) else ""
    with file_lock(q, wait_s=wait_s):
        state = _queue(q)
        items, now = state.get("items", []), time.time()
        shown, owed, expired = [], [], set()
        today = time.strftime("%Y-%m-%d")
        for i, item in enumerate(items):
            day = item.get("day")
            if not day and (item.get("tell") or {}).get("kind") == "budget":
                day = time.strftime("%Y-%m-%d", time.localtime(item["ts"]))
            if day and day < today:
                expired.add(i)
                with suppress(OSError, TimeoutError):
                    log_row(
                        cfg,
                        "activity.jsonl",
                        {
                            "ts": now,
                            "kind": "notice_dropped",
                            "reason": "day_passed",
                            "day": day,
                            "notice_host": item.get("for", item.get("host", cfg.legacy_host)),
                        },
                        wait_s=C.ACTIVITY_LOCK_WAIT_S,
                    )
                continue
            if item.get("host") == OWED:
                owed += [i] if item.get("for") in ("*", cfg.host) and _here(item, project, now) else []
            elif not (_account(item) and item.get("host", cfg.legacy_host) != cfg.host):
                shown.append(i)
                owed += [i] if _here(item, project, now) else []
        lines = [items[i]["line"] for i in shown]
        said = ""
        if fits and (owed or extra):
            clauses = [told(items[i]) for i in owed] + list(extra)
            shown_lines = compact(lines)
            said = block(clauses, words, texts=tuple(items[i]["line"] for i in owed))
            if not fits(said, shown_lines):
                said = block(clauses, words)
                said = said if fits(said, shown_lines) else ""
        if not shown and not (said and owed) and not expired:
            return lines, said
        gone = expired | (set(owed) if said else set())
        kept = [_owed(cfg, item) if i in shown else item for i, item in enumerate(items) if i not in gone]
        durable_write(
            q, json.dumps({"items": kept, "seen": state.get("seen", {}), "permanent": state.get("permanent", [])})
        )
    return lines, said


def drain(cfg: C.Config, *, wait_s: float = C.SQLITE_TIMEOUT_S) -> list[str]:
    """The notices to show the user, each once; they stay queued for the model until a prompt tells it."""
    return deliver(cfg, wait_s=wait_s)[0]


def compact(lines: list[str]) -> list[str]:
    if len(lines) <= C.NOTICE_MAX_LINES:
        return lines
    keep = lines[-(C.NOTICE_MAX_LINES - 1) :]
    return [f"{PREFIX} {len(lines) - len(keep)} earlier changes: /nowitknows:changes", *keep]


def osc9(lines: list[str]) -> str:
    text = "; ".join(l.removeprefix(PREFIX).split(" file://")[0].strip() for l in lines)[:200]
    return f"\x1b]9;nowitknows: {text}\x07"
