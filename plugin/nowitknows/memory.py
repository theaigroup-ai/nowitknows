from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from contextlib import ExitStack, contextmanager, nullcontext, suppress
from functools import wraps
from itertools import count
from pathlib import Path

from . import config as C
from .delivery import finish_delivery
from .safety import imperative, redact, scan, scan_change
from .state import _replace, append_line, atomic_write, durable_write, file_lock, jsonl_rows, keep_mode, temp_file

DELIM = "\n§\n"
_SPLIT = re.compile(r"\n\s*§\s*\n")
HEADERS = {
    "user": "USER PROFILE (who the user is)",
    "memory": "GLOBAL MEMORY (environment facts and standing conventions)",
}
TYPES = ("user", "feedback", "project", "reference")
_NAME = re.compile(rf"[a-z0-9][a-z0-9_-]{{0,{C.NOTE_NAME_MAX - 1}}}")  # Claude Code names its own topics in snake_case
SOURCES = (*C.aliases("nowitknows-claude"), *C.aliases("nowitknows-codex"), *C.aliases("nowitknows-cursor"))
_FRONTMATTER = re.compile(r"\A(﻿?---[ \t]*\r?\n)(.*?)(\r?\n---[ \t]*(?:\r?\n|\Z))", re.S)


def read_generation(path: Path) -> tuple[bytes | None, dict | None]:
    try:
        stat = path.stat()
        content = path.read_bytes()
        return content, {
            "inode": stat.st_ino,
            "mtime_ns": stat.st_mtime_ns,
            "digest": hashlib.sha256(content).hexdigest(),
        }
    except FileNotFoundError:
        return None, None


def _generation(path: Path) -> dict | None:
    return read_generation(path)[1]


ANY = object()  # no expectation about the version of a note a write replaces


def fingerprint(content: bytes | None) -> str | None:
    """A note's version: the hash of its bytes, None when there is no such file."""
    return hashlib.sha256(content).hexdigest() if content is not None else None


def notes_lock(data: Path, root: Path) -> Path:
    """nowitknows's lock for a notes folder it shares with Claude Code. It lives in nowitknows's data, which both hosts
    share, so Claude Code's folder holds only notes."""
    return data / "notes-locks" / hashlib.sha256(str(root.resolve()).encode()).hexdigest()[: C.LOCK_NAME_CHARS]


class IndexConflict(ValueError):
    """An index edit refused: MEMORY.md is at a limit nowitknows keeps (`limit` names it), or it kept changing."""

    def __init__(self, message: str, limit: str = "changing"):
        super().__init__(message)
        self.limit = limit


def _lines(text: str) -> list[str]:
    """MEMORY.md's lines with their endings, split on newlines only, as Claude Code counts them."""
    return [part for part in re.split(r"(?<=\n)", text) if part]


def _ending(line: str) -> str:
    return "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""


def _recorded(rows: list[dict], path: Path, field: str) -> tuple[bool, str | None]:
    """(whether nowitknows's ledger records it, the last `field` a write of the note at `path` recorded). A row that
    changed no index line, or wrote no version, carries no such field and leaves the record as it was."""
    for row in reversed(rows):
        if field in row and row.get("store") == "project" and row.get("path") == str(path):
            return True, row[field]
    return False, None


def recorded_line(rows: list[dict], path: Path) -> tuple[bool, str | None]:
    """The exact index line nowitknows last wrote for the note at `path` (None when it keeps none)."""
    return _recorded(rows, path, "index_line")


def written(rows: list[dict], path: Path, content: bytes | None) -> bool:
    """Whether the note at `path` is still exactly the version nowitknows last wrote there (its fingerprint)."""
    digest = _recorded(rows, path, "digest")[1]
    return content is not None and digest == hashlib.sha256(content).hexdigest()


def copies(content: bytes | None, line: str | None) -> int:
    """How many lines of MEMORY.md are exactly `line`: read before an edit, it tells a replay what the edit added."""
    return sum(text.rstrip("\r\n") == line for text in _lines((content or b"").decode("utf-8", "replace")))


def _entry(lines: list[str], edit: dict, replay: bool) -> int | None:
    """Where nowitknows's one entry for the note is in Claude Code's folder: the last line exactly as it last wrote it,
    or on replay a copy of its proposal beyond the `present` count read before the edit."""
    link, proposal = f"]({edit['name']}.md)", edit.get("claim", edit.get("line"))
    if replay and proposal is not None and isinstance(edit.get("present"), int):
        same = [i for i, text in enumerate(lines) if text.rstrip("\r\n") == proposal]
        if len(same) > edit["present"]:  # the interrupted edit landed: one copy more than were there before it
            return same[-1]
    if edit.get("recorded") is not None:
        found = [i for i, text in enumerate(lines) if link in text and text.rstrip("\r\n") == edit["recorded"]]
        return found[-1] if found else None
    return None


def _owned(lines: list[str], edit: dict, replay: bool) -> list[int]:
    """The lines that are nowitknows's for the note: in its separate store every line linking it, as in 0.7.2; in
    Claude Code's folder only its entry (`_entry`), so every other line there stays."""
    if edit.get("links"):
        return [i for i, text in enumerate(lines) if f"]({edit['name']}.md)" in text]
    own = _entry(lines, edit, replay)
    return [] if own is None else [own]


def foreign_lines(content: bytes | None, edit: dict) -> list[str]:
    """Lines linking the note that are not nowitknows's entry for it: nowitknows leaves them as they are."""
    lines = _lines((content or b"").decode("utf-8", "replace"))
    own, link = _owned(lines, edit, False), f"]({edit['name']}.md)"
    return [text.rstrip("\r\n") for i, text in enumerate(lines) if link in text and i not in own]


