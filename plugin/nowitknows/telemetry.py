from __future__ import annotations

import hashlib
from bisect import bisect_right
from pathlib import Path

from . import config as C
from .report import _activity_kind, _rows, tokens
from .router import trigger_source

HOSTS = ("claude", "codex", "cursor")
EDITS = ("create", "patch", "edit", "write_file")
LIFECYCLE = ("create", "patch", "edit", "archive", "keep", "restore")
FATES = ("kept", "reverted", "edited", "archived", "removed")
SHARED_LABELS = ("reflector", "triggers")
ESTIMATE = (
    "saved tokens are an estimate: {n:,} per session that loads a skill nowitknows made or changed, from a"
    " benchmark on synthetic fixtures, not measured in your sessions; nothing counts after a revert, archive or removal"
)
SPENT_BASIS = (
    "per-skill spent is the judge and reviser calls that name the skill plus an estimated equal share of each"
    " reviewer and trigger call of a run that proposed it; project note judgments are charged to no skill"
)

Identity = tuple[str, str] | None


def _host(row: dict) -> str:
    host = row.get("host")
    return host if host in HOSTS else "claude"


def _well_formed(row: dict) -> bool:
    """Hashes and paths are strings or absent; anything else is a corrupt row, skipped rather than compared."""
    return all(row.get(k) is None or isinstance(row[k], str) for k in ("before", "after", "path"))


def _identity(cfg: C.Config, row: dict, name: str) -> Identity:
    """The skill a row is about: its library and SKILL.md path; None when unrecorded."""
    source = row.get("skill_source")
    if (
        isinstance(source, dict)
        and source.get("host") in (*HOSTS, "cursor", "shared")
        and isinstance(source.get("path"), str)
    ):
        return source["host"], source["path"]
    path = Path(row["path"]) if isinstance(row.get("path"), str) else None
    folder = next((p for p in path.parents if p.name == name), None) if path and path.is_absolute() else None
    if folder is None:
        return None
    # Old ledger paths are evidence; resolving them now could follow a replacement link.
    source = trigger_source(folder / "SKILL.md", cfg, real=folder / "SKILL.md")
    return ("shared" if source["host"] == "shared" else _host(row)), source["path"]


def _same_skill(host: str, ident: Identity, other_host: str, other: Identity) -> bool:
    """Recorded sources must match; with one unrecorded, the name decides within the recorded library or host."""
    if ident is not None and other is not None:
        return ident == other
    if other is not None:
        return other[0] in ("shared", host)
    return host == other_host


