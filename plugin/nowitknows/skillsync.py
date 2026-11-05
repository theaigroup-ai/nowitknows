from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from contextvars import ContextVar
from pathlib import Path

from . import config as C
from . import notify
from .claude import hidden
from .safety import redact, scan_change, secret_shaped
from .skills import run_git
from .state import append_line, atomic_write, file_lock, jsonl_rows

SETTING = "skill-backup.json"
REMOTE = "nowitknows-backup"
MARKER = "nowitknows: initialize skill backup"
_DEADLINE = ContextVar("skill_backup_deadline", default=None)
_OBJECT_CACHE = ContextVar("skill_backup_objects", default=None)
_CHECKED = ContextVar("skill_backup_checked", default=None)
_SCAN_BYTES = ContextVar("skill_backup_scan_bytes", default=None)
_PATHS = ContextVar("skill_backup_paths", default=None)
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")


class Refused(Exception):
    pass


class Deferred(Exception):
    pass


def remaining() -> float:
    deadline = _DEADLINE.get()
    left = C.SKILL_BACKUP_TIMEOUT_S if deadline is None else min(C.SKILL_BACKUP_TIMEOUT_S, deadline - time.monotonic())
    if left <= 0:
        raise Refused("backup deadline reached; local skills are retained; retry on the next worker pass")
    return left


@contextlib.contextmanager
def ledger(cfg: C.Config):
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(file_lock(cfg.path("skills_ledger.jsonl"), wait_s=0))
        except TimeoutError:
            raise Deferred from None
        yield


def read(cfg: C.Config) -> dict:
    try:
        value = json.loads((cfg.data / SETTING).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"error": "unreadable setting"}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {"error": "unreadable setting"}


def save(cfg: C.Config, value: dict, *, expected: dict | None = None, destination: dict | None = None) -> bool:
    with file_lock(cfg.path(SETTING)):
        current = read(cfg)
        if current.get("error") or (expected is not None and current != expected):
            return False
        if destination is not None and any(
            current.get(key) != destination.get(key) for key in ("repo", "url", "branch")
        ):
            return False
        current.update(value)
        atomic_write(cfg.path(SETTING), json.dumps(current, indent=2) + "\n")
        return True


def save_results(cfg: C.Config, state: dict) -> None:
    keys = (
        "last_push",
        "last_push_at",
        "pushed_head",
        "visibility",
        "visibility_checked",
        "fetched_at",
        "retry_at",
        "failed_head",
    )
    save(cfg, {key: state[key] for key in keys if key in state}, destination=state)


def command(
    cfg: C.Config, argv: list[str], *, cwd: Path | None = None, input: bytes | None = None
) -> subprocess.CompletedProcess:
    overrides = {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    }
    env = {key: value for key, value in cfg.env.items() if key not in overrides}
    env.update(
        GH_HOST="github.com",
        GIT_TERMINAL_PROMPT="0",
        GCM_INTERACTIVE="never",
        GIT_LFS_SKIP_SMUDGE="1",
        GIT_NO_REPLACE_OBJECTS="1",
    )
    flags = hidden()
    if C.WINDOWS:
        flags["creationflags"] = flags.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        flags["start_new_session"] = True
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err, tempfile.TemporaryFile() as data:
        if input is not None:
            data.write(input)
            data.seek(0)
            # Batch requests are local data, never an interactive credential prompt.
            flags["stdin"] = data
        done = run_git(argv, out, err, timeout=remaining(), cwd=cwd, env=env, **flags)
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(argv, done.returncode, out.read(), err.read())


def git(cfg: C.Config, folder: Path, *args: str, check: bool = True, input: bytes | None = None) -> bytes:
    done = command(
        cfg,
        [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "rerere.enabled=false",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=NowItKnows",
            "-c",
            "user.email=nowitknows@localhost",
            "-C",
            str(folder),
            *args,
        ],
        **({"input": input} if input is not None else {}),
    )
    if check and done.returncode:
        raise Refused(f"git {args[0]} failed; local skills are retained")
    return done.stdout if not done.returncode else b""


def gh(cfg: C.Config, *args: str) -> dict:
    done = command(cfg, ["gh", *args])
    if done.returncode:
        raise Refused("GitHub could not verify the backup repository")
    try:
        value = json.loads(done.stdout)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    raise Refused("GitHub returned an unverifiable backup repository")


