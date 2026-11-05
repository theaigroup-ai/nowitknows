"""Observed session comparisons from local counts, without causal claims."""

from __future__ import annotations

import json
import math
import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import config as C
from .report import tokens

HOSTS = ("claude", "codex", "cursor")
MEASURES = (
    ("total_tokens", "total tokens/session including subagents"),
    ("subagent_tokens", "subagent tokens/session"),
    ("fresh_input", "fresh input/session"),
    ("cache_read", "cache read/session"),
    ("cache_write", "cache write/session"),
    ("output", "output tokens/session"),
    ("active_seconds", "active seconds/session"),
    ("failed_tool_calls", "failed tool calls/session"),
    ("corrections", "corrections/session"),
)


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _total(row: dict) -> float | None:
    if _number(row.get("total_tokens")):
        return row["total_tokens"]
    usage = row.get("tokens")
    if not isinstance(usage, dict):
        return None
    values = [usage.get(k) for k in ("fresh_input", "cache_read", "cache_write", "output")]
    return sum(values) if all(_number(n) for n in values) else None


def _value(row: dict, field: str) -> float | None:
    if field == "total_tokens":
        value = _total(row)
    elif field in ("fresh_input", "cache_read", "cache_write", "output"):
        usage = row.get("tokens")
        value = usage.get(field) if isinstance(usage, dict) else None
    else:
        value = row.get(field)
    if not _number(value):
        return None
    return value


def _stats(rows: list[dict], field: str) -> tuple[float, float, float, int] | None:
    values = sorted(n for row in rows if (n := _value(row, field)) is not None)
    if len(values) < C.METRICS_MIN_GROUP:
        return None
    if len(values) == 1:
        return values[0], values[0], values[0], 1
    lo, _, hi = statistics.quantiles(values, n=4, method="inclusive")
    return statistics.median(values), lo, hi, len(values)


def _display(stats: tuple[float, float, float, int]) -> str:
    median, lo, hi, count = stats
    return f"{median:,.2f} [{lo:,.2f}, {hi:,.2f}] (n={count})"


def _repo(row: dict) -> tuple[str, str] | None:
    identity = row.get("repository")
    if (
        isinstance(identity, dict)
        and identity.get("kind") in ("roots", "origin", "path")
        and isinstance(identity.get("value"), str)
        and identity["value"]
    ):
        return identity["kind"], identity["value"]
    return None


def _usage(cfg: C.Config, lo: float, hi: float) -> tuple[list[dict], bool]:
    try:
        with (cfg.data / "usage.jsonl").open("rb") as stream:
            data = stream.read(C.METRICS_MAX_BYTES + 1)
    except FileNotFoundError:
        return [], False
    except OSError:
        return [], False
    if len(data) > C.METRICS_MAX_BYTES:
        return [], False
    rows = []
    clean = True
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            clean = False
            continue
        if not isinstance(row, dict) or not _number(row.get("ts")):
            clean = False
            continue
        if lo <= row["ts"] < hi:
            models = row.get("models")
            counts = (
                list(models.values())
                if isinstance(models, dict)
                else [row[k] for k in ("input", "cache_read", "cache_write", "output") if k in row]
            )
            if not counts or not all(type(n) is int and n >= 0 for n in counts):
                clean = False
                continue
            if row.get("subtype") in ("error_timeout", "error_no_result"):
                clean = False
            rows.append({"host": cfg.legacy_host, **row})
    from .session_metrics import usage_coverage

    return rows, clean and usage_coverage(cfg, lo, hi)


def _background(rows: list[dict], host: str, now: float, weeks: int) -> tuple[int, list[str]]:
    selected = [row for row in rows if row.get("host") == host]
    spent = sum(sum(tokens(row).values()) for row in selected)
    roles, skills = defaultdict(int), defaultdict(int)
    for row in selected:
        amount = sum(tokens(row).values())
        label = row.get("label") if isinstance(row.get("label"), str) else "unknown"
        role, _, name = label.partition(":")
        roles[role] += amount
        if role in ("judge", "revise") and name:
            skills[name] += amount
    lines = [f"{host} Nik background: {spent:,} tokens in this window"]
    for index in range(weeks):
        hi, lo = now - index * C.WEEK_S, now - (index + 1) * C.WEEK_S
        amount = sum(sum(tokens(row).values()) for row in selected if lo <= row["ts"] < hi)
        date = time.strftime("%Y-%m-%d", time.gmtime(lo))
        lines.append(f"  background week starting {date} UTC: {amount:,} background tokens")
    lines.append("  by role: " + (", ".join(f"{name} {n:,}" for name, n in sorted(roles.items())) or "none"))
    lines.append(
        "  by named skill (judge/reviser only; shared review cost remains unassigned): "
        + (", ".join(f"{name} {n:,}" for name, n in sorted(skills.items())) or "none known")
    )
    return spent, lines


