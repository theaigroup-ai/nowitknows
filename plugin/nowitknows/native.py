from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

from . import config as C
from .memory import MASKED_WRITE_ERROR, TopicStore, commit, mutation, parse_topic, read_generation
from .safety import lines, scan_change

SANITIZE_MAX = 200


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _falsy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("0", "false", "no", "off")


def managed_settings_path() -> Path:
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    if sys.platform.startswith("win"):
        return Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "ClaudeCode" / "managed-settings.json"
    return Path("/etc/claude-code/managed-settings.json")


def _read(p: Path) -> dict:
    try:
        value = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


SETTINGS = ("managed settings", "the repo's local settings", "the repo's settings", "user settings")
LOCAL_SETTINGS, CHECKED_IN_SETTINGS = SETTINGS[1], SETTINGS[2]


def settings_chain(project: Path | None, claude_home: Path, managed: Path | None = None) -> list[dict]:
    """Highest precedence first, matching Claude Code (SETTINGS): managed > local > project > user. Without the repo's
    folder its own two files are unknown and count as empty."""
    repo = project / ".claude" if project is not None else None
    own = [_read(repo / "settings.local.json"), _read(repo / "settings.json")] if repo else [{}, {}]
    return [_read(managed or managed_settings_path()), *own, _read(claude_home / "settings.json")]


def first_setting(chain: list[dict], key: str):
    for s in chain:
        if key in s:
            return s[key]
    return None


def hosted_memory_active(env: dict) -> bool:
    if not _truthy(env.get("CLAUDE_CODE_POST_TURN_MEMORY")):
        return False
    try:
        return bool(json.loads(env.get("CLAUDE_CODE_POST_TURN_MEMORY_CONFIG") or "{}").get("instruction"))
    except ValueError:
        return False


def auto_memory_enabled(env: dict, chain: list[dict]) -> bool:
    flag = env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY")
    if _truthy(flag):
        return False
    if _falsy(flag):
        return True
    if _truthy(env.get("CLAUDE_CODE_SIMPLE")):
        return False
    relocated = env.get("CLAUDE_CODE_REMOTE_MEMORY_DIR") or env.get("CLAUDE_COWORK_MEMORY_PATH_OVERRIDE")
    if _truthy(env.get("CLAUDE_CODE_REMOTE")) and not relocated:  # a cloud VM keeps no memory folder of its own
        return False
    value = first_setting(chain, "autoMemoryEnabled")
    return True if value is None else bool(value)


def sanitize(path: str) -> str:
    """Claude Code's folder-name rule on its NFC path: each UTF-16 unit that is not [a-zA-Z0-9] becomes '-'."""
    return re.sub(
        r"[^a-zA-Z0-9]", lambda m: "-" * (2 if ord(m.group()) > 0xFFFF else 1), unicodedata.normalize("NFC", path)
    )


def java_hash(text: str) -> int:
    """Java's String.hashCode: 31-based over UTF-16 code units, in 32-bit signed arithmetic."""
    data, h = text.encode("utf-16-le", "surrogatepass"), 0
    for i in range(0, len(data), 2):
        h = (31 * h + int.from_bytes(data[i : i + 2], "little")) & 0xFFFFFFFF
    return h - (1 << 32) if h >> 31 else h


def _base36(n: int) -> str:
    out = ""
    while True:
        n, d = divmod(n, 36)
        out = "0123456789abcdefghijklmnopqrstuvwxyz"[d] + out
        if not n:
            return out


def folder_name(path: str) -> str:
    """Claude Code's folder for a path under <config>/projects: sanitized, and past 200 chars cut and given a hash."""
    path = unicodedata.normalize("NFC", path)
    name = sanitize(path)
    if len(name) <= SANITIZE_MAX:
        return name
    return f"{name[:SANITIZE_MAX]}-{_base36(abs(java_hash(path)))}"


def _git_root(start: Path) -> Path | None:
    here = Path(os.path.abspath(start))
    for folder in (here, *here.parents):
        try:
            if (folder / ".git").is_dir() or (folder / ".git").is_file():
                return folder
        except OSError:
            continue
    return None


def _text(path: Path, follow: bool = True) -> str | None:
    try:
        if not follow and path.is_symlink():  # as Claude Code: a link here could point the key at another repo
            return None
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None


