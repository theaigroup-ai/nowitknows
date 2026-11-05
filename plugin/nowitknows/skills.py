from __future__ import annotations

import contextlib
import difflib
import errno
import hashlib
import itertools
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from . import config as C
from . import notify, scope
from .claude import hidden
from .router import find_skills, frontmatter, skill_shadows, trigger_key, trigger_source
from .safety import redact, scan_change
from .state import append_line, atomic_write, durable_write, file_lock, jsonl_rows, link_target, log_row

NAME = C.SKILL_NAME
BAD_NAME = "name: kebab-case, max 64 chars, and not a Windows device name such as con or nul"
SUPPORT = re.compile(r"(references|templates|scripts)/[\w.\-/]+")
DEVICE = re.compile(r"(con|prn|aux|nul|com[0-9\u00b9\u00b2\u00b3]|lpt[0-9\u00b9\u00b2\u00b3])(\..*)?", re.I)
ACTIONS = ("create", "patch", "edit", "write_file", "archive")
UNATTENDED = ("background", "curator")
NOT_OURS = "nowitknows archives only skills it made and still owns; this one is the user's (made or kept by them)"
_SEQ = itertools.count()  # orders one run's staged changes when time.time() ties (about 16 ms steps on Windows)


def run_git(argv: list[str], out, err, *, timeout: float, **flags) -> subprocess.CompletedProcess:
    """Run git with regular-file output; give its process group time to clean up on POSIX."""
    proc = subprocess.Popen(argv, stdin=flags.pop("stdin", subprocess.DEVNULL), stdout=out, stderr=err, **flags)
    try:
        proc.wait(timeout=timeout)
    except BaseException:
        try:
            if C.WINDOWS:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=C.SKILL_GIT_STOP_GRACE_S,
                    **hidden(),
                )
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=C.SKILL_GIT_STOP_GRACE_S)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if not C.WINDOWS:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        raise
    return subprocess.CompletedProcess(argv, proc.returncode)


class RecoveryConflict(ValueError):
    pass