def _edited(content: bytes | None, edit: dict, replay: bool = False) -> bytes | None:
    """MEMORY.md after adding, replacing or dropping nowitknows's entry for one note (None when nothing changes); every
    other line keeps its bytes and place. Raises IndexConflict past the line limit, or the byte limit as it grows."""
    old = content or b""
    lines = _lines(old.decode("utf-8", "surrogateescape"))
    line, owned = edit.get("line"), _owned(lines, edit, replay)
    if owned:  # an identical copy elsewhere is someone else's line and stays
        if line is not None:
            lines[owned[-1]] = line + _ending(lines[owned[-1]])
            owned = owned[:-1]
        for i in reversed(owned):
            del lines[i]
    elif line is None:
        return None
    else:
        limit = min(edit.get("limit", C.INDEX_MAX_LINES), C.INDEX_MAX_LINES)
        if len(lines) >= limit:
            room = (
                f"nowitknows adds index lines only up to {limit} of {C.INDEX_MAX_LINES}, keeping the rest for "
                "Claude Code's own notes"
                if limit < C.INDEX_MAX_LINES
                else "the most an index holds"
            )
            raise IndexConflict(
                f"MEMORY.md has {len(lines)} lines and {room}; remove or shorten stale notes in the same call to free "
                "a line",
                f"{limit} lines",
            )
        if lines and not _ending(lines[-1]):
            lines[-1] += "\n"
        lines.append(line + "\n")
    text = "".join(lines).encode("utf-8", "surrogateescape")
    cap = min(edit.get("bytes", C.INDEX_MAX_BYTES), C.INDEX_MAX_BYTES)
    if len(text) > cap and len(text) > len(old):  # a line that does not grow, or goes, always passes
        room = (
            f"nowitknows adds to it only up to {cap} of its {C.INDEX_MAX_BYTES} bytes, keeping the rest for Claude "
            "Code's own notes"
            if cap < C.INDEX_MAX_BYTES
            else "the most an index holds"
        )
        raise IndexConflict(
            f"MEMORY.md would be {len(text)} bytes and {room}; remove or shorten stale notes in the same call to free "
            f"{len(text) - cap}",
            f"{cap} bytes",
        )
    return None if text == old else text