def ready(cfg: C.Config) -> bool:
    return bool(shutil.which("gh", path=cfg.env.get("PATH"))) and not command(cfg, ["gh", "auth", "status"]).returncode


def canonical(repo: str) -> str:
    if not REPO.fullmatch(repo) or repo.endswith(("/.", "/..")):
        raise Refused("name the backup as owner/repository on github.com")
    return f"https://github.com/{repo}.git"


def no_rewrites(cfg: C.Config, folder: Path) -> None:
    done = command(cfg, ["git", "-C", str(folder), "config", "--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$"])
    if done.returncode not in (0, 1) or done.stdout:
        raise Refused("git URL rewrite rules prevent verifying the backup destination")


def visibility(cfg: C.Config, state: dict) -> str:
    state.update(visibility="unknown", visibility_checked=time.time())
    value = gh(cfg, "repo", "view", state["repo"], "--json", "visibility").get("visibility")
    state["visibility"] = value.lower() if isinstance(value, str) else "unknown"
    if state["visibility"] != "private":
        raise Refused("backup refused: repository visibility is not verified private")
    return state["visibility"]


def destination(cfg: C.Config, state: dict) -> str:
    url = canonical(state["repo"])
    if state.get("url") != url:
        raise Refused("backup URL does not match its configured GitHub repository")
    no_rewrites(cfg, cfg.skills_library)
    for args in (("remote", "get-url", "--all", REMOTE), ("remote", "get-url", "--push", "--all", REMOTE)):
        if git(cfg, cfg.skills_library, *args).decode().splitlines() != [url]:
            raise Refused("backup remote has an unexpected fetch or push URL")
    visibility(cfg, state)
    return url


def safe_tree(cfg: C.Config, folder: Path, head: str) -> None:
    for entry in git(cfg, folder, "ls-tree", "-rz", head).split(b"\0"):
        if not entry:
            continue
        meta, name = entry.split(b"\t", 1)
        if meta.split()[0] not in (b"100644", b"100755") or secret_shaped(name.decode("utf-8", "replace")):
            raise Refused("backup tree contains an unsafe path or a link")


def _objects(cfg: C.Config, folder: Path, oids: list[str], budget: list[int]) -> dict[str, bytes]:
    remaining()
    if _SCAN_BYTES.get() is not None:
        budget = _SCAN_BYTES.get()
    if not oids:
        return {}
    cache = _OBJECT_CACHE.get()
    missing = [oid for oid in oids if cache is None or oid not in cache]
    if not missing:
        return {oid: cache[oid] for oid in oids}
    request = ("\n".join(missing) + "\n").encode("ascii")
    sizes = git(cfg, folder, "cat-file", "--batch-check", input=request).splitlines()
    for row in sizes:
        budget[0] += int(row.split()[2])
        if budget[0] > C.SKILL_BACKUP_SCAN_BYTES:
            raise Refused("backup secret scan exceeds its byte limit; push refused")
    data = git(cfg, folder, "cat-file", "--batch", input=request)
    result, offset = {}, 0
    for oid in missing:
        end = data.index(b"\n", offset)
        size = int(data[offset:end].split()[2])
        result[oid] = data[end + 1 : end + 1 + size]
        offset = end + size + 2
    if cache is not None:
        cache.update(result)
        return {oid: cache[oid] for oid in oids}
    return result


def _diagnostic(path: str, commit: str) -> Refused:
    label = json.dumps(redact(path), ensure_ascii=True)
    return Refused(
        f"backup secret scan found secret-shaped content at {label} in commit {commit}; push refused. "
        "Remove that commit from outgoing history, or use a new backup repo with a clean exported library"
    )


