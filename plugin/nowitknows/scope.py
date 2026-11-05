"""Repo-specific procedures must not route into unrelated projects."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
import uuid
from pathlib import Path

from . import config as C
from . import identity, native, notify
from .router import frontmatter, trigger_key, trigger_source
from .state import append_line, atomic_write, durable_write, file_lock, jsonl_rows

PROJECT, GENERAL, UNKNOWN = "project", "general", "unknown"  # unknown: a repo it was learned or loaded in is not known
SEEN = "activity"  # the scope file's record of how far the backfill has read each activity log
CODE = re.compile(r"```.*?```|`[^`\n]+`", re.S)  # fences and code spans, where a skill writes commands and paths
LOCAL = (
    re.compile(r"(?<![\w./~$-])\./[\w.-]+"),  # ./ names the repo root, not the skill's own support files
    re.compile(r"(?<![\w-])make\s+[a-z][\w-]*"),  # make check
    re.compile(r"(?<![\w./~$:-])\.venv/bin/\w+"),  # the repo's own environment
    re.compile(  # a file in a folder, not one of the skill's own support folders and not a URL
        r"(?<![\w./~$:@-])(?!references/|templates/|scripts/|assets/|https?:)[A-Za-z_][\w.-]*/[\w./-]*"
        r"\.(?:py|sh|md|json|toml|ts|js|yaml|yml|txt|cfg|ini)\b"
    ),
)


def project_mark(text: str, repo: str = "") -> bool:
    """Rule (b2): a code span in the body runs a repo-local command (`./x`, `make x`, `.venv/bin/x`), names a
    `dir/file.ext` path, or names `repo` (the folder of the repo it was learned in) as a slug or path segment."""
    spans = [m.group(0) for m in CODE.finditer(frontmatter(text)[1])]
    if any(rx.search(span) for span in spans for rx in LOCAL):
        return True
    words = re.findall(r"[a-z0-9]+", (repo or "").lower())
    if len("".join(words)) < C.SCOPE_REPO_NAME_MIN or "".join(words) in C.SCOPE_GENERIC_REPO_NAMES:
        return False
    body = r"[^a-z0-9]?".join(map(re.escape, words))  # its words, with or without a separator between them
    named = re.compile(rf"/{body}(?![a-z0-9])|(?<![a-z0-9]){body}/")  # whole, next to a slash: owner/repo, ~/x/repo
    return any(named.search(span.lower()) for span in spans)


def mark(text: str, repo: str = "") -> str:
    return PROJECT if project_mark(text, repo) else GENERAL


def load(cfg: C.Config) -> dict:
    """Scope rows by trigger key; {} without a readable file, when every skill routes everywhere."""
    try:
        path = cfg.data / C.SKILL_SCOPE_FILE
        if path.stat().st_size > C.SCOPE_READ_MAX_BYTES:
            return {}
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return rows if isinstance(rows, dict) else {}


def _row(rows: dict, source: dict | None, *, archived: bool = False) -> dict | None:
    """The well-formed row pinned to `source` (a project row names its origin among its repos); an archived skill's
    row, which scopes nothing, only when `archived`."""
    row = rows.get(trigger_key(source)) if isinstance(source, dict) else None
    if (
        not isinstance(row, dict)
        or row.get("source") != source
        or ("archived" in row and not archived)
        or row.get("scope") not in (PROJECT, GENERAL, UNKNOWN)
        or not isinstance(row.get("repos"), dict)
        or (row["scope"] == PROJECT and not (isinstance(row.get("origin"), str) and row["origin"] in row["repos"]))
    ):
        return None
    return row


@contextlib.contextmanager
def _edit(cfg: C.Config, wait_s: float = C.SCOPE_WRITE_WAIT_S, *, durable: bool = False):
    """The rows under the file's lock, written back when changed. An unparsable file is set aside as `*.bad`, never
    lost; the backfill derives its skills' rows again."""
    path = cfg.path(C.SKILL_SCOPE_FILE)
    with file_lock(path, wait_s=wait_s):
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            rows = {}
        except ValueError:
            rows = None
        if not isinstance(rows, dict):
            path.replace(path.with_name(f"{path.stem}-{uuid.uuid4().hex[:8]}.bad"))
            rows = {}
        before = json.dumps(rows, sort_keys=True)
        yield rows
        if json.dumps(rows, sort_keys=True) != before:
            (durable_write if durable else atomic_write)(path, json.dumps(rows, indent=1))


def _log(cfg: C.Config, message: str) -> None:
    with contextlib.suppress(OSError), open(cfg.path("errors.log"), "a", encoding="utf-8") as f:
        f.write(f"{time.ctime()} scope: {message}\n")