def _shas(path: Path) -> set[str] | None:
    """The ledger's content hashes of a SKILL.md now, with and without a BOM; None when it cannot be read."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    return {hashlib.sha256(t.encode()).hexdigest()[:16] for t in (text, text.removeprefix("﻿"))}


def _blank() -> dict:
    return {
        "spent": 0,
        "loads": 0,
        "saved_estimate": 0,
        "suggested": 0,
        "followed": 0,
        "accepted": 0,
        **dict.fromkeys(FATES, 0),
    }


def _now(path: object) -> tuple[str, set[str] | None]:
    if not isinstance(path, str) or not path.endswith("SKILL.md") or not Path(path).is_absolute():
        return "unknown", None
    if not Path(path).exists():
        return "removed", None
    return "present", _shas(Path(path))


def _fate(change: dict, nxt: dict | None) -> tuple[str, float | None]:
    """What the user did next to a judged background change, and when: the next ledger row for its SKILL.md, else
    the file now (time unknown)."""
    if nxt is not None:
        ts = nxt.get("ts")
        if nxt.get("source") == "background" or nxt.get("action") in ("keep", "restore"):
            return "kept", ts  # the next review built on it, or the user asked to keep it
        if nxt.get("action") == "archive":
            return ("archived" if nxt.get("source") == "user" else "kept"), ts
        return ("reverted" if change.get("before") and nxt.get("after") == change.get("before") else "edited"), ts
    if change.get("action") == "write_file":
        return "kept", None  # only SKILL.md hashes are comparable
    state, now = _now(change.get("path"))
    if state != "present" or now is None or change.get("after") in now:
        return ("removed" if state == "removed" else "kept"), None
    return ("reverted" if change.get("before") in now else "edited"), None


def _next_rows(history: list[dict]) -> list[dict | None]:
    """For each row, the first later row that decides its fate: not a support file or a scope record, and not another
    SKILL.md copy when both rows carry a path. One backward pass."""
    out: list[dict | None] = [None] * len(history)
    by_path: dict[str, tuple[int, dict]] = {}
    pathless: tuple[int, dict] | None = None
    any_: tuple[int, dict] | None = None
    for i in range(len(history) - 1, -1, -1):
        row = history[i]
        path = row.get("path")
        if isinstance(path, str):
            found = [c for c in (by_path.get(path), pathless) if c]
            out[i] = min(found, key=lambda c: c[0])[1] if found else None
        else:
            out[i] = any_[1] if any_ else None
        if row.get("action") not in ("write_file", "scope"):  # neither changes what SKILL.md says
            any_ = (i, row)
            if isinstance(path, str):
                by_path[path] = (i, row)
            else:
                pathless = (i, row)
    return out


class _Life:
    """One skill's learned-change lifecycle: intervals in which a judged background change stood."""

    def __init__(self, host: str, ident: Identity):
        self.host, self.ident = host, ident
        self.befores: list[str | None] = []
        self.where: dict[str, list[int]] = {}
        self.archived = False
        self.start: float | None = None
        self.starts: list[float] = []
        self.ends: list[float] = []
        self.last: float = 0

    def _set(self, ts: float) -> None:
        standing = bool(self.befores) and not self.archived
        if standing and self.start is None:
            self.start = ts
        elif not standing and self.start is not None:
            self.starts.append(self.start)
            self.ends.append(ts)
            self.start = None

    def _pop(self, to: int) -> None:
        for before in self.befores[to:]:
            if before is not None:
                self.where[before].pop()
        del self.befores[to:]

    def apply(self, row: dict, ts: float) -> None:
        self.last = ts
        action = row.get("action")
        if row.get("source") == "background" and row.get("verdict"):
            before = row.get("before")
            if before is not None:
                self.where.setdefault(before, []).append(len(self.befores))
            self.befores.append(before)
            self.archived = False
        elif action == "archive":
            self.archived = True
        elif action in ("keep", "restore"):
            self.archived = False
        elif action in EDITS and action != "write_file" and self.where.get(row.get("after") or ""):
            self._pop(self.where[row["after"]][-1])  # a revert to the content before a learned change undoes it
        self._set(ts)

    def close(self, *, current: bool = True) -> None:
        """A revert or removal seen only in the file now has no time: it ends the saving after the last ledger row."""
        if current and self.start is not None and self.ident is not None:
            state, now = _now(self.ident[1])
            if state == "removed":
                self._pop(0)
            elif now:
                hits = [i for h in now for i in self.where.get(h, ())]
                if hits:
                    self._pop(max(hits))
            self._set(self.last)
        if self.start is not None:
            self.starts.append(self.start)
            self.ends.append(float("inf"))
            self.start = None

    def standing(self, ts: float) -> bool:
        i = bisect_right(self.starts, ts) - 1
        return i >= 0 and ts < self.ends[i]


def learned_lives(cfg: C.Config, ledger: list[dict], *, current: bool = False) -> dict[str, list[_Life]]:
    """Build learned-change intervals from recorded identities and lifecycle events."""
    by_name: dict[str, list[dict]] = {}
    lives: dict[str, list[_Life]] = {}
    for row in sorted(
        (r for r in ledger if _well_formed(r) and type(r.get("ts")) in (int, float)), key=lambda r: r["ts"]
    ):
        name = row.get("name")
        if not isinstance(name, str) or type(row.get("ts")) not in (int, float):
            continue
        by_name.setdefault(name, []).append(row)
        if row.get("source") == "background" and row.get("verdict"):
            host, ident = _host(row), _identity(cfg, row, name)
            if not any(life.host == host and life.ident == ident for life in lives.get(name, ())):
                lives.setdefault(name, []).append(_Life(host, ident))
    # Savings eligibility: every lifecycle row of the same skill moves its lifecycle, in time order. A row with no
    # recorded source moves its own unrecorded lifecycle, else the only one it can match, never several.
    for name, history in by_name.items():
        for row in history:
            judged = row.get("source") == "background" and row.get("verdict")
            if not (judged or row.get("action") in LIFECYCLE):
                continue
            host, ident = _host(row), _identity(cfg, row, name)
            matched = [life for life in lives.get(name, ()) if _same_skill(host, ident, life.host, life.ident)]
            if ident is None and len(matched) > 1:
                matched = [life for life in matched if life.host == host and life.ident is None]
            for life in matched if ident is not None or len(matched) == 1 else ():
                life.apply(row, row["ts"])
    for group in lives.values():
        for life in group:
            life.close(current=current)

    return lives


def learned_at(cfg: C.Config, event: dict, lives: dict[str, list[_Life]]) -> bool:
    """Whether a matching judged change stood when the recorded content reached the session."""
    name, host = event.get("skill"), _host(event)
    ident = _identity(cfg, event, name)
    return any(
        _same_skill(host, ident, life.host, life.ident) and life.standing(event.get("ts", 0))
        for life in lives.get(name, ())
    )