def _screen_trees(cfg: C.Config, folder: Path, roots: list[tuple[str, str]], budget: list[int]) -> None:
    pending = [(oid, "", commit) for oid, commit in roots]
    objects = {}
    paths = _PATHS.get()
    if paths is None:
        paths = set()
    scanned = _CHECKED.get()
    if scanned is None:
        scanned = set()
    visited = set()
    while pending:
        remaining()
        objects.update(
            _objects(cfg, folder, list(dict.fromkeys(oid for oid, _, _ in pending if oid not in objects)), budget)
        )
        next_trees, blobs = [], {}
        for oid, prefix, commit in pending:
            remaining()
            if (oid, prefix) in visited:
                continue
            visited.add((oid, prefix))
            data, offset = objects[oid], 0
            while offset < len(data):
                remaining()
                end = data.index(b"\0", offset)
                mode, name = data[offset:end].split(b" ", 1)
                raw_size = len(oid) // 2
                child = data[end + 1 : end + 1 + raw_size].hex()
                offset = end + 1 + raw_size
                path = prefix + os.fsdecode(name)
                if path not in paths:
                    paths.add(path)
                    if secret_shaped(path):
                        raise _diagnostic(path, commit)
                if mode == b"40000":
                    next_trees.append((child, path + "/", commit))
                elif mode in (b"100644", b"100755") and child not in scanned:
                    blobs.setdefault(child, (path, commit))
                elif mode not in (b"100644", b"100755"):
                    raise Refused("backup tree contains an unsafe path or a link")
        for oid, data in _objects(cfg, folder, list(blobs), budget).items():
            remaining()
            scanned.add(oid)
            if secret_shaped(data.decode("utf-8", "replace")):
                raise _diagnostic(*blobs[oid])
        pending = next_trees


def screen(cfg: C.Config, folder: Path, head: str, remote: str = "") -> None:
    args = (head, "--not", remote) if remote else (head,)
    commits = git(cfg, folder, "log", "--format=%H %T", *args).decode("ascii").splitlines()
    budget = [0]
    for oid, data in _objects(cfg, folder, [row.split()[0] for row in commits], budget).items():
        if secret_shaped(data.decode("utf-8", "replace")):
            raise _diagnostic("<commit message>", oid)
    roots = {}
    for row in commits:
        commit, tree = row.split()
        roots.setdefault(tree, commit)
    _screen_trees(cfg, folder, list(roots.items()), budget)


def _marked(cfg: C.Config, folder: Path, head: str) -> bool:
    return (
        MARKER
        in git(cfg, folder, "log", "--format=%s", "--fixed-strings", "--grep=" + MARKER, head).decode().splitlines()
    )


def _require_backup(cfg: C.Config, folder: Path, head: str) -> None:
    if not _marked(cfg, folder, head):
        raise Refused(
            "this private repository is not a NowItKnows backup; "
            "choose an empty repository or an existing NowItKnows backup"
        )


def _snapshot(cfg: C.Config, home: Path, *, initialize: bool = False) -> str:
    git(cfg, home, "add", "-A")
    head = git(cfg, home, "rev-parse", "--verify", "HEAD", check=False).decode().strip()
    try:
        if head:
            screen(cfg, home, head)
        tree = git(cfg, home, "write-tree").decode().strip()
        _screen_trees(cfg, home, [(tree, "staged tree")], [0])
    except Refused:
        if head:
            git(cfg, home, "reset", "-q", "HEAD", "--", ".")
        else:
            git(cfg, home, "read-tree", "--empty")
        raise
    marker = initialize and (not head or not _marked(cfg, home, head))
    if not head or marker or git(cfg, home, "diff", "--cached", "--name-only"):
        git(cfg, home, "commit", "--allow-empty", "-qm", MARKER if initialize else "nowitknows: snapshot skill backup")
    return git(cfg, home, "rev-parse", "HEAD").decode().strip()


def failure(cfg: C.Config, state: dict, reason: str, *, backoff: bool = True) -> dict:
    state.update(
        last_push="failed: " + reason,
        last_push_at=time.time(),
        retry_at=time.time() + C.SKILL_BACKUP_RETRY_S if backoff else 0,
        failed_head=_library_head(cfg.skills_library),
    )
    save_results(cfg, state)
    with cfg.path("errors.log").open("a", encoding="utf-8") as out:
        out.write(f"{time.ctime()} skill backup: {reason}\n")
    day = time.strftime("%Y-%m-%d")
    notify.push(
        cfg,
        f"{notify.PREFIX} skill backup failed: {reason}; learning continues",
        key=f"skill-backup-failed:{day}",
        permanent=True,
        tell={"kind": "skill-backup-failed"},
    )
    return {"error": reason}