def split(cfg: C.Config, routed: list, folder: Path) -> tuple[list, list[dict]]:
    """`routed` (docs, or (doc, score) pairs) without the learned project skills whose repos leave out the prompt's
    repo, and a `scope_skip` activity row for each; nothing fills a left-out slot."""
    if not routed or not cfg.skill_scope:
        return routed, []
    rows = load(cfg)
    if not rows:
        return routed, []
    kept, skipped, repo = [], [], None
    known, value = {}, None
    for item in routed:
        doc = item[0] if isinstance(item, tuple) else item
        try:
            source = trigger_source(Path(doc.path), cfg)
        except (OSError, RuntimeError, ValueError):
            source = None
        row = _row(rows, source)
        if row is not None and row["scope"] == PROJECT:
            if repo is None:
                try:
                    repo = native.project_key(folder)
                except (OSError, RuntimeError, ValueError):  # no repo to tell: suggested as before
                    return routed, []
                try:
                    value = identity.cached(cfg, repo)
                    known = identity.history(cfg) if value is not None else {}
                except (OSError, RuntimeError, ValueError):  # unreadable identity: scope by path alone, as before
                    value, known = None, {}
            if repo not in row["repos"] and not identity.matches(row, value, known):
                skipped.append({"kind": "scope_skip", "skill": doc.name, "skill_source": source})
                continue
        kept.append(item)
    return kept, skipped


def widen(cfg: C.Config, source: dict | None, folder: Path) -> None:
    """An explicit load (a Skill tool call that ran, a typed /name, a Codex skill_view) adds the repo it ran in to the
    learned skill's repos; a busy file skips the add, which the activity log keeps for the next backfill."""
    if not isinstance(source, dict):
        return
    try:
        row = _row(load(cfg), source)
        if row is None:
            return  # a skill with no row yet gets this load's repo from the backfill that writes its row
        repo = native.project_key(folder)
        if repo in row["repos"]:
            return
        name = native.project_name(folder)
        with _edit(cfg, C.SCOPE_LOCK_WAIT_S) as rows:
            row = _row(rows, source)
            if row is not None:
                row["repos"].setdefault(repo, name)
                row["ts"] = round(time.time(), 3)
    except (OSError, RuntimeError, ValueError, TimeoutError) as error:
        _log(cfg, f"a repo was not added to a skill's scope: {error}")


def on_apply(cfg: C.Config, action: str, content: str | None, source: dict, project, where) -> dict:
    """The ledger fields of an applied SKILL.md change: a create's repo key (`project`) and mark (`scope`); a patch or
    edit of a skill with a scope row its mark again, on the new text with the name of the repo it was learned in."""
    if action == "create":
        if not (isinstance(project, str) and project):
            return {"project": None, "scope": None}
        return {
            "project": project,
            "scope": mark(content or "", where if isinstance(where, str) else ""),
        }
    if action not in ("patch", "edit"):
        return {}
    row = _row(load(cfg), source)
    if row is None:
        return {}
    if row["scope"] == UNKNOWN or not isinstance(row.get("origin"), str):
        return {"scope": UNKNOWN}
    if row["scope"] == PROJECT and re.search(r"\breferences/[\w.\-/]+", content or ""):
        return {"scope": PROJECT}
    origin = row["repos"].get(row["origin"])
    return {"scope": mark(content or "", origin if isinstance(origin, str) else "")}


def applied(cfg: C.Config, entry: dict, source: dict, where: str = "", ledger: list[dict] | tuple = ()) -> None:
    """Write what `on_apply` decided once the change is on disk, as recovery replays it: a create starts its row
    afresh; a patch or edit marks a row current with `ledger` (before it) again and makes one behind it `unknown`."""
    value, name, ts = entry.get("scope"), entry.get("name"), entry.get("ts")
    create = entry.get("action") == "create" and "project" in entry
    if not create and value not in (PROJECT, GENERAL, UNKNOWN):
        return  # a support file, or a skill with no row: nothing to record
    try:
        with _edit(cfg, durable=True) as rows:
            row = _row(rows, source)
            if create:
                origin = entry["project"] if isinstance(entry["project"], str) and entry["project"] else None
                replay = row is not None and row.get("origin") == origin and _fresh(row) == ts
                kept = row["repos"] if replay else {}
                rows[trigger_key(source)] = {
                    "name": name,
                    "source": source,
                    "scope": value if origin and value in (PROJECT, GENERAL) else UNKNOWN,
                    "origin": origin,
                    "repos": {**kept, origin: where or kept.get(origin) or ""} if origin else kept,
                    "ts": ts,
                    "fact": ts,
                }
            elif value in (PROJECT, GENERAL, UNKNOWN) and row is not None:
                if _fresh(row) >= _newest(list(ledger), name, ts):
                    row["scope"], row["ts"], row["fact"] = value, ts, ts
                else:  # it missed an earlier change: unscoped, not silenced, until it is rebuilt
                    row["scope"], row["ts"] = UNKNOWN, ts
    except (OSError, ValueError, TimeoutError) as error:
        _log(cfg, f"{name}: scope not recorded: {error}")