def repo_root(cwd: Path) -> Path | None:
    """The folder Claude Code keys a repo by, read from the files as Claude Code does (no git binary): the nearest
    `.git` holder, or for a linked worktree its main repo. A submodule keys by its own working folder."""
    root = _git_root(cwd)
    if root is None:
        return None
    head = _text(root / ".git") or ""
    if not head.startswith("gitdir:"):
        return root
    gitdir = Path(os.path.abspath(root / head[len("gitdir:") :].strip()))
    common_rel = _text(gitdir / "commondir", follow=False)
    if common_rel is None:
        return root
    common = Path(os.path.abspath(gitdir / common_rel))
    back = _text(gitdir / "gitdir", follow=False)
    if gitdir.parent != common / "worktrees" or back is None:
        return root
    if os.path.realpath(gitdir / back) != os.path.join(os.path.realpath(root), ".git"):
        return root
    if common.name != ".git":
        return root if (common / ".git").exists() else common
    return common.parent


def project_key(project: Path) -> str:
    """Claude Code's folder name for a repo under ~/.claude/projects."""
    return folder_name(str(repo_root(project) or project))


def project_name(project: Path) -> str:
    """The name of the folder a repo is keyed by, which notices name as the work they come from."""
    return (repo_root(project) or Path(project)).name


def _trust_records(env: dict) -> list[dict]:
    """The `projects` of the global config the CLI reads (C.global_config_files); with its default folder, which a
    Codex session names whether or not Claude Code was told, ~/.claude.json too."""
    first, second = C.global_config_files(env)
    files, home = [first if first.exists() else second], C.home_dir(env)
    if Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude").expanduser().resolve() == (home / ".claude").resolve():
        files.append(home / ".claude.json")
    records = []
    for path in files:
        try:
            projects = json.loads(path.read_text(encoding="utf-8")).get("projects")
        except (OSError, ValueError, AttributeError):
            continue
        records += [projects] if isinstance(projects, dict) else []
    return records


def workspace_trusted(env: dict, folder: Path) -> bool:
    """Claude Code's trust record (2.1.296): hasTrustDialogAccepted for the folder, a parent inside its repo or the
    repo's root; never kept for the home folder or the filesystem root."""
    here, home = Path(os.path.abspath(folder)), C.home_dir(env)
    walk = [here, *here.parents]
    if (root := repo_root(here)) is not None:
        top = Path(os.path.abspath(root))
        walk = [p for p in walk if p == top or top in p.parents] + [top]  # a linked worktree has its main repo's record
    keys = set()
    for p in walk:
        if p != home and p.parent != p:
            for spelled in (p, Path(os.path.realpath(p))):
                keys |= {
                    s for text in (str(spelled), spelled.as_posix()) for s in (text, unicodedata.normalize("NFC", text))
                }
    return any(
        isinstance(projects.get(k), dict) and projects[k].get("hasTrustDialogAccepted") is True
        for projects in _trust_records(env)
        for k in keys
    )


def _mounted(text: str) -> bool:
    """A network mount, where Claude Code keeps no notes: the automounter's /net, macOS /Network/Servers."""
    parts = [p.lower() for p in text.replace("\\", "/").split("/") if p]
    return len(parts) >= 2 and (parts[0] == "net" or parts[:2] == ["network", "servers"])


def _notes_path(raw, env: dict, expand: bool) -> Path | None:
    """A memory folder as Claude Code 2.1.296 takes it, else None: absolute, no root, share or mount, long enough;
    `~/` (settings only) is the session's home. nowitknows also refuses the home folder, its own data folder and
    anything in the Claude or Codex config folder but a project's memory folder."""
    if not isinstance(raw, str) or not raw:
        return None
    text = raw
    if expand and text.startswith(("~/", "~\\")):
        rest = os.path.normpath(text[2:] or ".")
        if rest in (".", "..") or rest.startswith(("../", "..\\", ".." + os.sep)):
            return None
        text = str(C.home_dir(env) / text[2:])
    text = os.path.normpath(text)
    if not C.WINDOWS:
        text = re.sub("^/{2,}", "/", text)  # Node's normalize keeps no leading double slash
    text = re.sub(r"[/\\]+$", "", text)
    if (
        not os.path.isabs(text)
        or len(text) < C.NOTES_FOLDER_MIN_CHARS
        or text.startswith("\\\\")
        or _mounted(text)
        or "\x00" in text
    ):
        return None
    folder = Path(unicodedata.normalize("NFC", text))
    if os.path.realpath(folder) == os.path.realpath(C.home_dir(env)) or _inside(folder, C.data_dir(env)):
        return None
    for root in _config_folders(env):  # what lies there runs: a note in agents, commands or skills becomes one
        fold = _case_insensitive(root)
        if (rel := _relative(folder, root, fold)) is not None and not (
            len(rel) >= 3 and rel[0] == _fold("projects", fold) and rel[2] == _fold("memory", fold)
        ):
            return None
    return folder