def _comparison(values: dict) -> tuple[bool, str]:
    medians = []
    different = False
    for field, label in (("turns", "turns"), ("active_seconds", "active seconds")):
        sides = [[r[field] for r in values[v] if _number(r.get(field))] for v in (True, False)]
        a, b = [statistics.median(side) if side else None for side in sides]
        medians.append(
            f"median {label} with Nik {a if a is not None else 'unknown'}, without {b if b is not None else 'unknown'}"
        )
        if a is not None and b is not None and max(a, b) > C.METRICS_COMPARABILITY_FACTOR * min(a, b):
            different = True
    return not different, "; ".join(medians)


def _with_subagents(rows: list[dict]) -> list[dict]:
    children = defaultdict(list)
    for row in rows:
        if row.get("origin") == "subagent" and row.get("parent_session"):
            children[row["host"], row["parent_session"]].append(row)

    def descend(row, seen):
        key = row["host"], row["session"]
        if key in seen:
            return []
        seen.add(key)
        linked = []
        for child in children[key]:
            if (child["host"], child["session"]) not in seen:
                linked.extend([child, *descend(child, seen)])
        return linked

    result = []
    for row in rows:
        if row.get("origin") == "subagent":
            continue
        linked = descend(row, set())
        parts = [row, *linked]
        combined = {**row, "tokens": dict(row.get("tokens", {}))}
        exposure = [r.get("learned_skill_used") for r in parts]
        combined["learned_skill_used"] = (
            True
            if any(value is True for value in exposure)
            else False
            if all(value is False for value in exposure)
            else None
        )
        combined["other_skills"] = {
            skill: sum(r.get("other_skills", {}).get(skill, 0) for r in parts)
            for skill in {skill for r in parts for skill in r.get("other_skills", {})}
        }
        totals = [_total(r) for r in parts]
        combined["total_tokens"] = sum(totals) if all(n is not None for n in totals) else None
        combined["subagent_tokens"] = sum(totals[1:]) if all(n is not None for n in totals[1:]) else None
        for field in ("fresh_input", "cache_read", "cache_write", "output"):
            counts = [r.get("tokens", {}).get(field) for r in parts]
            combined["tokens"][field] = sum(counts) if all(_number(n) for n in counts) else None
        combined["complete"] = all(r.get("complete") is True and r.get("verified") is not False for r in parts)
        for field, operation in (("first_ts", min), ("last_ts", max)):
            dates = [r.get(field) for r in parts]
            combined[field] = operation(dates) if all(_number(n) for n in dates) else None
        result.append(combined)
    return result


def _directions(groups: dict, field: str) -> str:
    counts = [0, 0, 0]
    sizes = [0, 0]
    for values in groups.values():
        if not _comparison(values)[0]:
            continue
        a, b = _stats(values[True], field), _stats(values[False], field)
        if a and b:
            counts[0 if a[0] < b[0] else 1 if a[0] > b[0] else 2] += 1
            sizes[0] += a[3]
            sizes[1] += b[3]
    if not sum(counts):
        if any(not _comparison(v)[0] for v in groups.values()):
            return "no comparable groups; no direction named"
        known = [sum(_value(r, field) is not None for g in groups.values() for r in g[v]) for v in (True, False)]
        return f"not enough data yet (known with Nik {known[0]}, without {known[1]}; null is unknown)"
    return (
        f"{counts[0]} groups favor with Nik, {counts[1]} favor without, {counts[2]} tied "
        f"(with Nik n={sizes[0]}, without n={sizes[1]})"
    )


