from __future__ import annotations

import difflib
import html
import math
import re
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from . import config as C
from . import memory, report, ui_page
from .router import frontmatter, trigger_key, trigger_source
from .skills import sha, valid_name

LOGS = (
    "skills_ledger.jsonl",
    "memory_ledger.jsonl",
    "runs.jsonl",
    "rejected.jsonl",
    "activity.jsonl",
    "usage.jsonl",
    C.REFUSALS_LOG,
    C.NOTE_JUDGMENTS_LOG,
)
RECEIPT_FIELDS = (
    "host",
    "session",
    "run_id",
    "project",
    "action",
    "source",
    "verdict",
    "change_id",
    "id",
    "path",
    "backup",
    "before",
    "after",
    "reason",
    "rounds",
    "status",
    "made",
    "digest_chars",
    "kind",
    "delivery",
    "turn",
    "typed",
    "skill_source",
    "step_id",
    "limit",
)
RECEIPT_LISTS = tuple(key for key, _ in ui_page.RECEIPT_LISTS)
UNAVAILABLE = "Version content unavailable in this data folder."
NO_VERSION = "No diff: the ledger records no new version for this event."
CONTEXT = 3  # unchanged lines shown around each change, as unified diffs do
NO_FINDINGS = "Findings unavailable in this data folder."


class Omitted(str):
    """A value left out for size: its own type, so it is never taken for missing data."""


OMITTED = Omitted("Omitted for size: this view's share of the page is full. The data folder still has it.")


def _oversize() -> Omitted:
    return Omitted(f"Omitted for size: the file is over {C.UI_FILE_MAX_BYTES:,} bytes, so it was not read.")


class LeftOut(NamedTuple):
    """Stands in for a card or record left out for size: how many records it stood for, never any of their content."""

    records: int = 1


def _overlong() -> Omitted:
    return Omitted("Omitted for size: too many changed lines to compare, so no diff was built.")


class TooLarge(Exception):
    pass


def _date(value) -> str:
    try:
        if isinstance(value, (int, float)) and math.isfinite(value):
            return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(value))
    except (OSError, OverflowError, ValueError):
        pass
    return "Not recorded"


