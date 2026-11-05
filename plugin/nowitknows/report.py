from __future__ import annotations

import json
import time
from pathlib import Path

from . import config as C
from .router import find_skills, skill_shadows, trigger_key, trigger_source
from .state import jsonl_rows

FAMILIES = ("opus", "sonnet", "haiku", "fable")
WEEK_S = C.WEEK_S


def read_jsonl(path: Path) -> list[dict]:
    """Rows with a numeric `ts`; torn or foreign lines are skipped (state.jsonl_rows)."""
    return [r for r in jsonl_rows(path) if isinstance(r.get("ts"), (int, float))]


def _rows(cfg: C.Config, filename: str, logs: dict[str, list[dict]] | None = None) -> list[dict]:
    if logs is not None and filename in logs:
        return logs[filename]
    return [{"host": cfg.legacy_host, **r} for r in read_jsonl(cfg.data / filename)]


def compressed_history_row(data: Path, *, unread_only: bool = False) -> str | None:
    """Which Codex history files the last index pass could not read (None before any pass, and with `unread_only`
    when it read them all)."""
    try:
        saved = json.loads((data / "codex-index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    unread = saved.get("compressed_unread") if isinstance(saved, dict) else None
    if not isinstance(unread, int):
        return None
    large = len(saved.get("too_large") or {})
    problems = [
        *(
            [
                f"{unread} compressed Codex history files not read: this Python has no zstd module and no zstd "
                "command is installed (Python 3.14 or later, or the zstd command, reads them on the next index pass)"
            ]
            if unread
            else []
        ),
        *(
            [f"{large} Codex history files over {C.HISTORY_FILE_MAX_BYTES:,} bytes, skipped until they change"]
            if large
            else []
        ),
    ]
    if problems or unread_only:
        return "; ".join(problems) or None
    return {"python": "read (Python's zstd module)", "command": "read (the zstd command)"}.get(
        saved.get("zstd_reader"), "read"
    )


def family(model: str) -> str:
    return next((f for f in FAMILIES if f in model), model)


def tokens(row: dict) -> dict[str, int]:
    """Tokens per model family of one child run (rows before per-model usage carry one total)."""
    models = row.get("models")
    if not isinstance(models, dict):
        total = sum(n for k in ("input", "cache_write", "cache_read", "output") if type(n := row.get(k, 0)) is int)
        models = {str(row.get("model", "unknown")): total}
    out: dict[str, int] = {}
    for m, n in models.items():
        if isinstance(n, int):
            out[family(m)] = out.get(family(m), 0) + n
    return out


def host_totals(cfg: C.Config, since: float) -> dict[str, dict]:
    usage, runs = _rows(cfg, "usage.jsonl"), _rows(cfg, "runs.jsonl")
    return {
        host: {
            "reviews": sum(r["ts"] >= since and r.get("host", "claude") == host for r in runs),
            "tokens": sum(
                sum(tokens(r).values()) for r in usage if r["ts"] >= since and r.get("host", "claude") == host
            ),
        }
        for host in ("claude", "codex", "cursor")
    }


def expired(row: dict) -> bool:
    """A retained refusal the judge did not decide."""
    return row.get("reason") in ("expired", "over_limit", "support write refused", "support write rejected")


def background_changes(ledger: list[dict]) -> list[dict]:
    """Ledger rows of changes a background run applied after the judge passed them."""
    return [e for e in ledger if e.get("source") == "background" and e.get("verdict")]


def _activity_kind(row: dict, codex: bool) -> str | None:
    kind, delivery = row.get("kind"), row.get("delivery")
    if kind in ("deliver", "load") and delivery == "hook_context":
        return "deliveries"  # Historic automatic deliveries were logged as loads.
    if kind == "delivery_reuse":
        return "pointers"
    if kind == "load" and (not codex or delivery == "tool_read"):
        return "loads"
    return None


class _SkillSources:
    def __init__(self, cfg: C.Config, rows: list[dict], *, skill_paths: list[Path] | None = None):
        self.cfg = cfg
        self.sources: dict[str, dict[str, dict]] = {}
        for row in rows:
            for name in row.get("loaded") or []:
                for source in (row.get("loaded_sources") or {}).get(name, []):
                    self.add(name, source)
            name = row.get("skill", row.get("name"))
            if isinstance(name, str):
                self.add(name, self.recorded(row, name))
        if skill_paths is not None:
            for path in skill_paths:
                self.add(path.parent.name, trigger_source(path, cfg))
            return
        if cfg.host == "cursor":
            home = C.home_dir(cfg.env)
            codex = Path(C.env("NOWITKNOWS_CODEX_HOME", cfg.env) or cfg.env.get("CODEX_HOME") or home / ".codex")
            named = {
                Path(C.env("NOWITKNOWS_CLAUDE_SKILLS_DIR", cfg.env) or home / ".claude/skills")
                .expanduser()
                .resolve(): "claude",
                Path(C.env("NOWITKNOWS_CODEX_SKILLS_DIR", cfg.env) or codex / "skills").expanduser().resolve(): "codex",
                cfg.skills_home.expanduser().resolve(): "cursor",
            }
            homes = [(named.get(path.expanduser().resolve(), cfg.host), path) for path in cfg.skill_link_homes]
        else:
            homes = list(
                zip((cfg.host, "claude" if cfg.host == "codex" else "codex"), cfg.skill_link_homes, strict=False)
            )
            homes.extend(("cursor", path) for path in cfg.skill_link_homes[len(homes) :])
        homes.append((cfg.host, cfg.skills_library))
        for host, home in homes:
            for path in find_skills(home):
                source = trigger_source(path, cfg)
                if source["host"] != "shared":
                    source["host"] = host
                self.add(path.parent.name, source)

    def add(self, name: str, source: dict | None) -> None:
        if (
            isinstance(source, dict)
            and set(source) == {"host", "path"}
            and source["host"] in ("claude", "codex", "cursor", "shared")
            and isinstance(source["path"], str)
            and Path(source["path"]).is_absolute()
        ):
            self.sources.setdefault(name, {})[trigger_key(source)] = source

    def recorded(self, row: dict, name: str) -> dict | None:
        if "skill_source" in row:
            return row["skill_source"]
        path = Path(row.get("path") or "")
        folder = next((p for p in path.parents if p.name == name), None)
        if not path.is_absolute() or folder is None:
            return None
        # Old ledger paths are evidence; resolving them now could follow a replacement link.
        source = trigger_source(folder / "SKILL.md", self.cfg, real=folder / "SKILL.md")
        if source["host"] != "shared":
            source["host"] = row["host"]
        return source

    def identity(self, row: dict, name: str, source: dict | None = None) -> str:
        source = source if source is not None else self.recorded(row, name)
        known = self.sources.get(name, {})
        if isinstance(source, dict) and trigger_key(source) in known:
            return trigger_key(source)
        # A bare name cannot select among sources, even when only one is visible in this host now.
        if len(known) == 1 and source is None:
            key, candidate = next(iter(known.items()))
            if candidate["host"] in ("shared", row["host"]):
                return key
        return f"legacy:{row['host']}:{name}"

    def label(self, outcome: dict, ambiguous: bool) -> str:
        if not ambiguous:
            return outcome["name"]
        source = outcome["skill_source"]
        suffix = f"{source['host']}: {source['path']}" if source else f"{outcome['host']}: legacy name-only"
        return f"{outcome['name']} [{suffix}]"


def skill_outcomes(
    cfg: C.Config, *, sources: _SkillSources | None = None, logs: dict[str, list[dict]] | None = None
) -> dict[str, dict]:
    """Per skill: deliveries, pointers, loads, reviewed-load corrections, rejections and undone archives.
    Codex reviewed loads require an observed explicit open in that session by the review's timestamp."""
    runs = _rows(cfg, "runs.jsonl", logs)
    ledger = _rows(cfg, "skills_ledger.jsonl", logs)
    rejected = _rows(cfg, "rejected.jsonl", logs)
    activity = _rows(cfg, "activity.jsonl", logs)
    sources = sources if sources is not None else _SkillSources(cfg, [*runs, *ledger, *rejected, *activity])
    out: dict[str, dict] = {}

    def at(key: str, name: str, host: str) -> dict:
        return out.setdefault(
            key,
            {
                "name": name,
                "host": host,
                "skill_source": sources.sources.get(name, {}).get(key),
                "deliveries": 0,
                "pointers": 0,
                "loads": 0,
                "corrections": 0,
                "rejections": 0,
                "undone_archives": 0,
                "recent": [],
            },
        )

    first_open: dict[tuple[str, str, str], float] = {}
    for row in activity:
        kind, name = _activity_kind(row, row.get("host", "claude") == "codex"), row.get("skill")
        if kind and isinstance(name, str):
            identity = sources.identity(row, name)
            at(identity, name, row["host"])[kind] += 1
            session = row.get("session")
            if row.get("host", "claude") == "codex" and kind == "loads" and isinstance(session, str) and session:
                key = (row["host"], session, identity)
                first_open[key] = min(first_open.get(key, row["ts"]), row["ts"])
    changed = {
        (e["host"], e["run_id"], sources.identity(e, e["name"]))
        for e in background_changes(ledger)
        if e.get("run_id") and isinstance(e.get("name"), str)
    }
    # A session's runs all list every skill it loaded so far, so a load counts once per session: when first
    # reviewed, and as a correction if any review of it changed the skill.
    reviewed: dict[tuple[str, str, str], dict] = {}
    for run in sorted(runs, key=lambda r: r["ts"]):
        key = str(run.get("session") or f"run:{run.get('run_id')}")  # rows before session ids: a session per run
        for name in run.get("loaded") or []:
            identities = {
                sources.identity(run, name, source)
                for source in (run.get("loaded_sources") or {}).get(name, []) or [None]
            }
            for identity in identities:
                # Native context availability alone cannot establish an explicit open of this source.
                if run["host"] == "codex" and (
                    not isinstance(run.get("evidence_ts"), (int, float))
                    or first_open.get((run["host"], key, identity), float("inf")) > run["evidence_ts"]
                ):
                    continue
                hit = (run["host"], run.get("run_id"), identity) in changed
                if run.get("status") == "failed" and not hit:
                    continue  # an interrupted review is no evidence that the skill was right
                entry = reviewed.setdefault((run["host"], key, identity), {"ts": run["ts"], "hit": False, "name": name})
                entry["hit"] = entry["hit"] or hit
    for (host, _, identity), entry in sorted(reviewed.items(), key=lambda kv: kv[1]["ts"]):
        outcome = at(identity, entry["name"], host)
        outcome["corrections"] += entry["hit"]
        outcome["recent"] = [*outcome["recent"], entry["hit"]][-C.ATTENTION_LOADS :]
    for row in rejected:
        if isinstance(row.get("name"), str) and not expired(row):
            at(sources.identity(row, row["name"]), row["name"], row["host"])["rejections"] += 1
    archived: dict[str, float] = {}
    for e in sorted(ledger, key=lambda e: e["ts"]):
        name = e.get("name")
        if not isinstance(name, str):
            continue
        identity = sources.identity(e, name)
        if e.get("action") == "archive" and e.get("source") == "curator":
            archived[identity] = e["ts"]
        elif e.get("action") == "create":  # made again since: a later restore brings back another archive
            archived.pop(identity, None)
        elif e.get("action") == "restore" and identity in archived:
            at(identity, name, e["host"])["undone_archives"] += e["ts"] - archived.pop(identity) <= 30 * 86400
    names = {o["name"] for o in out.values()}
    ambiguous = {
        name
        for name in names
        if len(set(sources.sources.get(name, {})) | {key for key, o in out.items() if o["name"] == name}) > 1
    }
    return {sources.label(o, o["name"] in ambiguous): o for o in out.values()}


def _flagged(cfg: C.Config, outcomes: dict[str, dict]) -> list[str]:
    """Skills in the user's folder now whose last reviewed loads were mostly followed by a change."""
    flagged = []
    for label, outcome in outcomes.items():
        name = outcome["name"]
        if sum(outcome["recent"]) < C.ATTENTION_CHANGES or not C.SKILL_NAME.fullmatch(name):
            continue
        path = cfg.skills_home / name / "SKILL.md"
        try:
            if path.is_file() and outcome["skill_source"] == trigger_source(path, cfg):
                flagged.append(label)
        except (OSError, RuntimeError):
            continue
    return sorted(flagged)


def needs_attention(cfg: C.Config) -> list[str]:
    """Skills changed after 3 of the last 5 reviewed sessions that loaded them: the rule keeps being wrong."""
    outcomes = skill_outcomes(cfg)
    return sorted(outcomes[label]["name"] for label in _flagged(cfg, outcomes))


def weekly(
    cfg: C.Config, weeks: int, now: float | None = None, logs: dict[str, list[dict]] | None = None
) -> list[dict]:
    now = time.time() if now is None else now
    usage = _rows(cfg, "usage.jsonl", logs)
    runs = _rows(cfg, "runs.jsonl", logs)
    ledger = _rows(cfg, "skills_ledger.jsonl", logs)
    rejected = _rows(cfg, "rejected.jsonl", logs)
    activity = _rows(cfg, "activity.jsonl", logs)
    # Tokens per applied change, by run: every review that started in the week (also those that applied nothing or
    # failed) over the changes those reviews applied, whenever they landed; only runs whose usage carries run_id.
    run_tokens: dict[str, int] = {}
    for row in usage:
        if row.get("run_id") and isinstance(row["run_id"], str):
            run_tokens[row["run_id"]] = run_tokens.get(row["run_id"], 0) + sum(tokens(row).values())
    run_changes: dict[str, int] = {}
    for e in background_changes(ledger):
        if e.get("run_id"):
            run_changes[e["run_id"]] = run_changes.get(e["run_id"], 0) + 1
    searched: dict[tuple[str, str, int], tuple[float, int]] = {}
    hint_activity: list[tuple[int, dict]] = []
    for i, a in enumerate(activity):
        if a.get("kind") == "recall_hint":
            hint_activity.append((i, a))
        session, turn = a.get("session"), a.get("turn")
        if a.get("kind") == "search" and a["ts"] < now and isinstance(session, str) and session and type(turn) is int:
            key = (a.get("host", "claude"), session, turn)
            searched[key] = max(searched.get(key, (float("-inf"), -1)), (a["ts"], i))
    out = []
    for w in range(weeks):
        hi, lo = now - w * WEEK_S, now - (w + 1) * WEEK_S

        def inside(rows: list[dict], lo: float = lo, hi: float = hi) -> list[dict]:
            return [r for r in rows if lo <= r["ts"] < hi]

        by_family: dict[str, int] = {}
        for row in inside(usage):
            for f, n in tokens(row).items():
                by_family[f] = by_family.get(f, 0) + n
        applied = background_changes(inside(ledger))
        week_runs = {r["run_id"] for r in inside(runs) if r.get("run_id") in run_tokens}
        spent, landed = sum(run_tokens[r] for r in week_runs), sum(run_changes.get(r, 0) for r in week_runs)
        acts = inside(activity)
        suggested = [a for a in acts if a.get("kind") == "suggest" and a.get("skills")]
        loads = [a for a in acts if _activity_kind(a, a.get("host", "claude") == "codex") == "loads"]
        loaded = {(a.get("host", "claude"), a.get("session"), a.get("turn"), a.get("skill")) for a in loads}
        followed = sum(
            any((s.get("host", "claude"), s.get("session"), s.get("turn"), n) in loaded for n in s["skills"])
            for s in suggested
        )
        hints = [(i, a) for i, a in hint_activity if lo <= a["ts"] < hi]
        # A search can follow a hint across the week boundary, so join against all activity before now.
        # Activity timestamps are rounded to milliseconds; append order breaks only equal-time ties.
        recall_followed = 0
        for i, a in hints:
            session, turn = a.get("session"), a.get("turn")
            if isinstance(session, str) and session and type(turn) is int:
                recall_followed += searched.get((a.get("host", "claude"), session, turn), (a["ts"], i)) > (a["ts"], i)
        week_reviews, week_rejected = inside(runs), inside(rejected)
        out.append(
            {
                "start": lo,
                "hosts": {
                    host: {
                        "runs": sum(r.get("host", "claude") == host for r in week_reviews),
                        "tokens": sum(
                            sum(tokens(r).values()) for r in inside(usage) if r.get("host", "claude") == host
                        ),
                    }
                    for host in ("claude", "codex", "cursor")
                },
                "runs": len(week_reviews),
                "failed": sum(r.get("status") == "failed" for r in week_reviews),
                "tokens": by_family,
                "applied": len(applied),
                "rejected": sum(not expired(r) for r in week_rejected),
                "expired": sum(expired(r) for r in week_rejected),
                "tokens_per_applied": round(spent / landed) if landed else None,
                "suggestions": len(suggested),
                "followed": followed,
                "deliveries": sum(_activity_kind(a, a.get("host", "claude") == "codex") == "deliveries" for a in acts),
                "pointers": sum(_activity_kind(a, a.get("host", "claude") == "codex") == "pointers" for a in acts),
                "loads": len(loads),
                "searches": sum(1 for a in acts if a.get("kind") == "search"),
                "recall_hints": len(hints),
                "recall_followed": recall_followed,
            }
        )
    return out


def refusals(cfg: C.Config, since: float, logs: dict[str, list[dict]] | None = None) -> str:
    rows = [r for r in _rows(cfg, C.REFUSALS_LOG, logs) if r.get("ts", 0) >= since]
    if not rows:
        return "none"
    last, n = rows[-1], len(rows)
    day = time.strftime("%Y-%m-%d", time.localtime(last["ts"]))
    why = last.get("limit") or last.get("reason")
    return f"{n}; latest {day} {last.get('name')} in {last.get('folder')} ({why}, {last.get('host')})"


def note_judge(
    cfg: C.Config, since: float = 0, run_id: str | None = None, logs: dict[str, list[dict]] | None = None
) -> dict:
    """What the project note judge did since `since` (or in one run): verdicts it gave and the tokens it spent."""
    rows = [
        r
        for r in _rows(cfg, C.NOTE_JUDGMENTS_LOG, logs)
        if r.get("ts", 0) >= since and run_id in (None, r.get("run_id"))
    ]
    spent = [
        r
        for r in _rows(cfg, "usage.jsonl", logs)
        if str(r.get("label", "")).startswith("judge-note:")
        and r.get("ts", 0) >= since
        and run_id in (None, r.get("run_id"))
    ]
    passed = sum(r.get("verdict") == "pass" for r in rows)
    return {
        "judged": len(rows),
        "accepted": passed,
        "rejected": len(rows) - passed,
        "tokens": sum(sum(tokens(r).values()) for r in spent),
    }


def unshared_row(cfg: C.Config, key: str) -> str:
    from .migrate import unshared_notes

    here, total = unshared_notes(cfg, key), unshared_notes(cfg)
    if not total:
        return "none"
    setup = (
        "$nowitknows-codex:nowitknows-setup"
        if cfg.host == "codex"
        else "/nowitknows-setup"
        if cfg.host == "cursor"
        else "/nowitknows:setup"
    )
    return f"{here} for this repo, {total} in all; {setup} previews moving them into the shared folder"


def render(cfg: C.Config, weeks: int = C.REPORT_WEEKS, now: float | None = None) -> str:
    codex = cfg.host == "codex" or any(r.get("host") == "codex" for r in _rows(cfg, "activity.jsonl"))
    lines = ["NowItKnows report (plan usage in tokens processed; no dollar figures)", ""]
    for shadow in skill_shadows(cfg, Path(C.env("NOWITKNOWS_PROJECT", cfg.env) or Path.cwd())):
        lines.append(
            f"shadowed skill copy {shadow['name']}: {shadow['path']}; "
            f"using {shadow['canonical']}; foreign copy left untouched"
        )
    if codex:
        lines += [
            "Automatic deliveries and context pointers show context availability;"
            " observed explicit opens show file reads."
            " These do not prove instructions were followed.",
            "",
        ]
    for w in weekly(cfg, weeks, now):
        day = time.strftime("%Y-%m-%d", time.localtime(w["start"]))
        fams = ", ".join(f"{f} {n:,}" for f, n in sorted(w["tokens"].items(), key=lambda kv: -kv[1])) or "none"
        per = f"{w['tokens_per_applied']:,}" if w["tokens_per_applied"] is not None else "n/a"
        follow = f"{w['followed']} of {w['suggestions']}" if w["suggestions"] else "none"
        failed = f" ({w['failed']} failed)" if w["failed"] else ""
        expired_ = f", {w['expired']} expired unjudged" if w["expired"] else ""
        routing = (
            f" automatic deliveries {w['deliveries']}; context pointers {w['pointers']};"
            f" observed explicit opens {w['loads']};"
            f" router suggestions with observed explicit opens the same turn {follow};"
            if codex
            else f" router suggestions loaded the same turn {follow};"
        )
        lines += [
            f"week from {day}: {w['runs']} reviews{failed}; tokens {fams}; {w['applied']} applied,"
            f" {w['rejected']} rejected by the judge{expired_}; tokens per applied change {per};"
            f"{routing} session searches {w['searches']};"
            f" recall hints followed by a search the same turn {w['recall_followed']} of {w['recall_hints']}"
        ]
    for w in weekly(cfg, weeks, now):
        for host, values in w["hosts"].items():
            if values["runs"] or values["tokens"]:
                day = time.strftime("%Y-%m-%d", time.localtime(w["start"]))
                lines.append(f"{host}, {day}: {values['runs']} reviews; {values['tokens']:,} tokens")
    refused = refusals(cfg, (time.time() if now is None else now) - weeks * WEEK_S)
    if refused != "none":
        lines += ["", f"project notes background reviews could not save: {refused}"]
    judged = note_judge(cfg, (time.time() if now is None else now) - weeks * WEEK_S)
    if judged["judged"] or judged["tokens"]:
        lines += [
            "",
            "project note judge: {judged} judged, {accepted} accepted, {rejected} rejected, {tokens:,} tokens".format(
                **judged
            ),
        ]
    outcomes = skill_outcomes(cfg)
    if outcomes:
        flagged = set(_flagged(cfg, outcomes))
        measures = (
            "automatic deliveries, context pointers, observed explicit opens, corrections"
            if codex
            else "loads, corrections"
        )
        lines += [
            "",
            f"skill outcomes (all time): {measures}, judge rejections, curator archives undone within 30 days",
        ]
        for name, o in sorted(outcomes.items()):
            flag = " (needs attention)" if name in flagged else ""
            counts = (
                f"{o['deliveries']} automatic deliveries, {o['pointers']} context pointers,"
                f" {o['loads']} observed explicit opens"
                if codex
                else f"{o['loads']} loads"
            )
            lines.append(
                f"{name}: {counts}, {o['corrections']} corrections, {o['rejections']} rejections,"
                f" {o['undone_archives']} undone archives{flag}"
            )
    from . import session_report, telemetry

    return "\n".join(lines + telemetry.render(telemetry.summary(cfg)) + session_report.render(cfg, weeks, now))
