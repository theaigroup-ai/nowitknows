from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import time
import uuid
from contextlib import ExitStack, nullcontext, suppress
from dataclasses import replace
from pathlib import Path

from . import config as C
from . import native, notify, sessions
from .memory import _SPLIT, DELIM, IndexConflict, _edited
from .skills import SkillStore, sha, valid_name
from .state import (
    append_line,
    durable_write,
    file_lock,
    jsonl_rows,
    lock_held,
    mark,
    noted,
    older_touch,
    own_writes,
    sync_file,
)

UPGRADE_INSTRUCTIONS = (
    "Restart Claude Code and Codex so every chat runs the nowitknows you installed (a chat opened before the update "
    "keeps its older nowitknows until it is closed), then ask /nowitknows:setup (in Codex, "
    "$nowitknows-codex:nowitknows-setup) to apply the migration. Writes by this nowitknows, the asking chat's "
    "included, do not hold it up; a nowitknows worker that is running does, until it finishes."
)

# A migration leaves <MARKER><stem>-<the import's key, FINGERPRINT_CHARS hex digits> beside an old note: where it went
MARKER = ".nowitknows-migrated-"
MARKERS = C.aliases(MARKER)
OWN_WRITES = "own-legacy-writes.json"  # in <data>/migration: what an apply left in the legacy store (Migration.fence)


def _clock(stamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))


def _alive(pid: int) -> bool:
    """Probe without signalling the process; inaccessible processes conservatively count as alive."""
    if pid <= 0:
        return True
    if C.WINDOWS:
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # it exists, owned by someone else
        return True
    return True


def _recorded_process(surface: object) -> bool:
    """Whether the build that last started the session recorded its process: it does for an attended start
    (hooks._attended, judged from the surface that start saved)."""
    if not isinstance(surface, dict):
        return False
    if surface.get("environment_kind") == C.REMOTE_CONTROL_KIND:
        return True
    if surface.get("attended") is not None:
        return surface.get("attended") == "1"
    return surface.get("entrypoint") not in C.HEADLESS_ENTRYPOINTS


def _superseded(sid: str, record: dict, touch: float, moved: object, section: dict, claims: dict[str, float]) -> bool:
    """An older touch a handoff superseded: this build has touched the chat since, and every Claude Code process the
    older builds recorded (`project_pids`, at each attended start) has exited or since runs a chat on this build."""
    if not (isinstance(moved, dict) and (moved.get("current_seen") or 0) > touch):
        return False  # the chat has not moved to this build
    if not _recorded_process(record.get("old_surface", record.get("surface"))):
        return False
    started: dict[str, float] = {}
    for repo in section.get("project_pids", {}).values():
        for process, when in repo.items():
            started[process] = max(started.get(process, 0), when)
    if sid in started:
        return False  # its process had no pid to record: the session id stands in for one
    # A process this build has run a chat in since that process last started one is no longer an older build's.
    older = {p: when for p, when in started.items() if p.isdigit() and (p not in claims or claims[p] < when)}
    if not older:
        return False  # no older build's process is recorded, so the one that made the touch is unknown
    return not any(_alive(int(p)) for p in older)


def unshared_notes(cfg: C.Config, key: str | None = None) -> int:
    """How many notes nowitknows's old project stores (before 0.8.0: Claude's, and the legacy Codex one inside it) hold
    for a repo, or for every repo, that no migration has moved: those with no marker beside them, and none that an
    owner flip of 0.7.x already copied (its marker names the folder; the copy there is the same bytes)."""
    prefix = "(?:" + "|".join(re.escape(m) for m in MARKERS) + ")"
    count, marker = 0, re.compile(prefix + rf"(.+)-[0-9a-f]{{{C.FINGERPRINT_CHARS}}}")
    flipped = re.compile(prefix + r"\d+")
    for stores in (cfg.data / "projects", cfg.data / "codex" / "projects"):
        for folder in [stores / key / "memory"] if key else sorted(stores.glob("*/memory")):
            with suppress(OSError):
                names = {e.name for e in os.scandir(folder) if e.is_file(follow_symlinks=False)}
                moved = {m[1] for name in names if (m := marker.fullmatch(name))}
                copied = [
                    Path(where)
                    for name in sorted(names)
                    if flipped.fullmatch(name) and os.path.isabs(where := _text(folder / name).strip())
                ]
                for n in names:
                    if n.endswith(".md") and n != "MEMORY.md" and n[:-3] not in moved:
                        count += not any(_same(folder / n, there / n) for there in copied)
    return count


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _same(path: Path, other: Path) -> bool:
    """Whether two files are the same bytes."""
    try:
        return other.is_file() and path.read_bytes() == other.read_bytes()
    except OSError:
        return False


def announce_unshared(cfg: C.Config, key: str, name: str = "") -> None:
    """Once per repo and app, say that notes from before sharing wait for the migration and how to run it; `name` is
    the repo's folder, which the model is told, since a session in another repo may tell it."""
    if count := unshared_notes(cfg, key):
        what = (
            "1 older project note for this repo is" if count == 1 else f"{count} older project notes for this repo are"
        )
        notify.push(
            cfg,
            f"{notify.PREFIX} {what} not in the shared notes folder yet. /nowitknows:setup previews moving them there; "
            "nothing changes until you ask it to apply.",
            f"unshared-notes:{key}",
            permanent=True,
            account=True,
            tell={"kind": "unshared"},
            project=(key, name),
        )


def digest(path: Path) -> str:
    h = hashlib.sha256()
    paths = sorted(path.rglob("*")) if path.is_dir() and not path.is_symlink() else [path]
    for p in paths:
        name = str(p.relative_to(path) if p != path else p.name).encode()
        h.update(len(name).to_bytes(8, "big") + name)
        if p.is_symlink():
            content = b"link:" + os.readlink(p).encode()
        elif p.is_file():
            content = b"file:" + p.read_bytes()
        else:
            content = b"directory"
        h.update(len(content).to_bytes(8, "big") + content)
    return h.hexdigest()


def _json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _entries(path: Path) -> list[str]:
    return [e.strip() for e in _SPLIT.split(path.read_text(encoding="utf-8-sig")) if e.strip()] if path.exists() else []


def _made(path: Path, host: str, skills: Path) -> dict[str, dict]:
    made = {}
    if not path.exists():
        return made
    for line in path.read_bytes().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            return {}
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            return {}
        name = row["name"]
        if row.get("host", host) != host or not valid_name(name):
            continue
        if row.get("action") == "archive":
            made.pop(name, None)
        elif row.get("action") in ("create", "restore"):
            if (
                isinstance(row.get("path"), str)
                and Path(row["path"]).expanduser().resolve() == (skills / name / "SKILL.md").expanduser().resolve()
            ):
                made[name] = {
                    "host": host,
                    "ledger": str(path),
                    "generation": hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest(),
                    "ts": row.get("ts"),
                }
            else:
                made.pop(name, None)
    return made


def escaping_links(path: Path) -> list[str]:
    escaped = []
    for link in path.rglob("*"):
        if link.is_symlink() and not os.path.isabs(os.readlink(link)):
            try:
                inside = link.resolve().is_relative_to(path.resolve())
            except (OSError, RuntimeError):
                inside = False
            if not inside:
                escaped.append(str(link.relative_to(path)))
    return sorted(escaped)


LEGACY = "legacy-codex"  # a planned action's `origin` when it imports from the legacy Codex store


def _identity(action: dict) -> dict:
    """An action without its `origin` label, so receipts and generations match plans made before the label."""
    return {k: v for k, v in action.items() if k != "origin"}


class SeparateStore(ValueError):
    """The Codex side keeps its own store (a non-default profile or an explicit data folder): nothing to migrate."""