def summary(cfg: C.Config) -> dict:
    """All-time telemetry per host, per skill and overall, from the local logs only."""
    usage = _rows(cfg, "usage.jsonl")
    runs = _rows(cfg, "runs.jsonl")
    ledger = sorted((r for r in _rows(cfg, "skills_ledger.jsonl") if _well_formed(r)), key=lambda r: r["ts"])
    rejected = _rows(cfg, "rejected.jsonl")
    activity = _rows(cfg, "activity.jsonl")
    per = C.EST_TOKENS_SAVED_PER_LOAD
    hosts = {h: _blank() for h in HOSTS}
    notes = dict.fromkeys(HOSTS, 0)
    skills: dict[str, dict[str, dict]] = {h: {} for h in HOSTS}

    def skill(host: str, name: str) -> dict:
        return skills[host].setdefault(name, {**_blank(), "spent_named": 0, "spent_shared": 0})

    # Reviewer and trigger tokens of a run go to the skills it proposed, in equal shares (an estimate); a judge or
    # reviser call names its skill. Note judgments and tokens with no skill stay in the host total only.
    touched: dict[tuple[str, str], set[str]] = {}
    for row in [*(r for r in ledger if r.get("source") == "background" and r.get("verdict")), *rejected]:
        if isinstance(row.get("run_id"), str) and isinstance(row.get("name"), str):
            touched.setdefault((_host(row), row["run_id"]), set()).add(row["name"])
    for row in usage:
        host, n = _host(row), sum(tokens(row).values())
        hosts[host]["spent"] += n
        label = row.get("label")
        kind, _, name = label.partition(":") if isinstance(label, str) else ("", "", "")
        if kind in ("judge", "revise") and name:
            skill(host, name)["spent"] += n
            skill(host, name)["spent_named"] += n
            continue
        if kind == "judge-note":
            notes[host] += n
        if label not in SHARED_LABELS or not isinstance(row.get("run_id"), str):
            continue
        names = sorted(touched.get((host, row["run_id"]), ()))
        for i, name in enumerate(names):
            share = n // len(names) + (i < n % len(names))
            skill(host, name)["spent"] += share
            skill(host, name)["spent_shared"] += share
    # Precision proxy: each judged background change and the next row that decides its fate.
    by_name: dict[str, list[dict]] = {}
    for row in ledger:
        if isinstance(row.get("name"), str):
            by_name.setdefault(row["name"], []).append(row)
    for name, history in by_name.items():
        for change, nxt in zip(history, _next_rows(history), strict=True):
            if not (change.get("source") == "background" and change.get("verdict")):
                continue
            host, (fate, _) = _host(change), _fate(change, nxt)
            for bucket in (hosts[host], skill(host, name)):
                bucket["accepted"] += 1
                bucket[fate] += 1
    lives = learned_lives(cfg, ledger, current=True)

    loaded: set[tuple] = set()
    counted: set[tuple] = set()
    earned: set[tuple] = set()
    for row in sorted(activity, key=lambda r: r["ts"]):
        name, host, session, turn = row.get("skill"), _host(row), row.get("session"), row.get("turn")
        if _activity_kind(row, host == "codex") != "loads" or not isinstance(name, str):
            continue
        if not (isinstance(session, str) and session and type(turn) is int):
            continue  # a load with no session turn cannot be matched or counted once per session
        loaded.add((host, session, turn, name))
        s = skill(host, name)
        if (host, session, name) not in counted:
            counted.add((host, session, name))
            s["loads"] += 1
        if (host, session, name) in earned:
            continue  # Benchmark savings are per repeat session, so count them only once per session.
        ident = _identity(cfg, {"skill_source": row.get("skill_source")}, name)
        if any(
            _same_skill(host, ident, life.host, life.ident) and life.standing(row["ts"]) for life in lives.get(name, ())
        ):
            earned.add((host, session, name))
            s["saved_estimate"] += per
            hosts[host]["loads"] += 1
            hosts[host]["saved_estimate"] += per
    skipped: dict[str, dict[str, int]] = {h: {} for h in HOSTS}
    for row in activity:
        if row.get("kind") == "delivery_skipped":
            reason = row.get("reason") if isinstance(row.get("reason"), str) and row.get("reason") else "unknown"
            skipped[_host(row)][reason] = skipped[_host(row)].get(reason, 0) + 1
    for row in activity:
        names, session, turn = row.get("skills"), row.get("session"), row.get("turn")
        if row.get("kind") != "suggest" or not isinstance(names, list) or not names:
            continue
        host = _host(row)
        keyed = isinstance(session, str) and session and type(turn) is int
        hits = [n for n in names if isinstance(n, str) and keyed and (host, session, turn, n) in loaded]
        hosts[host]["suggested"] += 1
        hosts[host]["followed"] += bool(hits)
        for name in {n for n in names if isinstance(n, str)}:
            s = skill(host, name)
            s["suggested"] += 1
            s["followed"] += name in hits
    overall = _blank()
    for values in hosts.values():
        for key in overall:
            overall[key] += values[key]
    for bucket in [overall, *hosts.values(), *(s for by in skills.values() for s in by.values())]:
        _rates(bucket)
    for by in skills.values():
        for s in by.values():
            s["spent_estimated"] = s["spent_shared"] > 0
    versions: dict[str, dict[str, int]] = {h: {} for h in HOSTS}
    for row in runs:
        tag = row.get("prompt_version")
        tag = tag if isinstance(tag, str) and tag else "unstamped"
        versions[_host(row)][tag] = versions[_host(row)].get(tag, 0) + 1
    return {
        "estimate": ESTIMATE.format(n=per),
        "tokens_saved_per_load_estimate": per,
        "spent_basis": SPENT_BASIS,
        "precision_excludes": ["edited"],
        "prompt_version_now": C.prompt_version(),
        "overall": overall,
        "hosts": hosts,
        "note_judge_spent": {**notes, "all": sum(notes.values())},
        "delivery_skipped": skipped,
        "skills": skills,
        "prompt_versions": versions,
    }