def folder_generation(folder: Path) -> str | None:
    """Pin all names, file bytes, modes and literal symlink targets without following directory links."""
    if not os.path.lexists(folder):
        return None
    digest = hashlib.sha256()
    for path in [folder, *sorted(folder.rglob("*"))]:
        mode = path.lstat().st_mode
        name = path.relative_to(folder).as_posix()
        content = (
            os.fsencode(os.readlink(path)) if stat.S_ISLNK(mode) else path.read_bytes() if stat.S_ISREG(mode) else b""
        )
        metadata = json.dumps([name, mode]).encode()
        digest.update(len(metadata).to_bytes(8, "big") + metadata + len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


def norm_file(file_path: str | None) -> str:
    """Canonical support path: '' means SKILL.md."""
    fp = (file_path or "").strip().removeprefix("./")
    return "" if fp in ("", "SKILL.md") else fp


def valid_name(name: str) -> bool:
    """Kebab-case and no Windows device name (Windows cannot create those), so a skill made anywhere installs there."""
    return bool(NAME.fullmatch(name or "")) and not DEVICE.fullmatch(name)


def sha(text: str | None) -> str | None:
    return hashlib.sha256(text.encode()).hexdigest()[:16] if text is not None else None


def lint(name: str, text: str, before: str | None = None) -> tuple[list[str], list[str]]:
    """Lint a SKILL.md; the safety scan reads only the lines this change adds to `before`."""
    errors, warnings = [], []
    if not valid_name(name):
        errors.append(BAD_NAME)
    meta, body = frontmatter(text)
    if not meta:
        errors.append("missing YAML frontmatter")
    elif meta.get("name") != name:
        errors.append("frontmatter name must equal the skill name")
    desc = meta.get("description", "")
    if not desc:
        errors.append("description required")
    elif len(desc) > C.SKILL_DESC_MAX:
        errors.append(f"description over {C.SKILL_DESC_MAX} chars")
    elif len(desc) > C.SKILL_DESC_SOFT:
        warnings.append(f"description over {C.SKILL_DESC_SOFT} chars; lead with the trigger")
    if len(text) > C.SKILL_BODY_MAX:
        errors.append(f"SKILL.md over {C.SKILL_BODY_MAX} chars")
    if err := scan_change(before, text):
        errors.append(err)
    if re.search(r"\b(PR|issue)\s?#\d+|\b20\d\d-\d\d-\d\d\b", body):
        warnings.append("contains PR/issue numbers or dates; state the rule without the incident")
    if re.search(r"^\s*(UPDATE|EDIT|NOTE):", body, re.M):
        warnings.append("appended correction marker; fix the original sentence instead")
    warnings.extend(platform_warnings(body))
    return errors, warnings


def size_label(text: str, chars: int | None = None) -> str:
    return f"{len(text) if chars is None else chars:,} of {C.SKILL_MAX_CHARS:,} characters"


def _size_warnings(content: str, file_path: str = "") -> list[str]:
    if len(content) > C.SKILL_MAX_CHARS:
        return [f"{file_path or 'SKILL.md'}: {size_label(content)}; saved whole"]
    return []


def _linked(path: Path) -> bool:
    if path.is_symlink() or getattr(os.path, "isjunction", lambda p: False)(path):
        return True
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except FileNotFoundError:
        return False


def platform_warnings(text: str) -> list[str]:
    platform = False
    level = 0
    for line in text.splitlines():
        if heading := re.match(r"^(#{1,6})\s+(.+)", line):
            depth, title = len(heading[1]), heading[2].lower()
            if depth <= level:
                platform = False
            if "platform notes" in title:
                platform = bool(re.search(r"\b(claude|codex|cursor)\b", title))
                level = depth
        if not platform and re.search(r"mcp__[\w-]+|\b(?:Skill|Bash) tool\b|\bapply_patch\b", line):
            return [
                "host-specific tool outside a Platform notes heading naming Claude or Codex; "
                "use shell commands and paths"
            ]
    return []


class SkillStore:
    def __init__(self, cfg: C.Config, overlay_run: str | None = None):
        """With `overlay_run`, reads see that run's staged changes, so staged writes build on each other."""
        self.cfg = cfg
        self.home = cfg.skills_library
        self.ledger = cfg.path("skills_ledger.jsonl")
        self.triggers = cfg.path("skill_triggers.json")
        self.staged = cfg.data / "staged"
        self.rejected = cfg.data / "rejected"
        self.archive_dir = cfg.data / "skill-archive"
        self.history = cfg.data / "skill-history"
        self.overlay_run = overlay_run

    def register_cursor_profile(self, *, hook: bool = False) -> None:
        """Reconcile canonical skills when Cursor joins this store, under the skill ledger lock."""
        with file_lock(self.ledger, wait_s=C.HOOK_STATE_LOCK_WAIT_S if hook else C.SQLITE_TIMEOUT_S):
            self._recover_applications()
            path = self.cfg.data / "cursor-profiles.json"
            profiles = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            destination = str(self.cfg.skills_home.resolve())
            if destination not in profiles:
                atomic_write(path, json.dumps([*profiles, destination]))
            manifest = self.cfg.data / "skill-links.json"
            links = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
            for skill in sorted(self.home.glob("*/SKILL.md")):
                if not skill.parent.is_symlink() and valid_name(skill.parent.name):
                    self.link(skill.parent.name, links=links)

    def reconcile_shadows(self, project: Path | None = None) -> None:
        """Archive only real namesakes whose exact bytes occur in this skill's history."""
        try:
            with file_lock(self.ledger):
                receipts = self.cfg.data / "shadow-moves"
                pending = set()
                for receipt in sorted(receipts.glob("*.json")):
                    try:
                        value = json.loads(receipt.read_text(encoding="utf-8"))
                        if not isinstance(value, dict) or not all(
                            isinstance(value.get(k), str) and value[k] for k in ("name", "source", "path", "digest")
                        ):
                            raise ValueError("invalid shadow receipt")
                        self._finish_shadow(receipt, value)
                        if receipt.exists():
                            pending.add(value["source"])
                    except (ValueError, TypeError, KeyError):
                        with contextlib.suppress(OSError):
                            receipt.replace(receipt.with_suffix(".conflict"))
                    except (OSError, RuntimeError):
                        continue
                for shadow in skill_shadows(self.cfg, project):
                    path = Path(shadow["path"])
                    if str(path.parent) in pending:
                        continue
                    try:
                        if path.parent.parent != C.home_dir(self.cfg.env) / ".agents/skills" or any(
                            _linked(parent) for parent in (path, *path.parents)
                        ):
                            continue
                        content = path.read_bytes()
                        history = self.history / shadow["name"]
                        if not any(
                            p.is_file() and not p.is_symlink() and p.read_bytes() == content
                            for p in history.glob("*-SKILL.md")
                        ):
                            continue
                        destination = self.cfg.data / "shadowed-copies" / time.strftime("%Y-%m-%d") / shadow["name"]
                        if os.path.lexists(destination):
                            destination = destination.with_name(f"{destination.name}-{uuid.uuid4().hex}")
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if path.parent.stat().st_dev != destination.parent.stat().st_dev:
                            continue
                        receipt = receipts / f"{uuid.uuid4().hex}.json"
                        value = {
                            "name": shadow["name"],
                            "source": str(path.parent),
                            "path": str(destination),
                            "digest": hashlib.sha256(content).hexdigest(),
                            "host": self.cfg.host,
                            "ts": time.time(),
                        }
                        durable_write(receipt, json.dumps(value))
                        self._finish_shadow(receipt, value)
                    except (OSError, RuntimeError):
                        continue
        except (OSError, RuntimeError, TimeoutError):
            return

    def _finish_shadow(self, receipt: Path, value: dict) -> None:
        """Finish or retire a move receipt without touching a replaced source or destination."""
        source, destination = Path(value["source"]), Path(value["path"])
        try:
            if source.parent != C.home_dir(self.cfg.env) / ".agents/skills" or not value.get("digest"):
                receipt.replace(receipt.with_suffix(".conflict"))
                return
            if any(_linked(p) for p in (source / "SKILL.md", source, *source.parents)) or any(
                _linked(p) for p in (destination / "SKILL.md", destination, *destination.parents)
            ):
                receipt.replace(receipt.with_suffix(".conflict"))
                return
            if not os.path.lexists(destination) and os.path.lexists(source):
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    source.rename(destination)
                except OSError as error:
                    if error.errno == errno.EXDEV or getattr(error, "winerror", None) == 17:
                        receipt.unlink(missing_ok=True)
                    raise
            moved = destination / "SKILL.md"
            if (
                any(_linked(p) for p in (moved, destination, *destination.parents))
                or not moved.is_file()
                or hashlib.sha256(moved.read_bytes()).hexdigest() != value["digest"]
            ):
                if os.path.lexists(destination) and not os.path.lexists(source):
                    destination.rename(source)
                receipt.replace(receipt.with_suffix(".conflict"))
                retained = destination if os.path.lexists(destination) else source
                if os.path.lexists(retained):
                    notify.push(self.cfg, f"{notify.PREFIX} old skill copy changed during move; retained at {retained}")
                return
            if not any(row.get("shadow_move_id") == receipt.stem for row in jsonl_rows(self.ledger)):
                append_line(self.ledger, json.dumps({**value, "action": "shadow_copy", "shadow_move_id": receipt.stem}))
            if self.skill_md(value["name"]).is_file():
                self.link(value["name"])  # the moved copy may have been this host's only visible one
            notify.push(
                self.cfg,
                f"{notify.PREFIX} moved old skill copy {value['name']}: {source} -> {notify.link(destination)}",
                key=f"shadow:{receipt.stem}",
            )
            receipt.replace(receipt.with_suffix(".done"))
        except (OSError, RuntimeError):
            return

    def skill_md(self, name: str) -> Path:
        return self.home / name / "SKILL.md"

    def folder(self, name: str) -> Path:
        visible = self.cfg.skills_home / name
        canonical = self.home / name
        if (canonical / "SKILL.md").is_file() and not canonical.is_symlink():
            try:
                if visible.resolve() != canonical.resolve():
                    return canonical
            except (OSError, RuntimeError):
                return canonical
        if os.path.lexists(visible):
            return visible
        for container in ("synced", ".system"):
            managed = self.cfg.skills_home / container / name
            if os.path.lexists(managed):
                return managed
        for root in (self.cfg.skills_home, self.cfg.skills_home / ".system"):
            for path in find_skills(root):
                if path.parent.name == name:
                    return path.parent
        return self.home / name

    def writable(self, name: str) -> str | None:
        if not valid_name(name):
            return BAD_NAME
        folder = self.folder(name)
        if name == "synced" or folder.is_relative_to(self.cfg.skills_home / "synced"):
            return "synced account skills are managed by Claude sync; create a new skill under another name instead"
        try:
            if folder.resolve() != (self.home / name).resolve():
                return "visible host skill is read-only; migrate nowitknows-made skills or create under another name"
        except (OSError, RuntimeError):
            return "visible host skill cannot be resolved; it is read-only"
        if (self.home / name).is_symlink():
            return "canonical skill folders cannot be symlinks"
        if self.home == self.cfg.skills_home and (self.home / name).exists():
            from .migrate import _made

            if name not in _made(self.ledger, self.cfg.legacy_host, self.home):
                return "user-authored skills are read-only; create a new skill under another name"
        return None

    def file(
        self, name: str, file_path: str = "", *, canonical: bool = False, folder: Path | None = None
    ) -> Path | None:
        """Path of a skill file, or None when the name or path is invalid or escapes the skill folder, or when a link
        (`scripts -> .`) takes a support path to the folder itself or onto SKILL.md, which would then skip lint."""
        fp = norm_file(file_path)
        parts = fp.split("/")
        if not valid_name(name) or (
            fp and (not SUPPORT.fullmatch(fp) or ".." in parts or any(DEVICE.fullmatch(part) for part in parts))
        ):
            return None
        return self._file(folder if folder is not None else self.home / name if canonical else self.folder(name), fp)

    def _file(self, folder: Path, fp: str) -> Path | None:
        skill = folder / "SKILL.md"
        p = folder / (fp or "SKILL.md")
        try:
            root = folder.resolve()
            p.resolve().relative_to(root)
            if fp:
                if any((folder / q).resolve() == root for q in Path(fp).parents if q != Path(".")):
                    return None
                entry = p.parent.resolve() / p.name  # what a write replaces; a linked support file is the link itself
                if os.path.lexists(entry) and skill.exists() and os.path.samestat(os.lstat(entry), os.stat(skill)):
                    return None  # also SKILL.md under another case on a case-insensitive volume
        except (ValueError, OSError, RuntimeError):  # RuntimeError: a link loop before Python 3.13
            return None
        return p

    def resolved_folder(self, name: str) -> str | None:
        """Where the skill folder resolves now; None for a link loop."""
        try:
            return str(self.folder(name).resolve())
        except (OSError, RuntimeError):
            return None

    def _overlay(self, name: str) -> list[dict]:
        """The overlay run's staged changes to this skill, oldest first."""
        return [c for c in self.staged_changes(self.overlay_run) if c["name"] == name] if self.overlay_run else []

    def read(self, name: str, file_path: str = "") -> str | None:
        fp = norm_file(file_path)
        for c in reversed(self._overlay(name)):
            if c.get("folder") != self.resolved_folder(name):
                continue
            if c["action"] == "archive":
                return None
            if norm_file(c.get("file_path")) == fp:
                return c["content"]
        p = self.file(name, fp)
        if not (p and p.is_file()):
            return None
        # A BOM (PowerShell 5.1 writes one) would carry into every patch and fail lint; a script may need its own.
        return p.read_text(encoding="utf-8" if fp else "utf-8-sig")

    def owned(self) -> set[str]:
        """Skills nowitknows created and still owns: not user-requested, not archived since. A ledger line that cannot
        be read may be a keep, so then it owns none: background runs and the curator archive nothing."""
        try:
            lines = self.ledger.read_bytes().splitlines()
        except FileNotFoundError:
            return set()
        created: dict = {}
        entries = []
        for n, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except ValueError:  # also undecodable bytes
                e = None
            if not (isinstance(e, dict) and isinstance(e.get("name"), str)):
                self._log(f"{self.ledger} line {n} is unreadable, so no skill is archived unattended until it is fixed")
                return set()
            entries.append(e)
        for e in sorted(entries, key=lambda e: e.get("ts", 0)):
            if e.get("action") == "create":
                created[e["name"]] = e.get("source")
            elif e.get("action") in ("archive", "keep", "restore"):  # kept or restored skills are the user's
                created.pop(e["name"], None)
        return {name for name, source in created.items() if source != "user"}

    def keep(self, name: str) -> dict:
        """The user wants this skill: the curator never archives it, and an archived copy comes back."""
        if not NAME.fullmatch(name or ""):
            return {"ok": False, "error": "skill names are kebab-case"}
        with file_lock(self.ledger):
            self._recover_applications()
            action = "keep"
            if not self.skill_md(name).exists():
                pat = re.compile(rf"{re.escape(name)}-(\d+)")
                copies = sorted(
                    (int(m.group(1)), p)
                    for p in (self.archive_dir.iterdir() if self.archive_dir.exists() else [])
                    if (m := pat.fullmatch(p.name))
                )
                if (self.home / name).exists():
                    return {"ok": False, "error": f"{self.home / name} has no SKILL.md; move that folder aside first"}
                if not copies:
                    return {"ok": False, "error": f"no skill or archived skill named {name}"}
                self.home.mkdir(parents=True, exist_ok=True)
                shutil.move(str(copies[-1][1]), str(self.home / name))
                action = "restore"
            entry = {
                "ts": round(time.time(), 3),
                "action": action,
                "name": name,
                "source": "user",
                "host": self.cfg.host,
                "skill_source": trigger_source(self.skill_md(name), self.cfg),
            }
            append_line(self.ledger, json.dumps({**entry, "path": str(self.skill_md(name))}))
            conflicts = self.link(name)
            self._git(name, f"nowitknows: {action} {name} (user)")
        return {"ok": True, "action": action, "name": name, "path": str(self.skill_md(name)), "warnings": conflicts}

    def link(self, name: str, *, links: dict | None = None) -> list[str]:
        """Called under the skill ledger lock; never replaces a foreign folder or link."""
        warnings = []
        manifest = self.cfg.data / "skill-links.json"
        if links is None:
            links = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
        target = self.home / name
        for home in self.cfg.skill_link_homes:
            path = home / name
            if path == target:
                continue
            try:
                if path.is_symlink() and path.resolve() == target.resolve():
                    continue
                if os.path.lexists(path):
                    raise FileExistsError(f"{path} already exists; left untouched")
                home.mkdir(parents=True, exist_ok=True)
                links[str(path)] = {
                    "name": name,
                    "target": str(target),
                    "resolved": str(target.resolve()),
                    "created": False,
                }
                atomic_write(manifest, json.dumps(links))
                path.symlink_to(target, target_is_directory=True)
                links[str(path)]["created"] = True
                atomic_write(manifest, json.dumps(links))
            except OSError as error:
                message = f"cannot link skill {name}: {error}"
                privilege = C.WINDOWS and getattr(error, "winerror", None) == 1314
                if privilege:
                    message += "; turn on Developer Mode in Windows settings to allow skill links"
                warnings.append(message)
                append_line(
                    self.ledger,
                    json.dumps(
                        {
                            "ts": time.time(),
                            "host": self.cfg.host,
                            "action": "link_conflict",
                            "name": name,
                            "path": str(path),
                            "reason": str(error),
                        }
                    ),
                )
                notify.push(self.cfg, f"{notify.PREFIX} {message}", key=f"link:{path}", permanent=privilege)
        return warnings

    def unlinked(self) -> list[str]:
        """Name canonical skills without a working link in this Windows host's skill folder."""
        if not C.WINDOWS or self.home == self.cfg.skills_home:
            return []
        missing = []
        for skill in sorted(self.home.glob("*/SKILL.md")):
            if not valid_name(skill.parent.name) or skill.parent.is_symlink():
                continue
            visible = self.cfg.skills_home / skill.parent.name
            try:
                working = (visible / "SKILL.md").is_file() and visible.resolve() == skill.parent.resolve()
            except (OSError, RuntimeError):
                working = False
            if not working:
                missing.append(skill.parent.name)
        return missing

    def unlink(self, name: str) -> None:
        manifest = self.cfg.data / "skill-links.json"
        links = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
        for key, record in list(links.items()):
            if record["name"] != name:
                continue
            path = Path(key)
            if (
                record.get("created")
                and path.is_symlink()
                and link_target(os.readlink(path)) == link_target(record["target"])
                and str(path.resolve()) == record["resolved"]
            ):
                path.unlink()
            del links[key]
        atomic_write(manifest, json.dumps(links))

    def prepare(self, change: dict) -> dict:
        """Validate a change and compute its final content; never writes."""
        action, name = change.get("action"), change.get("name", "")
        if action not in ACTIONS:
            return {"ok": False, "error": f"unknown action {action!r}; deletion is intentionally unsupported"}
        if err := self.writable(name):
            return {"ok": False, "error": err}
        current = self.read(name)
        replacement = change.get("new_string" if action == "patch" else "content", "")
        if "[redacted]" in replacement:
            return {
                "ok": False,
                "error": "the value was masked in your copy; patch around it without copying [redacted] into new text",
            }
        warnings: list[str] = []
        if action == "create":
            if current is not None:
                return {"ok": False, "error": "skill exists; patch it instead"}
            staged = self._overlay(name)
            if os.path.lexists(self.home / name) and not (staged and staged[-1]["action"] == "archive"):
                return {"ok": False, "error": f"{self.home / name} exists without a SKILL.md nowitknows can change"}
            new = change.get("content", "")
        elif action in ("patch", "edit"):
            if current is None:
                return {"ok": False, "error": "no such skill"}
            if action == "patch":
                old = change.get("old_string", "")
                n = current.count(old) if old else 0
                if n != 1:
                    return {"ok": False, "error": f"old_string matched {n} times; must match exactly once"}
                new = current.replace(old, change.get("new_string", ""))
            else:
                new = change.get("content", "")
        elif action == "write_file":
            fp = norm_file(change.get("file_path"))
            if not fp or self.file(name, fp) is None:
                return {"ok": False, "error": "file_path must be under references/, templates/, or scripts/"}
            if current is None:
                return {"ok": False, "error": "no such skill"}
            new = change.get("content", "")
            if err := scan_change(self.read(name, fp), new):
                return {"ok": False, "error": err}
            warnings.extend(platform_warnings(new))
            return {
                "ok": True,
                "action": action,
                "name": name,
                "file_path": fp,
                "base": sha(self.read(name, fp)),
                "content": new,
                "warnings": [*warnings, *_size_warnings(new, fp)],
            }
        else:
            if current is None:
                return {"ok": False, "error": "no such skill"}
            return {
                "ok": True,
                "action": action,
                "name": name,
                "base": sha(current),
                "content": None,
                "warnings": warnings,
            }
        errors, warnings = lint(name, new, current)
        if errors:
            return {"ok": False, "errors": errors, "warnings": warnings}
        return {
            "ok": True,
            "action": action,
            "name": name,
            "base": sha(current),
            "content": new,
            "warnings": [*warnings, *_size_warnings(new)],
        }

    def diff(self, prepared: dict) -> str:
        fp = prepared.get("file_path") or "SKILL.md"
        before = self.read(prepared["name"], prepared.get("file_path", "")) or ""
        after = prepared.get("content") or ""
        if prepared["action"] == "archive":
            return f"ARCHIVE skill {prepared['name']} (moved out of the skills folder, recoverable)\n"
        return "".join(
            difflib.unified_diff(
                before.splitlines(True),
                after.splitlines(True),
                f"a/{prepared['name']}/{fp}",
                f"b/{prepared['name']}/{fp}",
            )
        )

    def stage(self, prepared: dict, meta: dict) -> str:
        """`folder` pins where the skill folder resolved, so apply refuses one linked elsewhere while it waits."""
        with file_lock(self.ledger):
            cid = uuid.uuid4().hex[:12]
            folder = self.resolved_folder(prepared["name"])
            record = {
                **prepared,
                **meta,
                "host": self.cfg.host,
                "id": cid,
                "ts": time.time(),
                "seq": next(_SEQ),
                "folder": folder,
                "skill_source": trigger_source(
                    self.file(prepared["name"]) or self.skill_md(prepared["name"]), self.cfg
                ),
                "attempts": 0,
                "paid_tries": 0,
            }
            (durable_write if meta.get("effect_id") else atomic_write)(self.staged / f"{cid}.json", json.dumps(record))
            return cid

    def staged_changes(self, run_id: str | None = None) -> list[dict]:
        if not self.staged.exists():
            return []
        out = sorted(
            (json.loads(p.read_text(encoding="utf-8")) for p in self.staged.glob("*.json")),
            key=lambda c: (c.get("ts", 0), c.get("seq", 0)),
        )
        groups: dict[tuple[str, str], list[int]] = {}
        for i, change in enumerate(out):
            if change.get("run_id") and (
                (change.get("action") == "write_file" and change.get("file_path"))
                or (change.get("action") in ("edit", "patch") and not change.get("file_path"))
            ):
                groups.setdefault((change["run_id"], str(change.get("name"))), []).append(i)
        # Reuse each group's slots so creates and unrelated runs or skills keep their positions.
        for positions in groups.values():
            ordered = sorted((out[i] for i in positions), key=lambda c: c.get("action") != "write_file")
            for i, change in zip(positions, ordered, strict=True):
                out[i] = change
        return [c for c in out if run_id is None or c.get("run_id") == run_id]

    def defer(self, change: dict, *, paid: bool = False, step_id: str | None = None) -> int:
        """Count a try that got no judgment; 0 when the change was settled meanwhile, which is not brought back."""
        path = self.staged / f"{change['id']}.json"
        if not path.exists():
            return 0
        saved = json.loads(path.read_text(encoding="utf-8"))
        if not step_id or saved.get("deferred_step") != step_id:
            paid_steps = saved.get("paid_steps", [])
            count_paid = paid and (not step_id or step_id not in paid_steps)
            saved.update(attempts=saved.get("attempts", 0) + 1, paid_tries=saved.get("paid_tries", 0) + int(count_paid))
            if count_paid and step_id:
                saved["paid_steps"] = [*paid_steps, step_id]
            if step_id:
                saved["deferred_step"] = step_id
            atomic_write(path, json.dumps(saved))
        change.update(saved)
        return saved["attempts"]

    def drop_staged(self, cid: str) -> None:
        (self.staged / f"{cid}.json").unlink(missing_ok=True)

    def rebase_legacy_create(self, change: dict) -> None:
        if self.cfg.host != "claude" or change.get("host", "claude") != "claude" or change["action"] != "create":
            return
        with file_lock(self.ledger):
            self._recover_applications()
            original = self.cfg.skills_home / change["name"]
            if (
                change.get("base") is None
                and change.get("folder") == str(original.resolve())
                and not os.path.lexists(original)
                and not os.path.lexists(self.home / change["name"])
            ):
                change["folder"] = self.resolved_folder(change["name"])
                if change.get("id") and (path := self.staged / f"{change['id']}.json").exists():
                    durable_write(path, json.dumps(change))

    def save_evidence(self, run_id: str, text: str) -> None:
        atomic_write(self.staged / f"{run_id}.evidence.txt", text[-C.DIGEST_MAX_CHARS :])

    def evidence(self, run_id: str | None) -> str:
        p = self.staged / f"{run_id}.evidence.txt"
        return p.read_text(encoding="utf-8") if run_id and p.exists() else ""

    def drop_evidence_if_done(self, run_id: str | None) -> None:
        from .notejudge import staged

        if run_id and not self.staged_changes(run_id) and not staged(self.cfg, run_id):  # a note's judge reads it too
            (self.staged / f"{run_id}.evidence.txt").unlink(missing_ok=True)

    def apply(
        self,
        prepared: dict,
        source: str,
        verdict: str | None = None,
        run_id: str | None = None,
        triggers: list[str] | None = None,
    ) -> dict:
        if not prepared.get("ok"):
            return prepared
        name, action = prepared["name"], prepared["action"]
        with file_lock(self.ledger):
            self._recover_applications()
            if prepared.get("id"):
                previous = next(
                    (
                        r
                        for r in jsonl_rows(self.ledger)
                        if r.get("change_id") == prepared["id"] and r.get("host", self.cfg.legacy_host) == self.cfg.host
                    ),
                    None,
                )
                if previous:
                    return {
                        "ok": True,
                        "action": previous["action"],
                        "name": previous["name"],
                        "path": previous["path"],
                        **({"warnings": previous["warnings"]} if previous.get("warnings") else {}),
                    }
            if prepared.get("folder") and self.resolved_folder(name) != prepared["folder"]:
                return {"ok": False, "error": "conflict: the skill folder moved since staging"}
            if err := self.writable(name):
                return {"ok": False, "error": "conflict: " + err}
            if action == "archive" and source in UNATTENDED and name not in self.owned():
                return {"ok": False, "error": NOT_OURS}
            current = self.read(name, prepared.get("file_path", ""))
            if sha(current) != prepared.get("base"):
                return {"ok": False, "error": "conflict: the skill changed since this change was prepared"}
            if action == "write_file" and not self.skill_md(name).exists():
                return {"ok": False, "error": "conflict: the skill this file belongs to does not exist"}
            if prepared.get("folder") and self.resolved_folder(name) != prepared["folder"]:
                return {"ok": False, "error": "conflict: the skill folder moved or was linked elsewhere since staging"}
            if action == "create" and os.path.lexists(self.home / name):
                return {"ok": False, "error": f"conflict: {self.home / name} exists now"}
            target = self.file(name, prepared.get("file_path", ""))
            if target is None and action != "archive":
                return {"ok": False, "error": "conflict: the file now resolves outside the skill folder"}
            backup = None
            if action == "archive":
                path = self.archive_dir / f"{name}-{time.time_ns()}"
            else:
                path = target if norm_file(prepared.get("file_path")) else target.resolve()
                if current is not None:
                    fp = norm_file(prepared.get("file_path")) or "SKILL.md"
                    backup = self.history / name / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}-{fp.replace('/', '__')}"
            entry = {
                "ts": round(time.time(), 3),
                "action": action,
                "name": name,
                "source": source,
                "verdict": verdict,
                "run_id": run_id,
                "before": prepared.get("base"),
                "after": sha(prepared.get("content")),
                "path": str(path),
                "backup": str(backup) if backup else None,
                "host": self.cfg.host,
                "change_id": prepared.get("id"),
                "skill_source": trigger_source(self.skill_md(name), self.cfg),
            }
            size_warnings = _size_warnings(prepared.get("content") or "", prepared.get("file_path") or "")
            if size_warnings:
                entry["warnings"] = size_warnings
            if not norm_file(prepared.get("file_path")):  # a create's repo and mark; a SKILL.md change's mark again
                entry.update(
                    scope.on_apply(
                        self.cfg,
                        action,
                        prepared.get("content"),
                        entry["skill_source"],
                        prepared.get("project"),
                        prepared.get("where"),
                    )
                )
            receipt = self.cfg.path(
                "skill-writes",
                hashlib.sha256(f"{os.getpid()}:{time.time_ns()}:{next(_SEQ)}".encode()).hexdigest() + ".json",
            )
            value = {
                "entry": entry,
                "content": prepared.get("content"),
                "before": current,
                "folder": self.resolved_folder(name),
                "visible_folder": str(
                    self.folder(name) if os.path.lexists(self.cfg.skills_home / name) else self.cfg.skills_home / name
                ),
                "file_path": prepared.get("file_path", ""),
                "triggers": triggers,
                "where": prepared.get("where") if isinstance(prepared.get("where"), str) else "",
            }
            if action == "archive":
                value["folder_generation"] = folder_generation(self.home / name)
                manifest = self.cfg.data / "skill-links.json"
                value["managed_links"] = {
                    key: record
                    for key, record in (
                        json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
                    ).items()
                    if record.get("name") == name
                    and record.get("created")
                    and Path(key).is_symlink()
                    and link_target(os.readlink(key)) == link_target(record["target"])
                    and str(Path(key).resolve()) == record["resolved"] == value["folder"]
                }
            durable_write(receipt, json.dumps(value))
            try:
                warnings = self._finish_apply(receipt, value)
            except RecoveryConflict as error:
                self._quarantine(receipt, value, str(error))
                return {"ok": False, "error": "conflict: " + str(error)}
            prepared = {
                **prepared,
                "warnings": list(dict.fromkeys([*prepared.get("warnings", []), *warnings, *size_warnings])),
            }
            self._git(name, f"nowitknows: {action} {name} ({source})")
        return {
            "ok": True,
            "action": action,
            "name": name,
            "path": str(path),
            "warnings": prepared.get("warnings", []),
        }

    def _recover_applications(self) -> None:
        for receipt in sorted((self.cfg.data / "skill-writes").glob("*.json")):
            value = json.loads(receipt.read_text(encoding="utf-8"))
            if value.get("done"):
                receipt.replace(receipt.with_suffix(".done"))
            elif value.get("conflict"):
                self._quarantine(receipt, value, value["conflict"])
            else:
                try:
                    self._finish_apply(receipt, value)
                except RecoveryConflict as error:
                    self._quarantine(receipt, value, str(error))

    def _quarantine(self, receipt: Path, value: dict, reason: str) -> None:
        entry = value["entry"]
        saved = self.cfg.data / "skill-conflicts" / receipt.name
        durable_write(receipt, json.dumps({**value, "conflict": reason}))
        self._restore_archive_links(value)
        if not any(
            row.get("application_id") == receipt.stem and row.get("action") == "recovery_conflict"
            for row in jsonl_rows(self.ledger)
        ):
            append_line(
                self.ledger,
                json.dumps(
                    {
                        "ts": time.time(),
                        "host": entry.get("host", self.cfg.legacy_host),
                        "action": "recovery_conflict",
                        "name": entry["name"],
                        "application_id": receipt.stem,
                        "attempted_action": entry["action"],
                        "path": str(saved),
                        "reason": reason,
                    }
                ),
            )
        notify.push(
            self.cfg,
            f"{notify.PREFIX} interrupted skill change to {entry['name']} quarantined; "
            f"current skill retained {notify.link(saved)}",
            key="skill-conflict:" + receipt.stem,
            permanent=True,
        )
        saved.parent.mkdir(parents=True, exist_ok=True)
        receipt.replace(saved)

    def _restore_archive_links(self, value: dict) -> None:
        folder = self.home / value["entry"]["name"]
        if not value.get("unlink_started"):
            return
        try:
            if str(folder.resolve()) != value["folder"]:
                return
        except (OSError, RuntimeError):
            return
        archived = Path(value["entry"]["path"])
        if not os.path.lexists(folder) and archived.is_dir() and not archived.is_symlink():
            folder.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(archived), str(folder))
        if folder.is_symlink() or not folder.is_dir():
            return
        manifest = self.cfg.data / "skill-links.json"
        links = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
        for key, record in value.get("managed_links", {}).items():
            path = Path(key)
            if record.get("resolved") != str(folder.resolve()) or record.get("name") != folder.name:
                continue
            if os.path.lexists(path):
                if (
                    path.is_symlink()
                    and link_target(os.readlink(path)) == link_target(record["target"])
                    and str(path.resolve()) == record["resolved"]
                ):
                    links[key] = record
                    durable_write(manifest, json.dumps(links))
                continue
            links[key] = {**record, "created": False}
            durable_write(manifest, json.dumps(links))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(record["target"], target_is_directory=True)
            links[key]["created"] = True
            durable_write(manifest, json.dumps(links))

    def _finish_apply(self, receipt: Path, value: dict) -> list[str]:
        entry = value["entry"]
        name, path = entry["name"], Path(entry["path"])
        folder = self.home / name
        visible = Path(value.get("visible_folder", str(folder)))
        try:
            moved = str(folder.resolve()) != value["folder"] or (
                os.path.lexists(visible) and str(visible.resolve()) != value["folder"]
            )
        except (OSError, RuntimeError):
            moved = True
        if moved:
            raise RecoveryConflict("skill folder moved during interrupted application")
        if entry["action"] == "archive":
            folder = path if os.path.lexists(path) else self.home / name
            if not value.get("folder_generation") or folder_generation(folder) != value["folder_generation"]:
                raise RecoveryConflict("skill folder changed or was not pinned during interrupted application")
            if os.path.lexists(path):
                if os.path.lexists(self.home / name):
                    raise RecoveryConflict("skill folder replaced during interrupted application")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                value["unlink_started"] = True
                durable_write(receipt, json.dumps(value))
                self.unlink(name)
                shutil.move(str(self.home / name), str(path))
        else:
            target = self._file(self.home / name, norm_file(value["file_path"]))
            if target is None or target.resolve() != path.resolve():
                raise RecoveryConflict("skill file moved during interrupted application")
            current = (
                target.read_bytes().decode("utf-8" if value["file_path"] else "utf-8-sig") if target.exists() else None
            )
            if current is not None and sha(current) != entry["after"]:
                current = current.replace("\r\n", "\n").replace("\r", "\n")
            if sha(current) not in (entry["before"], entry["after"]):
                raise RecoveryConflict("skill changed during interrupted application")
            if entry["backup"] and not Path(entry["backup"]).exists():
                durable_write(Path(entry["backup"]), value["before"])
            if sha(current) != entry["after"]:
                durable_write(path, value["content"])
        known = jsonl_rows(self.ledger)
        if not any(r.get("application_id") == receipt.stem for r in known):
            append_line(self.ledger, json.dumps({**entry, "application_id": receipt.stem}))
        warnings = []
        if entry["action"] != "archive":
            warnings = self.link(name)
            if value["triggers"]:
                skill = self.skill_md(name)
                self.set_triggers(
                    name,
                    value["triggers"],
                    sha(skill.read_text(encoding="utf-8-sig")),
                    source=trigger_source(skill, self.cfg),
                )
            skill = trigger_source(self.skill_md(name), self.cfg)
            scope.applied(self.cfg, entry, skill, value.get("where") or "", known)  # the ledger before this change
        else:
            scope.archive(self.cfg, entry)
        durable_write(receipt, json.dumps({**value, "done": True}))
        receipt.replace(receipt.with_suffix(".done"))
        return warnings

    def reject(
        self, change: dict, rounds: list[list[dict]], reason: str | None = None, *, related: bool = True
    ) -> Path:
        """Record a dropped change: the judge's findings per round (round 2 judged the revision) and the last diff.
        `reason` marks a row the judge did not decide, such as `expired`."""
        path = self.rejected / f"{change['id']}.md"
        lines = [f"# Rejected {change['action']} of skill {change['name']}"]
        for i, findings in enumerate(rounds, 1):
            lines += ["", f"## Findings, round {i}"]
            lines += [f"- [{f.get('severity')}] {f.get('issue')} -> {f.get('fix')}" for f in findings]
        lines += ["", "## Last proposed diff", "```diff", self.diff(change), "```"]
        if reason == "support write refused":
            lines += ["", "## Proposed operation", "```json", json.dumps(change, ensure_ascii=False), "```"]
        atomic_write(path, redact("\n".join(lines)) + "\n")
        row = {"ts": round(time.time(), 3), "id": change["id"], "name": change["name"], "action": change["action"]}
        row.update(run_id=change.get("run_id"), rounds=len(rounds))
        source = change.get("skill_source")
        folder = Path(change["folder"]) if change.get("folder") else None
        if folder and (not source or not Path(source["path"]).is_relative_to(folder)):
            source = trigger_source(folder / "SKILL.md", self.cfg, real=folder / "SKILL.md")
        row["skill_source"] = source
        if reason:
            row["reason"] = reason
        log_row(self.cfg, "rejected.jsonl", row)
        if related and change["action"] == "write_file" and change.get("file_path") and change.get("run_id"):
            finding = {"severity": "major", "issue": "support write rejected", "fix": "re-propose the move together"}
            for other in self.staged_changes(change["run_id"]):
                if other["name"] == change["name"] and other["id"] != change["id"]:
                    record = self.reject(other, [[finding]], reason="support write rejected", related=False)
                    self.drop_staged(other["id"])
                    notify.push(
                        self.cfg,
                        f"{notify.PREFIX} skipped paired change to skill {other['name']}: "
                        f"support write rejected; proposal kept {notify.link(record)}",
                    )
        return path

    def keep_oversized(self, change: dict, run_id: str | None) -> Path:
        """Keep a proposal refused for exceeding SKILL_BODY_MAX, masked, so a size refusal never loses it silently."""
        path = self.rejected / f"{uuid.uuid4().hex}.md"
        name, action = str(change.get("name")), str(change.get("action"))
        parts = [f"# Refused {action} of skill {name}", f"Over the {C.SKILL_BODY_MAX:,}-character limit for SKILL.md."]
        for key in ("old_string", "new_string", "content"):
            if change.get(key):
                parts += [f"## {key}", "```", redact(str(change[key])), "```"]
        atomic_write(path, "\n\n".join(parts) + "\n")
        log_row(
            self.cfg,
            "rejected.jsonl",
            {
                "ts": round(time.time(), 3),
                "id": path.stem,
                "name": name,
                "action": action,
                "run_id": run_id,
                "rounds": 0,
                "reason": "over_limit",
            },
        )
        return path

    def set_triggers(
        self,
        name: str,
        queries: list[str],
        digest: str | None,
        tries: int = 0,
        *,
        inputs: dict | None = None,
        expected: dict | None = None,
        source: dict | None = None,
    ) -> bool:
        """Store queries and their fingerprint; backfill supplies generation inputs, other writers a file hash."""
        with file_lock(self.triggers):
            data = json.loads(self.triggers.read_text(encoding="utf-8")) if self.triggers.exists() else {}
            source = source or trigger_source(self.file(name) or self.skill_md(name), self.cfg)
            key = trigger_key(source)
            value = {
                "name": name,
                "source": source,
                "queries": [q.strip() for q in queries if q.strip()][: C.TRIGGERS_MAX],
                "sha": digest,
                "host": self.cfg.host,
            }
            if inputs is not None:
                value["inputs"] = inputs
            if tries:
                value["tries"] = tries
            current = data.get(key, {})
            if expected is not None and current != expected and current != value:
                return False
            data[key] = value
            durable_write(self.triggers, json.dumps(data, indent=1))
            return current != value

    def _git(self, name: str, message: str) -> None:
        """Commit only this skill's folder; other content in the skills folder (synced skills) is not ours.
        The change is applied and ledgered already, so a git that cannot start or fails is logged, not raised."""
        if not (self.home / ".git").exists():
            return

        def git(*args: str) -> subprocess.CompletedProcess:
            argv = ["git", "-C", str(self.home), *args]
            flags = hidden()
            if C.WINDOWS:
                flags["creationflags"] = flags.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                flags["start_new_session"] = True
            # A grandchild can inherit stdout after git dies; regular files never wait for that handle to close.
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                done = run_git(argv, out, err, timeout=C.SKILL_GIT_TIMEOUT_S, **flags)
                out.seek(0)
                err.seek(0)
                return subprocess.CompletedProcess(
                    argv,
                    done.returncode,
                    done.stdout if done.stdout is not None else out.read(),
                    done.stderr if done.stderr is not None else err.read(),
                )

        try:
            for step in (("add", "-A", "--", name), ("commit", "-q", "-m", message, "--", name)):
                if (p := git(*step)).returncode == 0:
                    continue
                # Nothing to record fails too: a keep of a live skill, or an archive of one git never tracked.
                if (s := git("status", "--porcelain", "--untracked-files=all", "--", name)).returncode == 0 and not (
                    s.stdout or b""
                ).strip():
                    return
                said = " ".join((p.stderr or b"").decode("utf-8", "replace").split())
                self._log(f"git {step[0]} of {name} failed (exit {p.returncode}): {said}")
                return
        except (OSError, subprocess.TimeoutExpired) as e:
            self._log(f"git commit of {name} skipped: {e}")

    def _log(self, message: str) -> None:
        with open(self.cfg.path("errors.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.ctime()} skills: {message}\n")
