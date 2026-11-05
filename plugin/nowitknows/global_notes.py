from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config as C
from . import memory as M
from .safety import imperative, redact, scan
from .state import atomic_write, jsonl_rows

LINK = re.compile(r" \[global/(g-[a-f0-9]+)\.md\](?:" + re.escape(C.MEMORY_NOTE_SUFFIX) + r")?$")


def _fit_hook(hook: str) -> str:
    """Trim an over-long hook at a word boundary instead of refusing it: the note suffix still signals the details."""
    budget = C.MEMORY_HOOK_CHARS - len(C.MEMORY_NOTE_SUFFIX)
    while max(len(hook), len(redact(hook))) > budget:
        cut = hook.rstrip().rsplit(" ", 1)[0].rstrip(" ,;:-")
        hook = cut if cut and cut != hook else hook[: max(1, budget - (len(redact(hook)) - len(hook)))]
    return hook


def note(text: str, hook: str, as_of: str, source: str = "", basis: str = "exchange") -> tuple[str, str, str]:
    hook, source = redact(hook), redact(source)
    payload = f"---\nas_of: {as_of}\nas_of_basis: {basis}\n" + (f"source: {source}\n" if source else "")
    payload += f"---\n\n{text}\n"
    name = "g-" + hashlib.sha256(redact(payload).encode()).hexdigest()[: C.GLOBAL_NOTE_ID_CHARS]
    return name, f"{hook} [global/{name}.md]{C.MEMORY_NOTE_SUFFIX}", payload


def date(value: str | float) -> str:
    if isinstance(value, (float, int)):
        return datetime.fromtimestamp(value, timezone.utc).date().isoformat()
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()


def save_sources(cfg: C.Config, run_id: str, sid: str, exchanges: list[dict]) -> None:
    sources = {}
    qualified = sid if sid.startswith(cfg.host + ":") else f"{cfg.host}:{sid}"
    for i, exchange in enumerate(exchanges):
        try:
            sources[str(i + 1)] = {"as_of": date(exchange["ts"]), "source": f"{qualified} {exchange['uuid']}"}
        except (TypeError, ValueError, AttributeError):
            continue
    atomic_write(cfg.path("memory-sources", f"{run_id}.json"), json.dumps(sources))