def archive(cfg: C.Config, entry: dict) -> None:
    """An archive closes its skill's row: kept, scoping nothing, so a skill written at that path next is not scoped by
    it; a restore's backfill opens it again."""
    try:
        with _edit(cfg, durable=True) as rows:
            row = _row(rows, entry.get("skill_source"))
            if row is not None:
                row["archived"] = entry.get("ts")
    except (OSError, ValueError, TimeoutError) as error:  # the next backfill closes it
        _log(cfg, f"{entry.get('name')}: an archived skill's scope is closed only at the next backfill: {error}")


def pin(cfg: C.Config, path: Path, project, start) -> None:
    """Pin a skill a session's own skill tool created to that session's repo (`project`; `start`, its first folder):
    a ledger row (`scope`, source `session`) and its row's origin, current with the ledger only if it was before."""
    if not (isinstance(project, str) and project):
        return
    try:
        source = trigger_source(path, cfg)
        row = _row(load(cfg), source)
        if (row is not None and row.get("origin") == project) or not path.resolve().is_relative_to(
            cfg.skills_library.resolve()
        ):
            return
        folder = Path(start) if isinstance(start, str) and start else None
        where = native.project_name(folder) if folder and native.project_key(folder) == project else ""
        value, now = mark(path.read_text(encoding="utf-8-sig"), where), round(time.time(), 3)
    except (OSError, RuntimeError, ValueError) as error:
        _log(cfg, f"a created skill's scope was not pinned to its session's repo: {error}")
        return
    name = path.parent.name
    entry = {"ts": now, "action": "scope", "name": name, "source": "session", "host": cfg.host}
    entry.update(path=str(path), skill_source=source, scope=value, project=project)
    ledger, prior = cfg.path("skills_ledger.jsonl"), None
    try:
        with file_lock(ledger, wait_s=C.SCOPE_LOCK_WAIT_S):
            known = _newest(jsonl_rows(ledger), name)
            append_line(ledger, json.dumps(entry))
            prior = known
    except (OSError, TimeoutError) as error:
        _log(cfg, f"a created skill's repo was not ledgered: {error}")
    try:
        with _edit(cfg, C.SCOPE_LOCK_WAIT_S) as rows:
            row = _row(rows, source)
            current = row is not None and prior is not None and _fresh(row) >= prior
            rows[trigger_key(source)] = {
                "name": name,
                "source": source,
                "scope": value,
                "origin": project,
                "repos": {project: where, **(row["repos"] if row is not None else {})},
                "ts": now,
                "fact": now if current else (_fresh(row) if row is not None else 0),
            }
    except (OSError, ValueError, TimeoutError) as error:  # the ledger row brings it at the next backfill
        _log(cfg, f"a created skill's scope was not pinned to its session's repo: {error}")


def _ts(row: dict) -> float:
    ts = row.get("ts")
    return ts if isinstance(ts, (int, float)) else 0


def _fresh(row: dict) -> float:
    """The skills ledger time a scope row reflects (`fact`); a load that added a repo moved only its `ts`."""
    fact = row.get("fact")
    return fact if isinstance(fact, (int, float)) else _ts(row)


def _migrated(row: dict) -> bool:
    return any(row.get(k) for k in ("migration_sources", "legacy_ledger", "migration_action"))


def _fact(row: dict) -> bool:
    """A skills ledger row after which a learned skill's scope row may be out of date: one that changes its text
    or where it was learned. A plain keep changes neither, and the backfill's own rows are derived."""
    action = row.get("action")
    if action == "keep":
        return _migrated(row)
    if action == "scope":
        return row.get("source") == "session"
    return action in ("create", "patch", "edit", "restore")


def _newest(rows: list[dict], name, before: float | None = None) -> float:
    """The time of the latest skills ledger fact about `name` (`_fact`), before `before` when given; 0 for none."""
    times = [_ts(r) for r in rows if r.get("name") == name and _fact(r) and (before is None or _ts(r) < before)]
    return max(times, default=0)


def _made(entries: list[dict]) -> dict[str, tuple[dict, float | None]]:
    """Each learned skill's latest creation row (a create, or a migration's create or keep), and the time of the
    archive that closed it when no restore or creation came after."""
    made: dict[str, tuple[dict, float | None]] = {}
    for row in sorted(entries, key=_ts):
        name, action = row.get("name"), row.get("action")
        if not isinstance(name, str):
            continue
        if action == "create" or (action == "keep" and _migrated(row)):
            made[name] = (row, None)
        elif name in made and action in ("archive", "restore"):
            made[name] = (made[name][0], _ts(row) if action == "archive" else None)
    return made


