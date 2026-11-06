from __future__ import annotations

import bisect
import contextlib
import gzip
import importlib
import io
import json
import os
import queue
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO

from . import config as C
from . import safety
from .router import STOP
from .state import _replace, file_lock

SCHEMA = """
create table if not exists sessions(id text primary key, project text, path text, first_ts text, last_ts text,
  n int, mtime real, size int);
create table if not exists exchanges(id integer primary key, session_id text, uuid text, ts text, text text);
create table if not exists transcript_sources(path text primary key, session_id text,
  mtime_ns integer, ctime_ns integer, size integer, inode integer);
create table if not exists imported_threads(session_id text primary key, original text,
  reread integer not null default 1, copied integer not null default 0);
create index if not exists ex_session on exchanges(session_id);
create index if not exists ex_uuid on exchanges(uuid);
create virtual table if not exists ex_fts using fts5(text, content='exchanges', content_rowid='id',
  tokenize='porter unicode61');
create trigger if not exists ex_ai after insert on exchanges begin
  insert into ex_fts(rowid, text) values (new.id, new.text); end;
create trigger if not exists ex_ad after delete on exchanges begin
  insert into ex_fts(ex_fts, rowid, text) values ('delete', old.id, old.text); end;
"""


INDEX_VERSION = 9  # 2: exchanges carry approved plans and rejection feedback; 3: long turns keep the user's words;
# 4: omission markers count against the limit, so the end of a turn with many rejections is no longer cut off;
# 5: a background-task notice starts an exchange as a notice, no longer as words the user typed;
# 6: each tool call is a line with its command or path and whether it worked, so reviews have evidence
# 7: host provenance and a common repository key, including normalized Codex transcripts
# 8: every session id is host-scoped
# 9: fingerprints cache every transcript source, before parsing


def connect(db: Path, legacy_host: str = "claude") -> sqlite3.Connection:
    c = sqlite3.connect(db, timeout=C.SQLITE_TIMEOUT_S)
    c.execute("pragma journal_mode=wal")  # searches keep working while the worker ingests
    c.executescript(SCHEMA)
    c.execute("begin immediate")
    columns = {r[1] for r in c.execute("pragma table_info(sessions)")}
    if "host" not in columns:
        c.execute("alter table sessions add column host text")
        c.execute("update sessions set host=?", (legacy_host,))
    if "repo" not in columns:
        c.execute("alter table sessions add column repo text")
    if "copied" not in {r[1] for r in c.execute("pragma table_info(exchanges)")}:
        # Filled when a listed thread is read again (record_imports), so the upgrade needs no version change.
        c.execute("alter table exchanges add column copied integer not null default 0")
    if c.execute("pragma user_version").fetchone()[0] < INDEX_VERSION:
        # transcripts still on disk are read again in the new format; sessions whose files are gone keep their rows
        for sid, host in c.execute("select id,host from sessions").fetchall():
            target = sid if sid.startswith(host + ":") else host + ":" + sid
            if target == sid:
                continue
            if c.execute("select 1 from sessions where id=?", (target,)).fetchone():
                c.execute("update exchanges set session_id=? where session_id=?", (target, sid))
                c.execute("delete from sessions where id=?", (sid,))
            else:
                c.execute("update sessions set id=? where id=?", (target, sid))
                c.execute("update exchanges set session_id=? where session_id=?", (target, sid))
        c.execute("update sessions set mtime = -1")
        c.execute("delete from transcript_sources")
        c.execute(f"pragma user_version = {INDEX_VERSION}")
    c.commit()
    return c


def _blocks(msg: dict) -> list:
    content = (msg or {}).get("content")
    return [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])


NOTICE_SUMMARY = re.compile(r"<summary>(.*?)</summary>", re.S)


def prompt_text(entry: dict) -> tuple[str, str | None] | None:
    """A main-thread prompt's text, and the label of a harness notice (None for the user's own words)."""
    if entry.get("type") != "user" or entry.get("isSidechain"):
        return None
    if entry.get("isMeta") or entry.get("isCompactSummary") or entry.get("isVisibleInTranscriptOnly"):
        return None
    parts = [
        b.get("text", "") for b in _blocks(entry.get("message")) if isinstance(b, dict) and b.get("type") == "text"
    ]
    text = "\n".join(p for p in parts if p).strip()
    if not text:
        return None
    origin = entry.get("origin") if isinstance(entry.get("origin"), dict) else {}
    kind, subkind = origin.get("kind"), origin.get("subkind")
    if kind is None:  # older transcripts stamp no origin; Claude Code's own test for a notice is its prefix
        return text, ("TASK NOTICE" if text.startswith(C.NOTICE_PROMPT) else None)
    if kind != C.NOTICE_ORIGIN or subkind in C.USER_NOTICES:
        return text, None
    return text, ("SESSION MESSAGE" if subkind in C.SESSION_NOTICES else "TASK NOTICE")


def assistant_text(entry: dict) -> str:
    if entry.get("type") != "assistant" or entry.get("isSidechain"):
        return ""
    out = []
    for b in _blocks(entry.get("message")):
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and not (entry.get("cursor") and b.get("cursor_outcome_evidence")):
            out.append(b.get("text", ""))
    return "\n".join(o for o in out if o)


LEADING_CD = re.compile(r"^cd\s+(\"[^\"]*\"|'[^']*'|\S+)\s*&&\s*")
TOOL_ARG_KEYS = ("command", "file_path", "notebook_path", "pattern", "skill", "url", "query", "description", "prompt")


def tool_line(block: dict) -> str:
    """One digest line per tool call: its name and the argument that says what it did (a command, a path)."""
    args = block.get("input") if isinstance(block.get("input"), dict) else {}
    key = next((k for k in TOOL_ARG_KEYS if isinstance(args.get(k), str) and args[k].strip()), None)
    # Other tools' arguments are named, never shown: they carry typed text, form values, and the like.
    what = args[key] if key else f"({', '.join(sorted(args))})" if args else ""
    if key == "command":
        what = safety.command_redact(safety.without_heredoc(what))
    what = LEADING_CD.sub("", " ".join(str(what).split()))  # agents prefix most commands with the same `cd <repo> &&`
    what = safety.redact(what)
    if len(what) > C.TOOL_ARG_CHARS:
        what = what[: C.TOOL_ARG_CHARS] + "…"
    return f"TOOL {block.get('name')}" + (f": {what}" if what else "")


