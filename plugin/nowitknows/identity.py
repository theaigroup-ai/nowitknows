from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import config as C
from . import native, notify
from .memory import nowitknows_note
from .state import State, append_line, file_lock, jsonl_rows

CACHE = "repo_identities"


def _state(cfg: C.Config) -> State:
    return State(cfg.path("state.json"), cfg.host, cfg.legacy_host)


def records(cfg: C.Config) -> dict:
    """Cached identities from both hosts; the current host wins for the same path key."""
    state = _state(cfg)
    raw = state._raw()
    hosts = raw.get("hosts", {cfg.legacy_host: raw})
    found = {}
    for _host, data in sorted(hosts.items(), key=lambda item: item[0] == cfg.host):
        if isinstance(data, dict) and isinstance(data.get(CACHE), dict):
            found.update({k: v for k, v in data[CACHE].items() if isinstance(v, dict) and valid(v.get("identity"))})
    return found


def valid(value) -> bool:
    return (
        isinstance(value, dict)
        and value.get("kind") in ("roots", "origin", "path")
        and isinstance(value.get("value"), str)
        and bool(value["value"])
    )


def cached(cfg: C.Config, key: str) -> dict | None:
    """Only the state file is read here; a missing cache preserves path-based routing."""
    return records(cfg).get(key, {}).get("identity")


def history(cfg: C.Config) -> dict[str, list[dict]]:
    """Repository identities by path key, including earlier roots at a reinitialized path."""
    found: dict[str, list[dict]] = {}
    for row in jsonl_rows(cfg.data / C.REPO_IDENTITIES_FILE):
        value = {"kind": row.get("kind"), "value": row.get("identity")}
        if isinstance(row.get("path_key"), str) and valid(value):
            group = found.setdefault(row["path_key"], [])
            if value not in group:
                group.append(value)
    return found


def remember(cfg: C.Config, key: str, value: dict) -> None:
    """Append each path/identity pair once under the log's lock; keep every previous byte."""
    path = cfg.path(C.REPO_IDENTITIES_FILE)
    with file_lock(path):
        if value in history(cfg).get(key, []):
            return
        append_line(
            path,
            json.dumps({"path_key": key, "identity": value["value"], "kind": value["kind"], "first_seen": time.time()}),
        )
        with path.open("ab") as stream:
            stream.flush()
            os.fsync(stream.fileno())


def normalize_origin(raw: str) -> str | None:
    """Rebuild an allowlisted network origin without authentication, query, fragment or .git."""
    from urllib.parse import urlsplit

    raw = raw.strip()
    if not raw or any(c.isspace() or ord(c) < 32 for c in raw):
        return None
    try:
        if re.match(r"(?:https?|ssh|git)://", raw, re.IGNORECASE):
            url = urlsplit(raw)
            host = url.hostname or ""
            port = f":{url.port}" if url.port is not None else ""
            path = url.path
        elif match := re.fullmatch(r"[^/@:\s]+@(?P<host>\[[0-9a-fA-F:]+\]|[\w.-]+):(?P<path>[^\s]+)", raw):
            host = urlsplit("ssh://" + match["host"]).hostname or ""
            port = ""
            path = "/" + urlsplit("/" + match["path"].lstrip("/")).path.lstrip("/")
        else:
            return None
        if not re.fullmatch(r"[a-zA-Z0-9._-]+|[0-9a-fA-F:]+", host) or not path.startswith("/") or not path.strip("/"):
            return None
        host = f"[{host.lower()}]" if ":" in host else host.lower()
        value = (host + port + path).rstrip("/")
    except ValueError:
        return None
    return value[:-4] if value.endswith(".git") else value or None