class _Ledgers:
    """Ledgers and run logs read once each, by folder."""

    def __init__(self):
        self.rows: dict[Path, list[dict]] = {}
        self.runs: dict[Path, dict[str, str]] = {}

    def ledger(self, path: Path) -> list[dict]:
        if path not in self.rows:
            self.rows[path] = jsonl_rows(path)
        return self.rows[path]

    def run_repo(self, folder: Path, run_id: str) -> str | None:
        if folder not in self.runs:
            self.runs[folder] = {
                r["run_id"]: r["project"]
                for r in jsonl_rows(folder / "runs.jsonl")
                if isinstance(r.get("run_id"), str) and isinstance(r.get("project"), str) and r["project"]
            }
        return self.runs[folder].get(run_id)

    def sources(self, row: dict) -> list[tuple[Path, dict]]:
        """The (ledger, row) a migration row was migrated from: each pinned by generation, else, for an import from
        before the pins, its legacy ledger's last create of the name made before it."""
        pinned = row.get("migration_sources")
        found = []
        for source in pinned if isinstance(pinned, list) else []:
            if not (isinstance(source, dict) and isinstance(source.get("ledger"), str)):
                continue
            other = Path(source["ledger"])
            rows = self.ledger(other)
            original = next((r for r in rows if _generation(r) == source.get("generation")), None)
            if original is not None and original.get("action") != "create":  # a restore: the create before it
                original = _last_create(rows, original.get("name"), _ts(original))
            found += [(other, original)] if original is not None else []
        if not found and not pinned and isinstance(row.get("legacy_ledger"), str):
            other = Path(row["legacy_ledger"])
            original = _last_create(self.ledger(other), row.get("name"), _ts(row))
            found += [(other, original)] if original is not None else []
        return found

    def origins(self, ledger: Path, row: dict, depth: int = 0) -> list[str]:
        """The repo keys a creation row names: its own `project`, its run's, else, for a migration, those of the rows
        it was migrated from and of the session pins that followed them in their own ledgers."""
        if isinstance(row.get("project"), str) and row["project"]:
            return [row["project"]]
        if isinstance(row.get("run_id"), str) and (repo := self.run_repo(ledger.parent, row["run_id"])):
            return [repo]
        found: list[str] = []
        for other, original in self.sources(row) if depth < C.SCOPE_TRACE_DEPTH else []:
            for repo in [*self.pinned(other, original), *self.origins(other, original, depth + 1)]:
                found += [repo] if repo not in found else []
        return found

    def resolved(self, ledger: Path, row: dict, depth: int = 0) -> bool:
        """Whether each copy a creation row stands for names the repo it was made in: its project, its run's, or, for
        a migration, every copy it brought, by those or by a session pin after it in its own ledger."""
        if (isinstance(row.get("project"), str) and row["project"]) or (
            isinstance(row.get("run_id"), str) and self.run_repo(ledger.parent, row["run_id"])
        ):
            return True
        found = self.sources(row) if depth < C.SCOPE_TRACE_DEPTH else []
        declared = row.get("migration_sources")
        if isinstance(declared, list) and len(found) < len(declared):  # a copy no ledger here shows any more
            return False
        return bool(found) and all(self.pinned(o, r) or self.resolved(o, r, depth + 1) for o, r in found)

    def pinned(self, ledger: Path, row: dict) -> list[str]:
        """The repos sessions pinned the skill `row` made to in that ledger, latest first, until an archive there."""
        name, made, rows = row.get("name"), _ts(row), self.ledger(ledger)
        archived = [_ts(r) for r in rows if r.get("name") == name and r.get("action") == "archive" and _ts(r) > made]
        return [p["project"] for p in reversed(_pins(rows, name, made)) if _ts(p) < min(archived, default=math.inf)]

    def lineage(self, entries: list[dict], name: str) -> tuple[float, list[dict], list[dict]]:
        """The local start, creation rows and merged copies: an archive bounds local history, while each merged
        copy's loads begin at its own creation; a restore continues the local skill."""
        start, creations, merged, closed, archived, floor = 0.0, [], [], True, 0.0, 0.0
        for r in sorted((r for r in entries if r.get("name") == name), key=_ts):
            action, migrated = r.get("action"), _migrated(r)
            if action == "archive":  # what was logged at its time, to the millisecond, was the archived skill's
                closed, archived = True, math.nextafter(_ts(r), math.inf)
            elif action == "restore":
                closed = False
            elif action == "create" or (action == "keep" and migrated):
                copies = [original for _, original in self.sources(r)] if migrated else []
                if closed:  # never at or before the archive that ended the skill of this name before it
                    floor = archived
                    start = max(min((_ts(c) for c in copies), default=_ts(r)), floor)
                    creations, merged, closed = [], [], False
                elif copies:  # a merged copy's own loads count from when it was made: the start only moves back
                    start = min(start, max(min(_ts(c) for c in copies), floor))
                creations.append(r)
                merged.extend(copies)
        return start, creations, merged

    def paths(self, entries: list[dict], name: str, creations: list[dict]) -> set[str]:
        """Where this skill's SKILL.md was, by its ledger rows here and in the ledgers it was migrated from."""
        rows = [r for r in entries if r.get("name") == name]
        rows += [
            r for c in creations for other, _ in self.sources(c) for r in self.ledger(other) if r.get("name") == name
        ]
        return {r["path"] for r in rows if isinstance(r.get("path"), str) and r["path"].endswith("SKILL.md")}