def tool_outcome(block: dict, entry: dict | None = None, name: str = "", command: str = "") -> str:
    """How the call to tool `name` ended; `entry` is the transcript entry that holds this result alone, so its record
    of the permission decision, when the host wrote one, belongs to this call."""
    if block.get("outcome_complete") is False:
        return " -> result not seen" if block.get("outcome_running") else " -> outcome unknown"
    if not block.get("is_error") and not own_skill_tool(name):
        return " -> ok"
    content = block.get("content")
    text = (
        content
        if isinstance(content, str)
        else " ".join(c.get("text", "") for c in content or [] if isinstance(c, dict))
    )
    if not block.get("is_error"):
        if (error := skill_tool_error(name, text)) is None:
            return " -> ok"
        text = error
    elif text.startswith(REJECTED_HEAD):  # the user's words, if any, come as their own line; the rest is Claude Code's
        return " -> rejected by the user"
    elif mode_denied(entry or {}, text):  # nobody was asked, so nobody corrected anything, and the call never ran
        return MODE_DENIED
    prefix = (
        "[redacted]" if safety.secret_output(command) else safety.redact(" ".join(text.split()))[: C.TOOL_ERROR_CHARS]
    )
    return f" -> failed: {prefix}"


REJECTED_HEAD = "The user doesn't want to proceed with this tool use."
REJECTED = "To tell you how to proceed, the user said:"
APPROVED_HEAD = "User has approved your plan."
APPROVED = re.compile(r"^## Approved Plan( \(edited by user\))?:\n", re.M)
# A refusal the permission mode or approval policy makes on its own. Claude Code 2.1.296 records the reason on the
# entry (`permissionDecision.reasonType`; a deny rule from settings is "rule"); 2.1.283 records only `toolDenialKind`,
# which names auto mode but files every other refusal under "permission-rule"; older transcripts and Codex rows
# record neither, so the host's own words decide, each host's on its own rows.
AUTOMATIC_REASONS = ("mode", "asyncAgent", "classifier")  # don't ask mode, nobody to ask, the auto mode classifier
CLAUDE_DENIAL = re.compile(
    r"Permission to use \S+ has been denied because Claude Code is running in don't ask mode\."
    r"|Permission to use \S+ has been denied\. IMPORTANT: You \*may\* attempt"  # nobody to ask; a deny rule ends at "."
    r"|Permission for this action was denied by the Claude Code auto mode classifier\."
    r"|Permission for this action has been denied\. Reason: "  # Claude Code's own auto mode check accepts both
    r"|Permission for this tool use was denied: it requires interactive approval, and permission prompts are not"
    r"|Permission for this tool use was denied\. It requires approval, and this session has no approval surface"
)
CODEX_DENIAL = re.compile(
    r"MCP tool call requires approval, but approval policy is never"
    r"|approval policy is [^;\n]{1,80}; reject command"  # the shell, asked for escalated permissions it cannot grant
    r"|patch rejected: (?:writing is blocked by read-only sandbox|writing outside of the project); rejected by user "
    r"approval settings"  # apply_patch
)
MODE_DENIED = " -> denied by the permission mode"
# nowitknows's own skill tools answer a failure inside a result the host counts as a success: {"ok": false, ...}
SKILL_TOOLS = ("skill_view", "skill_manage")


def ran(entry: dict) -> bool:
    """The host recorded that the call ran: Claude Code let it through, or Codex recorded how it ended."""
    decision, codex = entry.get("permissionDecision"), entry.get("codex")
    return (isinstance(decision, dict) and decision.get("decision") == "accept") or (
        isinstance(codex, dict) and codex.get("ran") is True
    )


def mode_denied(entry: dict, text: str) -> bool:
    if ran(entry):  # what a call that ran printed is its own output, never the host refusing it
        return False
    decision, kind = entry.get("permissionDecision"), entry.get("toolDenialKind")
    if isinstance(decision, dict) and isinstance(decision.get("reasonType"), str):  # a refusal, and who made it
        return decision["reasonType"] in AUTOMATIC_REASONS
    if isinstance(kind, str) and kind != "permission-rule":  # the user, an interruption, or auto mode
        return kind.startswith("automode-")
    return bool((CODEX_DENIAL if "codex" in entry else CLAUDE_DENIAL).match(text))


def own_skill_tool(name: str) -> bool:
    """One of nowitknows's own skill tools, by the id this host or a background run gives it; another server's tool
    with the same name answers in its own way."""
    return C.tool_name(name) in SKILL_TOOLS


def skill_tool_error(name: str, text: str) -> str | None:
    """The failure a skill tool reported in an answer the host took for a success, or None when it worked."""
    if not own_skill_tool(name):
        return None
    try:
        answer = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(answer, dict) or answer.get("ok") is True or not ("ok" in answer or answer.get("error")):
        return None
    errors = answer.get("errors")  # a skill that fails validation lists every problem
    listed = "; ".join(map(str, errors)) if isinstance(errors, list) else ""
    return str(answer.get("error") or listed or "ok: false")


def plan_calls(entry: dict) -> set[str]:
    """Ids of ExitPlanMode calls in a main-thread assistant entry: only their results carry an approved plan."""
    if entry.get("type") != "assistant" or entry.get("isSidechain"):
        return set()
    return {
        b.get("id")
        for b in _blocks(entry.get("message"))
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "ExitPlanMode"
    }


def user_response(entry: dict, plans: set[str]) -> str | None:
    """The user's answer inside a tool result: feedback when rejecting a tool call, or the plan they approved.
    Only Claude Code's own wording counts, so tool output that quotes it (a file, a web page) is never the user."""
    if entry.get("type") != "user" or entry.get("isSidechain"):
        return None
    for b in _blocks(entry.get("message")):
        if not (isinstance(b, dict) and b.get("type") == "tool_result"):
            continue
        content = b.get("content")
        text = (
            content
            if isinstance(content, str)
            else "\n".join(c.get("text", "") for c in content or [] if isinstance(c, dict))
        )
        said = text.partition(REJECTED)[2].strip() if b.get("is_error") and text.startswith(REJECTED_HEAD) else ""
        if said:
            return f"USER (rejecting that tool call): {said}"
        if b.get("tool_use_id") in plans and text.startswith(APPROVED_HEAD) and (m := APPROVED.search(text)):
            who = "edited and approved by the user" if m.group(1) else "approved by the user"
            return f"PLAN ({who}):\n{text[m.end() :].strip()[: C.PLAN_MAX_CHARS]}"
    return None