def _sync_folder(folder: Path) -> None:
    if C.WINDOWS:
        return
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_unchanged(path: Path, data: bytes, generation: dict | None, shared: bool = False) -> bool:
    """Atomically replace `path` with `data` only while it still has `generation`; False when it changed. A `shared`
    file (Claude Code's folder) keeps the mode it had."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = temp_file(path.parent, shared)
    try:
        if shared:
            keep_mode(fd, path)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if _generation(path) != generation:
            return False
        _replace(tmp, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(tmp)
    _sync_folder(path.parent)
    return True


def edit_index(path: Path, edit: dict, replay: bool = False, shared: bool = False) -> None:
    """Apply one note's index edit, reading MEMORY.md again on each try and replacing it only at an unchanged
    generation, since Claude Code takes no nowitknows lock; after INDEX_EDIT_TRIES changed reads it fails closed."""
    for _ in range(C.INDEX_EDIT_TRIES):
        content, generation = read_generation(path)
        text = _edited(content, edit, replay)
        if text is None or _replace_unchanged(path, text, generation, shared):
            return
    raise IndexConflict(
        "MEMORY.md kept changing while nowitknows edited it; nothing was saved; call the memory tool again"
    )


def _diverged(path: str, value: dict) -> bool:
    """Another writer changed this file since it was read, to something other than the intended version."""
    current, text = _generation(Path(path)), value["writes"][path]
    after = hashlib.sha256(text.encode()).hexdigest() if text is not None else None
    return current != value.get("before", {}).get(path) and (current or {}).get("digest") != after


def _finish(data: Path, receipt: Path, value: dict, replay: bool = False) -> None:
    if value.get("atomic") and any(_diverged(path, value) for path in value["writes"]):
        raise IndexConflict("global memory changed during the batch; intended text remains in its receipt")
    conflicts = dict(value.get("conflicts", {}))
    conflict_dir = data / "memory-conflicts" / receipt.stem
    topics = [r["path"] for r in value["rows"] if r.get("store") == "project" and r.get("path") in value["writes"]]
    # A note another writer changed keeps that version (below), so its index line is left as that writer has it.
    indexed = not any(_diverged(path, value) for path in topics)
    if indexed:
        for path, edit in value.get("index_edits", {}).items():
            edit_index(Path(path), edit, replay, value.get("shared", False))
    for path, text in value["writes"].items():
        current = _generation(Path(path))
        after = hashlib.sha256(text.encode()).hexdigest() if text is not None else None
        if (current or {}).get("digest") == after:
            continue
        if value.get("atomic"):
            if current != value["before"][path] or (
                path == value["lock"] and not _replace_unchanged(Path(path), text.encode(), current)
            ):
                raise IndexConflict("global memory changed during the batch; intended text remains in its receipt")
            if path == value["lock"]:
                continue
        # Native memory writers do not take our locks. Never replay over a newer generation.
        if "before" not in value or current != value["before"][path]:
            saved = conflict_dir / (hashlib.sha256(path.encode()).hexdigest() + ".md")
            if text is not None:
                durable_write(saved, text)
            conflicts[path] = {"intended": str(saved) if text is not None else None, "current": current}
            continue
        if text is None:
            Path(path).unlink(missing_ok=True)
        else:
            durable_write(Path(path), text, shared=value.get("shared", False) and path not in value.get("private", []))
    # A row records the index line nowitknows now keeps for its note only when the index edit ran, and the version it
    # wrote only when that version landed.
    _record(
        data,
        [
            {
                **{
                    k: v
                    for k, v in r.items()
                    if (indexed or k != "index_line") and (r.get("path") not in conflicts or k != "digest")
                },
                **({"recovery_conflict": conflicts[r["path"]]} if r.get("path") in conflicts else {}),
            }
            for r in value["rows"]
        ],
    )
    if conflicts:
        from . import notify

        durable_write(conflict_dir / "manifest.json", json.dumps(conflicts, indent=2))
        host = next((r["host"] for r in value["rows"] if r.get("host")), "claude")
        # The notice needs only nowitknows's data and the host; the home is the writer's if it recorded one, else none
        # of the real machine's, so that nothing outside nowitknows's data is looked at.
        home = {"HOME": str(data), "USERPROFILE": str(data), **(value.get("home") or {}).get("env", {})}
        notify.push(
            C.load({"NOWITKNOWS_DATA_DIR": str(data), "NOWITKNOWS_HOST": host, **home}, create=False),
            f"{notify.PREFIX} newer memory retained; interrupted versions saved {notify.link(conflict_dir)}",
            key="memory-conflict:" + receipt.stem,
            permanent=True,
        )
    durable_write(receipt, json.dumps({**value, "conflicts": conflicts, "done": True}))


def _record(data: Path, rows: list[dict]) -> None:
    """Add to the ledger the rows of a write it does not have yet."""
    ledger = data / "memory_ledger.jsonl"
    known = {r.get("write_id") for r in jsonl_rows(ledger)}
    fresh = [r for r in rows if r["write_id"] not in known]
    if fresh:
        content = ledger.read_bytes() if ledger.exists() else b""
        durable_write(
            ledger,
            content
            + (b"\n" if content and not content.endswith(b"\n") else b"")
            + b"".join((json.dumps(r) + "\n").encode() for r in fresh),
        )


def _close(receipt: Path, value: dict, error: IndexConflict) -> None:
    durable_write(receipt, json.dumps({**value, "done": True, "error": str(error)}))


def _abandon(data: Path, receipt: Path, value: dict, error: str) -> None:
    """Close a write whose folder is no longer where it goes, with `error`: nothing more is written there, and what
    landed is recorded where it is, without its effect id, so a retry writes the note where notes go now."""
    rows = []
    for row in value["rows"]:
        path = row.get("path")
        if path not in value["writes"]:
            continue
        text, index = value["writes"][path], Path(path).parent / "MEMORY.md"
        note = (_generation(Path(path)) or {}).get("digest") == (
            hashlib.sha256(text.encode()).hexdigest() if text is not None else None
        )
        edit = value.get("index_edits", {}).get(str(index))
        if edit is None or edit.get("line") is None:
            line = note  # a removal takes its line out before the note
        else:
            present = copies(read_generation(index)[0], edit["line"])
            line = isinstance(edit.get("present"), int) and present > edit["present"]
        if note or line:
            kept = {
                k: v
                for k, v in row.items()
                if k != "effect_id" and (note or k != "digest") and (line or k != "index_line")
            }
            rows.append({**kept, "abandoned": error})
    _record(data, rows)
    durable_write(receipt, json.dumps({**value, "done": True, "error": error}))


def _identity(folder: Path) -> dict:
    stat = folder.stat()
    return {"real": os.path.realpath(folder), "dev": stat.st_dev, "ino": stat.st_ino}


def _moved(value: dict) -> str | None:
    """An interrupted write must not resume into a replacement folder or follow a newly introduced link."""
    for path in [*value.get("writes", {}), *value.get("index_edits", {})]:
        target, folder = Path(path), Path(path).parent
        seen = value.get("folders", {}).get(str(folder))
        if target.is_symlink() or folder.is_symlink() or not folder.is_dir():
            return f"not finished: {folder} is a link or no folder now; nothing was written there"
        if seen and _identity(folder) != seen:
            return f"not finished: {folder} is not the folder this write was for; nothing was written there"
    return None


def _refuse(receipt: Path, value: dict, error: str) -> None:
    """Leave an interrupted write open with why it cannot go on; it goes on once its folder is as recorded."""
    if value.get("error") != error:
        durable_write(receipt, json.dumps({**value, "error": error}))


def imports(value: dict) -> set[str]:
    """The migration imports a receipt belongs to (their `migration_key`)."""
    return {r["migration_key"] for r in value.get("rows", []) if r.get("migration_key")}


def _writer(home: dict) -> dict:
    return {k: v for k, v in home.items() if k != "folder"}


@contextmanager
def mutation(data: Path | None, path: Path, own: str | None = None, home: dict | None = None):
    """Finish interrupted writes under both locks so another writer cannot race recovery.
    Imports resume only through their own import; project writes stay bound to their recorded notes folder."""
    with ExitStack() as locks:
        if data:
            locks.enter_context(file_lock(data / "memory_ledger.jsonl"))
            for receipt in sorted((data / "memory-writes").glob("*.json")):
                value = json.loads(receipt.read_text(encoding="utf-8"))
                if value.get("done") or not imports(value) <= {own}:
                    continue
                if moved := _moved(value):  # checked before anything, its lock included, is written
                    _refuse(receipt, value, moved)
                    continue
                went = value.get("home")
                if went and not (home and Path(went["folder"]).resolve() == Path(home["folder"]).resolve()):
                    if home and _writer(went) == _writer(home):
                        error = (
                            f"not finished: these notes now go to {home['folder']}, so nowitknows wrote nothing more"
                        )
                        with file_lock(Path(value["lock"])):
                            _abandon(data, receipt, value, f"{error} to {went['folder']}")
                    continue  # another writer's notes may still go there
                with file_lock(Path(value["lock"])):
                    # A refused index edit applied nothing, and a receipt left open would block every later write.
                    try:
                        _finish(data, receipt, value, replay=True)
                    except IndexConflict as error:
                        _close(receipt, value, error)
        locks.enter_context(file_lock(path))
        yield


def commit(
    data: Path | None,
    writes: dict[str, str | None],
    rows: list[dict],
    *,
    lock: Path | None = None,
    before: dict[str, dict | None] | None = None,
    index_edits: dict[str, dict] | None = None,
    home: dict | None = None,
    shared: bool = False,
    private: tuple[str, ...] = (),
    atomic: bool = False,
) -> None:
    """`shared`: the notes are in Claude Code's folder, so a file keeps its mode and a new one gets the default, all
    but the `private` ones, which are nowitknows's own."""
    index_edits = index_edits or {}
    if not data:
        for path, edit in index_edits.items():
            edit_index(Path(path), edit, shared=shared)
        for path, text in writes.items():
            if text is None:
                Path(path).unlink(missing_ok=True)
            else:
                atomic_write(Path(path), text, shared=shared and path not in private)
        return
    key = uuid.uuid4().hex
    lock = (
        str(lock)
        if lock
        else (
            (str(Path(rows[0]["path"]).parent / "MEMORY.md") if rows[0].get("store") == "project" else rows[0]["path"])
            if rows
            else next(iter(writes))
        )
    )
    folders = {Path(path).parent for path in [*writes, *index_edits]}
    for folder in folders:  # a replay checks it is still this folder
        folder.mkdir(parents=True, exist_ok=True)
    value = {
        **({"atomic": True} if atomic else {}),
        "lock": lock,
        "folders": {str(folder): _identity(folder) for folder in folders},
        "writes": writes,
        "before": {
            path: before[path] if before is not None and path in before else _generation(Path(path)) for path in writes
        },
        "rows": [{**r, "write_id": f"{key}:{i}"} for i, r in enumerate(rows)],
        "index_edits": index_edits,
        **({"home": home} if home else {}),
        **({"shared": True} if shared else {}),
        **({"private": list(private)} if private else {}),
    }
    for row in value["rows"]:  # every project version nowitknows replaces, removes or writes stays restorable
        content = row.get("before") if row.get("before") is not None else row.get("after")
        if row.get("store") == "project" and content is not None:
            backup = data / "memory-history" / key / Path(row["path"]).name
            durable_write(backup, content)
            row["backup"] = str(backup)
    receipt = data / "memory-writes" / (key + ".json")
    durable_write(receipt, json.dumps(value))
    try:
        _finish(data, receipt, value)
    except IndexConflict as error:
        _close(receipt, value, error)
        raise