def _last_create(rows: list[dict], name, before: float) -> dict | None:
    made = [
        r
        for r in rows
        if r.get("action") == "create" and r.get("name") == name and _ts(r) <= before and not _migrated(r)
    ]
    return max(made, key=_ts, default=None)


def _generation(row: dict) -> str:
    """How a migration pins the row it migrated a skill from (migrate._made)."""
    return hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()


def _states(cfg: C.Config) -> list[tuple[str, dict]]:
    """(host, session record) from the state files of this store and of the legacy Codex store beside it."""
    out = []
    for folder, legacy in ((cfg.data, cfg.legacy_host), (cfg.data / "codex", "codex")):
        try:
            state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(state, dict):
            continue
        views = state["hosts"] if isinstance(state.get("hosts"), dict) else {legacy: state}
        for host, view in views.items():
            sessions = view.get("sessions") if isinstance(view, dict) else None
            for sid, record in sessions.items() if isinstance(sessions, dict) else ():
                if isinstance(record, dict):
                    out.append((host, {**record, "id": sid}))
    return out


def _indexed(cfg: C.Config) -> list[tuple[str, str, str]]:
    """(session id with its host prefix, transcript path, repo key) from the search index, opened read-only."""
    db = cfg.data / "sessions.db"
    if not db.exists():
        return []
    try:
        c = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=C.SQLITE_TIMEOUT_S)
        try:
            return [
                (str(sid), str(path), str(repo))
                for sid, path, repo in c.execute("select id, path, repo from sessions where repo is not null")
            ]
        finally:
            c.close()
    except sqlite3.Error:
        return []


def _logs(cfg: C.Config) -> tuple[tuple[Path, str, str], ...]:
    """Each activity log loads are read from: (path, the host of its rows that name none, its name in `SEEN`)."""
    return (
        (cfg.data / "activity.jsonl", cfg.legacy_host, "activity.jsonl"),
        (cfg.data / "codex" / "activity.jsonl", "codex", "codex/activity.jsonl"),
    )


def _tail(path: Path, seen) -> tuple[list[dict], list | None]:
    """A log's rows after `seen` ([inode, offset] where a run stopped) through its last whole line, and where that
    ends; a log replaced or cut short since is read from its start."""
    try:
        with path.open("rb") as f:
            info = os.fstat(f.fileno())
            ok = isinstance(seen, list) and len(seen) == 2 and seen[0] == info.st_ino and isinstance(seen[1], int)
            start = seen[1] if ok and seen[1] > 0 else 0
            if start:  # past the end, or not after a line end: cut short or rewritten in place
                f.seek(start - 1)
                start = start if f.read(1) == b"\n" else 0
            f.seek(start)
            data = f.read()
    except FileNotFoundError:
        return [], None
    end = data.rfind(b"\n") + 1
    rows = []
    for line in data[:end].splitlines():
        with contextlib.suppress(ValueError):  # a torn line, as iter_jsonl skips it
            row = json.loads(line)
            rows += [row] if isinstance(row, dict) else []
    return rows, [info.st_ino, start + end]


def _sessions(states: list, indexed: list) -> dict[tuple[str, str], str]:
    """(host, session id) to the repo key nowitknows recorded for the session, else the search index's."""
    repos = {(host, r["id"]): r["project"] for host, r in states if isinstance(r.get("project"), str)}
    for sid, _, repo in indexed:
        host, _, raw = sid.partition(":")
        repos.setdefault((host, raw), repo)
    return repos


def _loads(scanned: list[tuple[str, dict]], skills: dict, sessions: dict) -> tuple[dict[str, set[str]], set[str]]:
    """Per skill (SKILL.md, known paths, local start, merged copies, local paths): its explicit loads' repos and
    whether a load's repo or copy is unknown; a delivery or a load of another skill never counts."""
    from .report import _activity_kind

    out: dict[str, set[str]] = {}
    lost: set[str] = set()
    for legacy, row in scanned:
        name, host = row.get("skill"), row.get("host") or legacy
        explicit = row.get("kind") == "scope_load" or _activity_kind(row, host == "codex") == "loads"
        if not isinstance(name, str) or name not in skills or not explicit:
            continue
        skill, known, since, merged, local = skills[name]
        recorded = row.get("skill_source")
        before = _ts(row) < since
        copies = [paths for c, paths in merged if _ts(c) <= _ts(row)] if before else []
        # Without a merged copy's path history, an earlier load cannot be proven foreign.
        if (
            isinstance(recorded, dict)
            and isinstance(recorded.get("path"), str)
            and recorded["path"] not in known
            and (not before or all(copies))
        ):
            with contextlib.suppress(OSError, RuntimeError, ValueError):
                other = Path(recorded["path"])
                if not (other.exists() and other.resolve() == skill.resolve()):
                    continue
        if before:
            if not copies:
                continue
            path = recorded.get("path") if isinstance(recorded, dict) else None
            paths = set().union(*copies)
            if isinstance(path, str) and path in paths and path not in local:
                pass  # the local archive cannot cut a different copy's own loads
            elif isinstance(path, str) and path in local and path not in paths and all(copies):
                continue
            else:  # the log cannot distinguish the merged copy from the archived one: hide nothing
                lost.add(name)
                continue
        own = row.get("repo")
        if repo := own if isinstance(own, str) and own else sessions.get((host, row.get("session"))):
            out.setdefault(name, set()).add(repo)
        else:  # a row from before 0.9.1 whose session nowitknows no longer knows
            lost.add(name)
    return out, lost