def _resolve(cfg: C.Config, folder: Path, local: str, remote: str, conflicts: list[str]) -> list[str]:
    git(cfg, folder, "rebase", "--abort")
    git(cfg, folder, "merge", "--no-commit", "--no-ff", "--allow-unrelated-histories", remote, check=False)
    merged = git(cfg, folder, "diff", "--name-only", "--diff-filter=U", "-z").split(b"\0")
    conflicts += [os.fsdecode(p) for p in merged if p]
    names = sorted({p.split("/", 1)[0] for p in conflicts})
    short = remote[: C.SKILL_BACKUP_SHA_CHARS]
    for name in names:
        archive = folder / ".backup-conflicts" / f"{name}.remote-{short}"
        for entry in git(cfg, folder, "ls-tree", "-rz", remote, "--", name).split(b"\0"):
            if not entry:
                continue
            meta, path = entry.split(b"\t", 1)
            relative = Path(os.fsdecode(path)).relative_to(name)
            target = archive / relative if relative.parts else archive
            target.parent.mkdir(parents=True, exist_ok=True)
            content = git(cfg, folder, "cat-file", "blob", meta.split()[2].decode())
            if target.exists() and target.read_bytes() != content:
                raise Refused("a retained conflict copy changed; backup left untouched")
            target.write_bytes(content)
            target.chmod(int(meta.split()[0], 8) & 0o777)
        git(cfg, folder, "rm", "-r", "-f", "--ignore-unmatch", "--", name)
        if git(cfg, folder, "ls-tree", local, "--", name):
            git(cfg, folder, "restore", f"--source={local}", "--staged", "--worktree", "--", name)
    if (folder / ".backup-conflicts").exists():
        git(cfg, folder, "add", "-f", "--", ".backup-conflicts")
    git(cfg, folder, "commit", "-qm", "nowitknows: retain both versions after backup conflict")
    return names


def _skills(cfg: C.Config, folder: Path, head: str) -> set[str]:
    paths = git(cfg, folder, "ls-tree", "-rz", "--name-only", head).split(b"\0")
    return {
        name
        for path in paths
        if path.endswith(b"/SKILL.md")
        and path.count(b"/") == 1
        and C.SKILL_NAME.fullmatch(name := os.fsdecode(path).split("/", 1)[0])
    }


def _archive_removed(cfg: C.Config, names: set[str], generation: str) -> None:
    from .skills import SkillStore

    store = SkillStore(cfg)
    for name in sorted(names):
        if any(
            row.get("backup_generation") == generation and row.get("name") == name for row in jsonl_rows(store.ledger)
        ):
            continue
        path = store.archive_dir / f"{name}-{time.time_ns()}"
        shutil.copytree(store.home / name, path)
        append_line(
            store.ledger,
            json.dumps(
                {
                    "ts": time.time(),
                    "host": cfg.host,
                    "action": "archive",
                    "name": name,
                    "source": "user",
                    "path": str(path),
                    "backup": True,
                    "backup_generation": generation,
                }
            ),
        )


def _publish(cfg: C.Config, before: set[str], after: set[str]) -> None:
    from .skills import SkillStore

    store = SkillStore(cfg)
    for name in sorted(after - before):
        append_line(
            store.ledger,
            json.dumps(
                {
                    "ts": time.time(),
                    "host": cfg.host,
                    "action": "restore",
                    "name": name,
                    "source": "user",
                    "path": str(store.skill_md(name)),
                    "backup": True,
                }
            ),
        )
    for name in sorted(after):
        store.link(name)
    for name in sorted(before - after):
        store.unlink(name)


def _incoming(cfg: C.Config, folder: Path, head: str, local: str = "") -> list[tuple[str, int, bytes]]:
    def entries(ref):
        result = {}
        for entry in git(cfg, folder, "ls-tree", "-rz", ref).split(b"\0") if ref else []:
            remaining()
            if entry:
                meta, path = entry.split(b"\t", 1)
                mode, _, oid = meta.decode("ascii").split()
                result[os.fsdecode(path)] = (mode, oid)
        return result

    before, after = entries(local), entries(head)
    changed = {path: meta for path, meta in after.items() if before.get(path) != meta}
    oids = {oid for _, oid in changed.values()} | {before[path][1] for path in changed if path in before}
    blobs = _objects(cfg, folder, sorted(oids), [0])
    rejected = []
    for path, (mode, oid) in changed.items():
        remaining()
        old = blobs[before[path][1]].decode("utf-8", "replace") if path in before else None
        text = blobs[oid].decode("utf-8", "replace")
        unsafe_change = scan_change(old, text)
        if secret_shaped(text) or unsafe_change:
            rejected.append((path, int(mode, 8) & 0o777, blobs[oid]))
    return rejected