USER_DEFAULTS = (
    "This user may run one chat through many compactions: track tasks and long-running work durably so nothing is lost "
    "across them.",
    "Put anything the user should know or act on in the reply text; tool and hook output may go unread.",
    "A request made once is not a permanent rule: verify what can be checked or measured instead of assuming.",
)
DEFAULT_MARKER = " (default: confirm with this user)"
MASKED_WRITE_ERROR = "the value was masked in your copy; preserve it without copying [redacted] into new text"


class EntryStore:
    def __init__(
        self,
        root: Path,
        limits: dict | None = None,
        ledger_dir: Path | None = None,
        host: str = "claude",
        *,
        create=True,
    ):
        self.root = root
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        self.limits = {"user": C.USER_CAP, "memory": C.GLOBAL_MEMORY_CAP, **(limits or {})}
        self.ledger_dir = ledger_dir
        self.host = host

    def seed_user(self) -> dict:
        return self.apply(
            "user",
            [{"action": "add", "content": text + DEFAULT_MARKER} for text in USER_DEFAULTS],
            source="default",
            only_if_empty=True,
        )

    def rewrite(
        self,
        target: str,
        text: str,
        version: dict,
        run_id: str | None,
        effect_id: str,
        *,
        locked=False,
        notes: dict[str, str] | None = None,
    ) -> dict:
        path = self.file(target)
        with nullcontext() if locked else mutation(self.ledger_dir, path):
            if any(r.get("effect_id") == effect_id for r in jsonl_rows(self.ledger_dir / "memory_ledger.jsonl")):
                return {"ok": True, "replayed": True}
            before, current = read_generation(path)
            if current != version:
                return {"ok": False, "stale": True}
            if text.count("[redacted]") > (before or b"").decode("utf-8-sig").count("[redacted]"):
                return {"ok": False, "error": MASKED_WRITE_ERROR}
            entries = [e.strip() for e in _SPLIT.split(text) if e.strip()]
            if target == "memory":
                from .global_notes import LINK

                added = [e for e in entries if (m := LINK.search(e)) and m[1] in (notes or {})]
                if sorted(e for e in entries if LINK.search(e) and e not in added) != sorted(
                    e for e in self.entries(target) if LINK.search(e)
                ) or set(notes or {}) != {LINK.search(e)[1] for e in added}:
                    return {"ok": False, "error": "a tidy must retain existing global note hooks and links exactly"}
            after = DELIM.join(entries) + ("\n" if entries else "")
            if (
                not entries
                or len(after.strip()) >= self.size(self.entries(target))
                or self.size(entries) > self.limits[target]
                or scan(after)
            ):
                return {"ok": False, "error": "a tidy must be safe, shorter, nonempty and within the block cap"}
            writes, versions = {str(path): after}, {str(path): version}
            if notes:
                from .global_notes import GlobalNotes

                global_notes = GlobalNotes(self)
                if target != "memory" or not global_notes.safe_paths():
                    return {"ok": False, "error": "invalid tidy notes"}
                for name, body in notes.items():
                    if not re.fullmatch(r"g-[a-f0-9]+", name):
                        return {"ok": False, "error": "invalid tidy note name"}
                    destination = global_notes.root / f"{name}.md"
                    if destination.is_symlink():
                        return {"ok": False, "error": "global notes must not be links"}
                    present = global_notes._snapshot(destination, versions)
                    if present is not None and present != body:
                        return {"ok": False, "error": "tidy note collision"}
                    writes[str(destination)] = body
                writes = {**{p: b for p, b in writes.items() if p != str(path)}, str(path): after}
            try:
                commit(
                    self.ledger_dir,
                    writes,
                    [
                        {
                            "ts": time.time(),
                            "host": self.host,
                            "store": "global",
                            "target": target,
                            "action": "tidy",
                            "before": before.decode("utf-8"),
                            "after": after,
                            "source": "background",
                            "run_id": run_id,
                            "effect_id": effect_id,
                            "path": str(path),
                            **({"notes": notes} if notes else {}),
                        }
                    ],
                    before=versions,
                    lock=path,
                    atomic=bool(notes),
                )
            except IndexConflict:
                return {"ok": False, "stale": True}
        return {"ok": True, "path": str(path)}

    def file(self, target: str) -> Path:
        if target not in self.limits:
            raise ValueError(f"unknown target {target!r}")
        return self.root / ("USER.md" if target == "user" else "MEMORY.md")

    def entries(self, target: str) -> list[str]:
        p = self.file(target)
        if not p.exists():
            return []
        return [e.strip() for e in _SPLIT.split(p.read_text(encoding="utf-8-sig")) if e.strip()]

    @staticmethod
    def size(entries: list[str]) -> int:
        return len(DELIM.join(entries))

    def _fail(self, target: str, current: list[str], msg: str) -> dict:
        return {
            "ok": False,
            "target": target,
            "error": msg,
            "usage": f"{self.size(current)}/{self.limits[target]}",
        }

    def apply(
        self,
        target: str,
        ops: list[dict],
        source: str = "session",
        run_id: str | None = None,
        *,
        effect_id: str | None = None,
        only_if_empty: bool = False,
    ) -> dict:
        if target == "memory":
            from .global_notes import GlobalNotes

            return GlobalNotes(self).apply(ops, source, run_id, effect_id)
        path = self.file(target)
        with mutation(self.ledger_dir, path):
            if effect_id and any(
                r.get("effect_id") == effect_id for r in jsonl_rows(self.ledger_dir / "memory_ledger.jsonl")
            ):
                return {"ok": True, "replayed": True}
            current = self.entries(target)
            initialized = self.root / ".user-initialized"
            if only_if_empty:
                if initialized.exists():
                    return {"ok": True, "seeded": False}
                if current:
                    atomic_write(initialized, "profile examined\n")
                    return {"ok": True, "seeded": False}
            new, warnings, changes = list(current), [], []
            for i, op in enumerate(ops):
                action = op.get("action")
                content = (op.get("content") or "").strip()
                old = (op.get("old_text") or "").strip()
                if action in ("add", "replace"):
                    if "[redacted]" in content:
                        return self._fail(target, current, f"op {i}: {MASKED_WRITE_ERROR}")
                    if not content:
                        return self._fail(target, current, f"op {i}: empty content")
                    if _SPLIT.search(f"\n{content}\n"):  # it would read back as several entries
                        return self._fail(
                            target, current, f"op {i}: a line holding only § separates entries; add one fact per op"
                        )
                    if err := scan(content):
                        return self._fail(target, current, f"op {i}: {err}")
                    if imperative(content):
                        warnings.append(f"op {i}: write a declarative fact, not an instruction")
                if action == "add":
                    if content in new:
                        warnings.append(f"op {i}: duplicate skipped")
                        continue
                    new.append(content)
                    changes.append(("add", None, content))
                elif action in ("replace", "remove"):
                    if not old:
                        return self._fail(target, current, f"op {i}: old_text required")
                    hits = [j for j, e in enumerate(new) if old in e]
                    if len(hits) != 1:
                        return self._fail(
                            target, current, f"op {i}: old_text matched {len(hits)} entries; must match exactly one"
                        )
                    before = new[hits[0]]
                    if action == "replace":
                        new[hits[0]] = content
                        changes.append(("replace", before, content))
                    else:
                        new.pop(hits[0])
                        changes.append(("remove", before, None))
                else:
                    return self._fail(target, current, f"op {i}: unknown action {action!r}")
            total, limit = self.size(new), self.limits[target]
            if total > limit:
                return self._fail(
                    target,
                    current,
                    f"would be {total}/{limit} chars; remove or shorten stale entries in the same batch "
                    f"to free {total - limit}",
                )
            commit(
                self.ledger_dir,
                {str(path): DELIM.join(new) + ("\n" if new else "")},
                [
                    {
                        "ts": time.time(),
                        "host": self.host,
                        "store": "global",
                        "target": target,
                        "action": action,
                        "before": before,
                        "after": after,
                        "source": source,
                        "run_id": run_id,
                        "effect_id": effect_id,
                        "path": str(path),
                    }
                    for action, before, after in (changes or ([("unchanged", None, None)] if effect_id else []))
                ],
            )
            if only_if_empty:
                atomic_write(initialized, "profile examined\n")
        return {
            "ok": True,
            "target": target,
            "path": str(path),
            "usage": f"{total}/{limit}",
            "changes": len(changes),
            "warnings": warnings,
        }

    def render(self, target: str, *, bounded: bool = True) -> str:
        with mutation(self.ledger_dir, self.file(target)) if self.ledger_dir else nullcontext():
            return self._render(target, bounded=bounded)

    def _render(self, target: str, *, bounded: bool = True) -> str:
        entries = self.entries(target)
        if not entries:
            return ""
        body = DELIM.join(entries)
        pct = round(100 * len(body) / self.limits[target])
        return f"=== {HEADERS[target]} [{pct}% of {self.limits[target]} chars] ===\n{body}"

    def snapshot(self) -> str:
        with mutation(self.ledger_dir, self.file("memory")) if self.ledger_dir else nullcontext():
            return "\n\n".join(b for b in (self._render("user"), self._render("memory")) if b)