def _folder_names(keys: set[str], states: list, indexed: list) -> dict[str, str]:
    """The folder each repo key names, for the mark's repo test and the listing: from where nowitknows's records say
    sessions ran, else a transcript's first cwd; a key without one stays out."""
    found: dict[str, str] = {}

    def take(text) -> None:
        if not isinstance(text, str) or not text or not keys - found.keys():
            return
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            path = Path(text)
            key = native.project_key(path)
            if key in keys:
                found.setdefault(key, native.project_name(path))
                return
            for folder in (path, *path.parents):  # a folder gone since: the key names its own path
                if (key := native.folder_name(str(folder))) in keys:
                    found.setdefault(key, folder.name)
                    return

    for _, record in states:
        take(record.get("start"))
        take(record.get("cwd"))
    from .sessions import read_jsonl

    for _, path, repo in indexed:
        if repo in keys and repo not in found:
            with contextlib.suppress(OSError, ValueError):
                for _, row in zip(range(C.SCOPE_CWD_ROWS), read_jsonl(Path(path)), strict=False):
                    if isinstance(row, dict) and row.get("cwd"):
                        take(row["cwd"])
                        break
    return found


def _pins(entries: list[dict], name: str, since: float) -> list[dict]:
    """The repos sessions pinned the skill to (scope rows, source `session`) since it began, oldest first."""
    pins = [
        r
        for r in entries
        if r.get("name") == name
        and r.get("action") == "scope"
        and r.get("source") == "session"
        and isinstance(r.get("project"), str)
        and r["project"]
        and _ts(r) >= since
    ]
    return sorted(pins, key=_ts)


