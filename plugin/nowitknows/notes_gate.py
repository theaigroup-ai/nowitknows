"""Codex chats can run without a sandbox, so notes writes are gated through the memory tool.
This checks the model's own tool calls; it does not sandbox programs they start."""

from __future__ import annotations

import contextlib
import contextvars
import json
import ntpath
import os
import re
import shlex
import signal
import threading
import time
from fnmatch import fnmatchcase
from itertools import islice, product, takewhile
from os.path import realpath
from pathlib import Path

from . import config as C
from . import native

OWN_TOOLS = (*C.aliases("mcp__nowitknows__"), *C.aliases("mcp__plugin_nowitknows_nowitknows__"))
EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
READS = frozenset({"cat", "head", "tail", "sed", "grep", "rg", "ls", "wc", "stat"})
PRINTS = frozenset({"echo", "printf", "true", "false", ":"})  # builtins that write no file without a redirection
# Commands that only look at their arguments and print, bar the options each has that write a file or run a program
QUIET = frozenset({"test", "[", "[[", "pwd", "basename", "dirname", "realpath", "diff", "cut", "tr", "sort", "find"})
SORT_WRITES = ("--output", "--compress-program")  # abbreviated or not, and -o, alone or in a cluster of short options
FIND_ACTS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"})
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
LAUNCHERS = frozenset({"env", "exec", "command", "builtin", "nohup", "time", "sudo", "doas", "nice", "timeout"})
# Builtins that write no file named among their words, beyond PRINTS and QUIET; a redirection's target is still a write
NO_WRITES = frozenset(
    {"cd", "pushd", "popd", "export", "local", "declare", "readonly", "typeset", "unset", "set", "shift", "sleep"}
    | {"wait", "exit", "return", "type", "which"}
    | {"for", "select"}  # a loop's head: its words are the values the loop takes
)
MESSAGE_HINT = re.compile(r"\bgit\b[^;&|\n]*\b(?:commit|tag)\b|\bgh\b")
MESSAGE_FLAGS = {"git": ("-m", "--message"), "gh": ("--title", "--body", "-t", "-b")}
MESSAGE_VALUE = re.compile(r"""'[^']*'|"(?:\\.|[^"\\])*"|[^\s'"\\$&|;<>()`]+""")  # one quoted string, or a plain word
PATCH_HEADER = re.compile(r"\*\*\* (?:Add File|Update File|Delete File|Move to):[ \t]*")
CHEAP_READ = re.compile(
    r"\s*(?:cat|head|tail|grep|rg|ls|wc|stat)(?:\s+(?!--(?:pre|pre-glob|hostname-bin))[^\s;&|<>`$(){}\\'\"*?\[\]!#]+)*\s*"
)
CHEAP_NOTE = (
    " This command is too large to check in full, so a mention of a notes folder is refused unless the whole "
    "command is one plain read: split it into smaller commands."
)
PATH_LIKE = re.compile(r"(?:~|\.\.?)?/[^\s'\"`;&|<>()]*")
SED_PRINT = re.compile(r"(?:(?:\d+|\$|/(?:\\.|[^/\\])*/)(?:,(?:\d+|\$|/(?:\\.|[^/\\])*/))?)?p")
RG_RUNS = ("--pre", "--pre-glob", "--hostname-bin")  # options that make rg start other programs
HOLE = re.compile(r"\$\{(\w+)\}")  # what a word keeps where nowitknows cannot resolve an expansion
GLOB = re.compile(r"[*?\[{]")  # a path part the shell fills in from the folders it finds
POSITIONAL = re.compile(r"\$\{(\d+|[@*])\}|\$(\d|[@*])")  # $0, $1 ... and $@ and $* in a script given to a shell
PWD_FORMS = re.compile(r"\$\(\s*pwd(?:\s+-[LP])?\s*\)|`\s*pwd(?:\s+-[LP])?\s*`")  # the working folder, as $PWD
HERE, VISITED = "\x01", "\x02"  # $PWD where a word runs, and in an assigned value any folder the command visits
BRACED = re.compile(r"([A-Za-z_]\w*)(?:(:?[-=+?])(.*))?", re.S)  # ${NAME} and ${NAME<op>word}
ASSIGNMENT = re.compile(r"([A-Za-z_]\w*)=(.*)", re.S)
NAME = re.compile(r"[A-Za-z_]\w*")
OPERATORS = (";", "&", "&&", "|", "||", "(", ")", "\n")
SEPARATING = frozenset(";&|()\n")
SEPARATORS = re.compile(r"&&|\|\||;;|[;&|()\n]")
REDIRECT = re.compile(r"&?[<>]+[&|]?")
# Runs of a command's text that could be paths: between spaces, quotes, operators and code punctuation, or quoted whole
FRAGMENT = re.compile(r"[^\s'\"`;&|<>(),=+]+")
QUOTED = re.compile(r"'([^'\n]*)'|\"([^\"\n]*)\"")
ESCAPED_SPACE = "\x03"
URL = re.compile(r"\b(?!file:)[A-Za-z][\w+.-]*://[^\s'\"`]*")  # a web address, never a path
# Quoted or escaped, these characters are text: the shell neither fills in a glob nor, in single quotes, expands $
INERT, LIVE = str.maketrans("*?[{$", "\x0e\x0f\x10\x11\x12"), str.maketrans("\x0e\x0f\x10\x11\x12", "*?[{$")
INERT_DOLLAR = str.maketrans("$", "\x12")  # a $ the shell leaves text, so code reads no variable
DEPTH = {"{": 1, "if": 1, "while": 1, "until": 1, "for": 1, "select": 1, "}": -1, "fi": -1, "done": -1}
CARRIED = "\x04"  # marks a word that carries a here-document body, by its number in this check's list of them
HOME_CODE = "\x05"  # in interpreter code, the home folder however the code names it
# The home folder as python, node, ruby or perl names it
HOMES = re.compile(
    r"\bos\.path\.expanduser\(\s*(['\"])~\1\s*\)|\b(?:pathlib\.)?Path\.home\(\s*\)|\bos\.homedir\(\s*\)|\bDir\.home\b"
    r"|\bENV\[\s*['\"]HOME['\"]\s*\]|[$\x12]ENV\{\s*HOME\s*\}|\bprocess\.env\.HOME\b|\bos\.environ\[\s*['\"]HOME['\"]\s*\]"
    r"|\bos\.(?:environ\.get|getenv)\(\s*['\"]HOME['\"]\s*\)"
)
LITERAL = re.compile(r"(['\"`])((?:\\.|(?!\1)[^\\\n])*)\1")  # a string in interpreter code, quotes kept out
SET_IN_CODE = re.compile(
    r"(?m)(?:^|[;{])[ \t]*(?:const |let |var |my |our )?[$]?([A-Za-z_]\w*)[ \t]*=(?![=~>])[ \t]*([^\n;]+)"
)
# A value in an expression: a string, a name (not a call, attribute or keyword argument), or the home folder; and a
# name a string fills in, written {name}, ${name} or #{name}
VALUE = re.compile(LITERAL.pattern + r"|(?<![\w.$])[$]?([A-Za-z_]\w*)\b(?!\s*[(.=:])(?![\'\"`])|(\x05)")
INTERPOLATED = re.compile(r"[$\x12#]?\{\s*(\x05|[A-Za-z_]\w*)\s*\}")
NOT_VALUES = frozenset(
    ("True", "False", "None", "true", "false", "null", "undefined", "nil", "new", "not", "and", "or")
)
# Calls that delete or move away their first path argument; calls that run a shell command; a path object's own
DESTROYS = re.compile(
    r"\b(?:shutil\.)?rmtree\b|\bFileUtils\.(?:rm_rf|rm_r|remove_dir|remove_entry(?:_secure)?|mv|move)\b|\bremove_tree\b"
    r"|\brimraf\b|\b(?:fs|fsp|promises|\(['\"]fs(?:/promises)?['\"]\))\.(?:rm|rmdir|rename)\b|\.(?:rmSync|rmdirSync|renameSync)\b"
    r"|\bos\.(?:rmdir|removedirs)\b|\bDir\.(?:rmdir|delete)\b|\bshutil\.move\b|\bos\.(?:renames?|replace)\b|\bFile\.rename\b"
    r"|(?<![\w.$])(?:rmdir|rename|move)(?=\s*\()"
)
RUNS = re.compile(
    r"\bsubprocess\.\w+|\bos\.(?:system|popen|exec\w*|spawn\w*)\b|\b(?:child_process|cp)\.\w+|\bIO\.popen\b|\bOpen3\.\w+"
    r"|\.(?:execSync|execFileSync|spawnSync|execFile)\b|(?<![\w.$])(?:system|exec|spawn|qx)(?=\s*[('\"`])|%x(?=[({\[|/!])"
)
METHODS = re.compile(r"\.(rmtree|rmdir|rename|replace)\b")  # a method on a Path or a name set to one
KEYWORDS = frozenset(
    {"{", "}", "!", "if", "then", "else", "elif", "do", "while", "until", "time", "builtin", "command"}
)
DENY = (
    "nowitknows: project notes change only through nowitknows's memory tool, mcp__nowitknows__memory (target "
    "'project'), which keeps Claude Code's index, the ledger and backups right. Read notes with cat, head or grep "
    "(also tail, sed -n, rg, ls, wc or stat, with echo or printf, piped, chained or in a loop); any other command "
    "that names a notes folder is denied, copies and interpreters included. If it only mentions a path in text (a "
    "message, a pipe to pbcopy), write the note's name without its folder."
)


class _Late(BaseException):
    """The full check has run out of time (not an Exception: nothing in the check may swallow it)."""


_BY: contextvars.ContextVar[float] = contextvars.ContextVar("notes_gate_by")


def _tick() -> None:
    """Give up on the full check once its time is up (where there is no alarm, only this ends a late check)."""
    by = _BY.get(None)
    if by is not None and time.monotonic() > by:
        raise _Late


def _alarmable() -> bool:
    """Whether an alarm can interrupt this process: signals exist, this is the main thread, no other timer runs."""
    return (
        hasattr(signal, "setitimer")
        and threading.current_thread() is threading.main_thread()
        and signal.getitimer(signal.ITIMER_REAL)[0] == 0
    )


@contextlib.contextmanager
def _deadline(seconds: float):
    """Time the full check: `_tick` raises `_Late` once `seconds` have passed, and where signals can, an alarm raises
    it from whatever code is running then, a pattern that backtracks or a lexer that slows on a long word included."""
    token = _BY.set(time.monotonic() + seconds)
    live, armed, old = [False], False, None
    try:
        if seconds > 0 and _alarmable():

            def late(*_):
                if live[0]:
                    raise _Late

            old, armed, live[0] = signal.signal(signal.SIGALRM, late), True, True
            signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        live[0] = False
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)
        _BY.reset(token)


def _headers(text: str) -> list[str]:
    """The paths a patch's Add File, Update File, Delete File and Move to lines name."""
    found = []
    for line in text.split("\n"):
        if (m := PATCH_HEADER.match(line)) and (path := line[m.end() :].rstrip(" \t\r")):
            found.append(path)
    return found


def _strings(value) -> list[str]:
    """Every string in a tool's input, however deeply nested (a loop, so no input overflows the stack)."""
    found, todo = [], [value]
    while todo:
        item = todo.pop()
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict):
            todo += list(item.values())
        elif isinstance(item, list):
            todo += item
    return found[::-1]