def _bottom(host: str, groups: dict, spent: int, cost_known: bool) -> list[str]:
    parts = [
        f"{label}: {_directions(groups, field)}"
        for field, label in (
            ("total_tokens", "tokens/session"),
            ("active_seconds", "seconds/session"),
            ("failed_tool_calls", "failures/session"),
        )
    ]
    background = f"{spent:,}" if cost_known else f"at least {spent:,} (log incomplete)"
    return [
        f"{host} bottom line (observed, not an experiment): "
        + "; ".join(parts)
        + f"; Nik background cost {background} tokens.",
        "  Descriptive session totals do not establish absolute savings; net saving is unknown.",
    ]


def _components(rows: list[dict], *, background: bool = False) -> tuple[int, int, int]:
    fresh_total, cached_total, known = 0, 0, 0
    for row in rows:
        if not background and (row.get("complete") is not True or row.get("verified") is False):
            continue
        values = row if background else row.get("tokens", {})
        fresh, cached, write, output = (
            values.get(k)
            for k in (
                ("input", "cache_read", "cache_write", "output")
                if background
                else ("fresh_input", "cache_read", "cache_write", "output")
            )
        )
        if not all(_number(n) for n in (fresh, cached, output)):
            continue
        if write is None and not background and row.get("host") == "codex":
            write = 0
        if not _number(write):
            continue
        fresh_total += fresh + write + output
        cached_total += cached
        known += 1
    return fresh_total, cached_total, known


def _ratio(spent: int, total: int, foreground_known: bool, background_known: bool) -> str:
    if not total:
        return "n/a (no recorded foreground)"
    share = f"{100 * spent / total:.2f}%"
    if not foreground_known and not background_known:
        return f"{share} of what's recorded; true share unknown"
    if not foreground_known:
        return f"at most {share}"
    if not background_known:
        return f"at least {share}"
    return share


def _share(
    host: str,
    foreground: list[dict],
    usage: list[dict],
    lo: float,
    hi: float,
    cost_known: bool,
    *,
    foreground_complete: bool = True,
) -> str:
    background = [r for r in usage if r["host"] == host]
    fresh, cached, known = _components(foreground)
    bg_fresh, bg_cached, bg_known = _components(background, background=True)
    complete = cost_known and bg_known == len(background)
    foreground_known = foreground_complete and known == len(foreground)
    share = _ratio(bg_fresh, fresh, foreground_known, complete)
    read_share = _ratio(bg_cached, cached, foreground_known, complete)
    qualifier = "verified cost coverage" if complete else "background cost coverage incomplete"
    return (
        f"  Share of use, same window {lo:.0f}..{hi:.0f}: fresh input + cache write + output: "
        f"Nik background {bg_fresh:,} / user foreground {fresh:,} = {share}; "
        f"cached reads separately: {bg_cached:,} / {cached:,} = {read_share}. "
        f"{qualifier}; known components {known}/{len(foreground)} foreground, {bg_known}/{len(background)} background. "
        "Foreground includes main sessions and their subagents; excludes Nik children, host memory and lab/temp work."
    )