def _quarantine(cfg: C.Config, rejected: list[tuple[str, int, bytes]], generation: str) -> None:
    if not rejected:
        return
    key = hashlib.sha256(generation.encode("ascii")).hexdigest()
    home = cfg.skills_library
    archive = home / ".backup-conflicts" / f"incoming-{key}"
    for path, mode, content in rejected:
        target = archive / path
        if not target.is_relative_to(archive) or any(part in ("", ".", "..") for part in path.split("/")):
            raise Refused("incoming quarantine contains an unsafe path")
        for parent in reversed(target.parents):
            if parent == home or parent.is_relative_to(home):
                if parent.is_symlink():
                    raise Refused("incoming quarantine contains a link; local files are retained")
                parent.mkdir(exist_ok=True)
        if target.is_symlink():
            raise Refused("incoming quarantine contains a link; local files are retained")
        if target.exists():
            if not target.is_file() or target.read_bytes() != content:
                raise Refused("a retained quarantine copy changed; local files are retained")
        else:
            with target.open("xb") as out:
                out.write(content)
            target.chmod(mode)
    labels = [json.dumps(redact(path), ensure_ascii=True) for path, _, _ in rejected]
    notify.push(
        cfg,
        f"{notify.PREFIX} skill backup quarantined unsafe incoming files: {', '.join(labels)}; "
        "candidate not applied; review the library's .backup-conflicts folder",
        key=f"skill-backup-quarantine:{key}",
        permanent=True,
        tell={"kind": "skill-backup-quarantine", "paths": labels},
    )
    raise Refused("unsafe incoming files retained in .backup-conflicts; candidate not applied")


def _sync(cfg: C.Config, state: dict) -> dict:
    home = cfg.skills_library
    url = destination(cfg, state)
    branch = state["branch"]
    git(cfg, home, "check-ref-format", "refs/heads/" + branch)
    with tempfile.TemporaryDirectory(prefix="nowitknows-backup-") as temp:
        folder = Path(temp)
        with ledger(cfg):
            local = _snapshot(cfg, home, initialize=True)
            git(cfg, folder, "init", "-q")
            git(cfg, folder, "fetch", "-q", "--no-tags", str(home), local)
        safe_tree(cfg, folder, local)
        git(cfg, folder, "checkout", "-q", "-b", "backup", local)
        remote = ""
        refs = git(cfg, folder, "ls-remote", "--refs", url).splitlines()
        if refs:
            wanted = "refs/heads/" + branch
            source = next(
                (row.split()[1].decode() for row in refs if row.split()[1].decode() == wanted),
                refs[0].split()[1].decode(),
            )
            git(cfg, folder, "fetch", "-q", "--no-tags", url, source)
            state["fetched_at"] = time.time()
            save_results(cfg, state)
            remote = git(cfg, folder, "rev-parse", "FETCH_HEAD").decode().strip()
            safe_tree(cfg, folder, remote)
            _require_backup(cfg, folder, remote)
            if source != wanted:
                remote = ""
        else:
            state["fetched_at"] = time.time()
            save_results(cfg, state)
        conflicts = []
        if remote:
            try:
                git(cfg, folder, "rebase", remote)
            except Refused:
                paths = git(cfg, folder, "diff", "--name-only", "--diff-filter=U", "-z").split(b"\0")
                if not any(paths):
                    raise Refused("rebase could not complete; backup will retry") from None
                conflicts = _resolve(cfg, folder, local, remote, [os.fsdecode(p) for p in paths if p])
        head = git(cfg, folder, "rev-parse", "HEAD").decode().strip()
        rejected = _incoming(cfg, folder, head, local)
        if rejected:
            with ledger(cfg):
                if git(cfg, home, "rev-parse", "HEAD").decode().strip() != local or git(
                    cfg, home, "status", "--porcelain", "--untracked-files=all"
                ):
                    raise Deferred("skills changed during backup; backup will retry")
                # Retrying snapshots the retained copies, so local HEAD is not a stable source identity.
                generation = json.dumps(
                    [remote, [(path, mode, hashlib.sha256(data).hexdigest()) for path, mode, data in sorted(rejected)]]
                )
                _quarantine(cfg, rejected, generation)
        screen(cfg, folder, head, remote)
        before, after = _skills(cfg, folder, local), _skills(cfg, folder, head)
        with ledger(cfg):
            if git(cfg, home, "rev-parse", "HEAD").decode().strip() != local or git(
                cfg, home, "status", "--porcelain", "--untracked-files=all"
            ):
                raise Deferred("skills changed during backup; backup will retry")
            git(cfg, home, "fetch", "-q", "--no-tags", str(folder), head)
            git(cfg, home, "update-ref", "refs/nowitknows/before-backup", local)
            _archive_removed(cfg, before - after, local + ":" + remote)
            if removed := sorted(before - after):
                notify.push(
                    cfg,
                    f"{notify.PREFIX} skills archived on another machine: {', '.join(removed)}; "
                    f"use /nowitknows:keep to restore them {notify.link(cfg.data / 'skill-archive')}",
                    key=f"skill-backup-archive:{local}:{remote}",
                    permanent=True,
                    tell={"kind": "skill-backup-archive", "names": removed},
                )
            git(cfg, home, "reset", "--keep", head)
            _publish(cfg, before, after)
        incoming = git(cfg, folder, "diff", "--name-only", "-z", local, head, "--", ".backup-conflicts").split(b"\0")
        names = sorted(
            {
                name
                for path in incoming
                if len(parts := os.fsdecode(path).split("/")) > 2
                and C.SKILL_NAME.fullmatch(name := parts[1].split(".remote-", 1)[0])
            }
        )
        names = sorted(set(names) | {name for name in conflicts if C.SKILL_NAME.fullmatch(name)})
        if names:
            notify.push(
                cfg,
                f"{notify.PREFIX} skill backup kept both versions: {', '.join(names)}; "
                "remote copies are in the library's .backup-conflicts folder",
                key=f"skill-backup-conflict:{remote}:{local}",
                permanent=True,
                tell={"kind": "skill-backup-conflict", "names": names},
            )
        # Recheck immediately before this exact URL is used, including the live repo's rewrite configuration.
        screen(cfg, folder, head, remote)
        no_rewrites(cfg, folder)
        destination(cfg, state)
        if not read(cfg).get("enabled"):
            save_results(cfg, state)
            return {"enabled": False}
        git(cfg, folder, "push", url, f"{head}:refs/heads/{branch}")
        state.update(last_push="ok", last_push_at=time.time(), pushed_head=head, retry_at=0, failed_head="")
        save_results(cfg, state)
        return {"pushed": head, "conflicts": conflicts}