def _config_folders(env: dict) -> list[Path]:
    """Claude Code's and Codex's config folders."""
    env = C.environment(env)
    home = C.home_dir(env)
    return [
        Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude"),
        Path(C.env("NOWITKNOWS_CODEX_HOME", env) or env.get("CODEX_HOME") or home / ".codex"),
    ]


def notes_folder(
    env: dict, project: Path | None, claude_home: Path, chain: list[dict], key: str = ""
) -> tuple[Path, str]:
    if folder := _notes_path(env.get("CLAUDE_COWORK_MEMORY_PATH_OVERRIDE"), env, expand=False):
        return folder, "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE"
    for where, settings in zip(SETTINGS, chain, strict=False):
        value = settings.get("autoMemoryDirectory")
        if value is None or where == CHECKED_IN_SETTINGS:
            continue
        if where == LOCAL_SETTINGS and not (project is not None and workspace_trusted(env, project)):
            continue
        if folder := _notes_path(value, env, expand=True):
            return folder, f"autoMemoryDirectory in {where}"
        break  # a value Claude Code drops leaves the default folder, not the next setting
    return claude_home / "projects" / (key or project_key(project)) / "memory", "Claude Code's default folder"


def native_memory_dir(env: dict, project: Path | None, claude_home: Path, chain: list[dict], key: str = "") -> Path:
    return notes_folder(env, project, claude_home, chain, key)[0]


def checked_in_folder(env: dict, chain: list[dict]) -> Path | None:
    """The valid autoMemoryDirectory of the repo's checked-in settings. nowitknows never keeps notes there, but Claude
    Code 2.1.296 may write there in a repo the user trusts or non-interactively, so its gate covers the folder too."""
    return _notes_path(chain[SETTINGS.index(CHECKED_IN_SETTINGS)].get("autoMemoryDirectory"), env, expand=True)