def _zstd():
    try:
        return importlib.import_module("compression.zstd")  # Python 3.14 and later, when built with zstd
    except ImportError:
        return None


def zstd_reader() -> str | None:
    """How Codex's `.zst` rollouts are read here: Python's own module, the zstd command, or not at all."""
    return "python" if _zstd() else "command" if shutil.which("zstd") else None


class HistoryTooLarge(OSError):
    """A transcript past HISTORY_FILE_MAX_BYTES once decompressed."""


class _Capped(io.RawIOBase):
    """A transcript's bytes, cut off past HISTORY_FILE_MAX_BYTES before more are buffered: a small `.zst` or `.gz`
    file can expand to gigabytes."""

    def __init__(self, inner, name: str):
        self.inner, self.name, self.total = inner, name, 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        n = self.inner.readinto(buffer) or 0
        self.total += n
        if self.total > C.HISTORY_FILE_MAX_BYTES:
            raise HistoryTooLarge(f"{self.name} is over {C.HISTORY_FILE_MAX_BYTES} bytes")
        return n


def _stop(process: subprocess.Popen) -> None:
    """End a child that may still run: terminate, then kill, and reap it either way."""
    if process.poll() is None:
        if C.WINDOWS:
            from .claude import hidden

            taskkill = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32/taskkill.exe"
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=C.ZSTD_KILL_GRACE_S,
                    **hidden(),
                )
            if process.poll() is not None:
                process.wait()
                return
        process.terminate()
        try:
            process.wait(timeout=C.ZSTD_KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


class _ZstdCommand(io.RawIOBase):
    """The zstd command's output for one file. A thread drains the pipe, so a command that stalls fails the read
    after ZSTD_WAIT_S of waiting instead of holding the worker; closing ends and reaps the command."""

    def __init__(self, path: Path):
        from .claude import hidden

        self.name = path.name
        self.process = subprocess.Popen(
            [shutil.which("zstd") or "zstd", "-q", "-d", "-c", "--", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            **hidden(),
        )
        self.chunks: queue.Queue = queue.Queue(maxsize=C.ZSTD_PIPE_CHUNKS)
        self.pending, self.waited, self.ended = b"", 0.0, False
        self.stopping = threading.Event()
        self.drain = threading.Thread(target=self._drain, daemon=True)
        self.drain.start()

    def _put(self, item) -> bool:
        while not self.stopping.is_set():
            with contextlib.suppress(queue.Full):
                self.chunks.put(item, timeout=0.1)
                return True
        return False

    def _drain(self) -> None:
        try:
            with contextlib.suppress(OSError, ValueError):
                while data := self.process.stdout.read1(C.ZSTD_CHUNK_BYTES):
                    if not self._put(data):
                        return
        finally:
            if C.WINDOWS:
                with contextlib.suppress(OSError):
                    self.process.stdout.close()
        self._put(None)

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if not self.pending:
            if self.ended:
                return 0
            began = time.monotonic()
            try:
                chunk = self.chunks.get(timeout=max(0.0, C.ZSTD_WAIT_S - self.waited))
            except queue.Empty:
                raise OSError(f"zstd gave no output for {C.ZSTD_WAIT_S} s on {self.name}") from None
            finally:
                self.waited += time.monotonic() - began
            if chunk is None:  # all of it read: the command must also exit, and cleanly
                self.ended = True
                try:
                    code = self.process.wait(timeout=max(0.0, C.ZSTD_WAIT_S - self.waited))
                except subprocess.TimeoutExpired:
                    raise OSError(f"zstd did not exit after {self.name}") from None
                if code:
                    raise OSError(f"zstd could not read {self.name}")
                return 0
            self.pending = chunk
        n = min(len(buffer), len(self.pending))
        buffer[:n] = self.pending[:n]
        self.pending = self.pending[n:]
        return n

    def close(self) -> None:
        if not self.closed:
            self.stopping.set()  # also after a reader stops early (identity), which is no damage
            _stop(self.process)
            self.drain.join(timeout=C.ZSTD_KILL_GRACE_S)
            if not C.WINDOWS or not self.drain.is_alive():
                with contextlib.suppress(OSError):
                    self.process.stdout.close()
        super().close()


def _source(path: Path) -> IO:
    """A transcript file's bytes, decompressed as its name says; the caller closes the stream."""
    if path.name.endswith(".zst"):
        reader = zstd_reader()
        if reader is None:
            raise OSError(f"no zstd reader for {path.name}")  # neither Python's module nor the command
        return _zstd().open(path, "rb") if reader == "python" else _ZstdCommand(path)
    return gzip.open(path, "rb") if path.suffix == ".gz" else open(path, "rb", 0)


@contextlib.contextmanager
def open_history(path: Path) -> Iterator[IO]:
    """A transcript's bytes: plain, gzip (nowitknows's archive) or zstd (Codex's older rollouts), cut off past
    HISTORY_FILE_MAX_BYTES; a damaged or cut-short file fails like any other read, with an OSError."""
    zstd = _zstd()
    damaged = (EOFError, zstd.ZstdError) if zstd else (EOFError,)
    with _source(path) as inner, io.BufferedReader(_Capped(inner, path.name)) as stream:
        try:
            yield stream
        except damaged as error:
            raise OSError(f"{path.name}: {error}") from error


def history_lines(stream, counts: dict | None = None, *, text: bool = False) -> Iterator:
    """Each line of an open transcript (bytes, or UTF-8 text), or None for one past HISTORY_LINE_MAX_BYTES bytes:
    skipped without being held and counted in `counts`, so the other lines keep their numbers."""
    limit = C.HISTORY_LINE_MAX_BYTES
    while line := stream.readline(limit):
        if len(line) >= limit and not line.endswith(b"\n"):
            while line and not line.endswith(b"\n"):
                line = stream.readline(limit)
            if counts is not None:
                counts["oversized_records"] = counts.get("oversized_records", 0) + 1
            yield None
            continue
        yield line.decode("utf-8", "replace") if text else line


def read_jsonl(path: Path):
    with open_history(path) as f:
        for line in history_lines(f, text=True):
            if line is None:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


OMITTED = "[... part of this turn omitted ...]"


def fit(parts: list[str], limit: int, *, caller: bool = False) -> str:
    """Parts joined within `limit`: the user's own words stay whole while they fit, and the assistant's narration
    loses its middle first, so what was asked, tried first, and finally done survive a long turn."""
    text = "\n".join(parts)
    if len(text) <= limit:
        return text
    if caller:
        parts = [line for part in parts for line in part.splitlines()]
    # A failed or refused call and the call after it (the fix) are evidence a review is paid to read: kept like the
    # user's words. Other tool lines and narration go first.
    mine = []
    for i, p in enumerate(parts):
        failed = p.startswith("TOOL ") and (" -> failed" in p or " -> rejected" in p)
        fix = p.startswith("TOOL ") and i and parts[i - 1].startswith("TOOL ") and " -> failed" in parts[i - 1]
        unprotected = ("ASSISTANT: ", "TOOL ", "CALLER") if caller else ("ASSISTANT: ", "TOOL ")
        mine.append(not p.startswith(unprotected) or failed or bool(fix))
    dropped = [not m for m in mine]  # narration comes back from both ends while it fits
    gaps = sum(1 for i, d in enumerate(dropped) if d and not (i and dropped[i - 1]))
    mark, words = len(OMITTED) + 1, sum(len(p) + 1 for p, m in zip(parts, mine, strict=True) if m)
    # Rejections between the steps split the omitted narration into many gaps; when a marker for each does not fit
    # beside the user's words, one marker stands for all of them.
    one = words + gaps * mark > limit
    marks = mark * (min(gaps, 1) if one else gaps)
    lengths = sorted(len(p) for p, m in zip(parts, mine, strict=True) if m)
    if lengths and words + marks > limit:  # the user's words alone overflow: the longest lose their tails
        budget = limit - marks - len(lengths)
        for i, n in enumerate(lengths):
            cap = max(0, budget // (len(lengths) - i))
            if n > cap:
                break
            budget -= n
        cut = _fit_part if caller else lambda text, room: text[:room]
        parts = [cut(p, cap) if m else p for p, m in zip(parts, mine, strict=True)]
        words = sum(len(p) + 1 for p, m in zip(parts, mine, strict=True) if m)
    room, order = limit - words - marks, [i for i, m in enumerate(mine) if not m]
    left = len(order)
    order = [order[k // 2] if k % 2 == 0 else order[-1 - k // 2] for k in range(len(order))]
    if caller:
        order.sort(key=lambda i: parts[i].startswith("CALLER"))
    for k, i in enumerate(order):
        if one:  # what a kept step changes in the markers: the last omission takes the one marker with it
            change = -mark if left == 1 else 0
        else:  # a step inside a gap splits it in two; a gap of one step closes
            before, after = i > 0 and dropped[i - 1], i + 1 < len(parts) and dropped[i + 1]
            change = mark if before and after else 0 if before or after else -mark
        # A step too long keeps its head; the first takes at most half the room, so the last one still has some.
        if (
            not (caller and parts[i].startswith("TOOL "))
            and len(parts[i]) + 1 + change > (room // 2 if k == 0 and len(order) > 1 else room)
            and room > 400
        ):
            cap = room - change - 1 if caller and parts[i].startswith("CALLER") else (room - change) // 2
            parts[i] = parts[i][:cap]
        if len(parts[i]) + 1 + change <= room:
            dropped[i], left, room = False, left - 1, room - (len(parts[i]) + 1 + change)
    out: list[str] = []
    marked = False
    for p, d in zip(parts, dropped, strict=True):
        if not d:
            out += [p] if p else []  # a user part cut to nothing leaves no blank line
        elif not (marked and (one or out[-1] == OMITTED)):
            out.append(OMITTED)
            marked = True
    return "\n".join(out)


def _fit_part(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    if text.startswith("TOOL "):
        command, separator, status = text.partition(" -> ")
        room = cap - len(separator + status) - 1
        return command[:room] + "…" + separator + status if room >= len("TOOL ") else ""
    return text[:cap]


def _summary(notice: str, label: str) -> str:
    m = NOTICE_SUMMARY.search(notice)
    if m:
        return " ".join(m.group(1).split())[: C.NOTICE_SUMMARY_CHARS]
    return (
        " ".join(notice.split())[: C.NOTICE_SUMMARY_CHARS]
        if label == "SESSION MESSAGE"
        else "a background task finished"
    )


def turn_id(entry: dict) -> str:
    """The Codex turn a normalized row came from; Claude Code rows have none."""
    codex = entry.get("codex")
    turn = codex.get("turn_id") if isinstance(codex, dict) else None
    return turn if isinstance(turn, str) else ""


def imported(path: Path) -> bool:
    """A Codex thread holding turns its importer copied from another app, wherever in the file they sit. A Claude Code
    transcript, whose first row carries no `codex` record, is not read past that row."""
    with open_history(path) as f:
        for n, line in enumerate(history_lines(f, text=True)):
            if line is None or not (n == 0 or C.CODEX_IMPORT_TURN.search(line)):  # parse only lines that can hold one
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if n == 0 and not (isinstance(row, dict) and "codex" in row):
                return False
            if isinstance(row, dict) and C.CODEX_IMPORT_TURN.fullmatch(turn_id(row)):
                return True
    return False


def command_read(command: str) -> str | None:
    """Recognize one literal file read without shell operators or substitutions."""
    if not isinstance(command, str) or re.search(r"[;&|<>`$\n\r]", command):
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    if len(words) < 2:
        return None
    tool, *args = words
    tool = Path(tool).name
    if tool in ("head", "tail"):
        if len(args) == 3 and args[0] == "-n" and re.fullmatch(r"[+\-]?\d+", args[1]):
            args = args[2:]
        elif len(args) == 2 and re.fullmatch(r"-\d+", args[0]):
            args = args[1:]
    elif tool == "sed":
        if len(args) != 3 or args[0] != "-n" or not re.fullmatch(r"\d+(?:,\d+|,\$)?p", args[1]):
            return None
        args = args[2:]
    elif tool != "cat":
        return None
    if len(args) != 1 or args[0].startswith("-") or re.search(r"[*?\[\]~]", args[0]):
        return None
    return args[0]


def _grouped(path: Path, *, listed: bool, classify: bool, caller: bool = False, outcomes: bool = False) -> list[dict]:
    """Exchanges before trimming, each part with whether a copied row made it; see `exchanges`."""
    copies = listed or imported(path)
    out, running, plans, calls = [], {False: None, True: None}, set(), {}
    for e in read_jsonl(path):
        turn = turn_id(e)
        # A `rollout-<n>` turn continues the last copied one; elsewhere it is history copied into a subagent.
        real = bool(C.CODEX_IMPORT_TURN.fullmatch(turn) or (copies and C.CODEX_COPY_TURN.fullmatch(turn)))
        copy = classify and real
        cur = running[copy]
        plans |= plan_calls(e)
        if p := prompt_text(e):
            # A notice starts an exchange too, so the work it sets off is reviewed after what came before it.
            text, label = p
            said = f"USER: {text}" if label is None else f"{label}: {_summary(text, label)}"
            if caller and label is None:
                said = "\n".join(f"CALLER: {line}" for line in text.splitlines())
            cur = running[copy] = {"uuid": e.get("uuid"), "ts": e.get("timestamp"), "parts": [said], "plan": None}
            cur.update(copied=copy, owners=[real])
            if outcomes:
                cur["outcomes"] = {}
                cur["folder"] = e.get("cwd", "")
            out.append(cur)
            continue
        if cur and e.get("type") == "user" and not e.get("isSidechain"):  # tool results: did each call work
            results = [b for b in _blocks(e.get("message")) if isinstance(b, dict) and b.get("type") == "tool_result"]
            own = e if len(results) == 1 else None  # the host records a decision only on an entry with one result
            for b in results:
                if at := calls.pop(b.get("tool_use_id"), None):
                    part, i, call, cwd = at
                    name = str(call.get("name") or "")
                    args = call.get("input") if isinstance(call.get("input"), dict) else {}
                    command = call.get("outcome_command", args.get("command", args.get("cmd", "")))
                    status = tool_outcome(b, own, name, command if isinstance(command, str) else "")
                    if part is cur and i < len(cur["parts"]):
                        cur["parts"][i] += status
                    if outcomes:
                        from .outcome import event

                        if observed := event(call, b, status, own):
                            part["outcomes"][i] = observed
                        fp = args.get("file_path")
                        if name == "Codex.command":
                            cwd = call.get("command_cwd", "")
                            fp = (
                                command_read(command)
                                if b.get("outcome_complete") is True
                                and type(b.get("outcome_exit_code")) is int
                                and b["outcome_exit_code"] == 0
                                and isinstance(cwd, str)
                                and Path(cwd).is_absolute()
                                else None
                            )
                        if part is cur and status == " -> ok" and isinstance(fp, str) and fp:
                            fp = safety.redact(fp)
                            if C.tool_name(name) == "skill_view" and isinstance(args.get("name"), str):
                                part.setdefault("support_reads", []).append(
                                    {"name": safety.redact(args["name"]), "file_path": fp}
                                )
                            elif name in ("Read", "ReadFile", "Codex.command"):
                                part.setdefault("support_reads", []).append(
                                    {"file_path": fp, "cwd": safety.redact(cwd)}
                                )
            if said := user_response(e, plans):  # after the outcome, so a rejection follows the call it refused
                if caller:
                    said = said.replace("USER (", "CALLER (").replace("PLAN (", "CALLER PLAN (")
                cur["parts"].append(said)
                cur["owners"].append(real)
                if said.startswith("PLAN ("):
                    cur["plan"] = said
            continue
        if cur and not e.get("isSidechain") and e.get("subtype") == "codex_file_change":
            cur["file_change"] = True
        at = assistant_text(e)
        if at and cur:
            cur["parts"].append(
                "\n".join(f"ASSISTANT: {line}" for line in at.splitlines())
                if caller
                else f"ASSISTANT: {_assistant_labels(at)}"
            )
            cur["owners"].append(real)
        if cur and e.get("type") == "assistant" and not e.get("isSidechain"):
            for b in _blocks(e.get("message")):
                if (
                    isinstance(b, dict)
                    and e.get("cursor")
                    and b.get("type") == "text"
                    and b.get("cursor_outcome_evidence")
                ):
                    # Ambiguous failures must survive narration trimming without assigning a call outcome.
                    cur["parts"].append(" ".join(b.get("text", "").split()))
                    cur["owners"].append(real)
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    i = len(cur["parts"])
                    calls[b.get("id")] = (cur, i, b, e.get("cwd", ""))
                    if outcomes:
                        from .outcome import event

                        if observed := event(b, {}, "", e if e.get("cursor") else None):
                            cur["outcomes"][i] = observed
                    cur["parts"].append(tool_line(b))
                    cur["owners"].append(real)
    for exchange in out:
        exchange["parts"] = [neutralize_outcome(part) for part in exchange["parts"]]
        if exchange["plan"]:
            exchange["plan"] = neutralize_outcome(exchange["plan"])
    return out


def neutralize_outcome(text: str) -> str:
    return text.replace("OUTCOME (computed by NowItKnows", "[quoted outcome] (computed by NowItKnows")


def _assistant_labels(text: str) -> str:
    return re.sub(
        r"\n(?=(?:USER(?::| \()|ASSISTANT:|TOOL |PLAN \(|CALLER(?::| \()|TASK NOTICE:|SESSION MESSAGE:"
        r"|OUTCOME \(|Last commands, newest last:|Check entry points in this repository:))",
        "\nASSISTANT: ",
        text,
    )


def exchanges(
    path: Path, limit: int = C.EXCHANGE_MAX_CHARS, *, listed: bool = False, caller: bool = False, outcomes: bool = False
) -> list[dict]:
    """Exchanges in file order; in an imported Codex thread (`listed` in Codex's import record, or holding importer
    turns) copied turns and turns made in Codex form exchanges of their own, each marked `copied` or not."""
    return [
        {
            "uuid": x["uuid"],
            "ts": x["ts"],
            "text": fit(x["parts"], limit, caller=caller),
            "plan": x["plan"],
            "copied": x["copied"],
            **(
                {"outcomes": [x["outcomes"][i] for i in sorted(x["outcomes"])], "folder": x["folder"]}
                if outcomes
                else {}
            ),
            **({"file_change": True} if x.get("file_change") else {}),
            **({"support_reads": x["support_reads"]} if x.get("support_reads") else {}),
        }
        for x in _grouped(path, listed=listed, classify=True, caller=caller, outcomes=outcomes)
    ]


def owned_lines(path: Path, *, listed: bool = False) -> dict[bool, set[str]]:
    """Every untrimmed line of a thread, by whether a copied row wrote it, as 0.8.0's single running exchange put
    them together: what that grouping could have put in an exchange of the other kind."""
    owned: dict[bool, set[str]] = {False: set(), True: set()}
    for x in _grouped(path, listed=listed, classify=False):
        for part, copied in zip(x["parts"], x["owners"], strict=True):
            owned[copied].update(part.splitlines())
    return owned


def _fingerprint(stat: os.stat_result) -> tuple[int, int, int, int]:
    return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino


def transcript_paths(roots: list[Path]) -> Iterator[Path]:
    seen: set[Path] = set()
    for root in roots:
        # os.walk skips a folder Claude Code's cleanup removes mid-walk; pathlib's glob raises there before 3.12.
        for folder, subfolders, files in os.walk(root):
            subfolders[:] = [d for d in subfolders if d != "subagents"]
            for name in files:
                p = Path(folder) / name
                if not (p.suffix == ".jsonl" or name.endswith((".jsonl.gz", ".jsonl.zst"))):
                    continue
                if p in seen:
                    continue
                seen.add(p)
                yield p


def transcript_files(roots: list[Path], *, cached: dict | None = None) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for p in transcript_paths(roots):
        sid = p.name.split(".jsonl")[0]
        try:
            prior = (cached or {}).get(str(p))
            key = prior[0] if prior and tuple(prior[1:]) == _fingerprint(p.stat()) else identity(p)[0] + ":" + sid
        except OSError:
            continue
        found[key if key not in found else key + "\0" + str(p)] = p
    return found


def _starts(ordered: list[str], prefix: str) -> bool:
    i = bisect.bisect_left(ordered, prefix)
    return i < len(ordered) and ordered[i].startswith(prefix)


def _misplaced(line: str, wrong: tuple, right: tuple) -> bool:
    """A stored line that only the other kind of row wrote: whole, or cut short by an older trim."""
    if not line or line in right[0]:
        return False
    return line in wrong[0] or (_starts(wrong[1], line) and not _starts(right[1], line))


def merge_exchanges(c: sqlite3.Connection, sid: str, incoming: list[dict], stale: dict | None = None) -> int:
    """Add a parse's exchanges to a session's. `stale` gives, by exchange, the lines only the other kind of row wrote
    and those its own kind wrote (each as a set and a sorted list): an older parser's misplaced lines leave first."""
    existing = [
        dict(zip(("uuid", "ts", "text", "copied"), row, strict=True))
        for row in c.execute("select uuid,ts,text,copied from exchanges where session_id=? order by id", (sid,))
    ]
    positions = {(x["uuid"] or (x["ts"], x["text"])): i for i, x in enumerate(existing)}
    added = 0
    for value in incoming:
        key = value["uuid"] or (value["ts"], value["text"])
        if key not in positions:
            positions[key] = len(existing)
            existing.append({**{k: value[k] for k in ("uuid", "ts", "text")}, "copied": int(bool(value.get("copied")))})
            added += 1
        else:
            current = existing[positions[key]]
            if "copied" in value:  # a parsed transcript knows; a legacy store's rows (migration) do not
                current["copied"] = int(bool(value["copied"]))
            if stale and key in stale:
                wrong, right = stale[key]
                current["text"] = "\n".join(l for l in current["text"].splitlines() if not _misplaced(l, wrong, right))
            before, after = current["text"], value["text"]
            if (type(before) is str and type(after) is str and before == after) or all(
                line in after for line in before.splitlines()
            ):
                current["text"] = after
            elif not all(line in before for line in after.splitlines()):
                # Conflicting copies can each contain unique evidence from the same turn.
                current["text"] = before + "\n" + after
            current["ts"] = current["ts"] or value["ts"]
    if existing:
        existing.sort(key=lambda x: (x["ts"] is None, x["ts"] or ""))
        c.execute("delete from exchanges where session_id=?", (sid,))
        c.executemany(
            "insert into exchanges(session_id,uuid,ts,text,copied) values (?,?,?,?,?)",
            [(sid, x["uuid"], x["ts"], x["text"], x["copied"]) for x in existing],
        )
    return added


def ingest(c: sqlite3.Connection, roots: list[Path]) -> int:
    added = 0
    cached = {r[0]: r[1:] for r in c.execute("select * from transcript_sources")}
    pending = {r[0] for r in c.execute("select session_id from imported_threads where reread")}
    reread: set[str] = set()
    for p in transcript_files(roots, cached=cached).values():
        sid = p.name.split(".jsonl")[0]
        # a transcript can vanish after the glob (Claude Code's cleanup; on Windows a pending delete refuses access)
        try:
            st = p.stat()
        except OSError:
            continue
        prior = cached.get(str(p))
        if prior and tuple(prior[1:]) == _fingerprint(st):
            continue
        try:
            host, repo = identity(p)
            listed = host == "codex" and bool(
                c.execute("select 1 from imported_threads where session_id=?", (f"{host}:{sid}",)).fetchone()
            )
            ex = exchanges(p, listed=listed)
            stale = None
            if f"{host}:{sid}" in pending:  # lines 0.8.0's single running exchange may have put in the wrong kind
                owned = owned_lines(p, listed=listed)
                kinds = {}
                for copied in (False, True):
                    wrong, right = owned[not copied] - owned[copied], owned[copied]
                    kinds[copied] = ((wrong, sorted(wrong)), (right, sorted(right)))
                stale = {x["uuid"]: kinds[x["copied"]] for x in ex}
        except OSError:
            continue
        with c:
            c.execute("begin immediate")
            sid = f"{host}:{sid}"
            row = c.execute("select mtime,size,path from sessions where id=?", (sid,)).fetchone()
            added += merge_exchanges(c, sid, ex, stale)
            if stale is not None:
                reread.add(sid)
            if listed:  # what record_imports compares, to see whether an older nowitknows has cleared the flags since
                c.execute(
                    "update imported_threads set copied=(select count(*) from exchanges where session_id=? and copied)"
                    " where session_id=?",
                    (sid, sid),
                )
            c.execute(
                "insert or replace into transcript_sources values (?,?,?,?,?,?)",
                (str(p), sid, *_fingerprint(st)),
            )
            count, first, last = c.execute(
                "select count(*),min(ts),max(ts) from exchanges where session_id=?", (sid,)
            ).fetchone()
            if row and row[2] != str(p):
                c.execute(
                    "update sessions set n=?,first_ts=?,last_ts=?,repo=coalesce(repo,?) where id=?",
                    (count, first, last, repo, sid),
                )
                continue
            c.execute(
                "insert or replace into sessions(id,project,path,first_ts,last_ts,n,mtime,size,host,repo) "
                "values (?,?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    p.parent.name,
                    str(p),
                    first,
                    last,
                    count,
                    st.st_mtime,
                    st.st_size,
                    host,
                    repo,
                ),
            )
    if reread:
        with c:
            c.execute("begin immediate")
            c.executemany("update imported_threads set reread=0 where session_id=?", [(sid,) for sid in reread])
    return added


def record_imports(c: sqlite3.Connection, pairs: list[tuple[str, str]]) -> None:
    """Remember Codex threads copied from another app and the session each copies (or ""); the next ingest reads one
    again when first listed or when an older nowitknows cleared its flags (fewer copied than last written)."""
    with c:
        c.execute("begin immediate")
        for thread, original in pairs:
            known = c.execute("select original, copied from imported_threads where session_id=?", (thread,)).fetchone()
            if known is None:
                c.execute("insert into imported_threads(session_id, original) values (?,?)", (thread, original))
            elif original and known[0] != original:
                c.execute("update imported_threads set original=? where session_id=?", (original, thread))
            flagged = c.execute("select count(*) from exchanges where session_id=? and copied", (thread,)).fetchone()
            if known is None or flagged[0] < known[1]:
                c.execute("update imported_threads set reread=1 where session_id=?", (thread,))
                c.execute("delete from transcript_sources where session_id=?", (thread,))


def reread_imported(c: sqlite3.Connection) -> None:
    """Read every listed thread's transcripts again, after rows were merged in without their flags (migration)."""
    with c:
        c.execute("begin immediate")
        c.execute("update imported_threads set reread=1")
        c.execute("delete from transcript_sources where session_id in (select session_id from imported_threads)")


def listed_thread(db: Path, session_id: str) -> bool:
    """Whether the index remembers this session as a Codex thread an import record listed; never creates the index."""
    if not db.exists():
        return False
    try:
        c = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=C.SQLITE_TIMEOUT_S)
        try:
            return bool(c.execute("select 1 from imported_threads where session_id=?", (session_id,)).fetchone())
        finally:
            c.close()
    except sqlite3.Error:
        return False


def identity(path: Path) -> tuple[str, str | None]:
    from .native import project_key

    host, cwd = "claude", None
    for row in read_jsonl(path):
        if not isinstance(row, dict):
            continue
        if "codex" in row:
            host = "codex"
        elif "cursor" in row:
            host = "cursor"
        cwd = cwd or row.get("cwd")
        if cwd:
            break
    return host, project_key(Path(cwd)) if isinstance(cwd, str) and cwd else None


def fts_query(q: str) -> str:
    words = [w for w in re.findall(r"\w+", q.lower()) if w not in STOP]
    return " OR ".join(f'"{w}"' for w in words) if words else '""'


def search(
    c: sqlite3.Connection,
    query: str,
    limit: int = C.SEARCH_RESULTS,
    sort: str | None = None,
    project: str | None = None,
) -> list[dict]:
    limit = max(1, min(int(limit), C.SEARCH_MAX_RESULTS))
    # the project and the sort pick the candidates, so other projects or better-ranked old matches never crowd them out
    order = {"newest": "x.ts is null, x.ts desc", "oldest": "x.ts is null, x.ts"}.get(sort or "", "r")
    rows = c.execute(
        f"""select x.session_id, bm25(ex_fts) r, x.ts, x.uuid, snippet(ex_fts, 0, '[', ']', ' … ', ?) snip, s.project,
                  s.last_ts, s.host,
                  case when x.copied and exists(select 1 from exchanges o where o.session_id = i.original)
                       then i.original end, case when i.session_id is not null then x.text end, i.original, x.text
           from ex_fts join exchanges x on x.id = ex_fts.rowid join sessions s on s.id = x.session_id
                left join imported_threads i on i.session_id = x.session_id
           where ex_fts match ? and (? is null or s.repo = ? or s.project = ?) order by {order} limit ?""",
        (
            C.SEARCH_SNIPPET_TOKENS,
            fts_query(query),
            project or None,
            project or None,
            project or None,
            C.SEARCH_CANDIDATES,
        ),
    ).fetchall()
    rows.sort(key=lambda row: row[6] or "", reverse=True)  # copies keep their uuid and time: the latest session
    rows.sort(key=lambda row: row[1])  # best rank first; of equal copies, the one in the session active last
    # A resumed or worktree session carries the exchanges of the one it continues: each exchange counts once, and a
    # session whose best match was claimed falls back to its next-best exchange of its own.
    owner: dict = {}
    moved: dict = {}  # a session whose exchange another session claimed continues in that one
    for row in rows:
        if row[9] is None and owner.setdefault((row[7], row[3], row[4]), row[0]) != row[0]:
            moved.setdefault(row[0], owner[row[7], row[3], row[4]])

    def chain(sid: str, stop=frozenset()) -> str:
        seen = set()
        while sid not in stop and sid in moved and sid not in seen:
            seen.add(sid)
            sid = moved[sid]
        return sid

    # Codex's importer numbers each thread's items afresh (`item-<n>`), and unrelated chats can open alike: an imported
    # exchange is another's only with the same id and text and the same lineage, the chat its record names, as one
    # conversation with the sessions that carry that chat's first exchange (a resume).
    lineage: dict = {}

    def chat(sid: str, source: str | None) -> str:
        if not source:
            return sid
        if source not in lineage:
            first = c.execute("select uuid from exchanges where session_id=? order by id limit 1", (source,)).fetchone()
            carried = (
                first
                and first[0]
                and c.execute(
                    "select min(session_id) from exchanges where uuid=? and session_id like 'claude:%'", (first[0],)
                ).fetchone()[0]
            )
            lineage[source] = carried or source
        return lineage[source]

    claim = {
        row: (row[7], row[3], row[4]) if row[9] is None else (row[7], row[3], row[4], row[9], chat(row[0], row[10]))
        for row in rows
    }
    first: dict = {}
    for row in rows:
        first.setdefault(claim[row], row[0])
    own = {row[0] for row in rows if first[claim[row]] == row[0]}  # sessions with a match of their own

    # Importing changes UUIDs and tool text. Match the prompt and its occurrence in transcript order, so repeated
    # prompts stay separate and a copied exchange uses its original's claim before spending a conversation's slots.
    def turns(sid: str, copied: bool = False) -> dict:
        found, occurrences = {}, {}
        for uuid, text in c.execute(
            "select uuid,text from exchanges where session_id=? and (?=0 or copied) order by id", (sid, copied)
        ):
            prompt = re.split(r"\n(?:ASSISTANT:|TOOL |USER \(|PLAN \()", text, maxsplit=1)[0]
            n = occurrences.get(prompt, 0)
            occurrences[prompt] = n + 1
            found[prompt, n] = uuid, text
        return found

    originals = {(row[3], row[11]): claim[row] for row in rows if row[9] is None}
    sources, copied_claims = {}, {}
    for sid, original in {(row[0], row[8]) for row in rows if row[8]}:
        if original not in sources:
            sources[original] = turns(original)
        for prompt, (uuid, _) in turns(sid, copied=True).items():
            if source := sources[original].get(prompt):
                copied_claims[sid, uuid] = originals.get(source, ("original", chat(sid, original), *source))

    counts: dict = {}
    out: list = []
    claimed: set = set()
    for row in rows:
        sid, r, ts, uuid, snip, proj, _, host, original, _, _, _ = row
        key = copied_claims.get((sid, uuid), claim[row])
        if key in claimed:
            continue
        claimed.add(key)
        # A Codex copy of an indexed chat lists with it, as one conversation, by whichever exchange ranks best, and
        # with the session that chat continues in when another session carries it.
        hit = {"session_id": sid, "rank": r, "uuid": uuid, "ts": ts, "snippet": snip, "project": proj, "host": host}
        group = (chain(original, own) if original else sid, (ts or "")[:10])
        if counts.get(group, 0) < C.SEARCH_PER_SESSION:
            counts[group] = counts.get(group, 0) + 1
            out.append({**hit, "copy_of": original} if original else hit)
    out.sort(key=lambda b: b["rank"])
    if sort in ("newest", "oldest"):
        out.sort(key=lambda b: b["ts"] or "", reverse=sort == "newest")
    for b in out:
        b["rank"] = round(b["rank"], 3)
    return out[:limit]


def scroll(
    c: sqlite3.Connection, session_id: str, around_uuid: str | None = None, window: int = C.SCROLL_WINDOW
) -> list[dict]:
    if ":" not in session_id:
        session_id = "claude:" + session_id
    # Custom decoding can change Python's UUID equality; large slices can exceed SQLite's signed limit.
    bounded = (
        type(window) is int
        and 0 <= window < 2**63 - 1
        and (around_uuid is None or type(around_uuid) is str)
        and c.text_factory is str
        and (c.row_factory is None or c.row_factory is sqlite3.Row)
    )
    if bounded:
        try:
            for value in (session_id, around_uuid):
                if isinstance(value, str):
                    str.encode(value, "utf-8")
        except UnicodeEncodeError:
            bounded = False
    if bounded:
        rows = c.execute(
            """with anchor(id) as (select coalesce(
              (select id from exchanges where session_id=? and uuid is ? order by id limit 1),
              (select min(id) from exchanges where session_id=?)))
            select uuid,ts,text from (
              select * from (select id,uuid,ts,text from exchanges
                where session_id=? and id<(select id from anchor) order by id desc limit ?)
              union all
              select * from (select id,uuid,ts,text from exchanges
                where session_id=? and id>=(select id from anchor) order by id limit ?)
            ) order by id""",
            (session_id, around_uuid, session_id, session_id, window, session_id, window + 1),
        ).fetchall()
    else:
        rows = c.execute(
            "select uuid, ts, text from exchanges where session_id=? order by id", (session_id,)
        ).fetchall()
        i = next((k for k, r in enumerate(rows) if r[0] == around_uuid), 0)
    host = c.execute("select host from sessions where id=?", (session_id,)).fetchone()
    if not bounded:
        rows = rows[max(0, i - window) : i + window + 1]
    return [{"uuid": r[0], "ts": r[1], "text": r[2], "host": host[0] if host else "claude"} for r in rows]


def archive(transcript: Path, dest_root: Path, max_mb: int) -> Path | None:
    with file_lock(dest_root):
        if not transcript.exists():
            return None
        host, _ = identity(transcript)
        dest = dest_root / host / transcript.parent.name / f"{transcript.stem}.jsonl.gz"
        if dest.exists() and dest.stat().st_mtime >= transcript.stat().st_mtime:
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".archive-")
        try:
            with (
                os.fdopen(fd, "wb") as raw,
                open(transcript, "rb") as src,
                gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=C.ARCHIVE_GZIP_LEVEL) as out,
            ):
                shutil.copyfileobj(src, out)
            _replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        files = sorted(dest_root.rglob("*.jsonl.gz"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        while files and total > max_mb * 1_048_576:
            victim = files.pop(0)
            if victim == dest:
                break
            total -= victim.stat().st_size
            victim.unlink()
        return dest


def exchanges_since(
    transcript: Path,
    after_uuid: str | None,
    limit: int = C.EXCHANGE_MAX_CHARS,
    *,
    listed: bool = False,
    caller: bool = False,
    outcomes: bool = False,
) -> tuple[list[dict], str | None, str | None]:
    """New exchanges made in this app, the last uuid of the stretch (copied ones included, so a review moves past
    them), and the session's latest approved plan when it was approved before them."""
    ex = exchanges(transcript, limit, listed=listed, caller=caller, outcomes=outcomes) if transcript.exists() else []
    earlier: list[dict] = []
    if after_uuid:  # split before leaving copies out, so a cursor on a copied exchange keeps its place
        idx = next((i for i, x in enumerate(ex) if x["uuid"] == after_uuid), None)
        if idx is not None:
            earlier, ex = ex[: idx + 1], ex[idx + 1 :]
    last = ex[-1]["uuid"] if ex else after_uuid
    # Copied turns were made, and are reviewed, in the app they came from.
    ex, earlier = [x for x in ex if not x["copied"]], [x for x in earlier if not x["copied"]]
    plan = None if any(x["plan"] for x in ex) else next((x["plan"] for x in reversed(earlier) if x["plan"]), None)
    return ex, last, plan