def _library_head(home: Path) -> str:
    """Read HEAD locally so an unchanged backup never needs a git subprocess."""
    try:
        folder = home / ".git"
        if folder.is_file():
            folder = home / folder.read_text(encoding="utf-8").strip().removeprefix("gitdir: ")
        head = (folder / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref = head.removeprefix("ref: ")
            if not ref.startswith("refs/") or any(part in ("", ".", "..") for part in ref.split("/")):
                return ""
            common = (
                folder / (folder / "commondir").read_text(encoding="utf-8").strip()
                if (folder / "commondir").exists()
                else folder
            )
            if (common / ref).exists():
                head = (common / ref).read_text(encoding="utf-8").strip()
            else:
                head = next(
                    (
                        line.split()[0]
                        for line in (common / "packed-refs").read_text(encoding="utf-8").splitlines()
                        if line.endswith(" " + ref)
                    ),
                    "",
                )
        return head if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head) else ""
    except (OSError, ValueError):
        return ""


def sync(cfg: C.Config) -> dict:
    state = read(cfg)
    if not state.get("enabled"):
        return {"enabled": False}
    try:
        with file_lock(cfg.path("skill-backup"), wait_s=0):
            state = read(cfg)
            if not state.get("enabled"):
                return {"enabled": False}
            token = _DEADLINE.set(time.monotonic() + C.SKILL_BACKUP_DEADLINE_S)
            cache_token = _OBJECT_CACHE.set({})
            checked_token = _CHECKED.set(set())
            bytes_token = _SCAN_BYTES.set([0])
            paths_token = _PATHS.set(set())
            try:
                if time.time() < state.get("retry_at", 0) and state.get("failed_head") == _library_head(
                    cfg.skills_library
                ):
                    return {"error": state.get("last_push", "backup retry deferred").removeprefix("failed: ")}
                age = time.time() - state.get("fetched_at", 0)
                if 0 <= age < C.SKILL_BACKUP_FETCH_S and state.get("pushed_head") == _library_head(cfg.skills_library):
                    return {"unchanged": True}
                return _sync(cfg, state)
            except Deferred:
                return {"deferred": True}
            except (
                Refused,
                OSError,
                ValueError,
                TypeError,
                KeyError,
                subprocess.SubprocessError,
                TimeoutError,
            ) as error:
                reason = str(error) if isinstance(error, Refused) else "backup unavailable; local skills are retained"
                return failure(cfg, state, reason, backoff=isinstance(error, (Refused, subprocess.SubprocessError)))
            finally:
                _DEADLINE.reset(token)
                _OBJECT_CACHE.reset(cache_token)
                _CHECKED.reset(checked_token)
                _SCAN_BYTES.reset(bytes_token)
                _PATHS.reset(paths_token)
    except (OSError, TimeoutError):
        return {"deferred": True}