class GlobalNotes:
    def __init__(self, store: M.EntryStore):
        self.store = store
        self.index = store.file("memory")
        self.root = store.root / "global"

    def safe_paths(self) -> bool:
        return not any(p.is_symlink() for p in (self.store.root, self.root, self.root / "archive", self.index))

    @staticmethod
    def _snapshot(path, versions) -> str | None:
        if path.is_symlink():
            raise ValueError("global notes must not be links")
        raw, generation = M.read_generation(path)
        versions.setdefault(str(path), generation)
        return raw.decode("utf-8") if raw is not None else None

    def _source(self, op: dict, source: str, run_id: str | None) -> tuple[str, str, str]:
        if source == "background":
            if not run_id or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
                raise ValueError("a long background fact needs a review's supporting source_exchange")
            path = self.store.root.parent / "memory-sources" / f"{run_id}.json"
            try:
                if type(op.get("source_exchange")) is not int:
                    raise ValueError("source_exchange must be the number from [exchange N]")
                item = json.loads(path.read_text(encoding="utf-8"))[str(op["source_exchange"])]
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise ValueError("source_exchange must name a supporting exchange in this review") from error
            return item["as_of"], item["source"], "exchange"
        stamp = date(op.get("as_of") or time.time())
        origin = op.get("source", "")
        if not isinstance(origin, str) or (
            origin and not re.fullmatch(r"(?:claude|codex|cursor):[\w-]+ [\w:-]+", origin)
        ):
            raise ValueError("source must be a host-qualified session id and message uuid")
        return stamp, origin, "exchange" if origin else "observed"

    def _prepare(
        self, content: str, op: dict, source: str, run_id: str | None, *, basis: str | None = None
    ) -> tuple[str, str, str]:
        hook = op.get("hook")
        if not isinstance(hook, str) or not hook.strip():
            raise ValueError("would be a long inline entry; supply a situation hook")
        hook = hook.strip()
        if C.MEMORY_NOTE_SUFFIX in hook:
            raise ValueError("the note suffix is added by NowItKnows; supply only the situation hook")
        if len(hook.splitlines()) != 1:
            raise ValueError("a hook is one line")
        hook = _fit_hook(hook)
        for part in (content, hook):
            if "[redacted]" in part:
                raise ValueError(M.MASKED_WRITE_ERROR)
            if error := scan(part):
                raise ValueError(error)
        stamp, origin, source_basis = self._source(op, source, run_id)
        name, line, payload = note(content, hook, stamp, origin, basis or source_basis)
        if scan(line) or len(payload.encode()) > C.GLOBAL_NOTE_MAX_BYTES:
            raise ValueError("unsafe global line or global note exceeds GLOBAL_NOTE_MAX_BYTES")
        return name, line, payload

    def apply(self, ops: list[dict], source: str, run_id: str | None, effect_id: str | None) -> dict:
        store = self.store
        if not self.safe_paths():
            return {"ok": False, "error": "global memory paths must not be links"}
        with M.mutation(store.ledger_dir, self.index):
            if effect_id and any(r.get("effect_id") == effect_id for r in self._rows()):
                return {"ok": True, "replayed": True}
            _raw, generation = M.read_generation(self.index)
            versions = {str(self.index): generation}
            current = store.entries("memory")
            new, writes, changes, warnings = list(current), {}, [], []
            for i, op in enumerate(ops):
                try:
                    action = op.get("action")
                    if "name" in op:
                        raise ValueError("names are derived from the body; supply old_text to replace a fact")
                    content, old = (op.get("content") or "").strip(), (op.get("old_text") or "").strip()
                    after, name, payload = content, None, None
                    if action in ("add", "replace"):
                        if "[redacted]" in content:
                            raise ValueError(M.MASKED_WRITE_ERROR)
                        if not content:
                            raise ValueError("empty content")
                        if M._SPLIT.search(f"\n{content}\n"):
                            raise ValueError("a line holding only § separates entries; add one fact per op")
                        if error := scan(content):
                            raise ValueError(error)
                        if LINK.search(content):
                            raise ValueError("global note links are written only with a full body and hook")
                        if imperative(content):
                            warnings.append(f"op {i}: write a declarative fact, not an instruction")
                        # an entry read back with CRLF normalized is still the same fact, not new long text
                        known = {entry.replace("\r\n", "\n") for entry in (*current, *new)}
                        if (
                            len(content) > C.MEMORY_ENTRY_CHARS
                            and content.replace("\r\n", "\n") not in known
                            and (source == "background" or "hook" in op)
                        ):
                            name, after, payload = self._prepare(content, op, source, run_id)
                    before = None
                    if action == "add":
                        duplicate = False
                        if payload:
                            for entry in new:
                                if match := LINK.search(entry):
                                    previous = writes.get(str(self.root / f"{match[1]}.md")) or self._read(match[1])
                                    if previous and previous.partition("\n---\n\n")[2].strip() == content:
                                        duplicate = True
                                        break
                        if after in new or duplicate:
                            warnings.append(f"op {i}: duplicate skipped")
                            continue
                        new.append(after)
                    elif action in ("replace", "remove"):
                        if not old:
                            raise ValueError("old_text required")
                        hits = [j for j, e in enumerate(new) if old in e]
                        if len(hits) != 1:
                            raise ValueError(f"old_text matched {len(hits)} entries; must match exactly one")
                        before = new[hits[0]]
                        if action == "replace":
                            new[hits[0]] = after
                        else:
                            new.pop(hits[0])
                            after = None
                    else:
                        raise ValueError(f"unknown action {action!r}")
                    row = self._row(action, before, after, source, run_id, effect_id)
                    if payload:
                        path = self.root / f"{name}.md"
                        if path.is_symlink():
                            raise ValueError("global notes must not be links")
                        present = writes.get(str(path)) or self._snapshot(path, versions)
                        if present is not None and present != payload:
                            raise ValueError("global note name collision; nothing was saved")
                        writes[str(path)] = payload
                        row["after"] = payload
                        row.update(note=name, as_of=payload.split("\n")[1].removeprefix("as_of: "), note_path=str(path))
                    if before and (match := LINK.search(before)):
                        path = self.root / f"{match[1]}.md"
                        previous = writes.get(str(path)) or self._snapshot(path, versions)
                        if previous is None:
                            raise ValueError("the old global note is missing; nothing was saved")
                        row["before"] = previous
                        if match[1] != name:
                            archive = self.root / "archive" / f"{match[1]}.md"
                            if path.is_symlink() or archive.is_symlink():
                                raise ValueError("global notes must not be links")
                            archived = self._snapshot(archive, versions)
                            if archived is not None and archived != previous:
                                raise ValueError("archive name collision; nothing was saved")
                            writes[str(archive)] = previous
                            writes[str(path)] = None
                            row.update(supersedes=match[1], superseded_by=name, archive=str(archive))
                    changes.append(row)
                except (OSError, ValueError, TypeError, AttributeError) as error:
                    return store._fail("memory", current, f"op {i}: {error}")
            total, limit = store.size([redact(e) for e in new]), store.limits["memory"]
            if total > limit:
                return store._fail("memory", current, f"would be {total}/{limit} chars; free {total - limit}")
            if any(M.read_generation(Path(p))[1] != v for p, v in versions.items()):
                return store._fail("memory", current, "MEMORY.md changed; nothing was saved")
            writes[str(self.index)] = M.DELIM.join(new) + ("\n" if new else "")
            try:
                if changes or effect_id:
                    self._commit(
                        writes,
                        changes or [self._row("unchanged", None, None, source, run_id, effect_id)],
                        versions,
                    )
            except M.IndexConflict as error:
                return store._fail("memory", current, str(error))
        return {
            "ok": True,
            "target": "memory",
            "path": str(self.index),
            "usage": f"{total}/{limit}",
            "changes": len(changes),
            "warnings": warnings,
        }

    def _rows(self) -> list[dict]:
        return jsonl_rows(self.store.ledger_dir / "memory_ledger.jsonl") if self.store.ledger_dir else []

    def _row(self, action, before, after, source, run_id=None, effect_id=None) -> dict:
        return {
            "ts": time.time(),
            "host": self.store.host,
            "store": "global",
            "target": "memory",
            "action": action,
            "before": before,
            "after": after,
            "source": source,
            "run_id": run_id,
            "effect_id": effect_id,
            "path": str(self.index),
        }

    def _commit(self, writes: dict, rows: list[dict], versions: dict) -> None:
        # Publish the index after its bodies, and retire replaced bodies only after publication.
        writes = {
            **{p: t for p, t in writes.items() if t is not None and p != str(self.index)},
            str(self.index): writes[str(self.index)],
            **{p: t for p, t in writes.items() if t is None},
        }
        M.commit(self.store.ledger_dir, writes, rows, lock=self.index, before=versions, atomic=True)

    def prepare_tidy(self, entries: list[str], conversions: list[dict]) -> tuple[list[str], dict[str, str]]:
        entries, bodies = list(entries), {}
        original = self.index.read_bytes().decode("utf-8-sig")
        old_entries = [e.strip() for e in M._SPLIT.split(original) if e.strip()]
        for op in conversions:
            content = op.get("content")
            if not isinstance(content, str) or len(content) <= C.MEMORY_ENTRY_CHARS:
                raise ValueError("tidy notes require a long existing entry")
            if entries.count(content) != 1 or content not in old_entries:
                raise ValueError("tidy notes must match an existing entry and the proposed block exactly")
            evidence = next((r for r in reversed(self._rows()) if r.get("after") == content and r.get("ts")), {})
            stamp = date(evidence.get("ts") or self.index.stat().st_mtime)
            name, line, payload = self._prepare(
                content,
                {"hook": op.get("hook"), "as_of": stamp},
                "tidy",
                None,
                basis="ledger_write" if evidence else "file_mtime",
            )
            entries[entries.index(content)] = line
            bodies[name] = payload
        return entries, bodies

    def read(self, name: str) -> str | None:
        if not self.safe_paths() or not re.fullmatch(r"g-[a-f0-9]+", name):
            return None
        with M.mutation(self.store.ledger_dir, self.index):
            return self._read(name)

    def _read(self, name: str) -> str | None:
        path = self.root / f"{name}.md"
        if not path.is_file():
            path = self.root / "archive" / f"{name}.md"
        if not self.safe_paths() or path.is_symlink() or not path.is_file():
            return None
        return path.read_bytes().decode("utf-8")

    def bodies(self, entries: list[str]) -> dict[str, str]:
        result = {}
        for entry in entries:
            if match := LINK.search(entry):
                body = self._read(match[1])
                if body is None:
                    raise ValueError("global note missing; keep its index entry unchanged")
                result[match[1]] = body
        return result