@dataclass(frozen=True)
class Modes:
    hosted_memory: bool
    auto_memory: bool
    native_dir: Path
    fallback_dir: Path
    key: str = ""  # the repo's project key; with a memory folder override, native_dir no longer names the repo

    shared: bool = False

    @property
    def global_tier(self) -> bool:
        return not self.hosted_memory

    @property
    def project_owner(self) -> str:
        if self.hosted_memory or self.auto_memory:
            return "native"
        return "nowitknows"

    @property
    def memory_review(self) -> bool:
        return not self.hosted_memory

    @property
    def project_dir(self) -> Path:
        return self.native_dir if self.project_owner == "native" else self.fallback_dir

    def to_json(self) -> str:
        return json.dumps(
            {
                "hosted": self.hosted_memory,
                "auto": self.auto_memory,
                "native_dir": str(self.native_dir),
                "fallback_dir": str(self.fallback_dir),
                "key": self.key,
                "shared": self.shared,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> Modes:
        m = json.loads(text)
        return cls(
            bool(m["hosted"]),
            bool(m["auto"]),
            Path(m["native_dir"]),
            Path(m["fallback_dir"]),
            m["key"],
            m.get("shared", False),
        )


def recorded(cfg: C.Config, s: dict, project: Path) -> Modes:
    """The modes SessionStart recorded for this session (its start folder names the repo), else detect from project."""
    m = s.get("modes")
    if not m:
        return detect(cfg, project)
    return Modes(
        m["hosted"],
        m["auto"],
        Path(m["native_dir"]),
        Path(m["fallback_dir"]),
        s.get("project") or "",
        m.get("shared", False),
    )


def current(cfg: C.Config, modes: Modes, start: Path) -> Modes:
    """`modes` with sharing and the notes folder (Claude Code's folder for the repo too) as the settings give them now,
    so a change reaches a running session's reviews and memory writes; the repo stays as recorded (`start`: its cwd)."""
    now = C.for_command(env=cfg.env, create=False)
    shared = now.project_notes_shared and not modes.hosted_memory
    key = modes.key or project_key(start)
    if shared:
        folder = native_memory_dir(now.env, start, now.claude_home, settings_chain(start, now.claude_home), key)
        return replace(modes, native_dir=folder, fallback_dir=folder, key=key, shared=True)
    if not modes.shared:
        return modes
    separate = now.data / "projects" / key / "memory"  # Codex keeps one folder, Claude Code's own only while shared
    native_dir = separate if now.host in ("codex", "cursor") else modes.native_dir
    return replace(modes, native_dir=native_dir, fallback_dir=separate, key=key, shared=False)


def detect(cfg: C.Config, project: Path) -> Modes:
    host = C.env("NOWITKNOWS_HOST", cfg.env)
    if host == "codex":
        from .codex_host import memory_modes

        return memory_modes(cfg, project)
    if host == "cursor":
        from .cursor_host import memory_modes as cursor_memory_modes

        return cursor_memory_modes(cfg, project)
    chain = settings_chain(project, cfg.claude_home)
    key = project_key(project)
    native = native_memory_dir(cfg.env, project, cfg.claude_home, chain, key)
    shared = cfg.project_notes_shared and not hosted_memory_active(cfg.env)
    fallback = native if shared else cfg.data / "projects" / key / "memory"
    return Modes(hosted_memory_active(cfg.env), auto_memory_enabled(cfg.env, chain), native, fallback, key, shared)


def _links(line: str) -> set[str]:
    return {Path(target).name for target in re.findall(r"\]\(([^)]+?\.md)\)", line)}


def merge_topic_dirs(
    src: Path, dst: Path, own: Path, data: Path | None = None, host: str = "claude", shared: bool = False
) -> dict:
    """Merge through the shared receipt protocol, locking the fallback index for either direction; `shared` when `dst`
    is Claude Code's folder."""
    if not src.exists():
        return {"copied": 0, "skipped": 0}
    dst.mkdir(parents=True, exist_ok=True)
    source, target = TopicStore(src), TopicStore(dst)
    copied = skipped = 0
    with mutation(data, TopicStore(own).index):
        writes, rows, before = {}, [], {}
        for f in source.topics():
            path = str(dst / f.name)
            _, generation = read_generation(Path(path))
            if generation is not None:
                skipped += 1
                continue
            content = f.read_text(encoding="utf-8")
            writes[path] = content
            before[path] = generation
            rows.append(
                {
                    "ts": time.time(),
                    "host": host,
                    "store": "project",
                    "action": "write",
                    "path": str(dst / f.name),
                    "source": "native-reconcile",
                    "after": content,
                }
            )
            copied += 1
        if source.index.exists():
            content, generation = read_generation(target.index)
            have = content.decode("utf-8").splitlines() if content is not None else []
            indexed = set().union(*map(_links, have))
            add = []
            for line in source.index.read_text(encoding="utf-8").splitlines():
                links = _links(line)
                if not line.strip() or line in have or line in add:
                    continue
                if links and (
                    links & indexed or not all((dst / name).exists() or str(dst / name) in writes for name in links)
                ):
                    continue
                add.append(line)
                indexed |= links
            add = add[: max(0, C.INDEX_MAX_LINES - len(have))]
            if add:
                writes[str(target.index)] = "\n".join(have + add) + "\n"
                before[str(target.index)] = generation
        marker = str(src / f".nowitknows-migrated-{int(time.time())}")
        before[marker] = read_generation(Path(marker))[1]
        writes[marker] = str(dst)
        commit(data, writes, rows, lock=TopicStore(own).index, before=before, shared=shared, private=(marker,))
    return {"copied": copied, "skipped": skipped}


def reconcile(modes: Modes, previous_owner: str | None, data: Path | None = None, host: str = "claude") -> dict | None:
    owner = modes.project_owner
    if (
        previous_owner is None
        or C.is_ours(previous_owner, owner)
        or modes.hosted_memory
        or modes.native_dir == modes.fallback_dir
    ):
        return None
    own = modes.fallback_dir
    if owner == "native":
        return {
            "direction": "nowitknows->native",
            **merge_topic_dirs(modes.fallback_dir, modes.native_dir, own, data, host, shared=True),
        }
    return {
        "direction": "native->nowitknows",
        **merge_topic_dirs(modes.native_dir, modes.fallback_dir, own, data, host),
    }


_CASE: dict[int, bool] = {}  # by device: the folders of one volume agree, so a volume is compared once


def _case_insensitive(path: Path) -> bool:
    """Whether the volume holding `path` ignores case, probed on the nearest existing folder whose name has letters."""
    for p in (path, *path.parents):
        other = "".join(c.swapcase() if c.isascii() else c for c in p.name)  # "ß" would become "SS", a different name
        if other == p.name:
            continue
        try:
            device = p.stat().st_dev
            if device not in _CASE:
                try:
                    _CASE[device] = os.path.samefile(p, p.with_name(other))
                except FileNotFoundError:
                    _CASE[device] = False  # p exists but its other casing does not: the volume tells them apart
            return _CASE[device]
        except FileNotFoundError:
            continue
        except OSError:
            return False
    return False


def _fold(path: str, fold: bool) -> str:
    return unicodedata.normalize("NFC", path).lower() if fold else path


def _relative(path: Path, root: Path, fold: bool = False) -> tuple[str, ...] | None:
    """The parts of `path` below `root`, links followed (case folded if the volume ignores case); None if outside."""
    try:
        return Path(_fold(str(path.resolve()), fold)).relative_to(_fold(str(root.resolve()), fold)).parts
    except (ValueError, OSError):
        return None


def _inside(path: Path, root: Path, fold: bool = False) -> bool:
    return _relative(path, root, fold) is not None


def _after_edit(path: Path, tool_name: str, tool_input: dict) -> tuple[str, str]:
    """(the file now, the whole file as it would be after the tool runs)."""
    try:
        before = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        before = ""
    if tool_name == "Write":
        result = tool_input.get("content") or ""
    else:
        result = before
        for e in [tool_input] if tool_name == "Edit" else list(tool_input.get("edits") or []):
            old, new = e.get("old_string") or "", e.get("new_string") or ""
            result = result.replace(old, new) if e.get("replace_all") else result.replace(old, new, 1)
    return before, result


GATED_TOOLS = ("Write", "Edit", "MultiEdit")


def gate(tool_name: str, tool_input: dict, modes: Modes, checked_in: Path | None = None) -> str | None:
    """Why Claude Code may not make this memory write; `checked_in`, a folder the repo's own settings name, is Claude
    Code's memory folder too while its auto memory is on."""
    raw = tool_input.get("file_path") or ""
    if tool_name not in GATED_TOOLS or not raw:
        return None
    if (
        checked_in is not None
        and modes.auto_memory
        and checked_in != modes.native_dir
        and _inside(Path(raw), checked_in, _case_insensitive(checked_in))
    ):
        modes = replace(modes, native_dir=checked_in, shared=False)
    if modes.project_owner != "native" and not modes.shared:
        return None
    fold = _case_insensitive(modes.native_dir)  # there memory.md is MEMORY.md (APFS, NTFS by default)
    if not _inside(Path(raw), modes.native_dir, fold):
        return None
    if modes.shared and not modes.auto_memory:
        return f"nowitknows: use {C.session_tools()}memory to change shared project notes."
    edits = tool_input.get("edits", []) if tool_name == "MultiEdit" else [tool_input]
    field = "content" if tool_name == "Write" else "new_string"
    if any("[redacted]" in edit.get(field, "") for edit in edits):
        return f"nowitknows: {MASKED_WRITE_ERROR}."
    before, result = _after_edit(Path(raw), tool_name, tool_input)
    had = set(lines(before))
    added = [line for line in lines(result) if line not in had]
    if err := scan_change(before, result):
        return f"nowitknows: {err}. Remove it before saving memory."
    if _fold(Path(raw).name, fold) == _fold("MEMORY.md", fold):
        if any(len(line) > C.INDEX_LINE_MAX for line in added):
            return (
                f"nowitknows: MEMORY.md index lines must stay under {C.INDEX_LINE_MAX} chars; "
                "move detail into the topic file."
            )
        return None
    if len(result.encode()) > C.TOPIC_FILE_MAX:
        return f"nowitknows: memory files stay under {C.TOPIC_FILE_MAX} bytes; split or summarize."
    if modes.global_tier and parse_topic(result).get("type") == "user":
        return (
            "nowitknows: facts about who the user is are global, not per-project. Save it with "
            f"{C.session_tools()}memory (target 'user') instead of this project's memory folder."
        )
    return None
