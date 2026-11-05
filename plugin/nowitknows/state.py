from __future__ import annotations

import contextvars
import json
import os
import stat
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path

from . import config as C

if C.WINDOWS:
    import msvcrt
else:
    import fcntl

SESSION_TTL_S = 30 * 86400
_OWN: contextvars.ContextVar[dict | None] = contextvars.ContextVar("own_writes", default=None)


def link_target(value: str) -> str:
    """Remove only the Windows namespace spelling, preserving the literal destination."""
    if os.name == "nt":
        if value.startswith("\\\\?\\UNC\\"):
            return "\\\\" + value[8:]
        if value.startswith("\\\\?\\"):
            return value[4:]
    return value


def _try_lock(fd: int) -> bool:
    try:
        if C.WINDOWS:
            os.lseek(fd, 0, os.SEEK_SET)  # msvcrt locks from the file position, so every holder must lock byte 0
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def lock_held(path: Path) -> bool:
    """Probe an existing lock without creating or writing a file; closing releases a successful probe."""
    try:
        fd = os.open(path.with_suffix(path.suffix + ".lock"), os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        return not _try_lock(fd)
    finally:
        os.close(fd)


@contextmanager
def file_lock(path: Path, wait_s: float = 5):
    """Exclusive OS lock on `<path>.lock`. The OS releases it if the holder dies, so there is no stale-lock logic."""
    lock = path.with_suffix(path.suffix + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    try:
        if C.WINDOWS and os.fstat(fd).st_size == 0:
            with suppress(OSError):  # a creator racing us may already hold byte 0, which refuses writes too
                os.write(fd, b"\0")
        deadline = time.monotonic() + wait_s
        while not _try_lock(fd):
            if time.monotonic() > deadline:
                raise TimeoutError(f"lock busy: {lock}")
            time.sleep(0.02)
        try:
            yield
        finally:
            if C.WINDOWS:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def temp_file(folder: Path, shared: bool = False) -> tuple[int, str]:
    """A new temporary file beside its target: private (mkstemp's 0600) or, `shared` with other programs, with the
    mode any new file gets (0666 less the umask)."""
    if not shared:
        return tempfile.mkstemp(dir=folder, prefix=".tmp-")
    name = str(folder / f".tmp-{uuid.uuid4().hex}")
    return os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666), name


def keep_mode(fd: int, replaced: Path) -> None:
    """The file that replaces `replaced` keeps its mode, so Claude Code's own 0644 notes stay as they were."""
    with suppress(FileNotFoundError, AttributeError):  # nothing to copy; or no fchmod (Windows)
        os.fchmod(fd, stat.S_IMODE(os.stat(replaced).st_mode))


def atomic_write(path: Path, data: str | bytes, *, shared: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = temp_file(path.parent, shared)
    try:
        if shared:
            keep_mode(fd, path)
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode("utf-8") if isinstance(data, str) else data)
        _replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise
    noted(path)


def sync_file(path: Path) -> None:
    """Flush a finished file to disk. Windows refuses fsync on a handle opened only for reading."""
    with path.open("r+b" if C.WINDOWS else "rb") as stream:
        os.fsync(stream.fileno())


def durable_write(path: Path, data: str | bytes, *, shared: bool = False) -> None:
    atomic_write(path, data, shared=shared)
    sync_file(path)
    if not C.WINDOWS:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _replace(src: str, dst: Path) -> None:
    """os.replace, retried briefly on Windows, which refuses it while another process has `dst` open."""
    for attempt in range(C.REPLACE_TRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not C.WINDOWS or attempt == C.REPLACE_TRIES - 1:
                raise
            time.sleep(C.REPLACE_RETRY_S)


def append_line(path: Path, line: str) -> None:
    """Append one line to a log; after a torn last line (a crash mid-append) it starts on a fresh line, so the new
    entry is not glued to the torn one and skipped with it. Callers serialize appends with a lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as f:
        end, torn = f.seek(0, os.SEEK_END), False
        if end:
            f.seek(end - 1)
            torn = f.read(1) != b"\n"
        f.write((b"\n" if torn else b"") + line.encode("utf-8") + b"\n")  # writes in "a" mode go to the end
    noted(path)


def mark(path: Path) -> list[int]:
    """What a later change of `path` alters: its mtime, ctime and size."""
    info = path.lstat()
    return [info.st_mtime_ns, info.st_ctime_ns, info.st_size]


@contextmanager
def own_writes():
    """While open, collect each file this process writes, with its mark as it is right after the write."""
    marks: dict[str, list[int]] = {}
    token = _OWN.set(marks)
    try:
        yield marks
    finally:
        _OWN.reset(token)


def noted(path: Path) -> None:
    """Record a write this process just made, inside `own_writes`; a no-op elsewhere."""
    marks = _OWN.get()
    if marks is not None:
        with suppress(OSError):
            marks[str(path)] = mark(path)


def iter_jsonl(path: Path):
    """The JSON objects of a log, one per line, read a line at a time. A torn or blank line (a crash mid-append, or
    appends interleaved on Windows) is skipped, so one bad line never breaks a reader."""
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return
    with handle:
        for chunk in handle:
            for line in chunk.splitlines():
                try:
                    row = json.loads(line)
                except ValueError:  # also undecodable bytes: UnicodeDecodeError is a ValueError
                    continue
                if isinstance(row, dict):
                    yield row


def jsonl_rows(path: Path) -> list[dict]:
    """Every row iter_jsonl yields."""
    return list(iter_jsonl(path))


def log_row(cfg: C.Config, filename: str, row: dict, *, wait_s: float = 5) -> None:
    path = cfg.path(filename)
    with file_lock(path, wait_s=wait_s):
        if row.get("step_id") and any(
            r.get("step_id") == row["step_id"] and r.get("host", cfg.legacy_host) == cfg.host for r in jsonl_rows(path)
        ):
            return
        if (
            filename == "runs.jsonl"
            and row.get("run_id")
            and any(
                r.get("run_id") == row["run_id"] and r.get("host", cfg.legacy_host) == cfg.host
                for r in jsonl_rows(path)
            )
        ):
            return
        append_line(path, json.dumps({**row, "host": cfg.host}))


def _empty() -> dict:
    return {
        "sessions": {},
        "usage": {},
        "usage_last": {},
        "projects": {},
        "project_sessions": {},
        "project_pids": {},
        "sessions_total": 0,
        "skill_activity": {},
        "worker": {},
    }


def count_session(data: dict, key: str, process: str | list[str], now: float, forget_after_s: float) -> bool:
    """One working session per repo per Claude Code process (or any of several keys naming one): /clear and /resume
    inside it add nothing, unless the process sat idle for `forget_after_s`."""
    seen = data["project_pids"].setdefault(key, {})
    for pid, ts in list(seen.items()):
        if now - ts > forget_after_s:
            del seen[pid]
    names = [process] if isinstance(process, str) else process
    counted = not any(n in seen for n in names)
    for n in names:
        seen[n] = now
    if counted:
        data["project_sessions"][key] = data["project_sessions"].get(key, 0) + 1
        data["sessions_total"] = data.get("sessions_total", 0) + 1
    return counted


def prune_projects(data: dict, now: float, forget_after_s: float) -> None:
    """Drop repo counters no skill record refers to once the repo has been quiet; the total stays."""
    used = {key for rec in data["skill_activity"].values() for key in rec.get("repos", {})}
    for key in list(data["project_sessions"]):
        recent = any(now - ts <= forget_after_s for ts in data["project_pids"].get(key, {}).values())
        if key not in used and not recent:
            data["project_sessions"].pop(key, None)
            data["project_pids"].pop(key, None)


def _total(data: dict) -> int:
    return data.get("sessions_total", 0)


def touch_skill(data: dict, name: str | None, project: str | None) -> None:
    """A skill was used or changed: from now on it ages only by sessions started in the repos it was used in."""
    if not name:
        return
    rec = data["skill_activity"].setdefault(name, {"repos": {}})
    for key in {*rec["repos"], *([project] if project else [])}:
        rec["repos"][key] = data["project_sessions"].get(key, 0)
    rec.update(ts=time.time(), total=_total(data))


def forget_skill(data: dict, name: str | None) -> None:
    """An archived skill starts fresh if a skill with its name comes back."""
    data["skill_activity"].pop(name, None)


def idle_sessions(data: dict, name: str) -> int:
    """Working sessions in the skill's repos since its last use or change; anywhere, when no repo is known."""
    rec = data["skill_activity"].get(name)
    if not rec:
        return 0
    if not rec["repos"]:
        return idle_total(data, name)
    return sum(max(0, data["project_sessions"].get(key, 0) - at) for key, at in rec["repos"].items())


def idle_total(data: dict, name: str) -> int:
    """Working sessions anywhere since the skill's last use or change."""
    rec = data["skill_activity"].get(name)
    return max(0, _total(data) - rec.get("total", 0)) if rec else 0


class State:
    def __init__(self, path: Path, host: str = "claude", legacy_host: str = "claude"):
        self.path, self.host, self.legacy_host = path, host, legacy_host

    def _raw(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return _empty()
        return data

    def read(self) -> dict:
        raw = self._raw()
        data = raw.get("hosts", {}).get(self.host, {})
        if not data and "hosts" not in raw and self.host == self.legacy_host:
            data = raw
        data = {**data}
        for k, v in _empty().items():
            data.setdefault(k, v)
        return data

    @contextmanager
    def edit(self, *, hook: bool = False, require_commit: bool = False):
        """Edit under lock; require_commit raises instead of yielding a disposable snapshot on hook timeout."""
        with ExitStack() as locks:
            try:
                locks.enter_context(
                    file_lock(self.path, wait_s=C.HOOK_STATE_LOCK_WAIT_S) if hook else file_lock(self.path)
                )
            except TimeoutError:
                if not hook or require_commit:
                    raise
                yield self.read()
                return
            data = self.read()
            yield data
            now = time.time()
            data["sessions"] = {k: s for k, s in data["sessions"].items() if now - s.get("seen", now) < SESSION_TTL_S}
            raw = self._raw()
            if "hosts" not in raw:
                raw = {"hosts": {self.legacy_host: raw}}
            raw["hosts"][self.host] = data
            atomic_write(self.path, json.dumps(raw))


def session(data: dict, sid: str) -> dict:
    s = data["sessions"].setdefault(sid, {})
    for k, v in (
        ("turns", 0),
        ("turns_since_memory", 0),
        ("iters_since_skill", 0),
        ("skills", []),
        ("instructions", []),
        ("reviewed_uuid", None),
    ):
        s.setdefault(k, v)
    if isinstance(s.get("seen"), (int, float)) and s["seen"] != s.get("current_seen"):  # an older build touched it last
        s["old_seen"] = older_touch(s)
        s["old_surface"] = s.get("surface")  # how that build last started the chat, before this one restarts it
    s["seen"] = s["current_seen"] = time.time()
    return s


def older_touch(s: dict) -> float:
    """When an older nowitknows last touched this record: those set `seen` alone and keep other keys, so a `seen`
    this version did not also write as `current_seen` is theirs (the migration fence reads it)."""
    seen, old = s.get("seen"), s.get("old_seen")
    old = old if isinstance(old, (int, float)) else 0
    return max(old, seen) if isinstance(seen, (int, float)) and seen != s.get("current_seen") else old


def review_factor(worker: dict) -> int:
    """How far apart reviews are stretched after a run of reviews that changed nothing."""
    over = worker.get("empty_streak", 0) - C.REVIEW_EMPTY_STREAK
    return 1 if over < 0 else min(C.REVIEW_BACKOFF_MAX, 2 ** (over + 1))