def _rates(b: dict) -> None:
    b["net_estimate"] = b["saved_estimate"] - b["spent"]
    b["hit_rate"] = round(b["followed"] / b["suggested"], 3) if b["suggested"] else None
    b["false_route_rate"] = round(1 - b["followed"] / b["suggested"], 3) if b["suggested"] else None
    judged = b["accepted"] - b["edited"]  # a later edit may fix the change or improve something else: no verdict
    b["precision_proxy"] = round(b["kept"] / judged, 3) if judged else None


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{round(100 * x)}%"


def _line(label: str, b: dict) -> str:
    return (
        f"{label}: spent {b['spent']:,} background tokens; estimated saved {b['saved_estimate']:,}"
        f" ({b['loads']} sessions loading a standing nowitknows skill); estimated net {b['net_estimate']:+,};"
        f" judge precision proxy {b['kept']} of {b['accepted'] - b['edited']} accepted changes kept"
        f" ({_pct(b['precision_proxy'])}; {b['reverted']} reverted, {b['archived']} archived, {b['removed']} removed);"
        f" router {b['suggested'] - b['followed']} of {b['suggested']} suggestions not followed by a load"
        f" (false-route rate {_pct(b['false_route_rate'])})"
    )


def render(data: dict) -> list[str]:
    lines = ["", f"is nowitknows paying for itself (all time; {data['estimate']})"]
    lines += [_line(h, data["hosts"][h]) for h in HOSTS] + [_line("all", data["overall"])]
    edited = ", ".join(f"{h} {data['hosts'][h]['edited']}" for h in HOSTS)
    lines.append(f"accepted changes later edited, not counted either way: {edited}, all {data['overall']['edited']}")
    notes = data["note_judge_spent"]
    split = ", ".join(f"{h} {notes[h]:,}" for h in HOSTS)
    lines.append(
        f"project note judge: spent {notes['all']:,} background tokens ({split});"
        " counted in the host totals, charged to no skill"
    )
    held = [f"{h} {r} {n}" for h in HOSTS for r, n in sorted(data["delivery_skipped"][h].items())]
    lines.append(f"skill deliveries withheld (not loads or suggestions): {'; '.join(held) or 'none'}")
    rows = [(h, n, s) for h in HOSTS for n, s in sorted(data["skills"][h].items())]
    if rows:
        lines += [
            "",
            "per skill: suggested, loaded when suggested (hit rate), sessions loading it, spent, estimated saved, kept"
            f" ({data['spent_basis']})",
        ]
        lines += [
            f"{h} {n}: {s['suggested']} suggested, {s['followed']} loaded ({_pct(s['hit_rate'])}),"
            f" {s['loads']} sessions, {s['spent']:,} spent ({s['spent_shared']:,} of it an estimated share),"
            f" {s['saved_estimate']:,} estimated saved, {s['kept']} of {s['accepted']} kept"
            for h, n, s in rows
        ]
    tags = [f"{h} {t} {n} runs" for h in HOSTS for t, n in sorted(data["prompt_versions"][h].items())]
    lines += ["", f"prompt versions (now {data['prompt_version_now']}): {'; '.join(tags) or 'no runs'}"]
    return lines