def _stranded(cfg: C.Config, shared: Path) -> str:
    """The note for a legacy store the shared-store plan would still import, so a skip never hides it."""
    legacy = shared.expanduser().resolve() / "codex"
    if not legacy.is_dir():
        return ""
    env = {k: v for k, v in cfg.env.items() if k != "NOWITKNOWS_CODEX_DATA_DIR"}
    try:
        migration = Migration(replace(cfg, data=legacy.parent, env=env))
        journal = _json(migration.journal, {}).get("pending")  # an unfinished import, from any version
        pending = bool(journal) or any(action.get("origin") == LEGACY for action in migration.plan())
    except Exception:  # an unreadable plan must not hide the store
        pending = True
    if not pending:
        return ""
    return (
        f"; a legacy store at {legacy} was not imported; "
        "unset NOWITKNOWS_CODEX_DATA_DIR and rerun setup with --apply to import it"
    )


class Migration:
    """Read-only planning, then durable imports after one legacy settlement pass."""

    def __init__(self, cfg: C.Config):
        self.cfg, self.root = cfg, cfg.data
        self._locked = False
        self.legacy = cfg.data / "codex"
        self.journal = self.root / "migration-state.json"
        self.saved = self.root / "migration"
        home = C.home_dir(cfg.env)
        profile = Path(C.env("NOWITKNOWS_CODEX_HOME", cfg.env) or cfg.env.get("CODEX_HOME") or home / ".codex")
        separate = C.env("NOWITKNOWS_CODEX_DATA_DIR", cfg.env)
        shared = Path(C.env("NOWITKNOWS_SHARED_DATA_DIR", cfg.env) or self.root)
        if profile.expanduser().resolve() != (home / ".codex").resolve():
            raise SeparateStore(
                "Shared-store migration is for the default Codex profile; other profiles stay isolated. "
                "Nothing needs migrating for this profile."
            )
        if separate and Path(separate).expanduser().resolve() != shared.expanduser().resolve():
            note = _stranded(cfg, shared)
            raise SeparateStore("NOWITKNOWS_CODEX_DATA_DIR is set, so this store stays separate" + note)

    def fence(self) -> dict:
        """Refuse while an older nowitknows may be mid-write: its session touches, unlocked ledger rows, legacy-store
        writes other than this migration's own, and held locks count; this version's other writes do not."""
        home = C.home_dir(self.cfg.env)
        views = []
        cutoff = time.time() - C.MIGRATE_QUIET_MINUTES * 60
        own = self._own()
        for host, root, skills in (
            (
                "claude",
                self.root,
                C.env("NOWITKNOWS_CLAUDE_SKILLS_DIR", self.cfg.env)
                or (self.cfg.skills_home if self.cfg.host == "claude" else home / ".claude/skills"),
            ),
            ("codex", self.legacy, self._codex_skills()),
        ):
            recent, held, errors = [], [], []
            newest = None
            paths = set()
            try:
                for folder in (root, Path(skills).expanduser().resolve()):
                    if not folder.exists():
                        continue
                    paths.add(folder)
                    for base, dirs, files in os.walk(
                        folder, onerror=lambda error, errors=errors: errors.append(str(error))
                    ):
                        # The nested legacy store gets its own Codex view.
                        dirs[:] = [d for d in dirs if host != "claude" or Path(base) / d != self.legacy]
                        paths.update(Path(base) / name for name in (*dirs, *files))
                for path in sorted(paths):
                    try:
                        info = path.lstat()
                        stamp = max(info.st_mtime, info.st_ctime)
                        item = {"path": str(path), "write_time": stamp}
                        if newest is None or stamp > newest["write_time"]:
                            newest = item
                        if stamp > cutoff:
                            # A folder or lock file holds no writer's data: the files it writes show the writer.
                            counts = (
                                host == "codex"
                                and path.is_relative_to(root)
                                and not stat.S_ISDIR(info.st_mode)
                                and not path.name.endswith(".lock")
                                and own.get(str(path)) != [info.st_mtime_ns, info.st_ctime_ns, info.st_size]
                            )
                            recent.append({**item, "blocks": counts})
                        if (
                            path.name.endswith(".lock")
                            and ("ledger" in path.name or path.name in ("worker.lock", "codex-worker.lock"))
                            and lock_held(path.with_suffix(""))
                        ):
                            held.append(str(path))
                    except FileNotFoundError:
                        errors.append(f"changed during fence inspection: {path}")
            except OSError as error:
                errors.append(str(error))
            old = self._old_chats(cutoff, errors) if host == "claude" else []
            unlocked = self._unlocked_rows(cutoff, errors) if host == "claude" else []
            views.append(
                {
                    "host": host,
                    "root": str(root),
                    "recent": recent,
                    "old_chats": old,
                    "unlocked": unlocked,
                    "held": held,
                    "errors": errors,
                    "newest": newest,
                }
            )
        return {
            "quiet_minutes": C.MIGRATE_QUIET_MINUTES,
            "hosts": views,
            "blocked": any(
                v["old_chats"] or v["unlocked"] or v["held"] or v["errors"] or any(i["blocks"] for i in v["recent"])
                for v in views
            ),
        }

    def _own(self) -> dict:
        """What the last apply wrote in the legacy store, as it left it (`_record_own`)."""
        try:
            own = json.loads((self.saved / OWN_WRITES).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return own if isinstance(own, dict) else {}

    def _record_own(self, written: dict[str, list[int]]) -> None:
        """Keep the marks this run's legacy-store writes left, and earlier runs' ones nothing has changed since."""
        own, root = {}, str(self.legacy)
        for path, recorded in self._own().items():
            with suppress(OSError):
                if mark(Path(path)) == recorded:
                    own[path] = recorded
        own.update({path: m for path, m in written.items() if path.startswith(root + os.sep)})
        with suppress(OSError):  # unrecorded, the next fence counts them: it refuses, never loses
            durable_write(self.saved / OWN_WRITES, json.dumps(own))

    def _old_chats(self, cutoff: float, errors: list[str]) -> list[dict]:
        """Sessions an older nowitknows touched after `cutoff`, from the shared store's session records, less touches a
        verified handoff of that chat to this build superseded (`_superseded`)."""
        path = self.root / "state.json"
        try:
            for attempt in range(C.REPLACE_TRIES):
                try:
                    text = path.read_text(encoding="utf-8")
                    break
                except PermissionError:  # Windows, while a hook replaces the file
                    if not C.WINDOWS or attempt == C.REPLACE_TRIES - 1:
                        raise
                    time.sleep(C.REPLACE_RETRY_S)
            raw = json.loads(text)
            hosts = raw.get("hosts", {})
            here = hosts.get("claude", {}).get("sessions", {})  # where this build keeps a Claude chat it runs
            claims: dict[str, float] = {}  # Claude Code process -> when this build last touched a chat it runs
            for record in here.values():
                if isinstance(record, dict) and isinstance(record.get("pid"), str):
                    claims[record["pid"]] = max(claims.get(record["pid"], 0), record.get("current_seen") or 0)
            # The top level holds pre-0.7 Claude's records, also beside `hosts`: those builds wrote the whole file.
            sections = [
                ("claude", raw, here),
                *((host, data, data.get("sessions", {})) for host, data in hosts.items()),
            ]
            found = []
            for host, data, moved in sections:
                for sid, record in data.get("sessions", {}).items():
                    touch = older_touch(record) if isinstance(record, dict) else 0
                    if touch > cutoff and not _superseded(sid, record, touch, moved.get(sid), data, claims):
                        found.append({"host": host, "session": sid, "write_time": touch})
        except FileNotFoundError:
            return []
        except (OSError, ValueError, AttributeError, TypeError) as error:
            errors.append(f"session records unreadable: {path}: {error}")
            return []
        return sorted(found, key=lambda item: item["write_time"])

    def _unlocked_rows(self, cutoff: float, errors: list[str]) -> list[float]:
        """When rows were added after `cutoff` by a nowitknows before 0.7: it appended memory_ledger.jsonl without the
        ledger lock that this version holds to rewrite it, and only the locked protocol gives a row a `write_id`."""
        path = self.root / "memory_ledger.jsonl"
        try:
            stat = path.stat()
            if max(stat.st_mtime, stat.st_ctime) <= cutoff:
                return []
            rows = jsonl_rows(path)
        except FileNotFoundError:
            return []
        except OSError as error:
            errors.append(f"memory ledger unreadable: {path}: {error}")
            return []
        stamps = [row.get("ts") for row in rows if "write_id" not in row]
        return sorted(ts for ts in stamps if isinstance(ts, (int, float)) and ts > cutoff)

    @staticmethod
    def describe_fence(view: dict) -> str:
        quiet = view["quiet_minutes"]
        lines = [
            f"Migration quiet fence: {'BLOCKED' if view['blocked'] else 'quiet'}; it waits {quiet} minutes after the "
            "last activity of a chat on an older nowitknows, the last memory ledger row a nowitknows before 0.7 added "
            "without the lock, and the last write to the legacy Codex store other than the migration's own, and "
            "while a worker or ledger lock is held. Other writes, the asking chat's among them, do not count: only "
            "their number and the newest are shown."
        ]
        for host in view["hosts"]:
            counted = [item for item in host["recent"] if item["blocks"]]
            lines.append(
                f"- {host['host']} ({host['root']}): {len(host['recent'])} recent writes, {len(counted)} counted; "
                f"{len(host['held'])} held worker/ledger locks"
            )
            if host["newest"]:
                newest = host["newest"]
                lines.append(f"  newest write: {newest['path']} (mtime/ctime {newest['write_time']:.3f})")
            for item in counted:
                lines.append(f"  recent write: {item['path']} (mtime/ctime {item['write_time']:.3f})")
            for chat in host["old_chats"]:
                lines.append(
                    f"  chat on an older nowitknows: {chat['host']} session {chat['session']}, last active "
                    f"{_clock(chat['write_time'])}; close it and wait {quiet} minutes from then"
                )
            if host["unlocked"]:
                lines.append(
                    f"  {len(host['unlocked'])} memory ledger rows from a nowitknows before 0.7, which writes without "
                    f"the ledger lock, the newest at {_clock(host['unlocked'][-1])}; close chats that run it and "
                    f"wait {quiet} minutes from then"
                )
            lines.extend(f"  held lock: {path}" for path in host["held"])
            lines.extend(f"  inspection error (refuse apply): {error}" for error in host["errors"])
        return "\n".join([*lines, UPGRADE_INSTRUCTIONS])

    def plan(self, *, fresh: bool = False) -> list[dict]:
        state = _json(self.journal, {})
        if state.get("pending"):
            if state.get("version") not in C.MIGRATION_COMPATIBLE:
                raise ValueError(
                    "Migration refused: older pending journal cannot safely replay runtime copies; "
                    f"retained at {self.journal}"
                )
            if not fresh:
                return self._current(state["pending"])
        home = C.home_dir(self.cfg.env)
        homes = [
            (
                "claude",
                self.cfg.skills_home
                if self.cfg.host == "claude"
                else Path(C.env("NOWITKNOWS_CLAUDE_SKILLS_DIR", self.cfg.env) or home / ".claude/skills"),
                self.root,
            ),
            ("codex", self._codex_skills(), self.legacy),
        ]
        homes = [(host, skills.expanduser().resolve(), data) for host, skills, data in homes]
        candidates: dict[str, list[dict]] = {}
        retained = []
        completed = [r for r in jsonl_rows(self.root / "skills_ledger.jsonl") if r.get("migration_action")]
        consumed = [source for row in completed for source in row.get("migration_sources", [])]
        for host, skills, data in homes:
            for name, ownership in sorted(_made(data / "skills_ledger.jsonl", host, skills).items()):
                # Older imports have no generation pin; only a provably later ownership receipt can renew them.
                older = [
                    row
                    for row in completed
                    if row.get("name") == name
                    and row.get("legacy_ledger") == ownership["ledger"]
                    and not row.get("migration_sources")
                ]
                if ownership in consumed or any(
                    not (
                        isinstance(ownership["ts"], (int, float))
                        and isinstance(row.get("ts"), (int, float))
                        and ownership["ts"] > row["ts"]
                    )
                    for row in older
                ):
                    continue
                path = skills / name
                if path.is_dir() and not path.is_symlink() and (path / "SKILL.md").is_file():
                    if links := escaping_links(path):
                        action = {"action": "retain-skill", "name": name, "path": str(path), "links": links}
                        if not self._receipt(action).exists():
                            retained.append(action)
                        continue
                    candidates.setdefault(name, []).append(
                        {
                            "host": host,
                            "ownership": ownership,
                            "path": str(path),
                            "digest": digest(path),
                            "mtime": max(p.stat().st_mtime_ns for p in path.rglob("*") if p.is_file()),
                        }
                    )
        # An escaping link in either host prevents linking or moving that skill in both hosts.
        unsafe = {a["name"] for a in retained}
        for _host, skills, _data in homes:
            for name in list(candidates):
                if (skills / name).is_dir() and escaping_links(skills / name):
                    unsafe.add(name)
        actions = retained
        for name, copies in sorted(candidates.items()):
            if name in unsafe:
                continue
            canonical = self.root / "skills" / name
            if canonical.is_dir() and not canonical.is_symlink():
                copies.append(
                    {
                        "host": "shared",
                        "path": str(canonical),
                        "digest": digest(canonical),
                        "mtime": max(p.stat().st_mtime_ns for p in canonical.rglob("*") if p.is_file()),
                    }
                )
            winner = max(copies, key=lambda c: (c["mtime"], c["host"] == "shared", c["host"]))
            actions.append(
                {
                    "action": "skill",
                    "name": name,
                    "copies": copies,
                    "winner": winner,
                    "conflict": len({c["digest"] for c in copies}) > 1,
                }
            )
        imported = set(state.get("memory_imported", []))
        for target, limit in (("USER", C.USER_CAP), ("MEMORY", C.GLOBAL_MEMORY_CAP)):
            current = _entries(self.root / "memory" / f"{target}.md")
            for entry in _entries(self.legacy / "memory" / f"{target}.md"):
                key = hashlib.sha256((target + "\0" + entry).encode()).hexdigest()
                if key in imported:
                    continue
                duplicate = entry in current
                fits = len(DELIM.join([*current, entry])) <= limit
                actions.append(
                    {
                        "action": "memory",
                        "target": target,
                        "key": key,
                        "entry": entry,
                        "result": "duplicate" if duplicate else "merge" if fits else "overflow",
                    }
                )
                if fits and not duplicate:
                    current.append(entry)
        for folder in ("archive", "transcripts"):
            for path in sorted((self.legacy / folder).rglob("*")):
                if not path.is_file() or path.is_symlink() or path.name.endswith(".lock"):
                    continue
                relative = path.relative_to(self.legacy)
                if relative.parts[1] not in ("claude", "codex"):
                    relative = Path(folder) / "codex" / Path(*relative.parts[1:])
                dest = self.root / relative
                action = {"action": "copy", "path": str(path), "dest": str(dest), "digest": digest(path)}
                if not self._receipt(action).exists():
                    actions.append(action)
        for path in [] if self.cfg.project_notes_shared else sorted((self.legacy / "projects").glob("*/memory/*.md")):
            if path.name == "MEMORY.md" or path.is_symlink():
                continue
            action = {
                "action": "topic",
                "path": str(path),
                "dest": str(self.root / path.relative_to(self.legacy)),
                "digest": digest(path),
            }
            if not self._receipt(action).exists():
                actions.append(action)
        if self.cfg.project_notes_shared:
            actions.extend(self._notes_plan())
        database = self.legacy / "sessions.db"
        if database.exists() and not state.get("database_imported"):
            actions.append({"action": "database", "path": str(database)})
        for action in actions:
            if self._from_legacy(action):
                action["origin"] = LEGACY
        if self.legacy.exists() and (not state.get("indexed") or any(a["action"] == "copy" for a in actions)):
            actions.append(
                {
                    "action": "index",
                    "path": str(self.root / "sessions.db"),
                    "generation": hashlib.sha256(
                        json.dumps([_identity(a) for a in actions], sort_keys=True).encode()
                    ).hexdigest(),
                }
            )
        return actions

    def _blocked(self, action: dict) -> str | None:
        """An interrupted import binds its folder identity, so imports there wait until that folder is restored."""
        from .memory import _moved, imports

        for receipt in sorted((self.root / "memory-writes").glob("*.json")):
            value = _json(receipt, {})
            if not imports(value) or value.get("done"):
                continue
            into = {str(Path(path).parent) for path in [*value.get("writes", {}), *value.get("index_edits", {})]}
            if action["home"] in into and (why := _moved(value)):
                return why
        return None

    @staticmethod
    def _stale(action: dict) -> bool:
        """A planned notes action whose source note changed or went away since it was planned."""
        source = Path(action["path"]) if action.get("action") == "notes" else None
        return source is not None and (not source.is_file() or digest(source) != action["digest"])

    def _current(self, pending: list[dict]) -> list[dict]:
        """A journal's pending actions as things stand now: an unfinished notes action is dropped once sharing is off,
        and planned again when its source or the repo's folder in Claude Code changed (dropped when its source went)."""
        finished = {r.get("migration_key") for r in jsonl_rows(self.root / "memory_ledger.jsonl")}
        unfinished = [a for a in pending if a.get("action") == "notes" and a["key"] not in finished]
        if not unfinished:
            return pending
        if not self.cfg.project_notes_shared:
            return [action for action in pending if action not in unfinished]
        repos = self._repo_paths({a["repo"] for a in unfinished})
        homes = {repo: str(self._notes_home(repo, repos.get(repo))[0]) for repo in {a["repo"] for a in unfinished}}
        moved = [a for a in unfinished if self._stale(a) or a["home"] != homes[a["repo"]]]
        if not moved:
            return pending
        replanned = {action["path"]: action for action in self._notes_plan()}
        return [
            replanned[action["path"]] if action in moved else action
            for action in pending
            if action not in moved or action["path"] in replanned
        ]

    def _repo_paths(self, keys: set[str]) -> dict[str, Path]:
        """The folder each repo key's sessions started in, from nowitknows's session records in both stores, else from
        the first cwd in that repo's transcripts; read from files only, never a database."""
        paths: dict[str, Path] = {}

        def take(cwd) -> None:
            if isinstance(cwd, str) and cwd and (key := native.project_key(Path(cwd))) in keys:
                paths.setdefault(key, Path(cwd))

        records: list[dict] = []
        for data in (self.root, self.legacy):
            try:
                state = json.loads((data / "state.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for view in [state, *state.get("hosts", {}).values()] if isinstance(state, dict) else []:
                for record in (view.get("sessions") or {}).values() if isinstance(view, dict) else []:
                    records += [record] if isinstance(record, dict) else []
        # Its settings are read where a session started (`start`); a later cd moves only `cwd`, which counts only for
        # sessions recorded before `start` was.
        for record in records:
            take(record.get("start"))
        for record in records:
            if not record.get("start"):
                take(record.get("cwd"))
        # A session started at the repo root names its transcript folder after the repo's key; others need a scan.
        near = [
            folder
            for key in sorted(keys - set(paths))
            for folder in (
                self.cfg.transcripts / key,
                self.root / "transcripts/codex" / key,
                self.legacy / "transcripts" / key,
                self.legacy / "transcripts/codex" / key,
            )
        ]
        for roots in (near, [self.cfg.transcripts, self.root / "transcripts", self.legacy / "transcripts"]):
            for path in sessions.transcript_paths([r for r in roots if r.is_dir()]):
                if not keys - set(paths):
                    return paths
                with suppress(OSError):
                    for _, row in zip(range(C.MIGRATE_CWD_ROWS), sessions.read_jsonl(path), strict=False):
                        if isinstance(row, dict) and row.get("cwd"):
                            take(row["cwd"])
                            break
        return paths

    def _notes_home(self, key: str, project: Path | None) -> tuple[Path, str]:
        """Claude Code's notes folder for the repo and what chose it, in Claude Code's order: the environment override,
        then managed, the repo's own (when its folder is known) and user settings, then the default folder."""
        chain = native.settings_chain(project, self.cfg.claude_home)
        home, via = native.notes_folder(self.cfg.env, project, self.cfg.claude_home, chain, key)
        return (
            home,
            via
            if project is not None
            else via
            + "; nowitknows does not know where this repo's folder is, so it did not look at the repo's own settings",
        )

    @staticmethod
    def _destination(folder: Path) -> tuple[dict[str, bytes], bytes]:
        notes = {p.name: p.read_bytes() for p in folder.glob("*.md") if p.name != "MEMORY.md" and p.is_file()}
        index = folder / "MEMORY.md"
        return notes, index.read_bytes() if index.is_file() else b""

    def _notes_plan(self) -> list[dict]:
        """One `notes` action per note in nowitknows's old project stores (Claude's first, then the legacy Codex one),
        repo by repo, each planned against its folder as the actions before it, any repo's, leave that folder."""
        found: dict[str, list[tuple[str, Path]]] = {}
        for host, data in (("claude", self.root), ("codex", self.legacy)):
            for path in sorted((data / "projects").glob("*/memory/*.md")):
                if path.name != "MEMORY.md" and path.is_file() and not path.is_symlink():
                    found.setdefault(path.parent.parent.name, []).append((host, path))
        if not found:
            return []
        repos = self._repo_paths(set(found))
        ledgers = {"claude": jsonl_rows(self.root / "memory_ledger.jsonl")}
        ledgers["codex"] = jsonl_rows(self.legacy / "memory_ledger.jsonl") if self.legacy != self.root else []
        imported = {r.get("migration_key") for r in ledgers["claude"]}
        actions, folders = [], {}  # each destination folder as the actions before leave it: repos may share one
        for key in sorted(found):
            home, via = self._notes_home(key, repos.get(key))
            place = str(home.resolve())
            if place not in folders:
                folders[place] = self._destination(home)
            notes, index = folders[place]
            for host, path in found[key]:
                if path.parent.resolve() == home.resolve():
                    continue
                action = {"action": "notes", "host": host, "repo": key, "path": str(path), "home": str(home)}
                action["digest"] = digest(path)
                # The identity names the source version and its destination, never the planned outcome, which can
                # change before apply; a removed import never comes back.
                action["key"] = hashlib.sha256(json.dumps(action, sort_keys=True).encode()).hexdigest()
                if action["key"] in imported:
                    continue
                raw = path.read_bytes()
                action.update(via=via, writer=self._writer(host, path, raw, ledgers[host]))
                dest, result, text = self._notes_choice(action, raw, notes, index)
                action.update(dest=str(dest), result=result, bytes=len(raw))
                if result in ("import", "conflict"):
                    notes[dest.name] = text.encode()
                    index = _edited(index, self._index_edit(dest, text)) or index
                if not self._receipt(action).exists():
                    actions.append(action)
            folders[place] = (notes, index)
        return actions

    @staticmethod
    def _writer(host: str, path: Path, raw: bytes, rows: list[dict]) -> str:
        """Which app's nowitknows wrote a note: its source tag, else the ledger row that wrote this version (or the last
        that wrote the note), else the app whose store holds it; both apps write the shared store."""
        from .memory import SOURCES, parse_topic

        text = raw.decode("utf-8", "replace")
        if (tag := parse_topic(text).get("source")) in SOURCES:
            return tag.rsplit("-", 1)[1]
        rows = [r for r in rows if r.get("store") == "project" and r.get("path") == str(path)]
        rows = [r for r in rows if r.get("host") in ("claude", "codex")]
        return ([r for r in rows if r.get("after") == text] or rows or [{"host": host}])[-1]["host"]

    @staticmethod
    def _index_edit(dest: Path, text: str, rows: list[dict] | None = None) -> dict:
        from .memory import index_line, parse_topic, recorded_line

        meta = parse_topic(text)
        line = index_line(dest.stem, meta.get("description") or meta.get("name") or dest.stem)
        recorded = recorded_line(rows or [], dest)[1]
        return {
            "name": dest.stem,
            "line": line,
            "recorded": recorded,
            "limit": C.INDEX_RESERVE_LINES,
            "bytes": C.INDEX_RESERVE_BYTES,
        }

    def _overflow(self, action: dict) -> Path:
        return (
            self.saved
            / "overflow/notes"
            / action["host"]
            / action["repo"]
            / action["digest"]
            / Path(action["path"]).name
        )

    def _notes_choice(self, action: dict, raw: bytes, notes: dict[str, bytes], index: bytes) -> tuple[Path, str, str]:
        """(destination, result, text written): `duplicate` of a note there, `import`, `conflict` (a free suffixed
        name), `overflow` (kept outside: past a limit or unsafe), or `linked` (a link, which breaks Claude's writes)."""
        from .memory import tag_source
        from .safety import scan

        folder, name, host = Path(action["home"]), Path(action["path"]).name, action.get("writer", action["host"])
        if folder.is_symlink() or (folder / "MEMORY.md").is_symlink():
            return folder, "linked", ""
        try:
            text = tag_source(raw.decode("utf-8"), host)
        except UnicodeDecodeError:
            return self._overflow(action), "overflow", ""
        for existing, content in sorted(notes.items()):
            with suppress(UnicodeDecodeError):
                # Compare source-neutral copies in memory; the note written below keeps its original owned tag.
                if tag_source(content.decode("utf-8"), host, preserve_owned=False) == tag_source(
                    text, host, preserve_owned=False
                ):
                    return folder / existing, "duplicate", ""
        fold = native._case_insensitive(folder)
        taken = {native._fold(n, fold) for n in notes}
        dest, result = folder / name, "import"
        if native._fold(name, fold) in taken:
            base, n = (
                Path(name).stem[: C.NOTE_COPY_STEM] + "-" + host + "-" + action["digest"][: C.FINGERPRINT_CHARS],
                1,
            )
            while native._fold(dest.name, fold) in taken:
                dest, n = folder / (f"{base}.md" if n == 1 else f"{base}-{n}.md"), n + 1
            result = "conflict"
        try:
            _edited(index, self._index_edit(dest, text))
        except IndexConflict:
            return self._overflow(action), "overflow", ""
        if len(text.encode()) > C.TOPIC_FILE_MAX or scan(text):
            return self._overflow(action), "overflow", ""
        return dest, result, text

    def _notes(self, action: dict) -> str | None:
        from .memory import commit, copies, mutation, notes_lock

        source, folder = Path(action["path"]), Path(action["home"])
        lock = notes_lock(self.root, folder)
        with mutation(self.root, lock, own=action["key"]):  # finishes this import's interrupted receipt first
            if why := self._blocked(action):  # refused: nothing is written until that folder is as recorded
                self._notice(
                    f"blocked:{action['key']}:{why}", f"{notify.PREFIX} {self.describe({**action, 'blocked': why})}"
                )
                return why
            rows = jsonl_rows(self.root / "memory_ledger.jsonl")
            done = next((r for r in rows if r.get("migration_key") == action["key"]), None)
            if done is None:
                if digest(source) != action["digest"]:
                    raise ValueError(f"migration source changed: {source}; retry later")
                raw = source.read_bytes()
                notes, index = self._destination(folder)
                dest, result, text = self._notes_choice(action, raw, notes, index)
                if result == "linked":  # nothing is written; the note stays where it is
                    self._notice(action["key"], f"{notify.PREFIX} {self.describe({**action, 'result': result})}")
                    return None
                done = {
                    "ts": time.time(),
                    "host": action.get("writer", action["host"]),
                    "store": "project",
                    "action": "import",
                    "name": dest.stem,
                    "before": None,
                    "after": text if result in ("import", "conflict") else None,
                    "path": str(dest),
                    "source": "migration",
                    "migration_key": action["key"],
                    "result": result,
                    "original": str(source),
                }
                if result == "overflow":
                    durable_write(dest, raw)  # verbatim, outside Claude Code's folder
                writes = {str(dest): text} if result in ("import", "conflict") else {}
                edits = {str(folder / "MEMORY.md"): self._index_edit(dest, text, rows)} if writes else {}
                for edit in edits.values():
                    edit["present"] = copies(index, edit["line"])  # kept in the receipt for a replay
                if writes:
                    done["index_line"] = edits[str(folder / "MEMORY.md")]["line"]
                    done["digest"] = hashlib.sha256(text.encode()).hexdigest()
                try:
                    commit(
                        self.root,
                        writes,
                        [done],
                        lock=lock,
                        before=dict.fromkeys(writes),
                        index_edits=edits,
                        shared=True,
                    )
                except IndexConflict:  # Claude Code filled the index since the check: keep the note outside instead
                    dest = self._overflow(action)
                    durable_write(dest, raw)
                    done.update(name=dest.stem, after=None, path=str(dest), result="overflow")
                    done.pop("index_line", None)
                    done.pop("digest", None)
                    commit(self.root, {}, [done], lock=lock)
            self._notes_mark(action, done)
        return None

    def _settle_imports(self, actions: list[dict]) -> None:
        """Before any import runs, close each interrupted import whose action is no longer planned (sharing went off, or
        its source or folder changed): a note that landed is recorded as it is, else its line is taken back."""
        from .memory import IndexConflict, _abandon, _moved, _refuse, edit_index, imports

        planned = {action["key"] for action in actions if action.get("action") == "notes"}
        with file_lock(self.root / "memory_ledger.jsonl"):
            for receipt in sorted((self.root / "memory-writes").glob("*.json")):
                value = _json(receipt, {})
                if not imports(value) or value.get("done") or imports(value) & planned:
                    continue
                if moved := _moved(value):  # its folder became a link or another folder: left as it is
                    _refuse(receipt, value, moved)
                    continue
                with file_lock(Path(value["lock"])):
                    if any(
                        text is not None and Path(path).is_file() and Path(path).read_bytes() == text.encode()
                        for path, text in value["writes"].items()
                    ):  # the note is there already: recorded as it is, its index line as Claude Code left it
                        _abandon(self.root, receipt, value, "superseded after its note landed; recorded as it is")
                        continue
                    for path, edit in value.get("index_edits", {}).items():
                        retract = {"name": edit["name"], "line": None, "claim": edit["line"]}
                        with suppress(IndexConflict):
                            edit_index(Path(path), {**retract, "present": edit.get("present")}, replay=True)
                    error = "superseded: the migration planned this note again for its current folder, or dropped it"
                    durable_write(receipt, json.dumps({**value, "done": True, "error": error}))

    def _notes_mark(self, action: dict, row: dict) -> None:
        """The old copy stays where it is, beside a marker naming where the note went."""
        if row["result"] in ("conflict", "overflow"):
            what = (
                "kept beside Claude Code's note of the same name"
                if row["result"] == "conflict"
                else "kept outside Claude Code's notes, past the index or note limits or with unsafe text"
            )
            self._notice(action["key"], f"{notify.PREFIX} migrated project note {what} {notify.link(row['path'])}")
        source = Path(action["path"])
        durable_write(
            source.parent / f"{MARKER}{source.stem}-{action['key'][: C.FINGERPRINT_CHARS]}", row["path"] + "\n"
        )

    def describe(self, action: dict) -> str:
        kind = action["action"]
        if kind == "retain-skill":
            return (
                f"retain skill {action['name']} at {action['path']}: relative links escape folder "
                f"({', '.join(action['links'])}); no move or link; notify"
            )
        if kind == "skill":
            sources = ", ".join(c["path"] for c in action["copies"])
            suffix = "; different versions retained, newer wins with notice" if action["conflict"] else ""
            return (
                f"skill {action['name']}: snapshot {sources}; "
                f"canonical from {action['winner']['host']}; link both hosts{suffix}"
            )
        if kind == "memory":
            return (
                f"memory {action['target']}: {action['result']} entry {action['key'][: C.FINGERPRINT_CHARS]} "
                f"({len(action['entry'])} chars)"
            )
        if kind == "notes":
            why = action.get("blocked") or self._blocked(action)  # as things are now: nothing about it is kept
            outcome = {
                "import": f"copy to {action['dest']}",
                "conflict": f"copy to {action['dest']}, since Claude Code's note keeps the name",
                "duplicate": f"already there as {action['dest']}; nothing copied",
                "overflow": f"keep at {action['dest']} with a notice, outside Claude Code's notes",
                "linked": f"leave in place: {action['home']} is a link, which breaks Claude Code's own writes",
            }[action["result"]]
            if why:
                outcome = (
                    f"wait: an interrupted import cannot go on ({why}); it goes on once that folder is back as it was"
                )
            home = f". Notes folder: {action['home']} ({action['via']})" if action.get("via") else ""
            app = {"claude": "Claude Code", "codex": "Codex"}.get(action.get("writer", action["host"]), "nowitknows")
            return (
                f"notes {action['repo']}: project note {action['path']} (saved by nowitknows in {app}, "
                f"{action['bytes']} bytes, version {action['digest'][: C.FINGERPRINT_CHARS]}): {outcome}; "
                f"the old copy stays where it is{home}"
            )
        if kind in ("copy", "topic"):
            return f"import {kind} {action['path']} -> {action['dest']}; retain conflicting versions"
        if kind == "database":
            return (
                f"merge database-only exchanges from {action['path']} via SQLite backup snapshot; "
                "host-scoped fuller-record deduplication"
            )
        return f"re-index both hosts into {action['path']}"

    def runtime_work(self) -> list[Path]:
        paths = set()
        for folder in (
            "pending",
            "codex-jobs",
            "staged",
            "mod/queue",
            "mod/steps",
            "mod/answers",
            "mod/continuations",
            "paid-replies",
        ):
            paths.update((self.legacy / folder).glob("*.json"))
        paths.update((self.legacy / "mod").rglob("active.json"))
        return sorted(paths)

    def _notice(self, key: str, line: str) -> None:
        path = self.saved / "notices" / (hashlib.sha256(key.encode()).hexdigest() + ".json")
        value = _json(path, {})
        if not value:
            value = {"key": "migration:" + key, "line": line}
            durable_write(path, json.dumps(value))
        if not value.get("done"):
            notify.push(self.cfg, value["line"], key=value["key"], permanent=True)
            durable_write(path, json.dumps({**value, "done": True}))

    def _flush_notices(self) -> None:
        for path in sorted((self.saved / "notices").glob("*.json")):
            value = _json(path, {})
            if not value.get("done"):
                notify.push(self.cfg, value["line"], key=value["key"], permanent=True)
                durable_write(path, json.dumps({**value, "done": True}))

    def _from_legacy(self, action: dict) -> bool:
        kind = action["action"]
        if kind in ("memory", "copy", "topic", "database"):
            return True
        if kind == "skill":
            return any(copy["host"] == "codex" for copy in action["copies"])
        if kind == "notes":
            return action["host"] == "codex"
        return kind == "retain-skill" and Path(action["path"]).expanduser().resolve().is_relative_to(
            self._codex_skills()
        )

    def _codex_skills(self) -> Path:
        """Codex's skills folder, chosen the way codex_host does: the skills override, else the profile's `skills`."""
        env = self.cfg.env
        profile = C.env("NOWITKNOWS_CODEX_HOME", env) or env.get("CODEX_HOME") or C.home_dir(env) / ".codex"
        return Path(C.env("NOWITKNOWS_CODEX_SKILLS_DIR", env) or Path(profile) / "skills").expanduser().resolve()

    def _receipt(self, action: dict) -> Path:
        key = hashlib.sha256(json.dumps(_identity(action), sort_keys=True).encode()).hexdigest()
        return self.saved / "receipts" / (key + ".json")

    def _save(self, state: dict) -> None:
        durable_write(self.journal, json.dumps(state, indent=2))

    def _snapshot(self, source: Path, dest: Path, expected: str) -> None:
        if dest.exists():
            if digest(dest) != expected:
                raise ValueError(f"migration snapshot changed: {dest}")
            return
        if digest(source) != expected:
            raise ValueError(f"source changed since migration plan: {source}; retry after inspecting the journal")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + "-pending-" + uuid.uuid4().hex)
        shutil.copytree(source, tmp, symlinks=True)
        tmp.rename(dest)

    def _skill(self, action: dict) -> None:
        store, name = SkillStore(self.cfg), action["name"]
        with nullcontext() if self._locked else file_lock(store.ledger):
            key = self._receipt(action).stem
            if any(r.get("migration_action") == key for r in jsonl_rows(store.ledger)):
                return
            sources = []
            for copy in action["copies"]:
                if copy["host"] != "shared":
                    source = Path(copy["path"])
                    ledger = (self.legacy if copy["host"] == "codex" else self.root) / "skills_ledger.jsonl"
                    ownership = _made(ledger, copy["host"], source.parent).get(name)
                    if ownership is None or (copy.get("ownership") and ownership != copy["ownership"]):
                        raise ValueError(f"skill ownership changed during migration: {source}")
                    sources.append(ownership)
                self._snapshot(
                    Path(copy["path"]),
                    self.saved / "skills" / copy["host"] / f"{name}-{copy['digest']}",
                    copy["digest"],
                )
            winner = action["winner"]
            snapshot = self.saved / "skills" / winner["host"] / f"{name}-{winner['digest']}"
            canonical = store.home / name
            if canonical.is_symlink():
                raise ValueError(f"refusing linked canonical folder: {canonical}")
            if canonical.exists() and digest(canonical) != winner["digest"]:
                expected = next((c["digest"] for c in action["copies"] if c["host"] == "shared"), None)
                if digest(canonical) != expected:
                    raise ValueError(f"canonical skill changed during migration: {canonical}")
                canonical.rename(self.saved / f"replaced-{name}-{uuid.uuid4().hex}")
            if not canonical.exists():
                store.home.mkdir(parents=True, exist_ok=True)
                tmp = store.home / f".migration-{uuid.uuid4().hex}"
                shutil.copytree(snapshot, tmp, symlinks=True)
                tmp.rename(canonical)
            self._rebase_claude(store, action)
            for copy in action["copies"]:
                source = Path(copy["path"])
                if source == canonical or source.is_symlink() or not source.exists():
                    continue
                if digest(source) != copy["digest"]:
                    raise ValueError(f"host skill changed during migration: {source}")
                source.rename(self.saved / f"original-{copy['host']}-{name}-{uuid.uuid4().hex}")
            store.link(name)
            rows = jsonl_rows(store.ledger) + jsonl_rows(self.legacy / "skills_ledger.jsonl")
            kept = any(
                r.get("name") == name
                and (
                    r.get("action") in ("keep", "restore")
                    or (r.get("action") == "create" and r.get("source") == "user")
                )
                for r in rows
            )
            if action["conflict"]:
                self._notice(
                    key,
                    f"{notify.PREFIX} migrated conflicting skill {name}; newer copy is canonical; "
                    f"all versions retained {notify.link(self.saved / 'skills')}",
                )
            store._git(name, f"nowitknows: migrate {name} (shared)")
            append_line(
                store.ledger,
                json.dumps(
                    {
                        "ts": time.time(),
                        "host": self.cfg.host,
                        "action": "keep" if kept else "create",
                        "name": name,
                        "source": "user" if kept else "migration",
                        "path": str(canonical / "SKILL.md"),
                        "migration": winner["digest"],
                        "migration_action": key,
                        "migration_sources": sources,
                        "legacy_ledger": str(
                            (self.legacy if any(c["host"] == "codex" for c in action["copies"]) else self.root)
                            / "skills_ledger.jsonl"
                        ),
                    }
                ),
            )

    def _rebase_claude(self, store: SkillStore, action: dict) -> None:
        original = next((c for c in action["copies"] if c["host"] == "claude"), None)
        if not original:
            return
        saved = self.saved / "skills/claude" / f"{action['name']}-{original['digest']}"

        def rebase(change: dict) -> dict:
            if change.get("name") != action["name"] or change.get("folder") != original["path"]:
                return change
            path = store.file(change["name"], change.get("file_path", ""), canonical=True)
            if path is None:
                return change
            before = saved / (change.get("file_path") or "SKILL.md")
            text = before.read_text(encoding="utf-8-sig") if before.is_file() else None
            current = path.read_text(encoding="utf-8-sig") if path.is_file() else None
            if sha(text) != change.get("base") or sha(current) != change.get("base"):
                return change
            return {**change, "folder": str((store.home / change["name"]).resolve())}

        for folder in (self.root / "staged", self.root / "mod/steps"):
            for path in sorted(folder.glob("*.json")):
                try:
                    value = _json(path, {})
                except (OSError, ValueError):
                    continue
                if (
                    not isinstance(value, dict)
                    or not isinstance(value.get("env", {}), dict)
                    or value.get("host", "claude") != "claude"
                    or C.env("NOWITKNOWS_HOST", C.environment(value.get("env", {})), "claude") != "claude"
                ):
                    continue
                updated = (
                    rebase(value)
                    if folder.name == "staged"
                    else {
                        **value,
                        **{
                            key: rebase(value[key])
                            for key in ("proposed", "change")
                            if isinstance(value.get(key), dict)
                        },
                    }
                )
                if updated != value:
                    durable_write(path, json.dumps(updated))

    def _memory(self, action: dict, state: dict) -> None:
        from .memory import commit, mutation

        path = self.root / "memory" / f"{action['target']}.md"
        ledger = self.root / "memory_ledger.jsonl"
        with mutation(self.root, path):
            if not any(r.get("migration_key") == action["key"] for r in jsonl_rows(ledger)):
                entries, entry = _entries(path), action["entry"]
                result, writes = "duplicate", {}
                if entry not in entries:
                    limit = C.USER_CAP if action["target"] == "USER" else C.GLOBAL_MEMORY_CAP
                    if len(DELIM.join([*entries, entry])) <= limit:
                        writes[str(path)] = DELIM.join([*entries, entry]) + "\n"
                        result = "add"
                    else:
                        path = self.saved / "overflow" / f"{action['target']}-{action['key']}.md"
                        writes[str(path)] = entry + "\n"
                        result = "archive"
                commit(
                    self.root,
                    writes,
                    [
                        {
                            "ts": time.time(),
                            "host": "codex",
                            "store": "global",
                            "target": action["target"].lower(),
                            "action": result,
                            "after": entry,
                            "source": "migration",
                            "path": str(path),
                            "migration_key": action["key"],
                        }
                    ],
                )
            row = next(r for r in jsonl_rows(ledger) if r.get("migration_key") == action["key"])
            if row["action"] == "archive":
                self._notice(action["key"], f"{notify.PREFIX} memory overflow retained {notify.link(row['path'])}")
            if action["key"] not in state.setdefault("memory_imported", []):
                state["memory_imported"].append(action["key"])

    def _copy(self, action: dict) -> None:
        source, dest = Path(action["path"]), Path(action["dest"])
        lock = self.root / "archive" if source.is_relative_to(self.legacy / "archive") else dest
        with file_lock(lock):
            if digest(source) != action["digest"]:
                raise ValueError(f"migration source changed: {source}; retry later")
            content = source.read_bytes()
            if dest.exists() and dest.read_bytes() != content:
                dest = self.saved / "conflicts" / action["digest"] / Path(action["dest"]).relative_to(self.root)
                self._notice(str(dest), f"{notify.PREFIX} retained conflicting migration file {notify.link(dest)}")
            durable_write(dest, content)

    def _topic(self, action: dict) -> None:
        from .memory import commit, index_line, mutation, parse_topic

        source, dest = Path(action["path"]), Path(action["dest"])
        key = self._receipt(action).stem
        index = dest.parent / "MEMORY.md"
        with mutation(self.root, index):
            previous = next(
                (r for r in jsonl_rows(self.root / "memory_ledger.jsonl") if r.get("migration_key") == key), None
            )
            if previous:
                self._topic_notice(key, previous)
                return
            if digest(source) != action["digest"]:
                raise ValueError(f"migration source changed: {source}; retry later")
            text = source.read_text(encoding="utf-8-sig")
            conflict = dest.exists() and dest.read_text(encoding="utf-8-sig") != text
            if conflict:
                candidate = dest.with_name(
                    dest.stem[: C.NOTE_COPY_STEM] + "-codex-" + action["digest"][: C.FINGERPRINT_CHARS] + ".md"
                )
                dest, suffix = candidate, 2
                while dest.exists() and dest.read_text(encoding="utf-8-sig") != text:
                    dest = candidate.with_stem(candidate.stem + f"-{suffix}")
                    suffix += 1
            topic = parse_topic(text)
            lines = index.read_text(encoding="utf-8").splitlines() if index.exists() else []
            if not any(f"]({dest.name})" in line for line in lines):
                lines.append(index_line(dest.stem, topic.get("description") or topic.get("name") or dest.stem))
            index_text = "\n".join(lines) + "\n"
            overflow = len(lines) > C.INDEX_MAX_LINES or len(index_text.encode()) > C.INDEX_MAX_BYTES
            if overflow:
                dest = self.saved / "overflow" / "projects" / source.relative_to(self.legacy / "projects")
            writes = {str(dest): text}
            if not overflow:
                writes[str(index)] = index_text
            commit(
                self.root,
                writes,
                [
                    {
                        "ts": time.time(),
                        "host": "codex",
                        "store": "project",
                        "action": "import",
                        "name": dest.stem,
                        "after": text,
                        "path": str(dest),
                        "source": "migration",
                        "migration_key": key,
                        "result": "overflow" if overflow else "conflict" if conflict else "import",
                    }
                ],
                lock=index,
            )
            self._topic_notice(
                key, {"result": "overflow" if overflow else "conflict" if conflict else "import", "path": str(dest)}
            )

    def _topic_notice(self, key: str, row: dict) -> None:
        if row["result"] in ("conflict", "overflow"):
            self._notice(key, f"{notify.PREFIX} retained project memory {row['result']} {notify.link(row['path'])}")

    @staticmethod
    def _generation(path: Path) -> list:
        return [
            [p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns]
            if p.exists() and (p == path or p.stat().st_size)
            else None
            for p in (path, Path(str(path) + "-wal"))
        ]

    def _database(self, action: dict, journal: dict) -> None:
        snapshot = self.saved / "sessions-codex.db"
        original = Path(action["path"]).expanduser().resolve()
        generation = self._generation(original)
        stamp = snapshot.with_suffix(".generation.json")
        if not snapshot.exists() or _json(stamp, []) != generation:
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            tmp = snapshot.with_name(snapshot.name + "-" + uuid.uuid4().hex)
            source = sqlite3.connect(original.resolve().as_uri() + "?mode=ro", uri=True)
            target = sqlite3.connect(tmp)
            try:
                source.backup(target)
                target.close()
                sync_file(tmp)
                if self._generation(original) != generation:
                    raise ValueError("legacy database changed during backup; retry")
                tmp.replace(snapshot)
                durable_write(stamp, json.dumps(generation))
            finally:
                target.close()
                source.close()
                tmp.unlink(missing_ok=True)
                for side in ("-wal", "-shm"):  # SQLite makes them for the read-only snapshot too
                    noted(Path(str(original) + side))
        source = sessions.connect(snapshot, legacy_host="codex")
        target = sessions.connect(self.cfg.path("sessions.db"), self.cfg.legacy_host)
        try:
            source.row_factory = sqlite3.Row
            with target:
                target.execute("begin immediate")
                for row in source.execute("select * from sessions"):
                    host = row["host"] or "codex"
                    sid = row["id"] if row["id"].startswith(host + ":") else host + ":" + row["id"]
                    target.execute(
                        "insert or ignore into sessions(id,project,path,host,repo,mtime,size) values (?,?,?,?,?,-1,0)",
                        (sid, row["project"], row["path"], host, row["repo"]),
                    )
                    incoming = [
                        dict(r)
                        for r in source.execute(
                            "select uuid,ts,text from exchanges where session_id=? order by id", (row["id"],)
                        )
                    ]
                    sessions.merge_exchanges(target, sid, incoming)
                    target.execute(
                        "update sessions set n=(select count(*) from exchanges where session_id=?),"
                        "first_ts=(select min(ts) from exchanges where session_id=?),"
                        "last_ts=(select max(ts) from exchanges where session_id=?),repo=coalesce(repo,?) where id=?",
                        (sid, sid, sid, row["repo"], sid),
                    )
            sessions.reread_imported(target)  # merged rows carry no copied flags; their transcripts restore them
        finally:
            source.close()
            target.close()
        if self._generation(original) != generation:
            raise ValueError("legacy database changed during import; retry")
        journal["database_imported"] = True

    def index_only(self) -> bool:
        """Durable knowledge is imported and only the re-index remains: workers rebuild that derived data under their
        own locks, so it needs no quiet period."""
        state = _json(self.journal, {})
        if (self.legacy.exists() and not state.get("legacy_worker_ran")) or self._recovering():
            return False
        pending = self.plan()
        return bool(pending) and self._only_index(pending)

    def _only_index(self, pending: list[dict]) -> bool:
        # A fresh scan too: sources added after an interrupted apply are not in its journal.
        return all(action["action"] == "index" for action in [*pending, *self.plan(fresh=True)])

    def _recovering(self) -> bool:
        """Durable work an interrupted run left: a skill write, or an import of a note into Claude Code's folder."""
        from .memory import imports

        if any(path for data in (self.root, self.legacy) for path in (data / "skill-writes").glob("*.json")):
            return True
        receipts = (_json(path, {}) for path in (self.root / "memory-writes").glob("*.json"))
        return any(imports(value) and not value.get("done") for value in receipts)

    def apply(self) -> list[str]:
        from .codex_host import configuration, settle_legacy

        unfenced = self.index_only()
        view = self.fence()
        # An index-only run waives what older writers left, never a held lock or an inspection error.
        if view["blocked"] and (not unfenced or any(host["held"] or host["errors"] for host in view["hosts"])):
            raise ValueError("Migration refused without writes. " + self.describe_fence(view))
        self.plan()
        with file_lock(self.root / "migration"), ExitStack() as finish:
            written = finish.enter_context(own_writes())
            finish.callback(self._record_own, written)  # also when interrupted
            state = _json(self.journal, {})
            if self.legacy.exists() and not state.get("legacy_worker_ran"):
                settle_legacy(self.cfg, self.legacy)
                state["legacy_worker_ran"] = True
            retained = self.runtime_work()
            if retained:
                self._notice(
                    "runtime:" + hashlib.sha256(str(retained).encode()).hexdigest(),
                    f"{notify.PREFIX} {len(retained)} legacy runtime records remain untouched at "
                    f"{notify.link(self.legacy)}; migrate imports durable knowledge only",
                )
            self._flush_notices()
            with (
                file_lock(self.root / "codex-worker"),
                file_lock(self.root / "worker"),
                file_lock(self.root / "skills_ledger.jsonl"),
                ExitStack() as legacy_locks,
            ):
                recovering = {
                    _json(path, {})["entry"]["name"]
                    for data in (self.root, self.legacy)
                    for path in (data / "skill-writes").glob("*.json")
                }
                if self.legacy.exists():
                    for name in ("codex-worker", "worker", "skills_ledger.jsonl", "memory_ledger.jsonl"):
                        legacy_locks.enter_context(file_lock(self.legacy / name))
                # With every lock held and before any recovery replays a skill write, so unfenced runs write none.
                if unfenced and (recovering or self._recovering()):
                    raise ValueError(
                        "Migration refused: durable work appeared after the re-index check. " + UPGRADE_INSTRUCTIONS
                    )
                if self.legacy.exists():
                    old = configuration(
                        {
                            **self.cfg.env,
                            "NOWITKNOWS_CODEX_DATA_DIR": str(self.legacy),
                            "NOWITKNOWS_LEGACY_STORE": str(self.legacy),
                            "NOWITKNOWS_LEGACY_HOST": "codex",
                        }
                    )
                    SkillStore(old)._recover_applications()
                self._locked = True
                try:
                    SkillStore(self.cfg)._recover_applications()
                    actions = self.plan(fresh=bool(recovering))
                    if unfenced and not self._only_index(actions):
                        raise ValueError(
                            "Migration refused: durable work appeared after the re-index check. " + UPGRADE_INSTRUCTIONS
                        )
                    if recovering:
                        for action in self._current(state.get("pending", [])):
                            if action not in actions and not (
                                action["action"] == "skill" and action["name"] in recovering
                            ):
                                actions.append(action)
                        actions.sort(key=lambda action: action["action"] == "index")
                    self._settle_imports(actions)
                    state.update(version=C.MIGRATION_VERSION, pending=actions)
                    self._save(state)
                    done = []
                    for action in list(actions):
                        receipt = self._receipt(action)
                        if receipt.exists():
                            state.update(_json(receipt, {})["markers"])
                        else:
                            kind = action["action"]
                            if kind == "skill":
                                self._skill(action)
                            elif kind == "retain-skill":
                                self._notice(receipt.stem, self.describe(action))
                            elif kind == "memory":
                                self._memory(action, state)
                            elif kind == "copy":
                                self._copy(action)
                            elif kind == "topic":
                                self._topic(action)
                            elif kind == "notes" and (why := self._notes(action)):  # it ran; blocked if `why`
                                done.append(self.describe({**action, "blocked": why}))
                                continue  # stays pending, with no receipt, for a later run
                            elif kind == "database":
                                self._database(action, state)
                            elif kind == "index":
                                index_all(self.cfg)
                                state["indexed"] = True
                            durable_write(
                                receipt, json.dumps({"markers": {k: v for k, v in state.items() if k != "pending"}})
                            )
                        done.append(self.describe(action))
                        state["pending"].remove(action)
                        self._save(state)
                    return done
                finally:
                    self._locked = False


def index_all(cfg: C.Config) -> int:
    from .codex_host import configuration, index_profile

    source = dict(cfg.env)
    source.pop("NOWITKNOWS_TRANSCRIPTS_DIR", None)
    home = C.home_dir(source)
    profile = Path(C.env("NOWITKNOWS_CODEX_HOME", source) or source.get("CODEX_HOME") or home / ".codex")
    separate = C.env("NOWITKNOWS_CODEX_DATA_DIR", source)
    shared = Path(C.env("NOWITKNOWS_SHARED_DATA_DIR", source) or cfg.data).expanduser().resolve()
    isolated = (
        separate and Path(separate).expanduser().resolve() != shared.expanduser().resolve()
    ) or profile.expanduser().resolve() != (home / ".codex").resolve()
    if not isolated:
        source.setdefault("NOWITKNOWS_CODEX_DATA_DIR", str(shared))
    codex = configuration(source)
    total = index_profile(codex)
    shared.mkdir(parents=True, exist_ok=True)
    connection = sessions.connect(shared / "sessions.db", "claude")
    try:
        claude = Path(C.env("NOWITKNOWS_CLAUDE_TRANSCRIPTS_DIR", cfg.env) or C.home_dir(cfg.env) / ".claude/projects")
        total += sessions.ingest(
            connection,
            [
                cfg.transcripts if cfg.host == "claude" else claude,
                claude,
                shared / "transcripts",
                shared / "archive",
                shared / "codex/transcripts",
                shared / "codex/archive",
                shared / "migration/conflicts",
            ],
        )
    finally:
        connection.close()
    return total


def run(cfg: C.Config, *, apply: bool = False) -> int:
    try:
        migration = Migration(cfg)
        actions = migration.apply() if apply else [migration.describe(a) for a in migration.plan()]
    except SeparateStore as error:
        print(f"Migration skipped: {error}")
        return 0
    except ValueError as error:
        print(str(error))
        return 1
    except TimeoutError as error:  # a worker took a lock after the fence; the journal keeps every finished step
        print(f"Migration paused: a nowitknows worker is busy ({error}). Nothing is lost; run it again in a minute.")
        return 1
    if not apply and not actions and not migration.legacy.exists():
        print("Migration: nothing to migrate.")
        return 0
    print("Migration applied:" if apply else "READ-ONLY migration plan (use --apply to execute):")
    unfenced = not apply and migration.index_only()
    if unfenced:
        print("Only the search re-index remains; it rebuilds derived data and runs without the quiet period.")
    elif not apply:
        print(migration.describe_fence(migration.fence()))
        print("- run the legacy Codex worker once to settle durable paid answers in place; no new model calls")
    for line in actions:
        print("- " + line)
    retained = migration.runtime_work()
    for path in retained:
        print(f"- retain legacy runtime work untouched: {path}")
    fence = (
        "" if apply else "; apply needs no quiet period" if unfenced else "; apply requires the migration quiet fence"
    )
    print(f"{len(retained)} runtime records retained{fence}.")
    print(f"{len(actions)} actions. Legacy store retained: {migration.legacy}")
    return 0