def _when(value) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(float(value))) if value else "never"
    except (ValueError, TypeError, OverflowError, OSError):
        return "unknown"


def rows(cfg: C.Config) -> list[tuple[str, str]]:
    state = read(cfg)
    return [
        ("skill backup remote", state.get("url", "off")),
        (
            "skill backup visibility",
            f"{state.get('visibility', 'unknown')} (last checked {_when(state.get('visibility_checked'))})",
        ),
        (
            "skill backup last push",
            f"{state.get('last_push', state.get('error', 'never'))} ({_when(state.get('last_push_at'))})",
        ),
    ]


def _instructions(cfg: C.Config, repo: str | None) -> None:
    name = repo or C.SKILL_BACKUP_REPO
    print("GitHub backup needs gh installed and logged in. Run these commands after installing gh:")
    print("gh auth login")
    print("owner=$(gh api user --jq .login)")
    target = shlex.quote(name) if "/" in name else '"$owner/' + name + '"'
    print(f"gh repo create {target} --private")
    # Setup performs the visibility and history scans before adding the remote and pushing.
    entry = {"codex": "nowitknows_codex.py", "cursor": "nowitknows_cursor.py"}.get(cfg.host, "nowitknows.py")
    print(shlex.join([sys.executable, str(C.ROOT / "bin" / entry), "setup", "--backup-repo", name]))


def _ask(question: str) -> bool | None:
    try:
        return input(question + " [y/N] ").strip().lower() == "y"
    except EOFError:
        return None