class _Notes:
    """Every notes folder: this repo's and the session's start repo's, as settings are now, folders sessions recorded,
    and `<projects>/<repo>/memory` under each host's project folders, before anything is there."""

    def __init__(self, cfg: C.Config, cwd: Path, session: str | None = None):
        from .codex_host import memory_modes

        repos, folders, seen = {cwd}, set(), []
        shared = Path(C.env("NOWITKNOWS_SHARED_DATA_DIR", cfg.env) or cfg.data)
        for data in {cfg.data, shared}:
            try:
                state = json.loads((data / "state.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            hosts = state.get("hosts") if isinstance(state, dict) else None
            views = [state, *hosts.values()] if isinstance(hosts, dict) else [state]
            for view in views:
                sessions = view.get("sessions") if isinstance(view, dict) else None
                for sid, record in sessions.items() if isinstance(sessions, dict) else []:
                    recorded = record.get("modes") if isinstance(record, dict) else None
                    when = record.get("seen") if isinstance(record, dict) else None
                    for key in ("native_dir", "fallback_dir"):
                        if isinstance(recorded, dict) and isinstance(recorded.get(key), str):
                            seen.append((when if type(when) in (int, float) else 0, recorded[key]))
                    if sid == session and isinstance(record, dict) and isinstance(record.get("start"), str):
                        repos.add(Path(record["start"]))  # where its notes go, as the settings are now
        # Each folder costs a few lookups on every call and a year of sessions records thousands, so only the folders
        # of the newest sessions are followed; any `<projects>/<repo>/memory` is a notes folder whether recorded or not.
        newest = dict.fromkeys(path for _, path in sorted(seen, key=lambda item: -item[0]))
        folders |= {Path(path) for path in islice(newest, C.NOTES_GATE_RECORDED)}
        for repo in repos:
            modes = memory_modes(cfg, repo)
            chain = native.settings_chain(repo, cfg.claude_home)
            folders |= {modes.fallback_dir, native.native_memory_dir(cfg.env, repo, cfg.claude_home, chain, modes.key)}
        bases = {cfg.claude_home / "projects", cfg.data / "projects", shared / "projects", shared / "codex/projects"}
        self.folders = [self._key(p) for p in folders]
        self.bases = [self._key(p) for p in bases]
        self._spelled = sorted({str(p) for p in folders | bases})  # as written, for the text check of cheap mode
        self._home = str(C.home_dir(cfg.env))
        self._needles: list[str] | None = None
        self._found: list[tuple[str, bool]] | None = None
        self._reals: dict[str, str] = {}  # paths of this check with their links followed
        # What a path nowitknows cannot wholly resolve must still show to name a notes folder: a folder holding
        # `projects/<repo>/memory` (Claude Code's or nowitknows's), or a notes folder's own tail, with each run of
        # names that may come right before it (the end of the folder above it, as spelled or with links followed).
        homes = (
            ".claude",
            *C.aliases(".nowitknows"),
            cfg.claude_home.name,
            cfg.data.name,
            shared.name,
            "${claude_config_dir}",
        )
        self.markers = {native._fold(name, True) for name in homes}
        self.tails: list[tuple[list[str], set[tuple[str, ...]]]] = []
        for folder in folders:
            parts = [native._fold(part, True) for part in folder.parts]
            size = 2 if parts[-1:] == ["memory"] else 1
            up = folder.parents[size - 1] if len(parts) > size else Path(folder.anchor)
            runs = {tuple(native._fold(part, True) for part in p.parts) for p in (up, Path(os.path.realpath(up)))}
            self.tails.append((parts[-size:], {run[k:] for run in runs for k in range(len(run) + 1)}))
        self.names = {native._fold("memory", True)} | {tail[-1] for tail, _ in self.tails if tail}

    def named(self, text: str) -> bool:
        """Whether `text` spells out a notes folder, or a projects folder, anywhere in it: the cheap mode's check, by
        text alone, with no link followed."""
        if self._needles is None:
            found = {root for root, _ in self.folders + self.bases}
            for spelled in self._spelled:
                found.add(spelled)
                if spelled.startswith(self._home + os.sep):
                    rest = spelled[len(self._home) + 1 :]
                    found |= {f"~/{rest}", f"$HOME/{rest}", f"${{HOME}}/{rest}"}
            found |= {f"{marker}/projects" for marker in self.markers}
            found |= {"$claude_config_dir/projects", "${claude_config_dir}/projects"}
            found |= {name.replace(os.sep, "/") for name in found}
            self._needles = sorted({native._fold(name, True) for name in found if name})
        folded = native._fold(text, True)
        return any(needle in folded for needle in self._needles)

    def mentions(self, word: str, folders: list[str]) -> bool:
        folded = native._fold(word, True)
        named = any(name in folded for name in self.names)
        if "/" in word or named:  # a path, or a notes folder's own name; never resolved from the hook's own folder
            paths = [word] if word.startswith("/") else [_join(folder, word) for folder in folders]
            # A bare word is a weak sign: it names a notes folder only in Claude Code's or nowitknows's layout.
            if any(self.hold(Path(path), "/" not in word) for path in paths if path.startswith("/")):
                return True
        parts = [part for part in folded.split("/") if part] if named else []
        return any(
            part == "projects" and parts[i + 2 : i + 3] == ["memory"] and self.markers & set(parts[:i])
            for i, part in enumerate(parts)
        )

    def _real(self, path: str) -> str:
        """`path` with its links followed, each folder resolved once for the whole check (a long command names many
        paths in a few folders)."""
        if path in self._reals:
            return self._reals[path]
        if os.name == "nt":  # junctions are links too, which only realpath sees
            self._reals[path] = os.path.realpath(path)
            return self._reals[path]
        todo, current = [], path
        while current not in self._reals:
            parent, name = os.path.split(current)
            if not name or name in (".", ".."):
                self._reals[current] = os.path.realpath(current)
                break
            todo.append((current, name))
            current = parent
        real = self._reals[current]
        while todo:
            current, name = todo.pop()
            there = os.path.join(real, name)
            real = os.path.realpath(there) if os.path.islink(there) else there
            self._reals[current] = real
        return real

    def near(self, path: str) -> bool:
        """Whether what follows `path`, once filled in, could still reach a notes folder it does not lie in: it is or
        holds a notes or projects folder (links followed); a path in one is judged resolved, by `hold`."""
        real = self._real(path)
        return any(
            (root + os.sep).startswith(native._fold(real, fold).rstrip(os.sep) + os.sep)
            for root, fold in self.folders + self.bases
        )

    @staticmethod
    def _key(path: Path) -> tuple[str, bool]:
        fold = native._case_insensitive(path)  # as native.gate: probed on the volume, not assumed by OS
        return native._fold(str(path.expanduser().resolve()), fold).rstrip(os.sep), fold

    def hold(self, path: Path | str, layout: bool = False) -> bool:
        """Whether a path, links followed, is a notes folder or in one; with `layout`, only by Claude Code's or
        nowitknows's `<projects>/<repo>/memory`."""
        try:
            real = self._real(str(path))
        except (OSError, RuntimeError, ValueError):
            return False
        folded = {fold: native._fold(real, fold) for fold in {fold for _, fold in self.folders + self.bases}}
        for root, fold in [] if layout else self.folders:
            here = folded[fold]
            if here == root or here.startswith(root + os.sep):
                return True
        for base, fold in self.bases:
            here = folded[fold]
            if here.startswith(base + os.sep):
                parts = here[len(base) + 1 :].split(os.sep)
                if len(parts) >= 2 and parts[1] == native._fold("memory", fold):
                    return True
        return False

    def points(self, word: str, above: bool = False, where: str | None = None, bare: bool = False) -> bool:
        start = _concrete(where or word)
        if start and not self.near(start):
            return False
        tails = [(t, runs) for t, runs in self.tails if t and (len(t) > 1 or above or not bare)]
        for spelled in _braces(word):
            parts = [native._fold(part, True) for part in spelled.split("/") if part]
            top = parts[-1:] == ["projects"] and self.markers & set(parts)
            if above and parts and (parts[-1] in self.markers or top):
                return True
            for i, part in enumerate(parts):
                rest = parts[i + 1 :]
                if (
                    part == "projects"
                    and rest
                    and self.markers & set(parts[:i])
                    and (len(rest) == 1 or "**" in rest or fnmatchcase("memory", rest[1].replace("[^", "[!")))
                ):
                    return True
            if any(
                _fits(parts[i : i + len(t)], t) and _names_before(parts[:i]) in runs
                for t, runs in tails
                for i in range(len(parts) - len(t) + 1)
            ):
                return True
        return False

    def covers(self, path: str) -> bool:
        for word in _braces(path):
            parts = {fold: self._parts(word, fold) for fold in {fold for _, fold in self.folders + self.bases}}
            if any(_could(parts[fold], Path(root).parts) for root, fold in self.folders):
                return True
            for base, fold in self.bases:
                if _could(parts[fold], (*Path(base).parts, None, native._fold("memory", fold))):
                    return True
        return False

    def lands(self, dest: str, names: tuple[str, ...], folder: bool) -> bool:
        under = folder or os.path.isdir(dest)
        landed = [f"{dest.rstrip(os.sep)}{os.sep}{name}" if name else dest for name in names] if under else [dest]
        return any(self.covers(path) or self.endangers(path) for path in landed)

    def endangers(self, path: str) -> bool:
        if self._found is None:
            found = {(root, fold) for root, fold in self.folders if os.path.isdir(root)}
            for base, _ in self.bases:
                with contextlib.suppress(OSError):
                    found |= {self._key(memory) for memory in Path(base).glob("*/memory") if memory.is_dir()}
            self._found = sorted(found)
        for word in _braces(path):
            if any(_could(self._parts(word, fold), Path(root).parts, above=True) for root, fold in self._found):
                return True
        return False

    def _parts(self, word: str, fold: bool) -> tuple[str, ...]:
        """A path's parts as the shell finds them: its literal start resolved, then a `..` after a glob going up from
        the folders it matches; case-folded like the folder it is compared with."""
        parts = Path(word).parts
        glob = next((i for i, part in enumerate(parts) if GLOB.search(part)), len(parts))
        try:
            found = list(Path(self._real(str(Path(*parts[:glob])))).parts) if glob else []
        except (OSError, RuntimeError, ValueError):
            found = list(parts[:glob])
        for part in parts[glob:]:
            if part != "..":
                found.append(part)
            elif len(found) > 1:
                found.pop()
        return tuple(native._fold(part, fold) for part in found)


def _fits(parts: list[str], names: list[str]) -> bool:
    """Whether path parts could be `names`: the same, or a glob with more than wildcards in it that matches."""
    return len(parts) == len(names) and all(
        part == name or (part.strip("*?") and fnmatchcase(name, part.replace("[^", "[!")))
        for part, name in zip(parts, names, strict=True)
    )


def _names_before(parts: list[str]) -> tuple[str, ...]:
    """The plain names that end `parts`, back to one nowitknows cannot resolve, a glob, `.` or `..`."""
    plain = takewhile(lambda part: not GLOB.search(part) and part not in (".", ".."), parts[::-1])  # a hole has a {
    return tuple(plain)[::-1]


def _concrete(word: str) -> str:
    """An absolute path's start up to the first part nowitknows cannot resolve or the shell fills in; '' for none."""
    parts = list(takewhile(lambda part: not GLOB.search(part), word.split("/")))  # a hole, ${NAME}, has a { too
    return "/".join(parts) if word.startswith("/") and parts != word.split("/") else ""


def _group(word: str) -> tuple[int, int, str | None] | None:
    """The first innermost brace group of `word` that expands, a {a,b} list or a {1..9} sequence, as (start, end, its
    inside for a list, None for a sequence); in one pass, since a pattern backtracks on long runs of commas."""
    i = word.find("{")
    while i >= 0:
        close, nested = word.find("}", i + 1), word.find("{", i + 1)
        if close < 0:
            return None
        if 0 <= nested < close:
            i = nested
            continue
        inside = word[i + 1 : close]
        if "," in inside:
            return i, close + 1, inside
        if ".." in inside:
            return i, close + 1, None
        i = nested
    return None


def _flat_braces(word: str) -> list[str] | None:
    """Expand unnested groups without copying and rescanning the growing prefix for every group."""
    parts = re.split(r"(\{[^{}]*\})", word)
    if any("{" in part or "}" in part for part in parts[::2]):
        return None
    out, todo = [], [(0, [])]
    while todo:
        index, prefix = todo.pop()
        while index < len(parts):
            part = parts[index]
            if index % 2:
                inside = part[1:-1]
                if "," in inside:
                    if len(out) + len(todo) + inside.count(",") >= C.NOTES_GATE_FILLINGS:
                        part = "*"
                    else:
                        todo += [(index + 1, [*prefix, alt]) for alt in inside.split(",")]
                        break
                elif ".." in inside:
                    part = "*"
            prefix.append(part)
            index += 1
        else:
            out.append("".join(prefix))
    return out


def _braces(word: str) -> list[str]:
    """`word` with its brace expansions spelled out, up to NOTES_GATE_FILLINGS words; a sequence, and any list past
    that, stands for any name."""
    if (flat := _flat_braces(word)) is not None:
        return flat
    out, todo = [], [word]
    while todo:
        word = todo.pop()
        found = _group(word)
        if found is None:
            out.append(word)
            continue
        start, end, inside = found
        if inside is None or len(out) + len(todo) + inside.count(",") >= C.NOTES_GATE_FILLINGS:
            todo.append(word[:start] + "*" + word[end:])
        else:
            todo += [word[:start] + alt + word[end:] for alt in inside.split(",")]
    return out


def _could(pattern: tuple[str, ...], folder: tuple[str | None, ...], above: bool = False) -> bool:
    """Whether path parts with globs among them could name `folder` (a None part is any one name) or a path inside it,
    and with `above` a folder holding it, once the shell fills them in; ** is any number of folders."""
    todo, seen = [(0, 0)], set()
    while todo:
        p, f = todo.pop()
        if (p, f) in seen:
            continue
        seen.add((p, f))
        if f == len(folder) or (p == len(pattern) and above):
            return True
        if p == len(pattern):
            continue
        if pattern[p] == "**":
            todo += [(p + 1, f), (p, f + 1)]
        elif folder[f] is None or fnmatchcase(folder[f], pattern[p].replace("[^", "[!")):
            todo.append((p + 1, f + 1))
    return False


class _Scope:
    """The environment `base` without the variables `settled` says the command set for good before position `at`."""

    def __init__(self, base: dict, settled: dict[str, int], at: int):
        self.base, self.settled, self.at = base, settled, at

    def gone(self, name: str) -> bool:
        return self.settled.get(name, self.at) < self.at

    def __contains__(self, name: str) -> bool:
        return not self.gone(name) and name in self.base

    def __getitem__(self, name: str) -> str:
        if self.gone(name):
            raise KeyError(name)
        return self.base[name]

    def get(self, name: str, default=None):
        return default if self.gone(name) else self.base.get(name, default)


def _hole(name: str) -> str:
    return "${" + (name or "_") + "}"


def _tilde(word: str, home: str | None) -> str:
    if word == "~" or word.startswith("~/"):
        return (_hole("HOME") if home is None else home) + word[1:]
    return word


def _closing(word: str, i: int, opening: str, closing: str) -> int:
    """The index just past the `closing` that ends a group whose body starts at i, nested groups counted; the end of
    the word when it never closes."""
    depth = 1
    for j in range(i, len(word)):
        depth += (word[j] == opening) - (word[j] == closing)
        if not depth:
            return j + 1
    return len(word)


def _params(word: str) -> list[tuple[int, int, str, str | None, str]]:
    """Each outermost expansion in `word` as (start, end, name, operator, word): '' for $NAME and ${NAME}, :- - := =
    :+ + :? ? for ${NAME<op>word}, None when unresolved ($(...), a lone $ split off it, $1, other ${...})."""
    found, i = [], 0
    while i < len(word):
        if word[i] != "$":
            i += 1
            continue
        elif word.startswith("${", i):
            end = _closing(word, i + 2, "{", "}")
            body = word[i + 2 : end - 1]
            if m := BRACED.fullmatch(body):
                found.append((i, end, m[1], m[2] or "", m[3] or ""))
            else:
                named = re.match(r"[#!]?([A-Za-z_]\w*)", body)
                found.append((i, end, named[1] if named else "", None, body))
        elif word.startswith("$(", i):
            end = _closing(word, i + 2, "(", ")")
            found.append((i, end, "", None, word[i + 2 : end - 1]))
        elif m := NAME.match(word, i + 1):
            found.append((i, m.end(), m[0], "", ""))
        elif i + 1 == len(word) or word[i + 1] in "0123456789@*#?$!-":
            found.append((i, min(i + 2, len(word)), "", None, ""))
        else:
            i += 1
            continue
        i = found[-1][1]
    return found


def _fill(word: str, value) -> str:
    """`word` with each outermost expansion replaced by value(expansion)."""
    parts, last = [], 0
    for param in _params(word):
        parts += [word[last : param[0]], value(param)]
        last = param[1]
    return "".join(parts) + word[last:]


def _expand(word: str, scope: dict) -> str:
    """`word` expanded with `scope`; an expansion NowItKnows cannot resolve is a hole."""

    def value(param) -> str:
        _, _, name, op, arg = param
        current = scope.get(name)
        if op is None:
            return _hole(name)
        if op in ("", ":?", "?"):
            return _hole(name) if current is None else current
        unset = current is None or (op.startswith(":") and not current)
        if op in (":+", "+"):
            return "" if unset else _expand(arg, scope)
        return _expand(arg, scope) if unset else current

    return _tilde(_fill(word, value), scope.get("HOME"))


def _locals(argv: list[str], env: dict) -> dict[str, set[str]]:
    """Every value the command's own assignments give each variable (${NAME:=word} too), a loop's each of its words,
    globs kept; what nowitknows cannot resolve and what `read` gets stay holes."""
    scope, values = dict(env), {}

    def assign(name: str, value: str) -> None:
        value = value.translate(LIVE)  # used unquoted, a value's globs are filled in after all
        scope[name] = value
        values.setdefault(name, set()).add(value)

    for i, word in enumerate(argv):
        for _, _, name, op, arg in _params(word):
            if op in (":=", "=") and (name not in scope or (op == ":=" and not scope[name])):
                assign(name, _expand(arg, scope))
        if m := ASSIGNMENT.fullmatch(word):
            assign(m[1], _expand(m[2], scope))  # `d=$(...)` leaves a lone `$`, a hole
        elif word in ("for", "select") and i + 1 < len(argv) and NAME.fullmatch(argv[i + 1]):
            items = []
            for later in argv[i + 3 :] if argv[i + 2 : i + 3] == ["in"] else []:
                if later == "do" or set(later) <= SEPARATING:
                    break
                items += [_tilde(w, scope.get("HOME")) for w in _fillings(later, values, scope)]
            for item in items or [_hole(argv[i + 1])]:  # without words, the loop takes the positional parameters
                assign(argv[i + 1], item)
        elif word in ("read", "mapfile", "readarray"):
            for later in argv[i + 1 :]:
                if later in OPERATORS:
                    break
                if NAME.fullmatch(later):
                    assign(later, _hole(later))
    return values


def _settled(argv: list[str]) -> dict[str, int]:
    settled, depth, command, before = {}, 0, [], ";"
    for i, token in enumerate([*argv, "\n"]):
        if token == "case":  # its patterns end in a `)` of their own
            break
        if not (token and set(token) <= SEPARATING):
            depth += DEPTH.get(token, 0) if not command else 0
            command.append((i, token))
            continue
        ops = SEPARATORS.findall(token)
        words = [word for _, word in command]
        start = 1 if words[:1] in (["export"], ["readonly"]) else 0
        top = depth == 0 and before in (";", "\n") and ops[0] not in ("|", "&")
        if top and all(ASSIGNMENT.fullmatch(word) for word in words[start:]):
            for j, word in command[start:]:
                settled.setdefault(ASSIGNMENT.fullmatch(word)[1], j)
        for op in ops:
            depth += (op == "(") - (op == ")")
        command, before = [], ops[-1]
    return settled


def _choices(param, values: dict[str, set[str]], env: dict, depth: int) -> list[str]:
    """Every value one expansion can take: what the command's assignments give the variable and its environment value,
    and for ${NAME:-word} and its kin the word as well."""
    _, _, name, op, arg = param
    if op is None or depth >= C.NOTES_GATE_NESTING:
        return [_hole(name)]
    known = sorted(values.get(name, set()) | ({env[name]} if name in env else set()))
    if op in ("", ":?", "?"):
        return known or [_hole(name)]
    words = _fillings(arg, values, env, depth + 1)  # a ~ that starts the word is expanded with the word
    return ["", *words] if op in (":+", "+") else known + words


def _fillings(word: str, values: dict[str, set[str]], env: dict, depth: int = 0) -> list[str]:
    """The word with its expansions filled in every way the command allows, up to NOTES_GATE_FILLINGS."""
    params = _params(word)
    choices = [_choices(p, values, env, depth) for p in params]
    out = []
    for combo in islice(product(*choices), C.NOTES_GATE_FILLINGS):
        parts, last = [], 0
        for (start, end, *_), value in zip(params, combo, strict=True):
            parts += [word[last:start], value]
            last = end
        out.append("".join(parts) + word[last:])
    return list(dict.fromkeys(out))


def _expansions(word: str, values: dict[str, set[str]], env: dict) -> list[str]:
    return [_tilde(w, env.get("HOME")) for w in _fillings(word, values, env)]


def _located(word: str, here: list[str], visited: list[str]) -> list[str]:
    """`word` with $PWD as each folder the shell may be in where the word runs, and as any folder visited in a value
    assigned earlier."""
    words = [word]
    for mark, folders in ((HERE, here), (VISITED, visited)):
        words = list(dict.fromkeys(w.replace(mark, folder) if mark in w else w for w in words for folder in folders))
    return words[: C.NOTES_GATE_FILLINGS]


def _uncommented(text: str) -> str:
    """`text` without its comments, as the shell reads them: a # that starts a word outside quotes, up to the end of
    its line, which still ends the command."""
    out, quote, i = [], "", 0
    while i < len(text):
        ch = text[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif ch == "\\":
            out.append(text[i : i + 2])
            i += 2
            continue
        elif quote:
            quote = "" if ch == quote[-1] else quote
        elif text.startswith("$'", i):  # backslash escapes hold inside
            out.append("$'")
            quote, i = "$'", i + 2
            continue
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t\r\n;&|()<>"):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _heredoc_word(text: str, i: int) -> tuple[str, int]:
    """The word at `i` as written (quotes kept) and where it ends."""
    start = i
    while i < len(text) and text[i] not in " \t\r\n;&|<>()":
        if text[i] in "'\"":
            close = text.find(text[i], i + 1)
            i = len(text) if close < 0 else close + 1
        else:
            i += 2 if text[i] == "\\" else 1
    return text[start:i], i


def _substitutions(body: str) -> list[str]:
    """The commands an unquoted here-document body runs: its $(...) and `...` substitutions."""
    found, i = [], 0
    while i < len(body):
        if body[i] == "\\":
            i += 2
        elif body.startswith("$(", i):
            end = _closing(body, i + 2, "(", ")")
            found.append(body[i + 2 : end - 1])
            i = end
        elif body[i] == "`":
            end = body.find("`", i + 1)
            found.append(body[i + 1 : len(body) if end < 0 else end])
            i = len(body) if end < 0 else end + 1
        else:
            i += 1
    return found


_BODIES: contextvars.ContextVar[tuple[dict, list]] = contextvars.ContextVar("notes_gate_bodies")


def _bodies() -> tuple[dict, list]:
    """The here-document bodies this check has met (number by body, bodies by number): a long body must not travel
    inside a word, which shlex reads in quadratic time."""
    found = _BODIES.get(None)
    if found is None:
        found = ({}, [])
        _BODIES.set(found)
    return found


def _heredocs(text: str) -> tuple[str, list[str]]:
    """`text` without its here-documents' bodies (data, never commands; an unquoted body's substitutions stay, each
    a line), the bodies, and each `<<WORD` made to carry its body by number, so the word that opened it still does."""
    out, quote, i, pending, bodies = [], "", 0, [], []
    while i < len(text):
        ch = text[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif ch == "\\":
            out.append(text[i : i + 2])
            i += 2
            continue
        elif quote:
            quote = "" if ch == quote[-1] else quote
        elif text.startswith("$'", i):
            out.append("$'")
            quote, i = "$'", i + 2
            continue
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t\r\n;&|()<>"):
            end = text.find("\n", i)
            out.append(text[i : len(text) if end < 0 else end])
            i = len(text) if end < 0 else end
            continue
        elif text.startswith("<<<", i):  # a here-string: its word stays a word of the command
            out.append("<<<")
            i += 3
            continue
        elif text.startswith("<<", i):
            tabs = text.startswith("<<-", i)
            start = i + 2 + tabs
            start += len(text[start:]) - len(text[start:].lstrip(" \t"))
            word, end = _heredoc_word(text, start)
            if word and not word.startswith(CARRIED):  # a word already carrying a body is left as it is
                delimiter = re.sub(r"\\(.)|['\"]", r"\1", word)
                pending.append((delimiter, tabs, delimiter != word, len(out)))
                out.append(text[i:end])
                i = end
                continue
        elif ch == "\n":  # the end of an opener's line, where its body starts
            out.append(ch)
            i += 1
            for delimiter, tabs, quoted, at in pending:
                body = []
                while i < len(text):
                    end = text.find("\n", i)
                    line, i = text[i : len(text) if end < 0 else end], len(text) if end < 0 else end + 1
                    if (line.lstrip("\t") if tabs else line) == delimiter:
                        break
                    body.append(line)
                bodies.append("\n".join(body))
                numbers, listed = _bodies()
                number = numbers.setdefault((bodies[-1], quoted), len(listed))
                listed += [(bodies[-1], quoted)] if number == len(listed) else []
                out[at] = "<<" + CARRIED + str(number)  # the opener word carries the body
                out += [] if quoted else [run + "\n" for run in _substitutions(bodies[-1])]
            pending = []
            continue
        out.append(ch)
        i += 1
    return "".join(out), bodies


def _unheredoc(text: str) -> str:
    return _heredocs(text)[0]


def _plain_quotes(text: str) -> str:
    """`text` with each $'...' string as a plain '...' one, its escapes dropped: shlex cannot read the escaped quote
    that leaves such a string open, and would end it early."""
    out, quote, i = [], "", 0
    while i < len(text):
        ch = text[i]
        if quote == "$'" and ch == "\\":
            i += 2
            continue
        if quote in ("'", "$'"):
            quote = "" if ch == "'" else quote
        elif ch == "\\":
            out.append(text[i : i + 2])
            i += 2
            continue
        elif quote:
            quote = "" if ch == quote else quote
        elif text.startswith("$'", i):
            out.append("'")
            quote, i = "$'", i + 2
            continue
        elif ch in "'\"":
            quote = ch
        out.append(ch)
        i += 1
    return "".join(out)


def _dollar_parens(text: str) -> str:
    """`text` with each `...` substitution outside single quotes written $(...), as the shell runs it."""
    out, quote, i = [], "", 0
    while i < len(text):
        ch = text[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif ch == "\\":
            out.append(text[i : i + 2])
            i += 2
            continue
        elif ch == "`":
            end = text.find("`", i + 1)
            end = len(text) if end < 0 else end
            out.append("$(" + text[i + 1 : end] + ")")
            i = end + 1
            continue
        elif quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        out.append(ch)
        i += 1
    return "".join(out)


def _inert_quotes(text: str) -> str:
    """`text` with each glob or brace character that is quoted or escaped, and each such $ but in double quotes, made
    inert: text, as the shell reads it (a substitution or expansion inside double quotes keeps its own quoting)."""
    out, quote, i = [], "", 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and quote != "'":
            out.append(ch + text[i + 1 : i + 2].translate(INERT))
            i += 2
            continue
        if quote == '"' and text.startswith(("$(", "${"), i):
            end = _closing(text, i + 2, text[i + 1], ")}"[text[i + 1] == "{"])
            out.append(text[i:end])
            i = end
            continue
        if quote:
            quote = "" if ch == quote else quote
            ch = ch.translate(INERT) if quote == "'" or ch != "$" else ch
        elif ch in "'\"":
            quote = ch
        out.append(ch)
        i += 1
    return "".join(out)


def _word_end(text: str, i: int) -> int:
    """Where the shell word that starts at `i` ends: at white space or an operator outside its quotes."""
    while i < len(text) and text[i] not in " \t\r\n;&|()":
        if text[i] == "\\":
            i += 2
        elif text[i] == "'":
            close = text.find("'", i + 1)
            i = len(text) if close < 0 else close + 1
        elif text[i] == '"':
            i += 1
            while i < len(text) and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
        else:
            i += 1
    return min(i, len(text))


def _emptied(word: str, before: list[str]) -> str:
    """`word` with its value emptied when it is the message of the git or gh command whose words come `before` it."""
    lead = next((n for n, w in enumerate(before) if not (ASSIGNMENT.fullmatch(w) or w in LAUNCHERS | KEYWORDS)), None)
    command = before[lead:] if lead is not None else []
    tool = command[0].rsplit("/", 1)[-1] if command else ""
    flags = MESSAGE_FLAGS.get(tool, ())
    if not flags or (tool == "git" and not {"commit", "tag"} & set(command[1:])):
        return word
    prefix, _, value = word.partition("=")
    if prefix in flags and prefix[:2] == "--" and MESSAGE_VALUE.fullmatch(value):  # --message="..."
        return prefix + "=''"
    after_flag = command[-1] in flags or (tool == "git" and re.fullmatch(r"-[A-Za-z]*m", command[-1]) is not None)
    return "''" if after_flag and MESSAGE_VALUE.fullmatch(word) else word


def _unmessaged(text: str) -> str:
    """Commit messages, tag messages and gh titles/bodies are data, not write paths; substitutions still execute."""
    if not MESSAGE_HINT.search(text) or any(mark in text for mark in ("$(", "`", "$'")):
        return text
    out, words, i = [], [], 0
    while i < len(text):
        if text[i] in " \t\r\n;&|()":
            words = [] if text[i] in "\n;&|()" else words
            out.append(text[i])
            i += 1
        elif text[i] == "#":  # at a word's start, so after white space or an operator
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            out.append(text[i:end])
            i = end
        else:
            end = _word_end(text, i)
            out.append(_emptied(text[i:end], words))
            words.append(text[i:end])
            i = end
    return "".join(out)


def _lexer(text: str) -> shlex.shlex:
    lexer = shlex.shlex(
        _inert_quotes(
            _unmessaged(_plain_quotes(_uncommented(_dollar_parens(_unheredoc(PWD_FORMS.sub("${PWD}", text))))))
        ),
        posix=True,
        punctuation_chars="();<>|&\n",
    )
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    return lexer


def _split(text: str) -> list[str]:
    """Shell words, with operators (; & | < > parentheses and newlines) as words of their own even without spaces
    around them; $(pwd) and `pwd` read as $PWD, so they stay part of their word; here-document bodies are data."""
    return list(_lexer(text))


def _tokens(text: str) -> tuple[list[str], bool]:
    """The shell words of `text` and whether it parsed whole; at an unbalanced quote, the words before it, whose
    bindings still count."""
    words = []
    try:
        for word in _lexer(text):
            words.append(word)
    except ValueError:
        return words, False
    return words, True


def _fragments(text: str) -> set[str]:
    """Every run of `text` that could be a path: its words between spaces, quotes, operators and code punctuation,
    each quoted string whole (an escaped space stays in its word), and each without the punctuation of prose."""
    text = text.replace("\\ ", ESCAPED_SPACE)
    found = set(FRAGMENT.findall(text))
    found |= {(single or double).rsplit("=", 1)[-1] for single, double in QUOTED.findall(text)}  # also --out=<path>
    found |= {f.rstrip(".,:;!?]}") for f in found}
    return {f.replace(ESCAPED_SPACE, " ") for f in found if 0 < len(f) < C.NOTES_GATE_WORD_MAX and "\0" not in f}


def _mentioned(text: str, notes: _Notes, values: dict[str, set[str]], env: dict, folders: list[str]) -> bool:
    text = PWD_FORMS.sub("${PWD}", text)
    known = {name: set(found) for name, found in values.items()}
    folders = [folder for folder in folders if not HOLE.search(folder)]
    shell, bodies = _heredocs(text)
    for part, bases in ((_unmessaged(shell), []), *((body, folders) for body in bodies)):
        for fragment in _fragments(part):
            _tick()
            for word in [w for w in _expansions(fragment, known, {**env, "PWD": HERE}) if not URL.match(w)]:
                if not bases and (HERE in word or VISITED in word):
                    continue  # where the shell is when a word runs: the parser follows that exactly
                if any(notes.mentions(w, bases) for w in _located(word, folders, folders)):
                    return True
    return False


def _pieces(token: str) -> set[str]:
    """A shell word and the paths inside it: its parts around = , : quotes and brackets, strings quoted inside it (code
    passed to an interpreter), and the path an option carries glued on (-C/path)."""
    parts = re.split(r"[=,:'\"`(){}\[\]<>|;&\s]+", token)
    pieces = {token, *parts, *(run for part in parts if part[:1] == "-" for run in PATH_LIKE.findall(part))}
    pieces.update(quoted for _, quoted in re.findall(r"(['\"])(.*?)\1", token))
    return {p for p in pieces if 0 < len(p) < C.NOTES_GATE_WORD_MAX and "\0" not in p}


def _script(words: list[str]) -> int | None:
    """Where the script is in `<shell> [options] -c script`, a literal script for bash, sh, zsh, dash or ksh (also after
    env, exec, sudo, nohup, time and the like, with their options); None for any other simple command."""
    i = 0
    while i < len(words) and (
        words[i] in KEYWORDS
        or words[i] in LAUNCHERS
        or ASSIGNMENT.fullmatch(words[i])
        or (i and (words[i].startswith("-") or re.fullmatch(r"[\d.]+[smhd]?", words[i])))
    ):
        i += 1
    if i >= len(words) or Path(words[i]).name not in SHELLS:
        return None
    i, script = i + 1, False
    while i < len(words) and words[i].startswith(("-", "+")) and words[i] not in ("-", "--"):
        option = words[i]
        if option.startswith("--"):
            i += 2 if option in ("--rcfile", "--init-file") else 1
            continue
        script = script or "c" in option[1:]
        i += 2 if option[-1] == "o" or option in ("-O", "+O") else 1  # -o and -O take a value
    i += words[i : i + 1] == ["--"]
    return i if script and i < len(words) else None


def _inline(words: list[str], depth: int = 0) -> list[str]:
    out, command = [], []
    for word in [*words, None]:
        _tick()
        if word is not None and not (word and set(word) <= SEPARATING):
            command.append(word)
            continue
        fed = _fed(command) if depth < C.NOTES_GATE_NESTING else None
        runs = [run.translate(LIVE) for word in command for run in _substitutions(word)]
        runs += [line for code in _codes(command, fed is None) for line in _code_commands(code)]
        for run in runs if depth < C.NOTES_GATE_NESTING else []:
            with contextlib.suppress(ValueError):  # what the shell runs first, before the command, or what it runs
                out += ["(", *_inline(_split(run), depth + 1), ")", ";"]
        out += _spliced(command, depth)
        if fed is not None:  # a script on the shell's input, with its words as $0, $1 ... like a -c script
            given = [f"__{n}={arg}" for n, arg in enumerate(fed[1])] + [f"__all={arg}" for arg in fed[1][1:]]
            with contextlib.suppress(ValueError):
                out += [
                    "(",
                    *given,
                    *([";"] if given else []),
                    *_inline(_split(POSITIONAL.sub(_positional, fed[0])), depth + 1),
                    ")",
                ]
        out += [] if word is None else [word]
        command = []
    return out


def _positional(match: re.Match) -> str:
    """$0, $1 ... as the variables a spliced script's words are bound to; $@ and $* as all of them but $0."""
    which = match[1] or match[2]
    return "${__" + ("all" if which in "@*" else which) + "}"


def _spliced(command: list[str], depth: int) -> list[str]:
    """A simple command with the literal script it gives a shell spliced in as a subshell, where the words after the
    script are its $0, $1 and so on ($@ and $* all but $0), as the shell gives them."""
    i = _script(command) if depth < C.NOTES_GATE_NESTING else None
    if i is None:
        return command
    args = command[i + 1 :]  # a redirection among them only adds values
    try:
        inner = _inline(_split(POSITIONAL.sub(_positional, command[i].translate(LIVE))), depth + 1)
    except ValueError:
        return command
    given = [f"__{n}={arg}" for n, arg in enumerate(args)] + [f"__all={arg}" for arg in args[1:]]
    return [*command[:i], "(", *given, *([";"] if given else []), *inner, ")", *command[i + 1 :]]


def _code_args(code: str, i: int, depth: int = 0) -> list[str]:
    """The arguments of the call whose name ends at `i`: inside its parentheses, else (Ruby, Perl) to the end of the
    statement; split at top-level commas, each without a keyword-argument name, a list's items as arguments too."""
    if depth > C.NOTES_GATE_NESTING:
        return []
    while i < len(code) and code[i] in " \t":
        i += 1
    if code[i : i + 1] in ("(", "["):
        level, quote, j = 0, "", i
        for j in range(i, len(code)):
            ch = code[j]
            if quote:
                quote = "" if ch == quote and code[j - 1] != "\\" else quote
            elif ch in "'\"`":
                quote = ch
            elif ch in "([{":
                level += 1
            elif ch in ")]}":
                level -= 1
                if not level:
                    break
        inner = code[i + 1 : j if not level else len(code)]
    else:
        inner = re.split(r"[;\n]|\s(?:do|if|unless|or|and)\s", code[i:], maxsplit=1)[0]
    args, level, quote, start = [], 0, "", 0
    for j, ch in enumerate(inner + ","):
        if quote:
            quote = "" if ch == quote and inner[j - 1 : j] != "\\" else quote
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            level += 1
        elif ch in ")]}":
            level -= 1
        elif ch == "," and not level:
            arg = re.sub(r"^\s*\w+\s*=(?!=)", "", inner[start:j]).strip()  # drop a keyword argument's name
            args += _code_args(arg, 0, depth + 1) if arg[:1] == "[" and arg[-1:] == "]" else [arg] if arg else []
            start = j + 1
    return args


def _code_paths(expr: str, names: dict[str, list[str]]) -> list[str]:
    """Each value an expression in interpreter code may give: its strings and named values in order, joined by / where
    a call or / joins them and as they are where + or . does; a name never set is a hole (NOTES_GATE_FILLINGS cap)."""
    parts, joins, end = [], [], None
    for m in VALUE.finditer(expr):
        if m[1]:
            fill = lambda n: "~" if n[1] == HOME_CODE else (names.get(n[1]) or [_hole("")])[0]  # noqa: E731
            value = [INTERPOLATED.sub(fill, re.sub(r"\\(.)", r"\1", m[2])).replace(HOME_CODE, "~")]
        elif m[4]:
            value = ["~"]
        elif m[3] in NOT_VALUES:
            continue
        else:
            value = names.get(m[3]) or [_hole("")]
        between = "" if end is None else expr[end : m.start()]
        joins.append("" if end is None or (("+" in between or " . " in between) and "," not in between) else "/")
        parts.append(value)
        end = m.end()
    combos = islice(product(*parts), C.NOTES_GATE_FILLINGS) if parts else []
    return [re.sub(r"(?<!:)/{2,}", "/", "".join(j + v for j, v in zip(joins, combo, strict=True))) for combo in combos]


def _shell_word(path: str) -> str:
    """`path` as one shell word with nothing in it filled in, keeping a leading ~ (the code reads the home folder from
    the environment) and what the shell had already expanded before the interpreter saw it."""
    head, rest = ("~", path[1:]) if path == "~" or path.startswith("~/") else ("", path)
    return head + ('"' + re.sub(r'(["\\`])', r"\\\1", rest) + '"' if rest else "")


def _receiver(code: str, dot: int) -> str:
    """The expression a method call at `dot` is made on: back over names, dots and one bracketed call."""
    i = dot
    while i > 0 and (code[i - 1] == ")" or code[i - 1].isalnum() or code[i - 1] in "_."):
        if code[i - 1] == ")":
            depth, i = 0, i - 1
            while i > 0:
                depth += (code[i] == ")") - (code[i] == "(")
                if not depth:
                    break
                i -= 1
        else:
            i -= 1
    return code[i:dot]


def _code_commands(code: str) -> list[str]:
    mods = {
        "fs": "fs",
        "fs/promises": "fsp",
        "node:fs": "fs",
        "node:fs/promises": "fsp",
        "path": "path",
        "node:path": "path",
        "os": "os",
        "node:os": "os",
        "child_process": "child_process",
    }
    code = re.sub(r"require\(\s*(['\"])([\w./:-]+)\1\s*\)\.", lambda m: mods.get(m[2], "_") + ".", code)
    code = HOMES.sub(HOME_CODE, code)
    names: dict[str, list[str]] = {}
    for name, value in SET_IN_CODE.findall(code):
        names[name] = [*names.get(name, []), *_code_paths(value, names)][: C.NOTES_GATE_FILLINGS]
    found: list[tuple[str, list[str], list[str]]] = []  # a verb, the paths it takes, a move's destinations
    for m in DESTROYS.finditer(code):
        call, args = m[0], _code_args(code, m.end())
        method = call.rsplit(".", 1)[-1]
        if method.startswith(("rename", "replace", "move", "mv")):
            moved = [path for arg in (args[:-1] or args) for path in _code_paths(arg, names)]
            found.append(("mv", moved, _code_paths(args[-1], names) if len(args) > 1 else [_hole("")]))
            continue
        paths = [path for arg in args for path in _code_paths(arg, names)]
        if (call[:1] == "." or call.startswith(("fs", "promises", "("))) and "recursive" not in ",".join(args):
            found += [("rmdir", paths, [])] if method.startswith("rmdir") else []  # node's rm needs recursive
        else:
            found.append(("rmdir" if method in ("rmdir", "removedirs", "delete") else "rm -r", paths, []))
    for m in METHODS.finditer(code):
        receiver, args = _receiver(code, m.start()), _code_args(code, m.end())
        made = re.match(r"(?:pathlib\.)?Path\(|Pathname\.new\(", receiver)
        if made or m[1] != "replace":  # a str.replace is text, not a move
            given = _code_args(receiver, made.end() - 1) if made else []
            paths = _code_paths(given[0], names) if given else names.get(receiver, [])
            if m[1] in ("rename", "replace"):
                found.append(("mv", paths, _code_paths(args[0], names) if args else [_hole("")]))
            else:
                found.append(("rm -r" if m[1] == "rmtree" else "rmdir", paths, []))
    lines = [f"{verb} {_shell_word(p)} {' '.join(map(_shell_word, to))}" for verb, paths, to in found for p in paths]
    for m in RUNS.finditer(code):
        values = [_code_paths(arg, names) for arg in _code_args(code, m.end())]
        if values[:1] and values[0][:1] and len(values[0][0].split()) > 1:  # a whole command line a shell runs
            lines += values[0]
        elif (words := [v[0] for v in values if v]) and Path(words[0]).name in ("rm", "rmdir", "mv"):
            lines.append(" ".join(map(_shell_word, words)))
    lines += [m[2] for m in LITERAL.finditer(code) if m[1] == "`" and m[2].split()[:1] in (["rm"], ["rmdir"], ["mv"])]
    return lines


def _carried(word: str) -> tuple[str, bool] | None:
    """The here-document body a word carries and whether its delimiter was quoted (then the body does not expand);
    None for a word that carries none."""
    if not word.startswith(CARRIED):
        return None
    listed = _bodies()[1]
    return listed[int(word[1:])] if word[1:].isdigit() and int(word[1:]) < len(listed) else None


def _fed(command: list[str]) -> tuple[str, list[str]] | None:
    """The script a shell runs from its input and the words bound to $0, $1 ... when `command` is a shell given a
    here-document or here-string and no -c or script file (`-s` allowed); None for any other command."""
    i = 0
    while i < len(command) and (
        command[i] in KEYWORDS
        or command[i] in LAUNCHERS
        or ASSIGNMENT.fullmatch(command[i])
        or (i and command[i][:1] == "-")
    ):
        i += 1
    if i >= len(command) or Path(command[i]).name not in SHELLS:
        return None
    words, script, j = [], None, i + 1
    while j < len(command):
        if REDIRECT.fullmatch(command[j]):
            target = command[j + 1] if j + 1 < len(command) else ""
            if words and words[-1].isdigit():  # the stream number a redirection names, not a word
                words.pop()
            body = _carried(target)
            if command[j] == "<<" and body:
                script = body[0]
            elif command[j] == "<<<":
                script = target.translate(LIVE)
            j += 2
            continue
        if not (command[j] and set(command[j]) <= SEPARATING):
            words.append(command[j])
        j += 1
    options = list(takewhile(lambda word: word.startswith(("-", "+")), words))
    operands = words[len(options) :]
    short = {c for o in options if o[:2] != "--" for c in o[1:]}
    if script is None or "c" in short or (operands and "s" not in short):  # -c, or a script file with no -s
        return None
    return script, [command[i], *operands]


def _codes(command: list[str], include_bodies: bool = True) -> list[str]:
    """The interpreter code a simple command passes: the body of a here-document it feeds an interpreter (a $ left
    text when the delimiter was quoted), and each of its words that calls a destructive or running function."""
    found = []
    for word in command:
        body = _carried(word)
        if body is not None:  # a here-document's body is code, but the word that carries it is not
            if include_bodies:
                found.append(body[0].translate(INERT_DOLLAR) if body[1] else body[0])  # a quoted body does not expand $
        elif DESTROYS.search(word) or RUNS.search(word) or METHODS.search(word) or "`" in word:
            found.append(word.translate(str.maketrans("\x0e\x0f\x10\x11", "*?[{")))  # globs back; a $ stays inert
    return found


def _union(*groups: list[str]) -> list[str]:
    """The folders of every group, in order; past NOTES_GATE_FOLDERS the rest is any folder (a hole)."""
    folders = list(dict.fromkeys(f for group in groups for f in group))
    return folders if len(folders) <= C.NOTES_GATE_FOLDERS else [*folders[: C.NOTES_GATE_FOLDERS], _hole("")]


def _join(folder: str, word: str) -> str:
    """`word` reached from `folder`; one that starts with a hole may be absolute, so it stands on its own."""
    return word if word.startswith("${") else str(Path(folder) / word)


def _move(words: list[str], here: list[str], stack: list, seen: list[str], fill) -> list[str] | None:
    """The folders the simple command `words` may leave the shell in when it is cd, pushd or popd; None otherwise."""
    while words and (words[0] in KEYWORDS or ASSIGNMENT.fullmatch(words[0])):
        words = words[1:]
    if not words or words[0] not in ("cd", "pushd", "popd"):
        return None
    command, args = words[0], [a for a in words[1:] if a == "-" or not a.startswith("-")]
    if command == "popd":
        return stack.pop() if stack else here
    if command == "pushd" and not args:  # it swaps the top two
        return _union(here, stack[-1]) if stack else here
    if command == "pushd":
        stack.append(here)
    if args[:1] == ["-"]:  # back to wherever it was before
        return list(seen)
    return _union([_join(folder, word) for folder in here for word in fill(args[0] if args else "~", here, seen)])


def _writes(words: list[str]) -> bool:
    """Whether a simple command may write a file its words name: anything but one plain read, a cd, or a builtin that
    only prints or sets variables."""
    while words and (words[0] in KEYWORDS or ASSIGNMENT.fullmatch(words[0])):
        words = words[1:]
    return bool(words) and words[0] not in NO_WRITES and not _plain_read(words)


def _places(argv: list[str], base: str, fill) -> tuple[list[list[str]], list[bool], list[tuple], list[str]]:
    """For each word of a script, the folders its command may run in, whether it may be written (a redirection's
    target or a writing command's word) and what it is to its command (`_roles`), and every folder visited."""
    here, stack, saved, seen = [base], [], [], [base]
    places, writes, roles = [], [False] * len(argv), [()] * len(argv)
    command, indexes, moved, redirect = [], [], None, None
    for k, token in enumerate([*argv, "\n"]):  # the last newline ends the last command
        _tick()
        places.append(here)
        if token and set(token) <= SEPARATING:
            if command:
                moved = _move(command, here, stack, seen, fill)
                written = _writes(command)
                for i, role in zip(indexes, _roles(command), strict=True):
                    writes[i], roles[i] = written, role
                command, indexes = [], []
            for op in SEPARATORS.findall(token):
                if op == "(":
                    saved.append((here, list(stack)))
                    continue
                if moved is not None:
                    here = moved if op == "&&" else _union(here, moved)
                    seen = _union(seen, moved)
                    moved = None
                if op == ")" and saved:
                    here, stack = saved.pop()
        elif REDIRECT.fullmatch(token):  # its target is not an argument, nor the digits naming its stream (2>)
            redirect = ">" in token
            if command and command[-1].isdigit():
                command.pop()
                indexes.pop()
        elif redirect is not None:
            writes[k], redirect = redirect, None
        else:
            command.append(token)
            indexes.append(k)
    return places[: len(argv)], writes, roles, seen


def _landing(command: str, source: str) -> str:
    """The name a copied or moved `source` (its expansions filled in) lands under in a destination folder; '' when its
    contents land there."""
    if command == "ditto" or (command == "rsync" and source.endswith("/")):
        return ""
    name = source.rstrip("/").rsplit("/", 1)[-1]
    return "*" if "$" in name or "`" in name else name  # what nowitknows cannot resolve may be any name


def _roles(words: list[str]) -> list[tuple]:
    i = 0
    while i < len(words) and (
        words[i] in KEYWORDS or words[i] in LAUNCHERS or ASSIGNMENT.fullmatch(words[i]) or (i and words[i][:1] == "-")
    ):
        i += 1
    name, rest, operands, ended = Path(words[i]).name if i < len(words) else "", range(i + 1, len(words)), [], False
    target, takes = None, name in ("cp", "mv", "install")  # -t names their destination folder
    for j in rest:
        if target == "next":
            target = (j, words[j])
        elif not ended and words[j] == "--":
            ended = True
        elif not ended and takes and words[j] in ("-t", "--target-directory"):
            target = "next"
        elif not ended and takes and words[j].startswith(("-t", "--target-directory=")):
            target = (j, words[j].split("=", 1)[1] if words[j].startswith("--") else words[j][2:])
        elif ended or words[j][:1] != "-" or words[j] == "-":
            operands.append(j)
    options = [words[j] for j in rest if words[j][:1] == "-"]
    short = {c for o in options if not o.startswith("--") for c in o[1:]}
    long = {o.split("=")[0] for o in options if o.startswith("--")}
    gone, into = [], []
    if name == "rm":
        gone = operands if short & set("rRd") or long & {"--recursive", "--dir"} else []
    elif name == "rmdir":
        gone = operands
    elif name == "install" and ("d" in short or "--directory" in long):
        into = [(j, words[j], ("",), False) for j in operands]
    elif name in ("cp", "mv", "install", "rsync", "ditto"):
        sources = operands if isinstance(target, tuple) else operands[:-1]
        k, dest = target if isinstance(target, tuple) else (operands[-1], words[operands[-1]]) if operands else (0, "")
        folder = isinstance(target, tuple) or len(sources) > 1 or dest.endswith("/") or name in ("rsync", "ditto")
        into = [(k, dest, tuple(words[j] for j in sources), folder)] if sources else []
        gone = sources if name == "mv" or "--remove-source-files" in long else []
    elif name in ("chmod", "chown", "chgrp"):
        gone = operands[1:] if "R" in short or "--recursive" in long else []
    elif name == "find" and any(
        words[j] == "-delete"
        or (
            words[j] in ("-exec", "-execdir", "-ok", "-okdir")
            and _writes([w for w in takewhile(lambda w: w not in (";", "+"), words[j + 1 :]) if w != "{}"])
        )
        for j in rest
    ):  # it deletes, or runs on what it finds something that may write
        gone = list(takewhile(lambda j: words[j][:1] not in ("-", "(", "!"), rest))
    roles: list[tuple] = [()] * len(words)
    for j in gone:
        roles[j] = ("gone",)
    for k, dest, sources, folder in into:
        roles[k] = ("into", dest, sources, folder, name)
    return roles


def _parameters(script: str) -> list[str]:
    """The expansions in `script` outside single quotes: $NAME, ${...} and $(...), which leaves a hole."""
    quote, i, found = "", 0, []
    script = PWD_FORMS.sub("${PWD}", script)
    while i < len(script):
        ch = script[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif ch == "\\":
            i += 1
        elif script.startswith(("${", "$("), i):
            end = _closing(script, i + 2, script[i + 1], "}" if script[i + 1] == "{" else ")")
            found.append(script[i:end])
            i = end
            continue
        elif ch == "$" and (m := re.match(r"\$(?:[A-Za-z_]\w*|[0-9@*#?$!-])", script[i:])):
            found.append(m[0])
            i += len(m[0])
            continue
        elif quote == '"':
            quote = "" if ch == '"' else quote
        elif ch in "'\"":
            quote = ch
        i += 1
    return found


def _only_reads(script: str, env: dict, resolved, depth: int = 0) -> bool:
    if depth > C.NOTES_GATE_NESTING:
        return False
    text, quote, i, start, stages = _uncommented(_unheredoc(PWD_FORMS.sub("${PWD}", script))), "", 0, 0, []
    while i < len(text):
        ch = text[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif ch == "\\":
            i += 1
        elif text.startswith("$(", i):
            end = _closing(text, i + 2, "(", ")")
            if not _only_reads(text[i + 2 : end - 1], env, resolved, depth + 1):
                return False
            i = end
            continue
        elif ch == "`":
            return False
        elif quote == '"':
            quote = "" if ch == '"' else quote
        elif ch in "'\"":
            quote = ch
        elif text.startswith("$'", i):  # quoting shlex reads otherwise, where $'\x2di' is -i
            return False
        elif ch in "|;\n()" or (ch == "&" and text[i + 1 : i + 2] != ">" and text[i - 1 : i] not in ("<", ">")):
            stages.append(text[start:i])
            start = i + 1
        i += 1
    return all(_reads(stage, env, resolved, depth) for stage in [*stages, text[start:]])


def _unredirected(words: list[str]) -> tuple[list[str], bool]:
    """(the words of a command without its redirections, whether those write nothing: input, /dev/null or a stream)."""
    kept, redirect = [], None
    for word in words:
        if redirect is not None:
            stream = redirect.endswith("&") and re.fullmatch(r"\d+|-", word)
            if ">" in redirect and word != "/dev/null" and not stream:
                return kept, False
            redirect = None
        elif REDIRECT.fullmatch(word):
            if kept and kept[-1].isdigit():
                kept.pop()
            redirect = word
        else:
            kept.append(word)
    return kept, True


def _reads(stage: str, env: dict, resolved, depth: int) -> bool:
    try:
        words = _split(stage)
    except ValueError:
        return False
    kept, quiet = _unredirected(words)
    if not quiet:
        return False
    fed = _fed(words)  # a shell given a here-document or here-string reads only if that script does
    if fed is not None:
        here = next(n for n, w in enumerate(words) if Path(w).name in SHELLS)
        launch = words[:here]
        if any(ASSIGNMENT.fullmatch(word) for word in launch) or "nohup" in launch:
            return False
        return _only_reads(fed[0], env, resolved, depth + 1)
    while kept and kept[0] in KEYWORDS | {"done", "fi"}:
        kept = kept[1:]
    lead = next((n for n, word in enumerate(kept) if not ASSIGNMENT.fullmatch(word)), len(kept))
    if lead and lead < len(kept):  # a variable set for one command is in its environment
        return False
    if not kept or kept[0] in ("cd", "pushd", "popd"):
        return True
    if lead or kept[0] in ("for", "select"):
        names = [ASSIGNMENT.fullmatch(word)[1] for word in kept] if lead else kept[1:2]
        return len(names) > 0 and all(NAME.fullmatch(name) and name not in {*env, "IFS"} for name in names)
    if (i := _script(kept)) is not None:
        if any(ASSIGNMENT.fullmatch(word) for word in kept[:i]) or "nohup" in kept[:i]:
            return False
        return _only_reads(kept[i].translate(LIVE), env, resolved, depth + 1)  # read again as the shell will
    if kept[0] in ("sed", "rg", "sort", "find") and (_glob_start(stage) or not resolved(stage)):
        return False  # a name the shell fills in, or a value, could be -i, --pre, -o or -delete
    return _plain_read(kept)


def _glob_start(stage: str) -> bool:
    """Whether a word of `stage` starts with a glob outside quotes, so the shell could fill in a name like -i."""
    quote, previous = "", " "
    for ch in stage:
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif GLOB.match(ch) and previous in " \t\r\n;&|()<>":
            return True
        previous = ch
    return False


def _writes_sorted(option: str) -> bool:
    """Whether a word given to sort is an option that writes a file or runs a program: -o in a cluster, or --output
    or --compress-program however far abbreviated."""
    if option.startswith("--"):
        name = option.split("=", 1)[0]
        return len(name) > 2 and any(long.startswith(name) for long in SORT_WRITES)
    return option[:1] == "-" and "o" in option


def _plain_read(argv: list[str]) -> bool:
    """One read: cat, head, tail, sed -n with one print script, grep, rg, ls, wc or stat by bare name and no option that
    edits or starts a program; or echo, test, sort, find and the like, bar sort -o and find -delete."""
    argv = [arg.translate(LIVE) for arg in argv]  # as the program sees them
    if argv[:1] and argv[0] in PRINTS:
        return True
    if argv[:1] and argv[0] in QUIET:
        args = argv[1:]
        if argv[0] == "sort":
            return not any(_writes_sorted(a) for a in args)
        return not (argv[0] == "find" and FIND_ACTS & set(args))
    if not argv or argv[0] not in READS:
        return False
    args = argv[1:]
    if argv[0] == "sed":
        return (
            len(args) >= 2
            and args[0] == "-n"
            and bool(SED_PRINT.fullmatch(args[1]))
            and not any(a.startswith("-") for a in args[2:])
        )
    return not (argv[0] == "rg" and any(a == f or a.startswith(f + "=") for a in args for f in RG_RUNS))


def _command(data: dict) -> tuple[list[str], str | None, bool]:
    """(argv, shell script, whether it parsed whole): a string runs in a shell; an argv list runs as it is, unless it
    starts a shell with a script or passes an interpreter destructive code, when it is read as a shell command."""
    command = data.get("command", data.get("cmd"))
    argv = [str(a) for a in command] if isinstance(command, list) else []
    runs_code = _script(argv) is not None or bool(_codes(argv))  # a shell, or an interpreter given destructive code
    script = command if isinstance(command, str) else shlex.join(argv) if runs_code else None
    if script is None:
        return argv, None, True
    words, whole = _tokens(script)
    return words, script, whole


PS_WRITES = frozenset(
    {
        "set-content",
        "add-content",
        "clear-content",
        "clc",
        "out-file",
        "tee-object",
        "tee",
        "new-item",
        "remove-item",
        "move-item",
        "copy-item",
        "rename-item",
        "sc",
        "ac",
        "ni",
        "ri",
        "del",
        "erase",
        "rm",
        "rmdir",
        "rd",
        "mi",
        "move",
        "mv",
        "cpi",
        "copy",
        "cp",
        "rni",
        "ren",
    }
)
PS_REMOVES = frozenset(
    {
        "remove-item",
        "move-item",
        "rename-item",
        "ri",
        "del",
        "erase",
        "rm",
        "rmdir",
        "rd",
        "mi",
        "move",
        "mv",
        "rni",
        "ren",
    }
)
PS_WORD = re.compile(
    r"@'\r?\n.*?\r?\n'@|@\"\r?\n.*?\r?\n\"@|"
    r"'(?:''|[^'])*'|\"(?:`.|[^\"`])*\"|(?:`[^\n]|[^\s'\";|&(){}>=,])+|>>?|[;|&(){}=,\n]",
    re.S,
)
PS_VARIABLE = re.compile(r"\$\{([^}]+)\}|\$((?:env:)?[\w]+)", re.I)


def _powershell(data: dict, env: dict, tool: str = "") -> bool:
    if not C.WINDOWS or tool.lower() == "bash":
        return False
    selected = str(data.get("shell") or env.get("SHELL", "")).replace("\\", "/").rsplit("/", 1)[-1].lower()
    explicit = str(data.get("shell", "")).replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
    if explicit in SHELLS:
        return False
    command = data.get("command", data.get("cmd"))
    first = command[0] if isinstance(command, list) and command else str(command).split(" ", 1)[0]
    named = any(name in str(first).lower() for name in ("powershell", "pwsh"))
    return (
        named
        or selected in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}
        or (C.WINDOWS and explicit not in SHELLS)
    )


def _ps_join(words: list[str], expand) -> str | None:
    """Resolve a literal Join-Path invocation, including its common named parameters."""
    if not words or words[0].casefold() != "join-path":
        return None
    paths, named, parameter = [], {}, None
    for word in words[1:]:
        if word == ",":
            continue
        if word.startswith("-"):
            parameter = word.lower()
            if parameter not in {"-path", "-childpath", "-additionalchildpath"}:
                return None
            continue
        value = expand(word)
        if parameter:
            named.setdefault(parameter, []).append(value)
            if parameter != "-additionalchildpath":
                parameter = None
        else:
            paths.append(value)
    parts = []
    for parameter in ("-path", "-childpath"):
        if parameter in named:
            parts.extend(named[parameter])
        elif paths:
            parts.append(paths.pop(0))
    parts += paths + named.get("-additionalchildpath", [])
    return ntpath.join(*parts) if len(parts) >= 2 else None


def _ps_fold_joins(words: list[str], expand) -> list[str]:
    """Fold only parenthesized Join-Path expressions; other syntax stays visible."""
    result, index = [], 0
    while index < len(words):
        if words[index] == "(":
            end, depth = index + 1, 1
            while end < len(words) and depth:
                depth += (words[end] == "(") - (words[end] == ")")
                end += 1
            if not depth:
                inner = _ps_fold_joins(words[index + 1 : end - 1], expand)
                if (joined := _ps_join(inner, expand)) is not None:
                    result.append("'" + joined.replace("'", "''") + "'")
                    index = end
                    continue
        result.append(words[index])
        index += 1
    return result


def _ps_file_call(words: list[str]) -> tuple[list[str], bool] | None:
    """Destinations of common static .NET file calls; reads return no destinations."""
    match = re.fullmatch(r"\[(?:System\.)?IO\.(File|Directory)\]::(\w+)", words[0], re.I)
    if not match:
        if re.match(r"\[[\w.]+\]::(?:write|append|create|delete|move|replace|set)", words[0], re.I):
            return words[1:], True
        return None
    method = match[2].lower()
    args = [[]]
    depth = 0
    for word in words[1:]:
        if word == "(":
            depth += 1
        elif word == ")":
            depth -= 1
        elif word == "," and depth == 1:
            args.append([])
        else:
            args[-1].append(word)
    paths = [arg[0] for arg in args if arg]
    if method == "copy":
        return paths[1:2], False
    if method in {"move", "replace"}:
        return paths[:3], True
    if (
        method == "open"
        and len(args) > 2
        and any(word.casefold() == "read" or word.casefold().endswith("::read") for word in args[2])
    ):
        return [], False
    if method in {
        "writealltext",
        "writealllines",
        "writeallbytes",
        "appendalltext",
        "appendalllines",
        "create",
        "createtext",
        "appendtext",
        "openwrite",
        "open",
        "delete",
        "createdirectory",
    }:
        return paths[:1], method == "delete"
    if re.match(r"write|append|create|delete|move|replace|set", method):
        return paths, True
    return [], False


def _ps_targets(words: list[str]) -> tuple[list[str], bool, bool]:
    """Return path operands, ancestor-removal behavior and item-pipeline binding."""
    name = words[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if (call := _ps_file_call(words)) is not None:
        return *call, False
    writes = name in PS_WRITES
    if not writes:
        if name in {"invoke-webrequest", "iwr", "wget", "curl", "invoke-restmethod", "irm"}:
            paths = []
            for index, word in enumerate(words[1:], 1):
                flag, colon, value = word.partition(":")
                if flag.casefold() == "-outfile":
                    paths.extend([value] if value else words[index + 1 : index + 2])
            return paths, False, False
        if re.fullmatch(r"(?:set|add|clear|remove|move|copy|rename|new)-(?:itemproperty|acl)", name) or (
            re.match(r"(?:write|update|save|export|out)-", name)
            and name
            not in {"write-output", "write-host", "write-warning", "write-verbose", "write-debug", "write-error"}
        ):
            return words[1:], True, False
        return [], False, False
    positional, named, parameter, previous = [], {}, None, None
    path_flags = {"-path", "-literalpath", "-filepath", "-destination", "-newname"}
    value_flags = {
        "-value",
        "-inputobject",
        "-encoding",
        "-filter",
        "-include",
        "-exclude",
        "-itemtype",
        "-name",
        "-erroraction",
        "-warningaction",
        "-informationaction",
        "-errorvariable",
        "-outvariable",
        "-width",
    }
    switches = {
        "-force",
        "-recurse",
        "-append",
        "-nonewline",
        "-noclobber",
        "-whatif",
        "-confirm",
        "-verbose",
        "-debug",
    }
    for word in words[1:]:
        if word == ",":
            parameter = previous
            continue
        if word in {"(", ")"}:
            continue
        if word.startswith("-"):
            flag, colon, value = word.partition(":")
            parameter = flag.casefold()
            if parameter not in path_flags | value_flags | switches:
                return words[1:], name in PS_REMOVES, False
            if colon and value:
                named.setdefault(parameter, []).append(value)
                previous = parameter
                parameter = None
            continue
        if parameter in path_flags | value_flags:
            named.setdefault(parameter, []).append(word)
            previous, parameter = parameter, None
        else:
            positional.append(word)
            previous = None
    paths = [value for flag in ("-path", "-literalpath", "-filepath") for value in named.get(flag, [])]
    destination = named.get("-destination", [])
    if name in {"copy-item", "cpi", "copy", "cp"}:
        if "-value" in named:
            return paths + positional + destination, False, False
        return destination or positional[-1:] or ["."], False, False
    if name in PS_REMOVES:
        return paths + positional + destination + named.get("-newname", []), True, not (paths or positional)
    if name in {"new-item", "ni"}:
        targets = paths or positional[:1] or ["."]
        if names := named.get("-name"):
            targets = [ntpath.join(path, child) for path in targets for child in names]
        return targets, False, False
    return paths or positional[:1], False, not (paths or positional)


def _ps_gate(notes: _Notes, data: dict, env: dict, base: Path) -> str | None:
    """Recognize common PowerShell writes without treating ordinary reads as writes."""
    command = data.get("command", data.get("cmd"))
    script = " ".join(str(arg) for arg in command) if isinstance(command, list) else command
    if isinstance(command, list) and command and any(name in command[0].lower() for name in ("powershell", "pwsh")):
        for index, arg in enumerate(command):
            if arg.lower() in {"-c", "-command"}:
                script = " ".join(command[index + 1 :])
                break
    elif re.match(r"(?i)^\s*(?:[^\s]*[/\\])?(?:pwsh|powershell)(?:\.exe)?\s", script):
        wrapped = re.search(r"(?i)\s-(?:c|command)\s+(.+)", script, re.S)
        if wrapped:
            script = wrapped[1]
            if script[:1] == script[-1:] and script[:1] in {"'", '"'}:
                script = script[1:-1]
    tokens = PS_WORD.findall(script.replace("`\r\n", "").replace("`\n", ""))
    values = {key.lower(): str(value) for key, value in env.items()}
    values.update(home=env["HOME"], pwd=str(base))

    def expand(word: str) -> str:
        if word.startswith("@'") and word.endswith("'@"):
            return word[2:-2].removeprefix("\r\n").removeprefix("\n").removesuffix("\r\n").removesuffix("\n")
        if word.startswith('@"') and word.endswith('"@'):
            word = word[2:-2].removeprefix("\r\n").removeprefix("\n").removesuffix("\r\n").removesuffix("\n")
        if word.startswith("'") and word.endswith("'"):
            return word[1:-1].replace("''", "'")
        word = word.strip('"')
        word = PS_VARIABLE.sub(lambda m: values.get((m[1] or m[2]).lower().removeprefix("env:"), m[0]), word)
        return re.sub(r"`(.)", r"\1", word)

    def key(word: str) -> str:
        word = re.sub(r"^-(?:LiteralPath|Path|FilePath|Destination|NewName):", "", word, flags=re.I)
        word = word.replace("\\", "/")
        if word.startswith("~/"):
            word = env["HOME"] + word[1:]
        word = re.sub(r"^Microsoft\.PowerShell\.Core/FileSystem::", "", word, flags=re.I)
        path = ntpath.normpath(ntpath.join(str(base), word)).replace("\\", "/")
        if C.WINDOWS:
            # The inventory resolves 8.3 names and links; tool paths must identify the same folders.
            path = realpath(path).replace("\\", "/")
            path = "//" + path[8:] if path[:8].lower() == "//?/unc/" else path.removeprefix("//?/")
        return path.casefold().rstrip("/")

    roots = {key(root) for root, _ in notes.folders}
    bases = {key(root) for root, _ in notes.bases}

    def protected(word: str, removes: bool) -> bool:
        path = key(word)
        if any(path == root or path.startswith(root + "/") for root in roots):
            return True
        if any(path.startswith(root + "/") and path[len(root) + 1 :].split("/")[1:2] == ["memory"] for root in bases):
            return True
        if removes and any(root.startswith(path + "/") for root in roots | bases):
            return True
        if GLOB.search(path):
            prefix = re.split(r"[*?\[]", path, maxsplit=1)[0]
            if any(root.startswith(prefix) or fnmatchcase(root, path) for root in roots | bases):
                return True
        return False

    stage, item_paths, locations = [], [], []
    for token in [*tokens, ";"]:
        if token not in {";", "|", "&", "{", "}", "\n"}:
            stage.append(token)
            continue
        if stage:
            stage = _ps_fold_joins(stage, expand)
            if len(stage) >= 3 and stage[0].startswith("$") and stage[1] == "=":
                expression = stage[2:]
                joined = _ps_join(expression, expand)
                if joined is not None or len(expression) == 1:
                    values[stage[0][1:].lower()] = joined if joined is not None else expand(expression[0])
                stage = expression
            expanded = [expand(word) for word in stage]
            for index, word in enumerate(expanded):
                if word in {">", ">>"} and index + 1 < len(expanded) and protected(expanded[index + 1], False):
                    return DENY
            paths, removes, binds_items = _ps_targets(expanded)
            if binds_items:
                paths += item_paths
            if any(protected(word, removes) for word in paths):
                return DENY
            name = expanded[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
            if name in {"set-location", "sl", "cd", "chdir", "push-location", "pushd", "pop-location", "popd"}:
                if name in {"pop-location", "popd"}:
                    if locations:
                        base = locations.pop()
                else:
                    target = next((word for word in expanded[1:] if not word.startswith("-")), env["HOME"])
                    if name in {"push-location", "pushd"}:
                        locations.append(base)
                    base = Path(key(target))
                values["pwd"] = str(base)
            if name in {"get-item", "get-childitem", "gi", "gci", "dir", "ls"}:
                item_paths = [word for word in expanded[1:] if not word.startswith("-")]
            else:
                item_paths = []
        if token != "|":
            item_paths = []
        stage = []
    return None


def gate(cfg: C.Config, payload: dict) -> str | None:
    """Codex cuts off slow hooks, so oversized or late checks use the bounded text check."""
    started = time.monotonic()
    tool = str(payload.get("tool_name", "")).removeprefix("functions.")
    if tool.startswith(OWN_TOOLS):  # nowitknows's memory tool is the way in
        return None
    data = payload.get("tool_input")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = {"input": data}
    data = data if isinstance(data, dict) else {}
    texts = _strings(data)
    # Any other tool, another MCP server's included, is checked when it carries a patch or a shell command; whatever
    # field an apply_patch call carries its patch in, only the headers of the patch count.
    patch = tool == "apply_patch" or any("*** Begin Patch" in t for t in texts)
    shell = tool != "apply_patch" and isinstance(data.get("command", data.get("cmd")), (str, list))
    edit = tool in EDIT_TOOLS
    if not (patch or shell or edit):
        return None
    home = C.home_dir(cfg.env)
    session = Path(payload.get("cwd") or os.getcwd())
    env = {**os.environ, **cfg.env, "HOME": str(home), "PWD": str(session)}
    workdir = data.get("workdir") or data.get("cwd")
    base = Path(_expand(workdir, env)) if isinstance(workdir, str) else session
    # A workdir that starts with what nowitknows cannot resolve may be absolute: judged by its own text.
    base = base if HOLE.match(str(base)) else session / base
    sid = payload.get("session_id")
    notes = _Notes(cfg, session, sid if isinstance(sid, str) else None)
    left = C.NOTES_GATE_DEADLINE_S - (time.monotonic() - started)  # building the folders counts against the deadline
    if edit:
        paths = [data.get(k) for k in ("file_path", "notebook_path", "path") if isinstance(data.get(k), str)]
        return DENY if any(notes.hold(base / _expand(p, env)) for p in paths) else None
    if not shell:  # a patch
        headers = [path for text in texts for path in _headers(text)]
        near, far = headers[: C.NOTES_GATE_HEADERS], headers[C.NOTES_GATE_HEADERS :]
        if any(notes.hold(base / _expand(path, env)) for path in near) or notes.named("\n".join(far)):
            return DENY
        return None
    if _powershell(data, env, tool):
        return _ps_gate(notes, data, env, base)
    token = _BODIES.set(({}, []))  # the here-document bodies of this check only
    try:
        if sum(len(text) for text in texts) <= C.NOTES_GATE_MAX_CHARS:
            try:
                with _deadline(left):
                    return _full(notes, data, texts, env, base)
            except _Late:
                pass
            except Exception as error:  # a bug, or input it was never meant for: answer the cheap way, and say so
                _report(cfg, error)
        return _cheap(notes, data, texts)
    finally:
        _BODIES.reset(token)


def _full(notes: _Notes, data: dict, texts: list[str], env: dict, base: Path) -> str | None:
    argv, script, whole_script = _command(data)
    if script is not None:
        argv = _inline(argv)
    # Variables the command sets, such as d=~/.claude/projects/<repo>, name paths too, with every value they take.
    values, located = _locals(argv, {**env, "PWD": VISITED}), {**env, "PWD": HERE}
    settled = _settled(argv)

    def fill(word: str, here: list[str], visited: list[str], at: int | None = None) -> list[str]:
        _tick()
        scope = located if at is None else _Scope(located, settled, at)  # what the command set for good is gone
        return [w for e in _expansions(word, values, scope) for w in _located(e, here, visited)]

    # A relative name resolves from each folder its command may run in. An argv list runs without a shell: each
    # argument is one word, spaces included, and nothing moves it.
    if script is not None:
        places, writes, roles, seen = _places(argv, str(base), fill)
    else:
        roles = _roles(argv)
        places, seen = [[str(base)]] * len(argv), [str(base)]
        writes = [_writes(argv)] * len(argv)
    pairs = [
        (p, place, w, r, k)
        for k, (t, place, w, r) in enumerate(zip(argv, places, writes, roles, strict=True))
        for p in _pieces(t)
    ]
    # The call's other strings, and patch headers in the script, from any folder the command may visit.
    loose = (set(texts) - {script}) | set(_headers(script or ""))
    if not whole_script:  # an unbalanced quote: its plain chunks, with what came before
        loose |= {piece for chunk in script.split() for piece in _pieces(chunk)}
    pairs += [
        (word, seen, True, (), None) for word in loose if 0 < len(word) < C.NOTES_GATE_WORD_MAX and "\0" not in word
    ]
    named = any(notes.hold(Path(folder)) for folder in seen if not HOLE.search(folder))
    # A path nowitknows cannot wholly resolve counts only where something may be written, and only when its text (with
    # the folder it is reached from) still names a notes folder specifically.
    blind: list[tuple[str, bool, str, str]] = []  # text, deleted or moved, where it starts as reached, as written
    checked: set[tuple[str, bool, bool]] = set()  # path, written, deleted or moved
    for piece, place, write, role, at in pairs:
        whole = role[:1] == ("gone",)
        for word in fill(piece, place, seen, at):
            if write and HOLE.search(word):  # judged from each folder it may be reached from
                blind += [(word, whole, _join(folder, word), word) for folder in place]
            for folder in place:
                path = _join(folder, word)
                if HOLE.search(folder) and path != word:  # an absolute word is judged whatever the folder
                    if write:
                        blind.append((path, whole, path, word))
                elif (path, write, whole) not in checked:
                    checked.add((path, write, whole))
                    # A write counts where a glob could reach notes; a delete or move, also at what holds them.
                    named = named or notes.hold(Path(path))
                    named = named or (write and GLOB.search(path) and not HOLE.search(path) and notes.covers(path))
                    named = named or (whole and not HOLE.search(path) and notes.endangers(path))
    for k, role in enumerate(roles):  # where a copy or move lands, each source by the value it takes
        if role[:1] == ("into",):
            _, dest, sources, folder, name = role
            names = tuple(dict.fromkeys(_landing(name, w) for s in sources for w in fill(s, places[k], seen, k)))
            for word in fill(dest, places[k], seen, k):
                for here in places[k]:
                    path = _join(here, word)
                    if HOLE.search(path):
                        blind.append((path, True, path, word))
                    else:
                        named = named or notes.lands(path, names, folder)
    named = named or any(notes.points(w, whole, start, "/" not in raw) for w, whole, start, raw in blind)
    # Fall back to literal notes paths because parsing cannot resolve every shell expression.
    raw = script if script is not None else "\n".join(argv)
    if not (named or _mentioned(raw, notes, values, env, seen)):
        return None
    if script is None:
        return None if _plain_read(argv) else DENY

    def resolved(stage: str) -> bool:  # an expansion could give sed -i or rg --pre
        values = [w for e in _parameters(stage) for w in fill(e, [str(base)], seen)]
        return not any(HOLE.search(w) or any(part[:1] == "-" or GLOB.match(part) for part in w.split()) for w in values)

    return None if _only_reads(script, env, resolved) else DENY


def _cheap(notes: _Notes, data: dict, texts: list[str]) -> str | None:
    """A bounded text check still protects explicit notes paths when full parsing cannot fit the hook's deadline."""
    command = data.get("command", data.get("cmd"))
    text = command if isinstance(command, str) else " ".join(map(str, command)) if isinstance(command, list) else ""
    if not notes.named("\n".join(texts)):
        return None
    return None if CHEAP_READ.fullmatch(text) else DENY + CHEAP_NOTE


def _report(cfg: C.Config, error: Exception) -> None:
    """Log an error of the full check where hooks log theirs."""
    from .codex_host import _error

    _error(cfg, f"notes gate: {type(error).__name__}: {error}")