def _week_start(now: float) -> float:
    date = datetime.fromtimestamp(now, timezone.utc)
    return (date - timedelta(days=date.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _foreground(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not r.get("excluded_work") and r.get("origin") not in ("nik_background", "host_memory")]


def _token_table(
    rows: list[dict], usage: list[dict], now: float, cost_known: bool, *, foreground_complete: bool = True
) -> list[str]:
    lines = [
        "Where your tokens go (across hosts; Monday weeks, UTC)",
        "Fresh input + cache write + output; cached reads on separate lines. "
        "Sessions are assigned to their last timestamp; the current week is partial.",
        "Week starting | Tokens | Claude main | Claude subagents | Codex | Cursor | "
        "Foreground | Nik background | Nik share",
    ]
    end = _week_start(now)
    for index in reversed(range(C.METRICS_TOKEN_WEEKS)):
        lo, hi = end - index * C.WEEK_S, min(now, end - (index - 1) * C.WEEK_S)
        foreground = _foreground([r for r in rows if _number(r.get("last_ts")) and lo <= r["last_ts"] < hi])
        buckets = [
            [r for r in foreground if r.get("host") == "claude" and r.get("origin") != "subagent"],
            [r for r in foreground if r.get("host") == "claude" and r.get("origin") == "subagent"],
            [r for r in foreground if r.get("host") == "codex"],
            [r for r in foreground if r.get("host") == "cursor"],
        ]
        values = [_components(bucket) for bucket in buckets]
        bg = [r for r in usage if lo <= r["ts"] < hi]
        background = _components(bg, background=True)
        complete = cost_known and background[2] == len(bg)
        foreground_known = foreground_complete and all(v[2] == len(b) for v, b in zip(values, buckets, strict=True))
        for component, label in enumerate(("fresh + write + output", "cached reads")):
            amounts = [v[component] for v in values]
            total, spent = sum(amounts), background[component]
            share = _ratio(spent, total, foreground_known, complete)
            shown = [
                "unknown" if bucket and not v[2] else f"{v[component]:,}"
                for bucket, v in zip(buckets, values, strict=True)
            ]
            lines.append(
                f"{datetime.fromtimestamp(lo, timezone.utc):%Y-%m-%d} | {label} | "
                + " | ".join(shown)
                + f" | {total:,} | {spent:,} | {share}"
            )
    lines.append(
        "Shares are background / recorded foreground. Missing foreground alone gives an upper bound; "
        "missing background alone gives a lower bound; both missing leave the true share unknown. "
        "Includes main sessions and subagents; "
        "excludes Nik children, host memory and lab/temp work. Codex fresh input includes unseparated cache writes."
    )
    return lines


def _session_rows(cfg: C.Config) -> list[dict]:
    from .session_metrics import read_rows

    return [r for r in read_rows(cfg) if r.get("schema_version") == C.METRICS_SCHEMA]


def render(cfg: C.Config, weeks: int = C.REPORT_WEEKS, now: float | None = None) -> list[str]:
    from .codex_host import configuration, import_records
    from .identity import history
    from .session_metrics import _cache, repository

    now = time.time() if now is None else now
    weeks = max(1, weeks)
    lo = now - weeks * C.WEEK_S
    rows = _session_rows(cfg)
    progress = _cache(cfg)["hosts"]
    known_repositories = history(cfg)
    rows = [{**r, "repository": repository(cfg, r, known_repositories)} for r in rows]
    table_lo = _week_start(now) - (C.METRICS_TOKEN_WEEKS - 1) * C.WEEK_S
    all_usage, cost_known = _usage(cfg, min(lo, table_lo), now)
    usage = [r for r in all_usage if lo <= r["ts"] < now]
    history_status = []
    if not cfg.metrics:
        history_status.append("Session collection is off (metrics=false). Retained counts are still shown.")
    for host in HOSTS:
        status = progress.get(host)
        if status and not status.get("done"):
            history_status.append(
                f"{host} history is still being collected: {status.get('files', 0) - status.get('pending', 0)} / "
                f"{status.get('files', 0)} files current, {status.get('pending', 0)} pending; results are provisional."
            )
        elif not status and any(r.get("host") == host for r in rows + all_usage):
            history_status.append(f"{host} history collection progress is unknown; retained rows may be incomplete.")
        if status and status.get("unsupported"):
            history_status.append(
                f"{host} history has {status['unsupported']} unsupported files; foreground coverage is unknown."
            )
        if status and status.get("skipped_compressed"):
            history_status.append(
                f"{host} history skipped {status['skipped_compressed']} compressed transcripts: "
                "no zstd reader is available in this Python or on PATH."
            )
        if status and status.get("limited"):
            history_status.append(
                f"{host} collection reached METRICS_MAX_FILES={C.METRICS_MAX_FILES}; retained counts were not evicted."
            )
    missing_hosts = {r["host"] for r in rows + all_usage if r["host"] not in progress}
    missing_hosts.update(
        host for host, status in progress.items() if not status.get("done") or status.get("unsupported")
    )
    missing_hosts.update(r["host"] for r in rows if not _number(r.get("first_ts")) or not _number(r.get("last_ts")))
    lines = [
        "",
        "Measured sessions: is Nik paying off?",
        *history_status,
        *_token_table(rows, all_usage, now, cost_known, foreground_complete=not missing_hosts),
        f"Local counts: {cfg.data / C.METRICS_FILE}; file cache: {cfg.data / C.METRICS_STATE_FILE}.",
        "Turn collection off with NOWITKNOWS_METRICS=0 (Claude plugin option metrics=false; "
        "Codex/Cursor configure metrics=off). After the collector exits, remove session_metrics* files "
        "from this data folder to clear counts and collection state. While collection is on, history is rebuilt.",
        f"Window: last {weeks} weeks; only sessions wholly inside it. Median [middle-half range]; with Nik vs without.",
        "With Nik means learned skill content reached the session: Claude load or Codex hook_context delivery/load.",
        "Unknown exposure is excluded. Repository and origin groups are compared separately, "
        "per session including subagents.",
        "Background totals use retained usage.jsonl records only; absent, deleted or rotated records "
        "leave full cost coverage unknown without verified continuity.",
        "Sessions that use skills differ from ones that don't, so this is evidence, not proof.",
    ]
    selected = [
        r
        for r in rows
        if _number(r.get("first_ts")) and _number(r.get("last_ts")) and lo <= r["first_ts"] <= r["last_ts"] < now
    ]
    undated = sum(not _number(r.get("first_ts")) or not _number(r.get("last_ts")) for r in rows)
    if undated:
        lines.append(f"Sessions without usable timestamps: {undated}; excluded from this window.")
    comparison_rows = [
        r
        for r in _with_subagents(rows)
        if _number(r.get("first_ts")) and _number(r.get("last_ts")) and lo <= r["first_ts"] <= r["last_ts"] < now
    ]
    copied = set()
    if any(r.get("host") == "codex" and r.get("turns") == 0 for r in selected):
        imported = {key for key, _ in import_records(configuration(cfg.env, create=False))}
        copied = {
            r["session"]
            for r in selected
            if r.get("host") == "codex" and r.get("turns") == 0 and f"codex:{r['session']}" in imported
        }
    for host in HOSTS:
        host_rows = [r for r in selected if r.get("host") == host]
        groups = defaultdict(lambda: {True: [], False: []})
        excluded = 0
        for row in (r for r in comparison_rows if r.get("host") == host):
            if host == "codex" and row["session"] in copied:
                continue
            origin = row.get("origin")
            exposure = row.get("learned_skill_used")
            if (
                row.get("complete") is not True
                or row.get("verified") is False
                or row.get("excluded_work")
                or not _number(row.get("turns"))
                or row["turns"] == 0
                or origin not in ("attended", "unattended", "exec")
                or type(exposure) is not bool
                or (not exposure and bool(row.get("other_skills")))
                or _repo(row) is None
            ):
                excluded += 1
                continue
            groups[(_repo(row), origin)][exposure].append(row)
        enough = {
            key: value
            for key, value in groups.items()
            if min(len(value[True]), len(value[False])) >= C.METRICS_MIN_GROUP
        }
        count = sum(
            r.get("origin") != "subagent" and not (host == "codex" and r["session"] in copied) for r in host_rows
        )
        lines.append(f"{host}: {count} sessions in window; {excluded} excluded from comparisons.")
        if host == "codex":
            lines.append(f"  copied chats: {len(copied)}")
        for origin in ("attended", "unattended", "exec"):
            matched = {key: values for key, values in enough.items() if key[1] == origin}
            counts = [
                sum(len(g[value]) for (_, kind), g in groups.items() if kind == origin) for value in (True, False)
            ]
            lines.append(
                f"  {origin}: with Nik {counts[0]}, without {counts[1]}; {len(matched)} matched repository groups."
            )
            lines.append("    Measure | Within-group directions and sample sizes")
            for field, label in MEASURES:
                if field in ("total_tokens", "active_seconds", "failed_tool_calls", "corrections"):
                    lines.append(f"    {label} | {_directions(matched, field)}")
        lines.append(
            f"  {len(groups) - len(enough)} repository/origin groups had too little data "
            f"(need {C.METRICS_MIN_GROUP} in each group)."
        )
        lines.append(
            f"  Router-note-only sessions: {sum(bool(r.get('note_only')) for r in host_rows)}; "
            f"lab/benchmark/temp sessions excluded: {sum(bool(r.get('excluded_work')) for r in host_rows)}."
        )
        other = [r for r in host_rows if r.get("other_skills")]
        lines.append(
            f"  Got a skill Nik didn't learn: {len(other)} sessions; "
            f"{sum(r.get('learned_skill_used') is not True for r in other)} without learned content, "
            "excluded from plain without."
        )
        unknown_origin = sum(r.get("origin") is None for r in host_rows)
        if unknown_origin:
            lines.append(f"  Unknown origin: {unknown_origin}; recorded counts lack attendance evidence.")
        children = [r for r in host_rows if r.get("origin") == "subagent"]
        background_sessions = sum(r.get("origin") == "nik_background" for r in host_rows)
        memory_sessions = sum(r.get("origin") == "host_memory" for r in host_rows)
        child_total = sum(_total(r) or 0 for r in children)
        parents = {r["session"]: r for r in host_rows if r.get("origin") != "subagent"}
        attached = [r for r in children if r.get("parent_session") in parents]
        parent_ids = {r["parent_session"] for r in attached}
        combined = sum(_total(parents[sid]) or 0 for sid in parent_ids) + sum(_total(r) or 0 for r in attached)
        lines.append(
            f"  Separate session costs: {background_sessions} Nik children; {memory_sessions} host memory jobs; "
            f"{len(children)} subagents, {child_total:,.0f} recorded tokens. "
            f"{len(parent_ids)} linked parents with their subagents: {combined:,.0f} recorded tokens "
            "; linked subagent cost is included in each parent comparison total."
        )
        if host == "cursor":
            lines.append("  Cursor transcripts record no token usage: null means unknown, never zero.")
        if host == "codex":
            lines.append("  Codex fresh input includes unseparated cache writes when cache-write usage is null.")
        for (repo, origin), values in sorted(enough.items()):
            with_nik, without = values[True], values[False]
            lines.append(
                f"  repository {repo[0]}:{repo[1]}, {origin}: with Nik {len(with_nik)}, without {len(without)}"
            )
            comparable, medians = _comparison(values)
            if not comparable:
                lines.append("    not comparable: these sessions are different kinds of work; " + medians)
            else:
                lines.append("    " + medians)
            for field, label in MEASURES:
                a, b = _stats(with_nik, field), _stats(without, field)
                if a and b:
                    lines.append(f"    {label}: {_display(a)} vs {_display(b)}")
                else:
                    lines.append(f"    {label}: not enough data yet (null is unknown).")
            totals = [sum(_total(r) for r in g if _total(r) is not None) for g in (with_nik, without)]
            counts = [sum(_total(r) is not None for r in g) for g in (with_nik, without)]
            turns = [sum(r["turns"] for r in g if _total(r) is not None) for g in (with_nik, without)]
            lines.append(
                f"    Actual matched totals: {totals[0]:,.0f} tokens / {turns[0]} turns / "
                f"{counts[0]} sessions with Nik; "
                f"{totals[1]:,.0f} tokens / {turns[1]} turns / {counts[1]} sessions without. "
                "Unequal work is not a saving estimate."
            )
        spent, background = _background(usage, host, now, weeks)
        lines.extend(background)
        foreground = _foreground(host_rows)
        overlapping = sum(
            r.get("host") == host
            and _number(r.get("first_ts"))
            and _number(r.get("last_ts"))
            and r["first_ts"] < now
            and r["last_ts"] >= lo
            and not (lo <= r["first_ts"] <= r["last_ts"] < now)
            for r in _foreground(rows)
        )
        if overlapping:
            lines.append(f"  {overlapping} overlapping sessions omitted from foreground; true share unknown.")
        lines.append(
            _share(
                host,
                foreground,
                usage,
                lo,
                now,
                cost_known,
                foreground_complete=host not in missing_hosts and not overlapping,
            )
        )
        lines.extend(_bottom(host, enough, spent, cost_known))
    if not cost_known:
        lines.append(
            "Background usage log continuity is unverified, absent, malformed, unreadable, contains calls "
            "with missing usage, or is over the configured read limit; cost is a lower bound. "
            "Background cost is incomplete, so net saving is unknown."
        )
    return lines