def configure(cfg: C.Config, repo: str | None = None) -> None:
    state = read(cfg)
    previous = state.copy()
    home = cfg.skills_library
    if state and repo is None:
        return
    empty = not home.exists() or not any(home.iterdir())
    if not empty and not (home / ".git").exists():
        if repo is not None:
            print("Skill backup needs library git history; enable it and run setup in a terminal")
        return
    try:
        if repo is not None and not (REPO.fullmatch(repo) or re.fullmatch(r"[A-Za-z0-9_.-]+", repo)):
            raise Refused("name the backup as owner/repository or a repository name")
        answer = _ask("Back up your learned skills to a private GitHub repo?")
        if answer is None:
            if repo is not None:
                print("Skill backup choice deferred at EOF; run setup in a terminal")
            else:
                entry = {"codex": "nowitknows_codex.py", "cursor": "nowitknows_cursor.py"}.get(
                    cfg.host, "nowitknows.py"
                )
                hint = shlex.join([sys.executable, str(C.ROOT / "bin" / entry), "setup", "--git"])
                print(f"To back up your learned skills to a private GitHub repo, run this in a terminal: {hint}")
            return
        if not answer:
            save(cfg, {"asked": True, "enabled": False}, expected=previous)
            return
        available = ready(cfg)
        if not available:
            _instructions(cfg, repo)
            return
        target = repo or C.SKILL_BACKUP_REPO
        if available and "/" not in target:
            owner = gh(cfg, "api", "user")["login"]
            target = f"{owner}/{target}"
        found = None
        if available and (empty or repo is not None):
            with contextlib.suppress(Refused):
                found = gh(cfg, "repo", "view", target, "--json", "visibility,defaultBranchRef")
        restoring = empty and found is not None and bool(found.get("defaultBranchRef"))
        if restoring and _ask(f"Restore your learned skills from {target}?") is not True:
            save(cfg, {"asked": True, "enabled": False}, expected=previous)
            return
        url = canonical(target)
        state = {"asked": True, "enabled": False, "repo": target, "url": url}
        if found is not None:
            visibility(cfg, state)
            branch = (found.get("defaultBranchRef") or {}).get("name") or "main"
        else:
            branch = git(cfg, home, "symbolic-ref", "--short", "HEAD").decode().strip() if not empty else "main"
        state["branch"] = branch
        if (previous.get("url"), previous.get("branch")) != (url, branch):
            state.update(pushed_head="", fetched_at=0)
        if restoring:
            restore(cfg, state)
        else:
            if empty:
                home.mkdir(parents=True, exist_ok=True)
                git(cfg, home, "init", "-q", "-b", branch)
            no_rewrites(cfg, home)
            if found is not None:
                with tempfile.TemporaryDirectory(prefix="nowitknows-verify-") as temp:
                    check = Path(temp)
                    git(cfg, check, "init", "-q")
                    no_rewrites(cfg, check)
                    if git(cfg, check, "ls-remote", "--refs", url):
                        git(cfg, check, "fetch", "-q", "--no-tags", url, "refs/heads/" + branch)
                        _require_backup(cfg, check, "FETCH_HEAD")
            with ledger(cfg):
                _snapshot(cfg, home, initialize=True)
            if found is None:
                done = command(cfg, ["gh", "repo", "create", target, "--private"])
                if done.returncode:
                    raise Refused("could not create a new private repository; name an existing private repo explicitly")
            visibility(cfg, state)
            _remote(cfg, state, previous)
        state.update(enabled=True, retry_at=0, failed_head="")
        if not save(cfg, state, expected=previous):
            print("Skill backup settings changed during setup; run setup in a terminal to choose again")
            return
        result = sync(cfg)
        print("Skill backup: " + (result.get("error") or state["url"]))
    except (Refused, Deferred, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        reason = str(error) if isinstance(error, Refused) else "GitHub backup unavailable; local skills are retained"
        print("Skill backup: " + reason)
        if state.get("repo"):
            save(cfg, {**state, "enabled": False}, expected=previous)
            failure(cfg, state, reason)


def _remote(cfg: C.Config, state: dict, previous: dict | None = None) -> None:
    home = cfg.skills_library
    if REMOTE in git(cfg, home, "remote").decode().splitlines():
        if git(cfg, home, "remote", "get-url", "--all", REMOTE).decode().splitlines() != [state["url"]]:
            if not previous or git(cfg, home, "remote", "get-url", "--all", REMOTE).decode().splitlines() != [
                previous.get("url")
            ]:
                raise Refused("the backup remote already names another repository")
            git(cfg, home, "remote", "set-url", REMOTE, state["url"])
            git(cfg, home, "config", "--unset-all", f"remote.{REMOTE}.pushurl", check=False)
    else:
        git(cfg, home, "remote", "add", REMOTE, state["url"])


def _umask() -> int:
    with tempfile.TemporaryDirectory() as temp:
        probe = Path(temp) / "mode"
        probe.mkdir()
        return 0o777 & ~probe.stat().st_mode


def restore(cfg: C.Config, state: dict) -> None:
    home = cfg.skills_library
    if home.is_symlink() or (home.exists() and any(home.iterdir())):
        raise Refused("restore requires an empty skill library")
    visibility(cfg, state)
    home.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".skill-restore-", dir=home.parent) as temp:
        folder = Path(temp)
        git(cfg, folder, "init", "-q")
        no_rewrites(cfg, folder)
        git(cfg, folder, "fetch", "-q", "--no-tags", state["url"], "refs/heads/" + state["branch"])
        state["fetched_at"] = time.time()
        head = git(cfg, folder, "rev-parse", "FETCH_HEAD").decode().strip()
        safe_tree(cfg, folder, head)
        _require_backup(cfg, folder, head)
        git(cfg, folder, "checkout", "-q", "-b", state["branch"], head)
        rejected = _incoming(cfg, folder, head)
        with ledger(cfg):
            if home.is_symlink() or (home.exists() and any(home.iterdir())):
                raise Refused("restore requires an empty skill library")
            _quarantine(cfg, rejected, head)
            if home.exists():
                home.rmdir()
            folder.chmod(0o777 & ~_umask())
            folder.rename(home)
            _publish(cfg, set(), _skills(cfg, home, head))
        _remote(cfg, state)