def parse_topic(text: str) -> dict:
    meta, body = {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            mm = re.match(r"^\s*(name|description|type|source):\s*(.*)$", line)
            if mm:
                meta[mm.group(1)] = mm.group(2).strip().strip("'\"")
    return {**meta, "body": body.strip()}


def nowitknows_note(text: str | None) -> bool:
    """A note nowitknows wrote: its frontmatter names nowitknows as metadata.source."""
    return text is not None and parse_topic(text).get("source") in SOURCES


def ownership(ledger_dir: Path | None, shared: bool):
    """Shared folders contain Claude's own notes, so only unchanged notes written by nowitknows are delivered whole."""
    rows: list[dict] | None = None

    def owns(path: Path, raw: bytes) -> bool:
        nonlocal rows
        if not shared:
            return True
        if not nowitknows_note(raw.decode("utf-8", "replace")):
            return False
        if rows is None:
            rows = jsonl_rows(ledger_dir / "memory_ledger.jsonl") if ledger_dir else []
        return written(rows, path, raw)

    return owns


def _topic_read_handle(name: str) -> str:
    # The colon cannot occur in a topic name, so a handle cannot shadow a real topic.
    return "topic:" + hashlib.sha256(name.encode()).hexdigest()


def memory_read_pointer(name: str = "", host: str | None = None) -> str:
    if name and redact(name) != name:
        name = _topic_read_handle(name)
    operations = [{"action": "read", **({"name": name} if name else {})}]
    prefix = "mcp__nowitknows__" if host == "codex" else C.session_tools()
    return redact(f"Read via {prefix}memory target 'project', operations={json.dumps(operations)}")


def note_pointer(path: Path, raw: bytes, cfg: C.Config | None = None) -> str | None:
    """One line for a note nowitknows does not deliver whole, never its body: its path, size and redacted description
    (else its name), checked like any text nowitknows injects; None when even that is not safe."""
    meta = parse_topic(raw.decode("utf-8", "replace"))
    original = meta.get("description") or ""
    clean = redact(original)
    about = " ".join(clean.split())[: C.NOTE_POINTER_CHARS]
    if not about or scan(about):
        original = meta.get("name") or path.stem
        clean = redact(original)
        about = " ".join(clean.split())[: C.NOTE_POINTER_CHARS]
    line = f"{path} ({len(raw)} bytes): {about}"
    shown = redact(line)
    return None if scan(shown) else finish_delivery(shown, clean != original or shown != line, cfg)


def tag_source(text: str, host: str, *, preserve_owned: bool = True) -> str:
    """Preserve owned tags; tag other imports with the current host, leaving every other byte unchanged. Inline
    metadata mappings are returned as they are, since a second metadata key would break their YAML."""
    if preserve_owned and nowitknows_note(text):  # keep existing bytes and the import identities derived from them
        return text
    source = f"nowitknows-{host}"
    m = _FRONTMATTER.match(text)
    if not m:
        return f"---\nmetadata:\n  source: {source}\n---\n\n{text}"
    newline = "\r\n" if m.group(1).endswith("\r\n") else "\n"
    lines = m.group(2).splitlines()
    out, i, placed = [], 0, False
    while i < len(lines):
        out.append(lines[i])
        if not placed and re.fullmatch(r"metadata:\s*", lines[i]):
            j, children = i + 1, []
            while j < len(lines) and (lines[j][:1] in (" ", "\t") or not lines[j].strip()):
                if not re.match(r"\s+source\s*:", lines[j]):
                    children.append(lines[j])
                j += 1
            indent = next((c[: len(c) - len(c.lstrip())] for c in children if c.strip()), "  ")
            out += [f"{indent}source: {source}", *children]
            i, placed = j, True
            continue
        i += 1
    if not placed:
        if any(re.match(r"metadata\s*:", line) for line in lines):
            return text
        out += ["metadata:", f"  source: {source}"]
    return m.group(1) + newline.join(out) + m.group(3) + text[m.end() :]


def render_topic(name: str, description: str, mtype: str, body: str, host: str = "claude") -> str:
    return (
        f"---\nname: {name}\ndescription: {description}\nmetadata:\n  type: {mtype}\n"
        f"  source: nowitknows-{host}\n---\n\n{body.strip()}\n"
    )


def index_line(name: str, description: str) -> str:
    line = f"- [{name}]({name}.md) - {description.strip().replace('](', '] (')}"
    return line if len(line) <= C.INDEX_LINE_MAX else line[: C.INDEX_LINE_MAX - 1] + "…"


def topic_error(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except IndexConflict as error:
            return {"ok": False, "error": str(error), "refused": error.limit}

    return wrapped


class TopicStore:
    """Native auto-memory layout: MEMORY.md index plus one file per fact. A store `shared` with Claude Code changes
    only the notes nowitknows wrote and their index lines, and keeps its lock in nowitknows's data."""

    def __init__(
        self,
        root: Path,
        ledger_dir: Path | None = None,
        host: str = "claude",
        *,
        shared: bool = False,
        writer: dict | None = None,
    ):
        self.root = root
        self.ledger_dir = ledger_dir
        self.host = host
        self.shared = shared
        self.writer = writer

    @property
    def home(self) -> dict | None:
        """This folder and who writes to it (host, repo, environment), kept with each write so that a replay goes only
        where notes still go."""
        return {"folder": str(self.root), **self.writer} if self.writer else None

    @property
    def index(self) -> Path:
        return self.root / "MEMORY.md"

    @property
    def lock(self) -> Path:
        return notes_lock(self.ledger_dir, self.root) if self.shared and self.ledger_dir else self.index

    def topics(self) -> list[Path]:
        return sorted(p for p in self.root.glob("*.md") if p.name != "MEMORY.md") if self.root.exists() else []

    def index_text(self, *, bounded: bool = True) -> str:
        if not self.index.exists():
            return ""
        # A shared index is Claude Code's file too; a byte that is not UTF-8 must not cost the hook its context.
        # Keep hidden separators inside their line so delivery screening can still refuse them.
        text = self.index.read_text(encoding="utf-8", errors="replace")
        if not bounded:
            return text
        lines = re.split(r"\r?\n", text.removesuffix("\n"))[: C.INDEX_MAX_LINES]
        return "\n".join(lines).encode()[: C.INDEX_MAX_BYTES].decode(errors="ignore")

    def _path(self, name: str) -> Path | None:
        """Topic file for a valid name; `memory` is reserved (MEMORY.md on case-insensitive filesystems)."""
        if not _NAME.fullmatch(name or "") or name == "memory":
            return None
        return self.root / f"{name}.md"

    def text(self, name: str) -> str | None:
        """The note's text, None when there is none (or no such name, or it is not text)."""
        if name and name.startswith("topic:"):
            name = next((p.stem for p in self.topics() if _topic_read_handle(p.stem) == name), "")
        path = self._path(name)
        try:
            return path.read_text(encoding="utf-8") if path else None
        except (OSError, UnicodeError):
            return None

    def snapshot(self, name: str) -> tuple[str | None, str | None]:
        """(the note's text, its fingerprint) from one read: (None, None) when there is no note; a note that is not text
        has a fingerprint and no text."""
        path = self._path(name)
        content = read_generation(path)[0] if path else None
        try:
            return (content.decode("utf-8") if content is not None else None), fingerprint(content)
        except UnicodeDecodeError:
            return None, fingerprint(content)

    def destination(self, name: str) -> str:
        """Resolve a write's final name before judging, including a redirect beside a note Claude Code changed."""
        path = self._path(name)
        return self._destination(self._rows(), path, read_generation(path)[0]).stem if path else name

    def landed(self, effect_id: str) -> dict | None:
        """The ledger row of the write with this effect id, once the writes a crash left open are finished."""
        with mutation(self.ledger_dir, self.lock, home=self.home):
            return next((r for r in self._rows() if r.get("effect_id") == effect_id), None)

    def problem(self, name: str, description: str = "", mtype: str = "", body: str = "", *, remove=False) -> str | None:
        """Why a write (or a remove, which needs only a name) can never succeed, before anything is judged or locked."""
        from .skills import DEVICE

        if self._path(name) is None:
            return (
                f"no topic {name!r}"
                if remove
                else "name: lowercase letters, digits, - or _, max 64 chars, not 'memory'"
            )
        if DEVICE.fullmatch(name) and not self._path(name).is_file():
            return f"no topic {name!r}" if remove else "new notes cannot use Windows device names in the shared store"
        if remove:
            return None
        if "[redacted]" in description or "[redacted]" in body:
            return MASKED_WRITE_ERROR
        if mtype not in TYPES:
            return f"type must be one of {TYPES}"
        description = " ".join((description or "").split())
        if not description:
            return "description required (one line)"
        if len(render_topic(name, description, mtype, body, self.host).encode()) > C.TOPIC_FILE_MAX:
            return f"topic file over {C.TOPIC_FILE_MAX} bytes; split or summarize"
        return None

    def _linked(self, path: Path) -> str | None:
        """A linked notes folder breaks Claude Code's own memory writes, so nowitknows writes none."""
        if any(p.is_symlink() for p in (self.root, path, self.index)):
            return "project notes must be real files and folders, not links"
        return None

    def _rows(self) -> list[dict]:
        return jsonl_rows(self.ledger_dir / "memory_ledger.jsonl") if self.ledger_dir else []

    def _free(self, rows: list[dict], name: str) -> Path:
        """The first `<name>-<host>[-<n>]` that is free or is still our unchanged note."""
        for n in count(1):
            suffix = f"-{self.host}" + (f"-{n}" if n > 1 else "")
            path = self.root / f"{name[: C.NOTE_NAME_MAX - len(suffix)]}{suffix}.md"
            content = read_generation(path)[0]
            if content is None or (
                nowitknows_note(content.decode("utf-8", "replace")) and written(rows, path, content)
            ):
                return path

    def _destination(self, rows: list[dict], path: Path, content: bytes | None) -> Path:
        try:
            before = content.decode("utf-8") if content is not None else None
        except UnicodeDecodeError:
            return path  # the write will refuse non-text notes as before
        if self.shared and before is not None and nowitknows_note(before) and not written(rows, path, content):
            return self._free(rows, path.stem)
        return path

    def _edit(self, rows: list[dict], path: Path, line: str | None) -> dict:
        """The index edit for one note, knowing the exact line nowitknows last wrote for it (kept in its ledger, never
        in Claude Code's folder)."""
        _, recorded = recorded_line(rows, path)
        limit = C.INDEX_RESERVE_LINES if self.shared else C.INDEX_MAX_LINES
        cap = C.INDEX_RESERVE_BYTES if self.shared else C.INDEX_MAX_BYTES
        return {
            "name": path.stem,
            "line": line,
            "recorded": recorded,
            "links": not self.shared,
            "limit": limit,
            "bytes": cap,
        }

    @topic_error
    def write(
        self,
        name: str,
        description: str,
        mtype: str,
        body: str,
        source: str = "session",
        run_id: str | None = None,
        *,
        effect_id: str | None = None,
        expect: str | object | None = ANY,
        destination: str | None = None,
    ) -> dict:
        """`expect`: the final note's fingerprint (None for no note); `destination`: its resolved name when judged.
        If either changes by the time the lock is held, leave it alone and return `stale`."""
        if error := self.problem(name, description, mtype, body):
            return {"ok": False, "error": error}
        path, description = self._path(name), " ".join((description or "").split())
        if linked := self._linked(path):
            return {"ok": False, "error": linked}
        with mutation(self.ledger_dir, self.lock, home=self.home):
            rows = self._rows()
            if effect_id and any(r.get("effect_id") == effect_id for r in rows):
                return {"ok": True, "replayed": True}
            previous, generation = read_generation(path)
            from .skills import DEVICE

            if previous is None and DEVICE.fullmatch(name):
                return {"ok": False, "error": "new notes cannot use Windows device names in the shared store"}
            moved = ""
            resolved = self._destination(rows, path, previous)
            if resolved != path:
                # Resolve the redirect before checking which note the caller judged.
                moved, path = path.name, resolved
                if linked := self._linked(path):
                    return {"ok": False, "error": linked}
                name, (previous, generation) = path.stem, read_generation(path)
            if (destination is not None and path.stem != destination) or (
                expect is not ANY and fingerprint(previous) != expect
            ):
                return {"ok": False, "stale": True, "error": f"{path.name} changed since it was judged"}
            try:
                before = previous.decode("utf-8") if previous is not None else None
            except UnicodeDecodeError:
                return {"ok": False, "error": f"{path.name} is not UTF-8 text; nowitknows leaves it as it is"}
            if self.shared and before is not None and not nowitknows_note(before):
                return {
                    "ok": False,
                    "error": f"{path.name} is Claude Code's own note, which nowitknows never changes; save this fact "
                    "under another name",
                }
            text = render_topic(name, description, mtype, body, self.host)
            if len(text.encode()) > C.TOPIC_FILE_MAX:
                return {"ok": False, "error": f"topic file over {C.TOPIC_FILE_MAX} bytes; split or summarize"}
            if err := scan_change(before, text):
                return {"ok": False, "error": err}
            edit = self._edit(rows, path, index_line(name, description))
            index = read_generation(self.index)[0]
            edit["present"] = copies(index, edit["line"])  # kept in the receipt for a replay
            _edited(index, edit)  # a full index refuses before anything is written
            kept = foreign_lines(index, edit)
            commit(
                self.ledger_dir,
                {str(path): text},
                [
                    {
                        "ts": time.time(),
                        "host": self.host,
                        "store": "project",
                        "action": "write" if before is None else "replace",
                        "name": name,
                        "before": before,
                        "after": text,
                        "source": source,
                        "run_id": run_id,
                        "effect_id": effect_id,
                        "path": str(path),
                        "index_line": edit["line"],
                        "digest": hashlib.sha256(text.encode()).hexdigest(),
                    }
                ],
                lock=self.lock,
                before={str(path): generation},
                index_edits={str(self.index): edit},
                home=self.home,
                shared=self.shared,
            )
        result = {"ok": True, "path": str(path)}
        if moved:
            result["note"] = (
                f"{moved} changed since nowitknows last wrote it, so it is Claude Code's now and stays as it is; "
                f"nowitknows saved this version as {path.name}"
            )
        if kept:
            result["index"] = (
                f"MEMORY.md also links {name}.md from a line nowitknows did not write or someone has changed "
                f"({kept[0][: C.INDEX_LINE_MAX]}); nowitknows left that line as it is and keeps its own entry for the "
                "note on a separate line"
            )
        return result

    @topic_error
    def remove(
        self,
        name: str,
        source: str = "session",
        run_id: str | None = None,
        *,
        effect_id: str | None = None,
        expect: str | object | None = ANY,
    ) -> dict:
        if error := self.problem(name, remove=True):
            return {"ok": False, "error": error}
        path = self._path(name)
        if linked := self._linked(path):
            return {"ok": False, "error": linked}
        with mutation(self.ledger_dir, self.lock, home=self.home):
            rows = self._rows()
            if effect_id and any(r.get("effect_id") == effect_id for r in rows):
                return {"ok": True, "replayed": True}
            previous, generation = read_generation(path)
            if expect is not ANY and fingerprint(previous) != expect:
                return {"ok": False, "stale": True, "error": f"{path.name} changed since it was judged"}
            if previous is None:
                return {"ok": False, "error": f"no topic {name!r}"}
            try:
                before = previous.decode("utf-8")
            except UnicodeDecodeError:
                return {"ok": False, "error": f"{path.name} is not UTF-8 text; nowitknows leaves it as it is"}
            if self.shared and not nowitknows_note(before):
                return {"ok": False, "error": f"{path.name} is Claude Code's own note, which nowitknows never removes"}
            if self.shared and not written(rows, path, previous):
                return {
                    "ok": False,
                    "error": f"{path.name} changed since nowitknows last wrote it, so it is Claude Code's now; "
                    "nowitknows never changes or removes it",
                }
            edit = self._edit(rows, path, None)
            kept = foreign_lines(read_generation(self.index)[0], edit)
            commit(  # the removed version is kept in memory-history (the row's backup)
                self.ledger_dir,
                {str(path): None},
                [
                    {
                        "ts": time.time(),
                        "host": self.host,
                        "store": "project",
                        "action": "remove",
                        "name": name,
                        "before": before,
                        "after": None,
                        "source": source,
                        "run_id": run_id,
                        "effect_id": effect_id,
                        "path": str(path),
                        "index_line": None,
                        "digest": None,
                    }
                ],
                lock=self.lock,
                before={str(path): generation},
                index_edits={str(self.index): edit},
                home=self.home,
                shared=self.shared,
            )
        result = {"ok": True, "path": str(path)}
        if kept:
            result["index"] = (
                f"MEMORY.md still links {name}.md from a line nowitknows did not write or someone has changed "
                f"({kept[0][: C.INDEX_LINE_MAX]}); nowitknows left that line as it is, so it points at the archived "
                "note until someone edits it"
            )
        return result


def refuse(
    cfg: C.Config,
    folder: Path,
    name: str,
    reason: str,
    run_id: str | None,
    limit: str | None = None,
    where: Path | None = None,
    change: str | None = None,
) -> None:
    """Replays must not duplicate a refused change or repeat its notice more than once per folder per day."""
    from . import notify

    log, today = cfg.data / C.REFUSALS_LOG, time.strftime("%Y-%m-%d")
    with file_lock(log):
        rows = jsonl_rows(log)
        if change and any(r.get("change") == change for r in rows):
            return
        said = any(r.get("folder") == str(folder) and r.get("day") == today and r.get("noticed") for r in rows)
        if not said:
            notify.push(cfg, notify.refused_line(name, reason, where or folder / "MEMORY.md", limit))
        row = {"ts": time.time(), "day": today, "host": cfg.host, "folder": str(folder), "name": name}
        append_line(
            log,
            json.dumps(
                {
                    **row,
                    "limit": limit,
                    "reason": reason,
                    "run_id": run_id,
                    "noticed": not said,
                    **({"change": change} if change else {}),
                }
            ),
        )


def note_refusals(
    cfg: C.Config, folder: Path, operations: list[dict], result: dict, run_id: str | None, change: str | None = None
) -> None:
    for op, r in zip(operations, result.get("results") or [], strict=False):
        if isinstance(op, dict) and isinstance(r, dict) and not r.get("ok"):
            name, why = str(op.get("name") or ""), str(r.get("error") or "not saved")
            refuse(cfg, folder, name, why, run_id, r.get("refused"), change=change)


def project_store(cfg: C.Config, folder: Path, shared: bool = False, project: str | None = None) -> TopicStore:
    env = {k: cfg.env[k] for k in C.NOTES_FOLDER_ENV if cfg.env.get(k)}
    writer = {"host": cfg.host, "project": project, "env": env} if project else None
    return TopicStore(folder, ledger_dir=cfg.data, host=cfg.host, shared=shared, writer=writer)


def apply_operations(
    cfg: C.Config,
    fallback_dir: Path,
    target: str,
    operations: list[dict],
    source: str,
    run_id: str | None,
    *,
    effect_id: str | None = None,
    shared: bool = False,
    project: str | None = None,
    expect: dict[str, str | None] | None = None,
    destinations: dict[str, str] | None = None,
) -> dict:
    """The same validated memory writes for the MCP tool and a tools-free reviewer; `expect` maps a note's name to the
    version the caller judged; `destinations` maps it to the resolved name (see TopicStore.write)."""
    if target in ("user", "memory"):
        from . import memorytidy

        if (
            source == "background"
            and memorytidy.assigned(cfg, run_id, target)
            and not (len(operations) == 1 and operations[0].get("action") == "tidy")
        ):
            return {"ok": False, "error": "an assigned tidy target requires one lossless-judged tidy operation"}
        if any(op.get("action") == "tidy" for op in operations):
            if source != "background" or len(operations) != 1:
                return {"ok": False, "error": "a tidy is one background operation for a supplied block"}
            return memorytidy.stage(
                cfg, target, operations[0].get("content"), run_id, effect_id, notes=operations[0].get("notes")
            )
        return EntryStore(cfg.data / "memory", ledger_dir=cfg.data, host=cfg.host).apply(
            target, operations, source, run_id, effect_id=effect_id
        )
    store, results = project_store(cfg, fallback_dir, shared, project), []
    for i, op in enumerate(operations):
        effect = f"{effect_id}:{i}" if effect_id else None
        judged = {"expect": expect[op.get("name")]} if expect and op.get("name") in expect else {}
        if op.get("action") == "write":
            results.append(
                store.write(
                    op.get("name", ""),
                    op.get("description", ""),
                    op.get("type", ""),
                    op.get("body", ""),
                    source,
                    run_id,
                    effect_id=effect,
                    destination=destinations.get(op.get("name")) if destinations else None,
                    **judged,
                )
            )
        elif op.get("action") == "remove":
            results.append(store.remove(op.get("name", ""), source, run_id, effect_id=effect, **judged))
        else:
            results.append({"ok": False, "error": f"unknown action {op.get('action')!r}"})
    return {"ok": all(r.get("ok") for r in results), "results": results}