def _by_name(entries: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in entries:
        if isinstance(name := row.get("name"), str):
            grouped.setdefault(name, []).append(row)
    return grouped


def _relocated(creation: dict, entries: list[dict], skill: Path, row: dict | None) -> bool:
    """Recognize a restored shared-library copy from ledger provenance and its latest content hash."""
    from .skills import sha

    recorded, source = creation.get("path"), creation.get("skill_source")
    if (
        not isinstance(recorded, str)
        or source != {"host": "shared", "path": recorded}
        or Path(recorded).parts[-3:] != ("skills", skill.parent.name, "SKILL.md")
        or (row is not None and _fresh(row) < _ts(creation))
    ):
        return False
    latest = next(
        (r for r in reversed(entries) if r.get("action") in ("create", "patch", "edit") and not r.get("file")),
        {},
    )
    return bool(latest.get("after") and sha(skill.read_text(encoding="utf-8-sig")) == latest["after"])


def backfill(cfg: C.Config, folder: Path | None = None) -> dict:
    if not cfg.skill_scope:
        return {"scoped": 0, "skipped": "skill scoping is off"}
    from .skills import SkillStore

    store, rows = SkillStore(cfg), load(cfg)
    entries = jsonl_rows(store.ledger)
    by_name = _by_name(entries)
    todo: dict[str, tuple[dict, Path]] = {}
    live: dict[str, tuple[dict, Path]] = {}
    shut: dict[str, dict] = {}
    unreadable = False
    for name, (creation, closed) in _made(entries).items():
        skill = store.skill_md(name)
        try:
            source = trigger_source(skill, cfg)
            if closed is not None:  # archived: a row left open would scope whatever is written at its path next
                if _row(rows, source) is not None:
                    shut[name] = source
                continue
            recorded = creation.get("path")
            row = _row(rows, source)
            prior = _row(rows, source, archived=True)
            moved = isinstance(recorded, str) and Path(recorded).resolve() != skill.resolve()
            if not _regular(skill) or (
                moved and not (source["host"] == "shared" and _relocated(creation, by_name.get(name, []), skill, prior))
            ):
                continue  # not the SKILL.md nowitknows made under this name (a moved library, a namesake)
            (todo if row is None or _fresh(row) < _newest(by_name.get(name, []), name) else live)[name] = (
                source,
                skill,
            )
        except (OSError, RuntimeError, ValueError):
            unreadable = True
    if not (todo or live or shut):
        return {"scoped": 0}
    ledgers, states, indexed, logs = _Ledgers(), _states(cfg), _indexed(cfg), _logs(cfg)
    sessions = _sessions(states, indexed)
    lineage = {name: ledgers.lineage(by_name.get(name, []), name) for name in (*todo, *live)}
    skills = {
        name: (
            skill,
            ledgers.paths(by_name.get(name, []), name, lineage[name][1]),
            lineage[name][0],
            [
                (original, ledgers.paths(ledgers.ledger(other), name, []))
                for creation in lineage[name][1]
                for other, original in ledgers.sources(creation)
            ],
            {str(skill)} | {r["path"] for r in by_name.get(name, []) if isinstance(r.get("path"), str)},
        )
        for name, (_, skill) in (*todo.items(), *live.items())
    }
    origins = {
        name: list(dict.fromkeys(repo for c in lineage[name][1] for repo in ledgers.origins(store.ledger, c)))
        for name in todo
    }
    seen = rows.get(SEEN) if isinstance(rows.get(SEEN), dict) else {}
    # A rebuild, or a row not yet `read` (new since the last run), counts every load since its skill began; the
    # other rows, the loads logged since the last run.
    full = bool(todo) or any(not _row(rows, source).get("read") for source, _ in live.values())
    first = [_tail(path, None if full else seen.get(label)) for path, _, label in logs]
    scanned = [(legacy, row) for (_, legacy, _), (found, _) in zip(logs, first, strict=True) for row in found]
    loads, lost = _loads(scanned, skills, sessions)
    keys = {k for name in todo for k in (*origins[name], *loads.get(name, ()))}
    keys |= {p["project"] for name in todo for p in _pins(by_name.get(name, []), name, lineage[name][0])}
    keys |= {k for name, (source, _) in live.items() for k in loads.get(name, set()) - set(_row(rows, source)["repos"])}
    names = _folder_names(keys, states, indexed)  # outside the locks: it may read transcripts
    counts, made, widened = dict.fromkeys((PROJECT, GENERAL, UNKNOWN), 0), 0, 0
    now = round(time.time(), 3)
    try:
        with contextlib.ExitStack() as held:
            held.enter_context(file_lock(store.ledger))
            entries = jsonl_rows(store.ledger)  # as it is now, under its lock, including live generations
            by_name = _by_name(entries)
            changed = {name for name in lineage if ledgers.lineage(by_name.get(name, []), name) != lineage[name]}
            current = held.enter_context(_edit(cfg, durable=True))
            # Last, under the scope file's lock: a load logged after this read is the next run's, whether or not its
            # hook could add the repo itself.
            last = [_tail(path, end) for (path, _, _), (_, end) in zip(logs, first, strict=True)]
            tail = [(legacy, row) for (_, legacy, _), (found, _) in zip(logs, last, strict=True) for row in found]
            more, lost_more = _loads(tail, skills, sessions)
            for name, repos in more.items():
                loads.setdefault(name, set()).update(repos)
            lost |= lost_more
            closed = _made(entries)
            for name, source in shut.items():
                row = _row(current, source)
                if row is not None and closed.get(name, ({}, None))[1] is not None:
                    row["archived"] = closed[name][1]
            texts = {}
            for name, (_, skill) in todo.items():
                with contextlib.suppress(OSError, ValueError):
                    texts[name] = skill.read_text(encoding="utf-8-sig")
            written = {
                (r.get("name"), json.dumps(r.get("skill_source"), sort_keys=True), r.get("fact"))
                for r in (entries if todo else ())
                if r.get("action") == "scope" and r.get("source") == "backfill"
            }
            for name, (source, _) in (*todo.items(), *live.items()):
                if name in changed:
                    continue  # the next run derives the new generation's paths and loads together
                row, fact = _row(current, source), _newest(by_name.get(name, []), name)
                if row is not None and (name in live or _fresh(row) >= fact):  # current: the loads it lacks
                    for repo in sorted(loads.get(name, set()) - set(row["repos"])):
                        row["repos"][repo] = names.get(repo, "")
                        row["ts"], widened = now, widened + 1
                    if name in lost:  # a load ran where no record tells: routed everywhere
                        row["scope"] = UNKNOWN
                    if full:
                        row["read"] = True
                    continue
                if name in live or closed.get(name, ({}, None))[1] is not None:
                    continue  # archived since this run began
                since = lineage[name][0]
                kept = _row(current, source, archived=True)  # after a restore, the archived row is this skill's
                own = kept is not None and _fresh(kept) >= since  # written for this skill, not an archived namesake
                old = dict(kept["repos"]) if own else {}
                pins = _pins(by_name.get(name, []), name, since)
                if pins:
                    origin = pins[-1]["project"]
                elif own and isinstance(kept.get("origin"), str):
                    origin = kept["origin"]
                else:
                    origin = origins[name][0] if origins[name] else None
                found = list(dict.fromkeys([*([origin] if origin else []), *origins[name], *old]))
                found += sorted(loads.get(name, set()) - set(found))
                where = {key: names.get(key) or old.get(key) or "" for key in found}
                creations = lineage[name][1]
                times = [*(_ts(c) for c in creations), math.inf]
                unsure = name in lost or any(  # a copy made where no record tells, or a load where none does
                    not ledgers.resolved(store.ledger, c) and not any(times[i] <= _ts(p) < times[i + 1] for p in pins)
                    for i, c in enumerate(creations)
                )
                text = texts.get(name)  # unreadable: routed everywhere until its next change
                value = mark(text, where.get(origin, "")) if origin and not unsure and text is not None else UNKNOWN
                current[trigger_key(source)] = {
                    "name": name,
                    "source": source,
                    "scope": value,
                    "origin": origin,
                    "repos": where,
                    "ts": now,
                    "fact": fact,
                    "read": True,
                }
                counts[value] += 1
                made += value == PROJECT and kept is None  # its first scope, as after an upgrade
                if (name, json.dumps(source, sort_keys=True), fact) not in written:  # a crash after it, before the rows
                    entry = {"ts": now, "action": "scope", "name": name, "source": "backfill", "host": cfg.host}
                    entry.update(path=str(store.skill_md(name)), skill_source=source, scope=value, project=origin)
                    append_line(store.ledger, json.dumps({**entry, "repos": found, "fact": fact}))
            # Deferred work must replay from the earlier cursors; duplicate adds are harmless.
            current[SEEN] = (
                seen
                if changed or unreadable
                else {label: end for (_, _, label), (_, end) in zip(logs, last, strict=True) if end is not None}
            )
    except TimeoutError:  # another worker holds the ledger or a hook the scope file: the next start job retries
        return {"scoped": 0, "skipped": "busy"}
    if made:
        many = made != 1
        notify.push(
            cfg,
            f"{notify.PREFIX} now suggests {made} learned project skill{'s' if many else ''} only in the repos "
            f"{'they were' if many else 'it was'} learned or loaded in; /nowitknows:changes shows each scope and how "
            "to widen it",
            tell={"kind": "scoped", "count": made},
        )
    out = {"scoped": counts[PROJECT], "general": counts[GENERAL], "unknown": counts[UNKNOWN]} if todo else {"scoped": 0}
    return {**out, "widened": widened} if widened else out


def _regular(path: Path) -> bool:
    """Path.is_file() that lets a transient stat error count as unreadable, not as a missing file."""
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False


def _shown(key: str, name) -> str:
    return name if isinstance(name, str) and notify.WHERE.fullmatch(name) else key


def listing(cfg: C.Config, n: int) -> list[str]:
    """/nowitknows:changes lines: the scope of up to `n` learned skills, project skills first, and how to widen one."""
    if not cfg.skill_scope:
        switch = "NOWITKNOWS_SKILL_SCOPE=0" if cfg.host == "claude" else "configure skill_scope=off"
        return [f"- scope · off ({switch}): every learned skill is suggested in every repo"]
    rows, lines, first = load(cfg), [], {PROJECT: 0, UNKNOWN: 1, GENERAL: 2}
    ordered = sorted(
        (r for r in rows.values() if isinstance(r, dict)),
        key=lambda r: (first.get(r.get("scope"), 3), str(r.get("name"))),
    )
    for row in ordered:
        source = row.get("source")
        with contextlib.suppress(OSError, ValueError, TypeError):
            if (
                _row(rows, source) is not row
                or not isinstance(row.get("name"), str)
                or not Path(source["path"]).is_file()
            ):
                continue
            name, link = row["name"], notify.link(source["path"])
            if row["scope"] == PROJECT:
                where = ", ".join(_shown(key, folder) for key, folder in row["repos"].items())
                how = (
                    f"type /{name} or have Claude load it in another repo"
                    if cfg.host == "claude"
                    else f"type /{name} or have Cursor read its SKILL.md or open it with skill_view in another repo"
                    if cfg.host == "cursor"
                    else "have Codex open it with nowitknows's skill_view in another repo"
                )
                lines.append(f"- scope · [{name}]({link}) · project skill, suggested only in {where}; {how} to add it")
            elif row["scope"] == GENERAL:
                lines.append(f"- scope · [{name}]({link}) · general, suggested in every repo")
            else:
                lines.append(
                    f"- scope · [{name}]({link}) · suggested in every repo: "
                    "a repo it was learned or loaded in is not known"
                )
    if lines:
        switch = "NOWITKNOWS_SKILL_SCOPE=0" if cfg.host == "claude" else "configure skill_scope=off"
        more = [f"- scope · {len(lines) - n} more learned skills not listed here"] if len(lines) > n else []
        lines = [*lines[:n], *more, f"- scope · {switch} suggests every learned skill in every repo"]
    return lines