def _inside(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
        return (
            ".." not in relative.parts
            and path.resolve().is_relative_to(root.resolve())
            and not any((root / part).is_symlink() for part in (relative, *relative.parents) if part != Path("."))
        )
    except (OSError, RuntimeError, ValueError):
        return False


class _Share:
    """The page bytes one view may still use, and how many records and values it left out for size."""

    def __init__(self, size: int):
        self.left, self.records, self.values = size, 0, 0

    def left_out(self, html: str) -> bool:
        """Whether even this smallest form of a record is over what is left; such a record counts as left out."""
        if len(html.encode("utf-8")) <= self.left:
            return False
        self.records += 1
        return True

    def place(self, render: Callable[[dict], str], record: dict, big: str | None = None) -> bool:
        """Charge one record's rendered HTML. A big value that does not fit becomes OMITTED and a record that still
        does not fit is left out, so a value that does not fit never shrinks what the next record may use."""
        cost = len(render(record).encode("utf-8"))
        if cost > self.left and big and not isinstance(record[big], Omitted):
            record[big] = OMITTED
            cost = len(render(record).encode("utf-8"))
        if cost > self.left:
            self.records += 1
            return False
        self.left -= cost
        self.values += bool(big) and isinstance(record[big], Omitted)
        return True


def _read(root: Path, path: Path | None, *, strip_bom: bool = True) -> str | None:
    """A file's text; an Omitted, unread, for a file over UI_FILE_MAX_BYTES; None for a missing or unreadable one."""
    if path is None or not _inside(root, path):
        return None
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > C.UI_FILE_MAX_BYTES:
            return _oversize()
        with path.open(encoding="utf-8-sig" if strip_bom else "utf-8") as stream:
            text = stream.read(C.UI_FILE_MAX_BYTES + 1)
        return _oversize() if len(text) > C.UI_FILE_MAX_BYTES else text
    except (OSError, UnicodeError, ValueError):
        return None


def _files(root: Path, folder: Path, pattern: str) -> list[Path]:
    if not _inside(root, folder):
        return []
    return sorted(p for p in folder.glob(pattern) if _inside(root, p) and p.is_file())


def _receipt(row: dict, runs: dict) -> dict:
    result = {key: row[key] for key in RECEIPT_FIELDS if key in row}
    result["date"] = _date(row.get("ts"))
    run = runs.get((row.get("host"), row.get("run_id")), {})
    result["session"] = row.get("session") or run.get("session") or "Not recorded"
    result["project"] = row.get("project") or run.get("project") or "Not recorded"
    if row.get("verdict"):
        result["findings"] = row.get("findings", "Findings were not retained in this ledger.")
    return result


def _source(sources: report._SkillSources, key: str, name: str) -> str:
    value = sources.sources.get(name, {}).get(key)
    return f"{value['host']}: {value['path']}" if value else key


def _backup(cfg: C.Config, row: dict, name: str) -> Path | None:
    raw = row.get("backup")
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    # A copied data folder retains absolute ledger paths. Only the known history layout can be relocated.
    if path.parent.name != name or path.parent.parent.name != "skill-history":
        return None
    return cfg.data / "skill-history" / name / path.name


def _file_name(row: dict, name: str) -> str:
    path = Path(str(row.get("path") or "SKILL.md"))
    source = row.get("skill_source")
    if isinstance(source, dict) and isinstance(source.get("path"), str):
        root = Path(source["path"]).parent
        if root.name == name and path.is_relative_to(root) and path != root:
            return path.relative_to(root).as_posix()
    # A linked skill records its link path, so find the skill folder whose remainder is a skill file layout.
    roots = [
        index
        for index, part in enumerate(path.parts[:-1])
        if part == name
        and (
            path.parts[index + 1 :] == ("SKILL.md",) or path.parts[index + 1] in ("references", "templates", "scripts")
        )
    ]
    if not roots:
        return "SKILL.md"
    index = next((i for i in roots if i and path.parts[i - 1] == "skills"), roots[0])
    return Path(*path.parts[index + 1 :]).as_posix()


def _current(cfg: C.Config, plan: dict, filename: str) -> Callable[[], str | None]:
    """A reader of the current file a skill's changes to filename lead to; a SKILL.md whose text is on the page is not
    read again."""
    parts = Path(filename).parts
    current = plan["current"]
    if (
        current is None
        or ".." in parts
        or (filename != "SKILL.md" and parts[0] not in ("references", "templates", "scripts"))
    ):
        return lambda: None
    if filename == "SKILL.md" and plan["content"] is not None:
        return lambda: plan["content"]
    path = current if filename == "SKILL.md" else current.parent / filename
    return lambda: _read(cfg.data, path, strip_bom=filename == "SKILL.md")


class _Versions:
    """One skill file's contents by hash, so a change gets its diff whatever order edits came in: the backups recorded
    under that hash, then the current file, read for the change that needs them and never kept after it."""

    def __init__(self, cfg: C.Config, rows: list[dict], name: str, current: Callable[[], str | None]):
        self.cfg, self.current = cfg, current
        self.current_hash: str | None = None  # learned from the first read, so other hashes never read it again
        self.current_large = False
        self.wrong: set[Path] = set()  # backups missing, unreadable or not holding the hash their change recorded
        self.backups: dict[str, list[Path]] = {}
        for row in rows:
            path = _backup(cfg, row, name)
            if path is not None and isinstance(row.get("before"), str):
                self.backups.setdefault(row["before"], []).append(path)

    def get(self, digest: str | None) -> str | None:
        """The content that hashes to digest; an Omitted when a file that may hold it was too large to read."""
        if digest is None:
            return None
        large = self.current_large
        for path in self.backups.get(digest, []):
            if path in self.wrong:
                continue
            text = _read(self.cfg.data, path, strip_bom=False)
            if isinstance(text, Omitted):
                large = True
            elif text is not None and sha(text) == digest:
                return text
            else:
                self.wrong.add(path)
        if not self.current_large and self.current_hash in (None, digest):
            text = self.current()
            if isinstance(text, Omitted):
                self.current_large = large = True
            else:
                self.current_hash = sha(text) or ""
                if self.current_hash == digest:
                    return text
        return _oversize() if large else None


def _diff(filename: str, row: dict, old: str | None, new: str | None, room: int) -> str:
    """One change's unified diff, or why it is not shown. Only the lines between the versions' common start and end
    are compared, and only when there are few enough of them and even their largest rendering fits room."""
    if isinstance(old, Omitted) or isinstance(new, Omitted):
        return old if isinstance(old, Omitted) else new
    if old is None or new is None:
        return UNAVAILABLE
    before, after = old.splitlines(keepends=True), new.splitlines(keepends=True)
    if before == after:
        return "No content change."
    same = min(len(before), len(after))
    head = next((i for i in range(same) if before[i] != after[i]), same)
    tail = next((i for i in range(same - head) if before[-1 - i] != after[-1 - i]), same - head)
    start, cut = max(0, head - CONTEXT), max(0, tail - CONTEXT)
    a, b = before[start : len(before) - cut], after[start : len(after) - cut]
    if len(a) * len(b) > C.UI_DIFF_MAX_PAIRS:
        return _overlong()
    fromfile, tofile = f"{filename} ({row.get('before') or 'new file'})", f"{filename} ({row.get('after')})"
    # A unified diff prints each compared line at most once, after a one-character mark, and starts another hunk
    # only after 2 * CONTEXT + 1 unchanged lines, so this bounds its escaped size before difflib runs.
    largest = 8 + sum(len(html.escape(text).encode("utf-8")) + 1 for text in (fromfile, tofile, *a, *b))
    largest += 64 * (1 + sum((Counter(a) & Counter(b)).values()) // (2 * CONTEXT + 1))
    if largest > room:
        return OMITTED
    lines = difflib.unified_diff(a, b, fromfile=fromfile, tofile=tofile, n=CONTEXT)
    return "".join(_moved(line, start) for line in lines)


def _moved(line: str, start: int) -> str:
    """A diff line, its hunk's line numbers moved past the start lines difflib did not see."""
    if not start or not line.startswith("@@ -"):
        return line
    return re.sub(r"([-+])(\d+)", lambda match: f"{match[1]}{int(match[2]) + start}", line, count=2)


def _findings_path(cfg: C.Config, row: dict, *, notes: bool) -> Path | None:
    identity = row.get("change") if notes else row.get("id")
    if not isinstance(identity, str) or Path(identity).name != identity or identity in (".", ".."):
        return None
    return cfg.data / "rejected" / f"{'note-' if notes else ''}{identity}.md"


def _findings(cfg: C.Config, path: Path | None) -> str | None:
    """The findings in a rejection's retained record, without the proposed change."""
    record = _read(cfg.data, path)
    if record and not isinstance(record, Omitted):
        record = record.split("\n## Last proposed diff", 1)[0].split("\n## Proposed operation", 1)[0]
    return record


def _rejection(row: dict, runs: dict, found: str | None, *, notes: bool) -> dict:
    verdict = "expired unjudged" if report.expired(row) else "fail"
    if notes and found is None:
        reason = str(row.get("reason") or "")
        if reason.startswith("no judge answered for "):
            verdict = "expired unjudged"
        elif not reason.startswith("the judge dropped it ("):
            verdict = "not recorded (note not saved)"
    return {
        **_receipt(row, runs),
        "name": row.get("name"),
        "target": "project note" if notes else "skill",
        "verdict": verdict,
        "findings": found or row.get("findings") or NO_FINDINGS,
    }


def _place_rejection(cfg, share: _Share, render, row: dict, runs: dict, shown: dict, *, notes: bool) -> dict | None:
    """Charge one rejection; its findings file is read only when the rest of the record fits, and not again for the
    other view while its findings are on the page."""
    if share.left_out(render({**_rejection(row, runs, OMITTED, notes=notes), "findings": ""})):
        return None
    path = _findings_path(cfg, row, notes=notes)
    found = shown.pop(path) if path in shown else _findings(cfg, path)
    record = _rejection(row, runs, found, notes=notes)
    if not share.place(render, record, None if record["findings"] == NO_FINDINGS else "findings"):
        return None
    if found and record["findings"] is found:
        shown[path] = found
    return record


def _overview(data: dict, share: _Share) -> dict:
    """Charge the report tables, always shown, then the skill outcome rows in name order."""
    rows = data["outcomes"]
    share.left -= len(ui_page.overview({**data, "outcomes": [], "outcomes_omitted": len(rows)}).encode("utf-8"))
    for index, row in enumerate(rows):
        if not share.place(ui_page.outcome_row, row):
            rows[index] = LeftOut()
    data["outcomes"] = [row for row in rows if not isinstance(row, LeftOut)]
    data["outcomes_omitted"] = share.records
    return data


def _load(cfg: C.Config, plan: dict) -> None:
    """Read a skill's current, or else archived, SKILL.md into its card."""
    item, current = plan["item"], plan["current"]
    item["status"] = "Current" if current else "Ledger only; current file unavailable in this data folder"
    content = _read(cfg.data, current) if current else None
    if current is None and plan["archived"]:
        content = _read(cfg.data, plan["archived"])
        if content is not None:
            plan["current"], item["status"] = plan["archived"], "Archived"
    item["text"] = content
    if isinstance(content, Omitted):
        item["description"] = item["version"] = content
    elif content is not None:
        item["description"] = frontmatter(content)[0].get("description", "Description unavailable")
        item["version"] = sha(content)
    else:
        item["description"], item["version"] = "Description unavailable", "Unavailable"


def _skills_view(cfg: C.Config, plans: list[dict], share: _Share, history: _Share) -> list[int]:
    """Charge the skill cards, most recently changed first; a card that cannot fit even empty is left out unread,
    and a card left out takes its version history with it."""
    shown = []
    for index in sorted(range(len(plans)), key=lambda i: plans[i]["recency"], reverse=True):
        plan = plans[index]
        changes = len(plan["shown"])

        def card(item: dict, changes: int = changes) -> str:
            return ui_page.skill_card({**item, "history_omitted": changes})

        if not share.left_out(card(plan["item"])):
            _load(cfg, plan)
            if share.place(card, plan["item"], "text" if isinstance(plan["item"]["text"], str) else None):
                # A text left out for size is dropped now; the version history reads the file if a diff needs it.
                text = plan["item"]["text"]
                plan["content"] = None if isinstance(text, Omitted) else text
                shown.append(index)
                continue
        history.records += changes
        plans[index] = LeftOut(1 + changes)
    return sorted(shown)


def _turns(lists: list[list[dict]]) -> list[tuple[int, int]]:
    """(list, position) of every record of lists kept oldest first: the newest of each list in turn, then the next."""
    order = sorted(
        (len(rows) - 1 - position, -row["ts"], number, position)
        for number, rows in enumerate(lists)
        for position, row in enumerate(rows)
    )
    return [(number, position) for _, _, number, position in order]


def _history_view(cfg: C.Config, plans: list[dict], share: _Share, runs: dict) -> None:
    """Charge the shown skills' version changes, newest first in each skill, a skill at a time in turn; a change that
    cannot fit even without its diff reads no file."""
    files = [[_file_name(row, plan["item"]["name"]) for row in plan["shown"]] for plan in plans]
    versions: dict[tuple[int, str], _Versions] = {}
    for index, position in _turns([plan["shown"] for plan in plans]):
        plan, filename = plans[index], files[index][position]
        item, row, key = plan["item"], plan["shown"][position], (index, filename)
        if key not in versions:
            rows = [
                change
                for change, name in zip(plan["shown"], files[index], strict=True)
                if name == filename and isinstance(change.get("after"), str)
            ]
            versions[key] = _Versions(cfg, rows, item["name"], _current(cfg, plan, filename))
        # Only a change that records the content it leaves has versions to compare; archive, keep and restore
        # events record none.
        linked = isinstance(row.get("after"), str)
        entry = {
            "date": _date(row.get("ts")),
            "action": row.get("action"),
            "file": filename,
            "before": row.get("before"),
            "after": row.get("after"),
            "diff": "" if linked else NO_VERSION,
            "receipt": _receipt(row, runs),
        }
        least = ui_page.history_entry(entry)
        if not linked:
            placed = share.place(ui_page.history_entry, entry)
        elif share.left_out(least):
            placed = False
        else:
            new = versions[key].get(row.get("after"))
            created = row.get("before") is None and row.get("action") in ("create", "write_file")
            old = "" if created else versions[key].get(row.get("before"))
            entry["diff"] = _diff(filename, row, old, new, share.left - len(least.encode("utf-8")))
            placed = share.place(ui_page.history_entry, entry, None if entry["diff"] == UNAVAILABLE else "diff")
        if placed:
            plan["kept"].append((position, entry))
        else:
            item["history_omitted"] += 1
    for plan in plans:
        plan["item"]["history"] = [entry for _, entry in sorted(plan["kept"], key=lambda pair: pair[0])]


def _receipts_view(cfg: C.Config, plans: list[dict], share: _Share, runs: dict, findings: dict) -> list[int]:
    """Charge the receipt cards, most recent first, then their records newest first in each list, a list at a time
    in turn."""
    shown = []
    for index in sorted(range(len(plans)), key=lambda i: plans[i]["recency"], reverse=True):
        plan = plans[index]
        counts = {key: len(rows) for key, rows in plan["lists"].items()}

        def card(item: dict, counts: dict = counts) -> str:
            return ui_page.receipt_card({**item, "omitted": counts})

        if share.place(card, plan["item"]):
            shown.append(index)
        else:
            share.records += sum(counts.values())
            plans[index] = LeftOut(1 + sum(counts.values()))
    slots = [(index, key) for index in shown for key in RECEIPT_LISTS]
    kept: dict[tuple[int, str], list] = {}
    for slot, position in _turns([plans[index]["lists"][key] for index, key in slots]):
        index, key = slots[slot]
        plan, row = plans[index], plans[index]["lists"][key][position]
        if key == "rejections":
            record = _place_rejection(cfg, share, ui_page.receipt_record, row, runs, findings, notes=False)
        else:
            record = _receipt(row, runs)
            record = record if share.place(ui_page.receipt_record, record) else None
        if record is None:
            plan["item"]["omitted"][key] += 1
        else:
            kept.setdefault((index, key), []).append((position, record))
    for (index, key), records in kept.items():
        plans[index]["item"][key] = [record for _, record in sorted(records, key=lambda pair: pair[0])]
    return sorted(shown)


def _rejections_view(cfg: C.Config, share: _Share, skill: list, note: list, runs: dict, findings: dict) -> list:
    """Charge skill and note rejections newest first, one of each in turn; they show skill rejections first, each
    group oldest first."""
    kept = {}
    for group, position in _turns([skill, note]):
        row = (skill, note)[group][position]
        record = _place_rejection(cfg, share, ui_page.rejection_card, row, runs, findings, notes=bool(group))
        if record is not None:
            kept[(group, position)] = record
    return [kept[key] for key in sorted(kept)]


def _note_source(content: str, path: Path, ledger: list[dict]) -> str:
    source = memory.parse_topic(content).get("source")
    if source:
        return "Claude's own" if source == "claude" else source
    for row in reversed(ledger):
        recorded = Path(str(row.get("path") or ""))
        same = recorded == path or (
            len(path.parts) >= 4 and path.parts[-4] == "projects" and recorded.parts[-4:] == path.parts[-4:]
        )
        if same and row.get("after") == content and row.get("source") not in ("native-reconcile", "migration"):
            return f"nowitknows-{row['host']}"
    return "Source not recorded"


def _topic(content: str, stem: str, path: Path, ledger: list[dict]) -> dict:
    """A note file's name, description, source and text; one too large to read has only its file name."""
    if isinstance(content, Omitted):
        return {"name": stem, "description": "", "source": content, "text": content}
    meta = memory.parse_topic(content)
    return {
        "name": meta.get("name") or stem,
        "description": meta.get("description", ""),
        "source": _note_source(content, path, ledger),
        "text": content,
    }


def _notes(cfg: C.Config, ledger: list[dict], share: _Share) -> tuple[list[dict], int]:
    """Charge global memory entries and project notes, then retained note backups newest first; a note file whose
    card cannot fit even empty is left out unread."""
    result = []

    def place(note: dict) -> bool:
        return share.place(ui_page.note_card, note, "text" if isinstance(note["text"], str) else None)

    def unread(note: dict) -> bool:
        return share.left_out(ui_page.note_card({**note, "name": "", "description": "", "source": "", "text": ""}))

    entries = memory.EntryStore(cfg.data / "memory", create=False)
    for target in ("user", "memory"):
        path = entries.file(target)
        text = _read(cfg.data, path)
        found = (
            [text] if isinstance(text, Omitted) else [e.strip() for e in memory._SPLIT.split(text or "") if e.strip()]
        )
        for content in found:
            row = next((r for r in reversed(ledger) if r.get("target") == target and r.get("after") == content), {})
            source = f"nowitknows-{row['host']}" if row else "Source not recorded"
            note = {
                "name": memory.HEADERS[target],
                "project": "Global",
                "path": str(path),
                "text": content,
                "source": content if isinstance(content, Omitted) else source,
                "status": "Current",
            }
            if place(note):
                result.append(note)
    projects = cfg.data / "projects"
    if _inside(cfg.data, projects):
        for folder in sorted(projects.glob("*/memory")):
            if not _inside(cfg.data, folder):
                continue
            for path in memory.TopicStore(folder).topics():
                note = {"project": folder.parent.name, "path": str(path), "status": "Stored project note"}
                if unread(note) or (content := _read(cfg.data, path)) is None:
                    continue
                note.update(_topic(content, path.stem, path, ledger))
                if place(note):
                    result.append(note)
    backups = [
        row
        for row in ledger
        if row.get("store") != "global"
        and isinstance(row.get("backup"), str)
        and Path(row["backup"]).parent.parent.name == "memory-history"
    ]
    shown, kept = backups[-C.UI_RECORDS_SHOWN :], []
    for position in reversed(range(len(shown))):
        row = shown[position]
        original = Path(row["backup"])
        path = cfg.data / "memory-history" / original.parent.name / original.name
        note = {
            "project": row.get("project") or row.get("path", "Not recorded"),
            "path": str(path),
            "status": f"Retained backup, {_date(row.get('ts'))}; current native note may differ",
        }
        if unread(note) or (content := _read(cfg.data, path)) is None:
            continue
        note.update(_topic(content, original.stem, Path(str(row.get("path") or "")), ledger))
        if place(note):
            kept.append((position, note))
    result += [note for _, note in sorted(kept, key=lambda pair: pair[0])]
    return result, max(0, len(backups) - C.UI_RECORDS_SHOWN)


def _newest(rows: list) -> tuple[list, int]:
    return rows[-C.UI_RECORDS_SHOWN :], max(0, len(rows) - C.UI_RECORDS_SHOWN)


def snapshot(cfg: C.Config, *, now: float | None = None) -> dict:
    """Read the selected store without locks, recovery, transcript reads, or directory creation."""
    return _snapshot(cfg, time.time() if now is None else now)


def _snapshot(cfg: C.Config, now: float) -> dict:
    total = 0
    for filename in LOGS:
        if not _inside(cfg.data, cfg.data / filename):
            raise ValueError("A ledger points outside the selected data folder.")
        try:
            total += (cfg.data / filename).stat().st_size
        except FileNotFoundError:
            pass
    # Every aggregate needs all rows, so ledgers past the limit are refused before any is parsed.
    if total > C.UI_LEDGERS_MAX_BYTES:
        raise TooLarge("The ledgers are larger than the page size limit, so the snapshot was not served.")
    logs = {filename: report._rows(cfg, filename) for filename in LOGS}  # each ledger is read once, line by line
    ledger, runs, rejected, activity = (
        logs[f]
        for f in (
            "skills_ledger.jsonl",
            "runs.jsonl",
            "rejected.jsonl",
            "activity.jsonl",
        )
    )
    run_map = {(r["host"], r.get("run_id")): r for r in runs if r.get("run_id")}
    paths = _files(cfg.data, cfg.data / "skills", "*/SKILL.md")
    sources = report._SkillSources(cfg, [*runs, *ledger, *rejected, *activity], skill_paths=[])
    local_sources = {}
    for path in paths:
        name = path.parent.name
        source = trigger_source(path, cfg)
        key = trigger_key(source)
        known = sources.sources.get(name, {})
        copied = [
            k
            for k, s in known.items()
            if s["host"] == "shared" and Path(s["path"]).parts[-3:] == ("skills", name, "SKILL.md")
        ]
        if key not in known and len(copied) == 1:
            key = copied[0]
        else:
            sources.add(name, source)
        local_sources[path] = key
    outcomes = report.skill_outcomes(cfg, sources=sources, logs=logs)
    grouped: dict[str, dict] = {}
    for row in ledger:
        name = row.get("name")
        if not isinstance(name, str):
            continue
        key = sources.identity(row, name)
        group = grouped.setdefault(key, {"name": name, "changes": [], "current": None})
        group["changes"].append(row)
    for path in paths:
        name = path.parent.name
        key = local_sources[path]
        grouped.setdefault(key, {"name": name, "changes": [], "current": None})["current"] = path
    skill_plans, receipt_plans = [], []
    for key, group in sorted(grouped.items(), key=lambda pair: (pair[1]["name"], pair[0])):
        name, current = group["name"], group["current"]
        changes = sorted(group["changes"], key=lambda r: r["ts"])
        archived = None
        if current is None and changes and changes[-1].get("action") == "archive":
            path = Path(str(changes[-1].get("path") or ""))
            if path.parent.name == "skill-archive":
                archived = cfg.data / "skill-archive" / path.name / "SKILL.md"
        outcome = next(
            (
                o
                for o in outcomes.values()
                if o["name"] == name
                and (trigger_key(o["skill_source"]) if o["skill_source"] else f"legacy:{o['host']}:{name}") == key
            ),
            {},
        )
        selected_activity = [r for r in activity if r.get("skill") == name and sources.identity(r, name) == key]
        selected_runs = [
            r
            for r in runs
            if name in (r.get("loaded") or [])
            and any(
                sources.identity(r, name, source) == key
                for source in (r.get("loaded_sources") or {}).get(name, []) or [None]
            )
        ]
        related, related_older = _newest(
            [row for row in rejected if row.get("name") == name and sources.identity(row, name) == key]
        )
        learned, learned_older = _newest([r for r in changes if r.get("action") == "create"])
        loads, loads_older = _newest([r for r in selected_activity if report._activity_kind(r, r["host"] == "codex")])
        recent_changes, changes_older = _newest(changes)
        reviews, reviews_older = _newest(selected_runs)
        source = _source(sources, key, name)
        skill_plans.append(
            {
                "item": {
                    "name": name,
                    "description": "",
                    "source": source,
                    "status": "",
                    "version": "",
                    "text": "",
                    "history": [],
                    "history_older": max(0, len(changes) - C.UI_RECORDS_SHOWN) if valid_name(name) else 0,
                    "history_omitted": 0,
                },
                "shown": changes[-C.UI_RECORDS_SHOWN :] if valid_name(name) else [],
                "current": current,
                "archived": archived,
                "content": None,
                "recency": changes[-1]["ts"] if changes else -math.inf,
                "kept": [],
            }
        )
        lists = {
            "learned": learned,
            "changes": recent_changes,
            "loads": loads,
            "reviews": reviews,
            "rejections": related,
        }
        receipt_plans.append(
            {
                "item": {
                    "name": name,
                    "source": source,
                    "outcome": outcome,
                    **{key: [] for key in RECEIPT_LISTS},
                    "older": {
                        "learned": learned_older,
                        "changes": changes_older,
                        "loads": loads_older,
                        "reviews": reviews_older,
                        "rejections": related_older,
                    },
                    "omitted": dict.fromkeys(RECEIPT_LISTS, 0),
                },
                "lists": lists,
                "recency": max((row["ts"] for rows in lists.values() for row in rows), default=-math.inf),
            }
        )
    current_sources = set(local_sources.values())
    outcome_rows = [
        {
            **o,
            "label": label,
            "attention": bool(
                o["skill_source"]
                and trigger_key(o["skill_source"]) in current_sources
                and sum(o["recent"]) >= C.ATTENTION_CHANGES
            ),
        }
        for label, o in sorted(outcomes.items())
    ]
    since = now - C.REPORT_WEEKS * C.WEEK_S
    skill_rejections, skill_rejections_older = _newest(rejected)
    note_rejections, note_rejections_older = _newest([r for r in logs[C.REFUSALS_LOG] if r.get("change")])
    # Each view spends only its own share of the page, so one view's records never crowd out another's.
    shares = {
        "overview": _Share(C.UI_OVERVIEW_BYTES),
        "skills": _Share(C.UI_SKILLS_BYTES),
        "history": _Share(C.UI_HISTORY_BYTES),
        "receipts": _Share(C.UI_RECEIPTS_BYTES),
        "rejections": _Share(C.UI_REJECTIONS_BYTES),
        "notes": _Share(C.UI_NOTES_BYTES),
    }
    findings: dict[Path, str] = {}  # findings on the page, so the other view need not read them again
    overview = _overview(
        {
            "weeks": report.weekly(cfg, C.REPORT_WEEKS, now, logs=logs),
            "outcomes": outcome_rows,
            "refusals": report.refusals(cfg, since, logs=logs),
            "note_judge": report.note_judge(cfg, since, logs=logs),
        },
        shares["overview"],
    )
    skills = [skill_plans[i] for i in _skills_view(cfg, skill_plans, shares["skills"], shares["history"])]
    _history_view(cfg, skills, shares["history"], run_map)
    receipts = _receipts_view(cfg, receipt_plans, shares["receipts"], run_map, findings)
    rejections = _rejections_view(cfg, shares["rejections"], skill_rejections, note_rejections, run_map, findings)
    notes, notes_older = _notes(cfg, logs["memory_ledger.jsonl"], shares["notes"])
    return {
        "generated": _date(now),
        "data_dir": str(cfg.data),
        "overview": overview,
        "skills": [plan["item"] for plan in skills],
        "receipts": [receipt_plans[i]["item"] for i in receipts],
        "rejections": rejections,
        "rejections_older": skill_rejections_older + note_rejections_older,
        "notes": notes,
        "notes_older": notes_older,
        "omitted": {name: {"records": share.records, "values": share.values} for name, share in shares.items()},
        "warnings": [
            "This snapshot reads only the selected data folder. Native note and skill folders outside it are omitted.",
            "Retained note backups are historical copies, not the current contents of native note folders.",
            "Deliveries and file opens do not prove instructions were followed or that a task succeeded.",
        ],
    }