def _git(cfg: C.Config, folder: Path, *args: str) -> str | None:
    overrides = {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    }
    env = {k: v for k, v in cfg.env.items() if k not in overrides}
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    try:
        result = subprocess.run(
            ["git", "-C", str(folder), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=C.REPO_ID_GIT_TIMEOUT_S,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if C.WINDOWS else 0,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def refresh(cfg: C.Config, folder: Path, modes: native.Modes | None = None) -> dict:
    """Refresh on SessionStart or a worker job, never on UserPromptSubmit."""
    key = native.project_key(folder)
    old = records(cfg).get(key, {})
    head = shallow = None
    value = None
    if native.repo_root(folder) is not None:
        head = _git(cfg, folder, "rev-parse", "--verify", "HEAD")
        shallow = _git(cfg, folder, "rev-parse", "--is-shallow-repository")
        if head and shallow == "false":
            if (
                old.get("head") == head
                and old.get("shallow") == shallow
                and old.get("identity", {}).get("kind") == "roots"
            ):
                value = old["identity"]
            else:
                roots = (_git(cfg, folder, "rev-list", "--max-parents=0", "HEAD") or "").splitlines()
                if roots and all(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", root) for root in roots):
                    value = {"kind": "roots", "value": ",".join(sorted(set(roots)))}
        if value is None:
            origin = normalize_origin(_git(cfg, folder, "config", "--get", "remote.origin.url") or "")
            if origin:
                value = {"kind": "origin", "value": origin}
    value = value or {"kind": "path", "value": key}
    remember(cfg, key, value)
    row = {"head": head, "shallow": shallow, "identity": value}
    if modes is not None and modes.shared:
        row["notes"] = str(modes.native_dir)
        row["default_notes"] = modes.native_dir == cfg.claude_home / "projects" / key / "memory"
    elif "notes" in old:
        row.update({k: old[k] for k in ("notes", "default_notes") if k in old})
    with _state(cfg).edit() as state:
        state.setdefault(CACHE, {})[key] = row
    return value


def associations(row: dict, known: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Derive every authorized path key's identities from the append-only log, without dated associations."""
    return {key: known[key] for key in row["repos"] if key in known}


def matches(row: dict, value: dict | None, known: dict[str, list[dict]]) -> bool:
    return bool(value and any(value in group for group in associations(row, known).values()))


def announce_notes(cfg: C.Config, modes: native.Modes) -> None:
    """Point to another path's notes once; never create, move or copy a notes file."""
    if not modes.shared:
        return
    target = modes.native_dir
    with contextlib.suppress(OSError):
        if any(p.is_file() for p in target.glob("*.md")):
            return
    known = records(cfg)
    value = known.get(modes.key, {}).get("identity")
    if not value or value.get("kind") == "path":
        return
    sources = {}
    for key, row in known.items():
        if key == modes.key or row.get("identity") != value or not row.get("notes"):
            continue
        source = cfg.claude_home / "projects" / key / "memory" if row.get("default_notes") else Path(row["notes"])
        if source == target or source.is_symlink():
            continue
        with contextlib.suppress(OSError, UnicodeError):
            files = sorted(p for p in source.glob("*.md") if p.is_file() and not p.is_symlink())
            notes = [p for p in files if p.name != "MEMORY.md" and nowitknows_note(p.read_text(encoding="utf-8"))]
            if notes:
                sources[str(source)] = [p.name for p in notes] + (
                    ["MEMORY.md"] if source / "MEMORY.md" in files else []
                )
    if not sources:
        return
    steps = " ".join(
        f"Notes are in {source}. Copy these files from that folder into {target}, "
        f"without replacing existing files: {', '.join(names)}."
        for source, names in sorted(sources.items())
    )
    notice_key = "repo-notes:" + modes.key + ":" + json.dumps(value, sort_keys=True)
    notify.push(
        cfg,
        f"{notify.PREFIX} found notes for this repository at another path. Create {target} if needed. "
        f"{steps} NowItKnows has not copied or moved any notes.",
        key=notice_key,
        permanent=True,
        tell={"kind": "repo-notes"},
    )


def start(cfg: C.Config, folder: Path, modes: native.Modes) -> None:
    refresh(cfg, folder, modes)
    announce_notes(cfg, modes)
