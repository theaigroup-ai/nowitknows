"""Local counts from whole transcript files; parser identities live only during a lineage scan."""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import io
import json
import math
import os
import stat as statlib
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from . import config as C
from . import identity, native, sessions
from .state import State, atomic_write, file_lock, lock_held

TOKEN_FIELDS = ("fresh_input", "cache_read", "cache_write", "output")


def _hash(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()


def _object(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _stamp(value: object) -> float | None:
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            value = parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
        if type(value) in (int, float) and math.isfinite(value):
            return value / 1000 if value > 10**11 else value
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _count(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def _valid_usage(usage: object) -> bool:
    return isinstance(usage, dict) and all(
        type(usage.get(key)) is int and usage[key] >= 0 for key in ("input_tokens", "output_tokens")
    )


def log_rows(path: Path, *, diagnostics: dict | None = None) -> list[dict]:
    """Read a bounded log tail; torn and malformed records are ignored."""
    counts = diagnostics if diagnostics is not None else {}
    try:
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if size > C.METRICS_MAX_BYTES:
                counts["truncated"] = True
                stream.seek(size - C.METRICS_MAX_BYTES)
                stream.readline(C.METRICS_LINE_BYTES)
            data = stream.read(C.METRICS_MAX_BYTES)
    except FileNotFoundError:
        counts["missing"] = True
        return []
    rows = []
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            counts["malformed"] = True
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            counts["malformed"] = True
    return rows


def read_rows(cfg: C.Config) -> list[dict]:
    try:
        with (cfg.data / C.METRICS_FILE).open(encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
    except (FileNotFoundError, ValueError):
        return []
    evidence = _evidence(cfg)
    return [enrich(cfg, row, evidence) if row.get("schema_version") == C.METRICS_SCHEMA else row for row in rows]


def _new(host: str, sid: str, backfilled: bool) -> dict:
    row = {
        "schema_version": C.METRICS_SCHEMA,
        "host": host,
        "session": sid,
        "project": None,
        "repository": None,
        "origin": None,
        "first_ts": None,
        "last_ts": None,
        "active_seconds": None,
        "turns": 0,
        "model_calls": None,
        "tool_calls": 0,
        "failed_tool_calls": 0,
        "compactions": 0,
        "tokens": dict.fromkeys(TOKEN_FIELDS),
        "total_tokens": None,
        "model": [],
        "backfilled": backfilled,
        "complete": False,
        "malformed_rows": 0,
        "oversized_rows": 0,
    }
    return {"row": row, "line": 0, "seen": {}, "prompts": {}, "calls": {}, "results": [], "timeline": []}


def _once(state: dict, kind: str, key: object, value: object = True) -> bool:
    identity_ = _hash(f"{kind}:{key}")
    if identity_ in state["seen"]:
        return False
    state["seen"][identity_] = {**(value if isinstance(value, dict) else {}), "kind": kind, "stamp": state.get("stamp")}
    return True


def _clock(state: dict, stamp: float | None, *, prompt: bool = False, reply: bool = False) -> None:
    row = state["row"]
    if stamp is not None:
        row["first_ts"] = min(row["first_ts"], stamp) if row["first_ts"] is not None else stamp
        row["last_ts"] = max(row["last_ts"], stamp) if row["last_ts"] is not None else stamp
    state["timeline"].append((stamp, state.get("prompt_key") if prompt else None, reply, state.get("prompt_key")))


def _usage(state: dict, key: str, values: dict, total: int, model: object) -> None:
    row = state["row"]
    digest = _hash("usage:" + key)
    _once(state, "model", key)
    previous = state["seen"].get(digest, {})
    if (state.get("stamp") or 0) >= (previous.get("stamp") or 0):
        state["seen"][digest] = {**values, "total": total, "kind": "usage", "stamp": state.get("stamp")}
    if isinstance(model, str) and model and model != "<synthetic>" and model not in row["model"]:
        row["model"].append(model)
        row["model"].sort()


def _tool(state: dict, block: dict, entry: dict) -> None:
    kind = block.get("type")
    key = _hash(block.get("id") if kind == "tool_use" else block.get("tool_use_id"))
    if kind == "tool_use":
        state["calls"][key] = sessions.own_skill_tool(str(block.get("name", "")))
        _once(state, "tool", key)
    elif kind == "tool_result":
        outcomes = []
        for name in ("", "mcp__nowitknows__skill_view"):
            status = sessions.tool_outcome(block, entry, name)
            rejected = status == " -> rejected by the user"
            outcomes.append((status.startswith(" -> failed:") or rejected, rejected))
        state["results"].append((key, outcomes))


def _prompt(state: dict, text: str, stamp: float | None) -> None:
    from .worker import SIGNALS

    key = state.get("prompt_key") or _hash(f"{state.get('source')}:{state['line']}")
    state["prompt_key"] = key
    state["prompts"][key] = {
        "seconds": 0,
        "stamp": stamp,
        "correction": int(bool(SIGNALS[1][1].search(text) or text.startswith("[Request interrupted by user"))),
    }
    _clock(state, stamp, prompt=True)


def _claude_origin(row: dict, raw: dict) -> None:
    """Prefer explicit attendance and headless evidence over an entrypoint's default."""
    if row.get("origin") in ("subagent", "nik_background", "host_memory"):
        return
    if raw.get("sessionId") not in (None, row["session"]) or raw.get("isSidechain"):
        return
    surface = {**_object(raw.get("env")), **_object(raw.get("surface")), **raw}
    entrypoint = surface.get("entrypoint", surface.get("CLAUDE_CODE_ENTRYPOINT"))
    attended = surface.get("attended", surface.get("CLAUDE_CODE_SESSION_ATTENDED"))
    environment = surface.get("environment_kind", surface.get("CLAUDE_CODE_ENVIRONMENT_KIND"))
    kind = surface.get("session_kind", surface.get("CLAUDE_CODE_SESSION_KIND"))
    origin, evidence = None, None
    if environment == C.REMOTE_CONTROL_KIND:
        origin, evidence = "attended", "bridge"
    elif (
        entrypoint in C.HEADLESS_ENTRYPOINTS
        or kind in C.METRICS_HEADLESS_SESSION_KINDS
        or surface.get("isNonInteractive") is True
        or surface.get("printMode") is True
        or surface.get("isInteractive") is False
        or attended in ("0", False)
    ):
        origin, evidence = "unattended", "headless"
    elif attended in ("1", True) or surface.get("isInteractive") is True:
        origin, evidence = "attended", "attendance"
    elif entrypoint in C.METRICS_INTERACTIVE_ENTRYPOINTS:
        origin, evidence = "attended", "entrypoint"
    priority = (None, "entrypoint", "attendance", "headless", "bridge")
    if origin and priority.index(evidence) >= priority.index(row.get("origin_evidence")):
        row.update(origin=origin, origin_evidence=evidence)


def _claude(state: dict, raw: dict) -> None:
    row, message = state["row"], _object(raw.get("message"))
    state["stamp"] = _stamp(raw.get("timestamp"))
    if row["host"] == "claude":
        _claude_origin(row, raw)
    stamp = _stamp(raw.get("timestamp"))
    attachment = _object(raw.get("attachment"))
    if (
        attachment.get("type") == "skill_listing"
        and attachment.get("isInitial") is True
        and type(attachment.get("skillCount")) is int
    ) and row.get("skills_available") is None:
        row["skills_available"] = attachment["skillCount"]
    if raw.get("isSidechain") and row["origin"] != "subagent":
        return
    if (
        raw.get("type") == "system"
        and raw.get("subtype") == "compact_boundary"
        and _once(state, "compact", raw.get("uuid") or f"{state.get('source')}:{state['line']}")
    ):
        row["compactions"] += 1
    prompt = sessions.prompt_text({**raw, "isSidechain": False})
    if (
        prompt
        and prompt[1] is None
        and _once(state, "prompt", raw.get("uuid") or f"{state.get('source')}:{state['line']}")
    ):
        state["prompt_key"] = _hash(raw.get("uuid") or f"{state.get('source')}:{state['line']}")
        _prompt(state, prompt[0], stamp)
    else:
        _clock(state, stamp, reply=raw.get("type") == "assistant")
    if raw.get("type") == "assistant" and message.get("model") != "<synthetic>":
        model = message.get("model")
        if isinstance(model, str) and model and model not in row["model"]:
            row["model"] = sorted([*row["model"], model])
        key = str(message.get("id") or raw.get("uuid") or f"{state.get('source')}:{state['line']}")
        if message.get("id") and _once(state, "model", key):
            row["model_calls"] = (row["model_calls"] or 0) + 1
        usage = message.get("usage")
        if _valid_usage(usage):
            values = dict(
                zip(
                    TOKEN_FIELDS,
                    (
                        _count(usage.get(k))
                        for k in (
                            "input_tokens",
                            "cache_read_input_tokens",
                            "cache_creation_input_tokens",
                            "output_tokens",
                        )
                    ),
                    strict=True,
                )
            )
            _usage(
                state,
                str(message.get("id") or raw.get("uuid") or f"{state.get('source')}:{state['line']}"),
                values,
                sum(values.values()),
                message.get("model"),
            )
    for block in sessions._blocks(message):
        if isinstance(block, dict):
            _tool(state, block, raw)


def _codex_foreign(raw: dict, sid: str) -> bool:
    payload = _object(raw.get("payload"))
    explicit = (raw.get("session_id"), raw.get("sessionId"), payload.get("thread_id"), payload.get("threadId"))
    return bool(
        raw.get("isSidechain") is True
        or any(v and v != sid for v in explicit)
        or (raw.get("type") == "session_meta" and payload.get("id") != sid)
    )


def _codex(state: dict, raw: dict) -> None:
    payload = _object(raw.get("payload"))
    state["stamp"] = _stamp(raw.get("timestamp"))
    if raw.get("type") == "turn_context":
        state["model"] = payload.get("model")
    if raw.get("type") != "event_msg" or payload.get("type") != "token_count":
        return
    info = _object(payload.get("info"))
    usage = info.get("last_token_usage")
    if not _valid_usage(usage):
        state["unknown_usage"] = True
        return
    total = info.get("total_token_usage")
    key = json.dumps(total, sort_keys=True) if isinstance(total, dict) else f"{state['source']}:{state['line']}"
    inputs, cached, output = (_count(usage.get(k)) for k in ("input_tokens", "cached_input_tokens", "output_tokens"))
    write = next((usage[k] for k in ("cache_write_input_tokens", "cache_creation_input_tokens") if k in usage), None)
    write = _count(write) if write is not None else None
    _usage(
        state,
        key,
        dict(zip(TOKEN_FIELDS, (max(0, inputs - cached - (write or 0)), cached, write, output), strict=True)),
        inputs + output,
        state.get("model"),
    )


def _cursor(state: dict, raw: dict) -> None:
    from .cursor_transcript import _extract_user_text, _timestamp

    if raw.get("type") in ("user", "assistant", "system"):
        _claude(state, raw)
        return
    role, message = raw.get("role"), _object(raw.get("message"))
    blocks = sessions._blocks(message)
    text = "\n".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    stamp = _stamp(_timestamp(raw, text).get("timestamp"))
    prompt = _extract_user_text(text) if role == "user" else None
    if prompt and prompt[1] is None:
        state["prompt_key"] = _hash(raw.get("uuid") or f"{state.get('source')}:{state['line']}")
        _prompt(state, prompt[0], stamp)
    else:
        _clock(state, stamp, reply=role == "assistant")
    for block in blocks:
        if isinstance(block, dict):
            _tool(state, block, raw)


def _excluded(cwd: str, metadata: dict) -> bool:
    parts = Path(cwd).parts
    if any(fnmatch.fnmatch(cwd.lower(), pattern) for pattern in C.METRICS_EXCLUDED_PATHS):
        return True
    return any(fnmatch.fnmatch(part.lower(), pattern) for part in parts for pattern in C.METRICS_EXCLUDED_PARTS) or any(
        str(key).startswith(C.METRICS_BENCH_MARKERS) and bool(value)
        for values in (metadata, _object(metadata.get("env")))
        for key, value in values.items()
    )


def _project(cfg: C.Config, state: dict, cwd: str, metadata: dict) -> None:
    row = state["row"]
    if row["project"] is not None or not cwd:
        return
    row["project"] = native.project_key(Path(cwd))
    row["repository"] = identity.cached(cfg, row["project"]) or {"kind": "path", "value": row["project"]}
    row["excluded_work"] = _excluded(cwd, metadata)
    if (
        Path(cwd) == cfg.data
        or Path(cwd).is_relative_to(cfg.data)
        or any(part in C.METRICS_NIK_DATA_PARTS for part in Path(cwd).parts)
        or any(
            a == ".codex" and b in C.METRICS_NIK_CODEX_PARTS
            for a, b in zip(Path(cwd).parts, Path(cwd).parts[1:], strict=False)
        )
    ):
        row["origin"] = "nik_background"


def repository(cfg: C.Config, row: dict, history: dict | None = None) -> dict | None:
    """Use stable recorded identities, preserving ambiguity at reinitialized paths."""
    key = row.get("project")
    known = history if history is not None else identity.history(cfg)
    choices = [v for v in known.get(key, ()) if v["kind"] != "path"]
    if len(choices) == 1:
        return choices[0]
    if len(choices) > 1:
        dated = [
            r
            for r in log_rows(cfg.data / C.REPO_IDENTITIES_FILE)
            if r.get("path_key") == key
            and r.get("first_seen", math.inf) <= (row.get("last_ts") or 0)
            and identity.valid({"kind": r.get("kind"), "value": r.get("identity")})
        ]
        if dated:
            latest = max(dated, key=lambda r: r["first_seen"])
            return {"kind": latest["kind"], "value": latest["identity"]}
        return {"kind": "path", "value": key}
    return row.get("repository")


def _evidence(cfg: C.Config) -> dict:
    from .telemetry import learned_lives

    diagnostics = {}
    activity = log_rows(cfg.data / "activity.jsonl", diagnostics=diagnostics)
    if any(type(r.get("ts")) not in (int, float) or not math.isfinite(r["ts"]) for r in activity):
        diagnostics["malformed"] = True
        activity = [r for r in activity if type(r.get("ts")) in (int, float) and math.isfinite(r["ts"])]
    from .report import tokens

    by_session, windows, runs, usage = defaultdict(list), {}, defaultdict(set), defaultdict(int)
    children = set()
    for event in activity:
        host = event.get("host", cfg.legacy_host)
        if host not in ("claude", "codex", "cursor") or not isinstance(event.get("session", ""), str):
            diagnostics["malformed"] = True
            continue
        by_session[host, event.get("session")].append(event)
        lo, hi = windows.get(host, (event["ts"], event["ts"]))
        windows[host] = min(lo, event["ts"]), max(hi, event["ts"])
    for record in log_rows(cfg.data / "runs.jsonl"):
        if isinstance(record.get("run_id"), str):
            runs[record.get("host", cfg.legacy_host), record.get("session")].add(record["run_id"])
    for record in log_rows(cfg.data / "usage.jsonl"):
        if isinstance(record.get("child_session"), str):
            children.add((record.get("host", cfg.legacy_host), record["child_session"]))
        if isinstance(record.get("run_id"), str):
            usage[record.get("host", cfg.legacy_host), record["run_id"]] += sum(tokens(record).values())
    mark = _load_state(cfg.data / C.METRICS_ACTIVITY_COVERAGE_FILE)
    coverage = (mark.get("since"), mark.get("through"))
    if not all(type(n) in (int, float) for n in coverage) or not activity_coverage(cfg, *coverage):
        coverage = None
    return {
        "coverage": coverage,
        "children": children,
        "activity": by_session,
        "windows": windows,
        "diagnostics": diagnostics,
        "lives": learned_lives(cfg, log_rows(cfg.data / "skills_ledger.jsonl")),
        "runs": runs,
        "usage": usage,
        "repositories": identity.history(cfg),
    }


def enrich(cfg: C.Config, row: dict, evidence: dict | None = None) -> dict:
    """Join recorded exposure using telemetry's historical identity and lifecycle rules."""
    from .report import _activity_kind
    from .telemetry import learned_at

    evidence = evidence or _evidence(cfg)
    row = dict(row)
    host, sid = row["host"], row["session"]
    if (host, sid) in evidence["children"]:
        row["origin"] = "nik_background"
    end, start = row.get("last_ts"), row.get("first_ts")
    activity = evidence["activity"].get((host, sid), ())
    row.setdefault("skills_available", None)
    counts_fields = ("skills_delivered", "skills_loaded", "learned_skills", "other_skills")
    previous = {key: row.get(key) for key in (*counts_fields, "learned_skill_used", "note_only")}
    row.update({field: {} for field in counts_fields})
    row.update(learned_skill_used=False, note_only=False)
    windows = [evidence["windows"].get(host), evidence["coverage"]]
    row["nik_logs_known"] = bool(
        start is not None
        and end is not None
        and any(window and window[0] <= start <= end <= window[1] for window in windows)
    )
    content, notes = False, False
    for event in activity:
        if event.get("host", cfg.legacy_host) != host or event.get("session") != sid:
            continue
        if row["backfilled"] and (end is None or event.get("ts", math.inf) > end):
            continue
        kind = _activity_kind(event, host == "codex")
        if event.get("kind") == "deliver":
            kind = "deliveries"
        name = event.get("skill")
        if kind not in ("deliveries", "loads") or not isinstance(name, str) or not C.SKILL_NAME.fullmatch(name):
            continue
        counts = row["skills_delivered" if kind == "deliveries" else "skills_loaded"]
        counts[name] = counts.get(name, 0) + 1
        reached = event.get("kind") == "load" and event.get("delivery") != "hook_context"
        reached |= host == "codex" and event.get("delivery") == "hook_context"
        content |= reached
        notes |= not reached
        learned = reached and learned_at(cfg, {"host": host, **event}, evidence["lives"])
        row["learned_skill_used"] |= learned
        if reached:
            counts = row["learned_skills" if learned else "other_skills"]
            counts[name] = counts.get(name, 0) + 1
    for field in counts_fields:
        for name, count in (previous[field] or {}).items():
            row[field][name] = max(count, row[field].get(name, 0))
    row["learned_skill_used"] |= previous["learned_skill_used"] is True
    row["note_only"] = (notes or bool(previous["note_only"])) and not (content or row["skills_loaded"])
    if not row["learned_skill_used"] and not row["nik_logs_known"]:
        row["learned_skill_used"] = None
    ids = evidence["runs"].get((host, sid), ())
    row["background_tokens"] = sum(evidence["usage"].get((host, run), 0) for run in ids) if ids else None
    row["repository"] = repository(cfg, row, evidence["repositories"])
    row.setdefault("corrections", None)
    return row


def _load_state(path: Path) -> dict:
    try:
        return _object(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return {}


def _records(path: Path, counts: dict):
    with sessions._source(path) as source, io.BufferedReader(source) as stream:
        remaining = math.inf if path.suffix in (".gz", ".zst") else os.fstat(stream.fileno()).st_size
        while remaining:
            line = stream.readline(min(remaining, C.METRICS_LINE_BYTES + 1))
            if not line:
                break
            remaining -= len(line)
            if len(line) > C.METRICS_LINE_BYTES:
                counts["oversized_rows"] += 1
                while remaining and line and not line.endswith(b"\n"):
                    line = stream.readline(min(remaining, C.METRICS_LINE_BYTES))
                    remaining -= len(line)
                continue
            if not line.endswith(b"\n"):
                counts["torn"] = True
                break
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError
                yield raw
            except (ValueError, UnicodeError):
                counts["malformed_rows"] += 1


def _scan(cfg: C.Config, path: Path, sid: str = "", cwd: str = "", *, backfilled: bool = True) -> dict:
    """Read a whole source; the original stat makes concurrent writes changed on the next pass."""
    before = path.stat()
    if not statlib.S_ISREG(before.st_mode):
        raise ValueError("transcript must be a regular file")
    host, states, counts = cfg.host, {}, defaultdict(int)
    saved = State(cfg.data / "state.json", host, cfg.legacy_host).read()["sessions"]
    imported, meta = False, {}
    if host == "codex":
        from .codex_host import import_records
        from .codex_transcript import _Reader

        for raw in _records(path, defaultdict(int)):
            payload = _object(raw.get("payload"))
            if raw.get("type") == "session_meta" and not meta:
                meta = payload
                sid = sid or str(payload.get("id") or path.stem)
            turn = payload.get("turn_id", payload.get("turnId")) or _object(
                payload.get("internal_chat_message_metadata_passthrough")
            ).get("turn_id")
            imported |= isinstance(turn, str) and bool(C.CODEX_IMPORT_TURN.fullmatch(turn))
        imported |= bool(saved.get(sid, {}).get("imported")) or any(
            key == f"codex:{sid}"
            for key, _ in import_records(
                replace(
                    cfg,
                    env={
                        **cfg.env,
                        "NOWITKNOWS_CODEX_HOME": C.env("NOWITKNOWS_CODEX_HOME", cfg.env) or cfg.env["CODEX_HOME"],
                        "NOWITKNOWS_CLAUDE_TRANSCRIPTS_DIR": C.env("NOWITKNOWS_CLAUDE_TRANSCRIPTS_DIR", cfg.env)
                        or str(cfg.claude_home / "projects"),
                    },
                )
            )
        )
        reader = _Reader(path, "")
        reader.sid = sid
        clocks = []
    sidechain = host == "claude" and "subagents" in path.parts
    primary = sid
    copied = False
    for number, raw in enumerate(_records(path, counts), 1):
        payload = _object(raw.get("payload"))
        if host == "codex" and _codex_foreign(raw, sid):
            continue
        if host == "codex":
            turn = payload.get("turn_id", payload.get("turnId")) or _object(
                payload.get("internal_chat_message_metadata_passthrough")
            ).get("turn_id")
            if isinstance(turn, str):
                copied = bool(C.CODEX_IMPORT_TURN.fullmatch(turn) or (imported and C.CODEX_COPY_TURN.fullmatch(turn)))
            if copied:
                continue
        primary = primary or str(raw.get("sessionId") or path.stem)
        owner = str(raw.get("sessionId") or primary) if host == "claude" else primary
        if sidechain or (host == "claude" and raw.get("isSidechain") and not states):
            sidechain = True
            owner = primary + ":subagent:" + _hash(path.stem)
        state = states.setdefault(owner, _new(host, owner, backfilled))
        row = state["row"]
        state.update(source=str(path), line=number)
        if sidechain:
            row.update(origin="subagent", parent_session=primary)
        start = saved.get(owner, {})
        folder = start.get("start") or raw.get("cwd") or payload.get("cwd") or cwd
        _project(cfg, state, folder if isinstance(folder, str) else "", raw if host != "codex" else meta)
        if not backfilled and _excluded("", {"env": cfg.env}):
            row["excluded_work"] = True
        if host == "claude":
            _claude_origin(row, {"surface": _object(start.get("surface"))})
        if host == "codex" and raw.get("type") == "session_meta":
            source = payload.get("source")
            subagent = source.get("subagent") if isinstance(source, dict) else None
            row["origin"] = (
                (
                    "host_memory"
                    if (isinstance(subagent, str) and subagent in C.METRICS_MEMORY_SOURCES)
                    or (isinstance(subagent, dict) and any(k in subagent for k in C.METRICS_MEMORY_SOURCES))
                    else "subagent"
                    if isinstance(source, dict) or source == "subagent"
                    else "exec"
                    if source == "exec"
                    else "attended"
                    if payload.get("originator") in C.CODEX_ATTENDED_ORIGINATORS and source != "mcp"
                    else "unattended"
                )
                if row["origin"] != "nik_background"
                else row["origin"]
            )
            parent = _object(_object(subagent).get("thread_spawn")).get("parent_thread_id")
            if isinstance(parent, str):
                row["parent_session"] = parent
        if meta.get("nowitknows_child") or raw.get("nowitknows_child"):
            row["origin"] = "nik_background"
        try:
            if host == "codex":
                _codex(state, raw)
                if (
                    raw.get("type") == "response_item"
                    and payload.get("type")
                    in ("function_call_output", "custom_tool_call_output", "local_shell_call_output")
                    and payload.get("call_id") not in reader.tool_names
                ):
                    probe = _Reader(path, "")
                    call = str(payload.get("call_id") or payload.get("id") or number)
                    outcomes = []
                    for name in ("", "mcp__nowitknows__skill_view"):
                        probe.tool_names[call] = name
                        result = probe.result(
                            raw,
                            number,
                            call,
                            payload.get("output"),
                            rejected=payload.get("toolDenialKind") == "user-rejected",
                        )
                        block = result.row["message"]["content"][0] if result else {}
                        status = sessions.tool_outcome(block, result.row if result else {}, name)
                        rejected = status == " -> rejected by the user"
                        outcomes.append((status.startswith(" -> failed:") or rejected, rejected))
                    state["results"].append((_hash(call), outcomes))
                reader.parse(raw, number)
                stamp = _stamp(raw.get("timestamp"))
                if raw.get("type") != "session_meta":
                    clocks.append((number, stamp, payload.get("type") == "task_complete"))
                for entry in reversed(reader.entries):
                    if entry.line != number:
                        break
                    entry.row["timestamp"] = stamp
                    if entry.family == "user":
                        entry.row["uuid"] = (
                            entry.turn or entry.identity or (str(stamp) if stamp is not None else f"{path}:{number}")
                        )
            else:
                (_claude if host == "claude" else _cursor)(state, raw)
        except (ValueError, TypeError, AttributeError, RecursionError):
            counts["malformed_rows"] += 1
    if host == "codex" and states:
        state = states[primary]
        normalized = defaultdict(list)
        for raw in reader.reconcile():
            if _object(raw.get("codex")).get("source_line"):
                normalized[raw["codex"]["source_line"]].append(raw)
        for number, stamp, reply in clocks:
            state["line"] = number
            for raw in normalized.get(number, ()):
                if state["row"]["origin"] == "subagent":
                    raw["isSidechain"] = False
                _claude(state, raw)
            _clock(state, stamp, reply=reply)
    for state in states.values():
        state["row"].update(
            complete=not any(counts.values()),
            verified=True,
            malformed_rows=counts["malformed_rows"],
            oversized_rows=counts["oversized_rows"],
            usage_missing=bool(state.get("unknown_usage")),
            sources=1,
        )
    return dict(
        path=str(path), host=host, size=before.st_size, mtime=before.st_mtime_ns, scanned=time.time(), states=states
    )


def _timing(states: list[dict]) -> None:
    prompts = {key: value for state in states for key, value in state["prompts"].items()}
    timeline = [event for state in states for event in state["timeline"]]
    if any(stamp is None and (prompt or reply) for stamp, prompt, reply, _ in timeline):
        for value in prompts.values():
            value["seconds"] = None
        return
    clocks, current = {}, None
    for stamp, prompt, reply, local in sorted(timeline, key=lambda e: (e[0] is None, e[0] or 0, not bool(e[1]))):
        if prompt:
            if prompt not in clocks:
                clocks[prompt] = (stamp, 0)
                current = prompt
        elif stamp is not None:
            key = local if local in clocks else current
            if key is None:
                continue
            clock, elapsed = clocks[key]
            elapsed += min(max(0, stamp - clock), C.METRICS_GAP_CAP_S)
            clocks[key] = (max(clock, stamp), elapsed)
            if reply and key in prompts:
                prompts[key]["seconds"] = elapsed


def _contributions(scans: dict) -> None:
    """Assign repeated identities once within each session, then discard all parser facts."""
    groups = defaultdict(list)
    for scan in scans.values():
        for sid, state in scan["states"].items():
            groups[sid].append(state)
    for states in groups.values():
        calls = {key: value for state in states for key, value in state["calls"].items()}
        for state in states:
            for key, outcomes in state["results"]:
                failed, rejected = outcomes[int(calls.get(key, False))]
                if failed:
                    _once(state, "failed", key)
                if rejected:
                    _once(state, "correction-result", key)
    owners = {}
    for _path, scan in sorted(scans.items()):
        for sid, state in scan["states"].items():
            for field in ("seen", "prompts"):
                for key, value in list(state[field].items()):
                    identity_ = sid, field, key
                    if identity_ in owners:
                        target = owners[identity_]
                        if (value.get("stamp") or 0) >= (target[field][key].get("stamp") or 0):
                            target[field][key] = value
                        del state[field][key]
                    else:
                        owners[identity_] = state
    for states in groups.values():
        _timing(states)
    for scan in scans.values():
        rows = []
        for state in scan.pop("states").values():
            row, facts, prompts = state["row"], list(state["seen"].values()), list(state["prompts"].values())
            for kind, field in (
                ("model", "model_calls"),
                ("tool", "tool_calls"),
                ("failed", "failed_tool_calls"),
                ("compact", "compactions"),
                ("correction-result", "corrections"),
            ):
                row[field] = sum(v.get("kind") == kind for v in facts)
            row["turns"] = len(prompts)
            row["corrections"] += sum(v["correction"] for v in prompts)
            row["active_seconds"] = (
                sum(v["seconds"] for v in prompts) if all(v["seconds"] is not None for v in prompts) else None
            )
            usage = [v for v in facts if v.get("kind") == "usage"]
            row["usage_calls"] = len(usage)
            row["tokens"] = {
                k: sum(v[k] for v in usage) if all(v[k] is not None for v in usage) else None for k in TOKEN_FIELDS
            }
            row["total_tokens"] = sum(v["total"] for v in usage)
            rows.append(row)
        scan["rows"] = rows


def _combine(parts: list[dict]) -> dict:
    ordered = sorted(parts, key=lambda r: (r.get("first_ts") is None, r.get("first_ts") or 0))
    row = dict(ordered[0])
    priority = (None, "entrypoint", "attendance", "headless", "bridge")
    for source in ordered:
        if source.get("origin_evidence") and priority.index(source["origin_evidence"]) > priority.index(
            row.get("origin_evidence")
        ):
            row.update(origin=source["origin"], origin_evidence=source["origin_evidence"])
    for field in (
        "turns",
        "model_calls",
        "tool_calls",
        "failed_tool_calls",
        "compactions",
        "corrections",
        "sources",
        "malformed_rows",
        "oversized_rows",
    ):
        row[field] = sum(r.get(field) or 0 for r in parts)
    for field, operation in (("first_ts", min), ("last_ts", max)):
        row[field] = operation((r[field] for r in parts if r.get(field) is not None), default=None)
    row["active_seconds"] = (
        round(sum(r["active_seconds"] for r in parts), 3)
        if row["turns"] and all(r["active_seconds"] is not None for r in parts)
        else None
    )
    row["model"] = sorted({m for r in parts for m in r["model"]})
    row["complete"] = all(r["complete"] for r in parts)
    row["backfilled"] = all(r["backfilled"] for r in parts)
    row["usage_complete"] = (
        bool(row["model_calls"])
        and sum(r["usage_calls"] for r in parts) == row["model_calls"]
        and not any(r["usage_missing"] for r in parts)
    )
    row["tokens"] = {
        k: sum(r["tokens"][k] for r in parts)
        if row["usage_complete"] and all(r["tokens"][k] is not None for r in parts)
        else None
        for k in TOKEN_FIELDS
    }
    row["total_tokens"] = sum(r["total_tokens"] for r in parts) if row["usage_complete"] else None
    row["model_calls"] = row["model_calls"] or None
    return row


def _cache(cfg: C.Config) -> dict:
    saved = _load_state(cfg.data / C.METRICS_STATE_FILE)
    return saved if saved.get("format") == "whole-files" else {"format": "whole-files", "files": {}, "hosts": {}}


def _save(cfg: C.Config, cache: dict) -> None:
    evidence, groups = _evidence(cfg), defaultdict(list)
    previous = {(r["host"], r["session"]): r for r in read_rows(cfg)}
    for record in cache["files"].values():
        for row in record["rows"]:
            groups[row["host"], row["session"]].append(
                {**row, "complete": row["complete"] and record.get("current", True)}
            )
    rows = []
    for key, parts in sorted(groups.items()):
        row = _combine(parts)
        for field in (
            "skills_loaded",
            "skills_delivered",
            "learned_skills",
            "other_skills",
            "learned_skill_used",
            "note_only",
        ):
            if field in previous.get(key, {}):
                row[field] = previous[key][field]
        rows.append(enrich(cfg, row, evidence))
    atomic_write(cfg.data / C.METRICS_STATE_FILE, json.dumps(cache, separators=(",", ":"), sort_keys=True))
    atomic_write(cfg.data / C.METRICS_FILE, "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def _lineage(record: dict) -> set[str]:
    return {r["session"] for r in record.get("rows", ())} | set(record.get("states", {}))


def _rebuild(cfg: C.Config, cache: dict, path: Path, **kwargs) -> set[str]:
    records = cache["files"]
    key = str(path)
    scans = {key: _scan(cfg, path, **kwargs)}
    lineage = _lineage(scans[key]) | _lineage(records.get(key, {}))
    while True:
        related = [p for p, r in records.items() if r["host"] == cfg.host and p not in scans and lineage & _lineage(r)]
        if not related:
            break
        for name in related:
            scans[name] = _scan(cfg, Path(name))
            lineage |= _lineage(scans[name]) | _lineage(records[name])
    _contributions(scans)
    records.update(scans)
    return set(scans)


def collect(cfg: C.Config, path: Path, sid: str = "", cwd: str = "", *, backfilled: bool = False) -> dict:
    """Collect one whole source and its related pages; no transcript text is persisted."""
    if not cfg.metrics or cfg.child:
        return {}
    path = Path(path).absolute()
    with file_lock(cfg.data / C.METRICS_STATE_FILE, wait_s=0):
        cache = _cache(cfg)
        before = cache["files"].get(str(path), {})
        info = path.stat()
        if not statlib.S_ISREG(info.st_mode):
            raise ValueError("transcript must be a regular file")
        if not before and len(cache["files"]) >= C.METRICS_MAX_FILES:
            raise ValueError("metrics file limit reached")
        if (before.get("size"), before.get("mtime")) != (info.st_size, info.st_mtime_ns):
            _rebuild(cfg, cache, path, sid=sid, cwd=cwd, backfilled=backfilled)
        _save(cfg, cache)
    wanted = sid or next(iter(_lineage(cache["files"].get(str(path), {}))), "")
    return next((r for r in read_rows(cfg) if r["host"] == cfg.host and r["session"] == wanted), {})


def _error(cfg: C.Config, error: Exception) -> None:
    with contextlib.suppress(OSError), (cfg.data / "errors.log").open("a", encoding="utf-8") as stream:
        stream.write(f"session metrics unavailable: {type(error).__name__}\n")


def safe_collect(cfg: C.Config, path: Path | None, sid: str = "", cwd: str = "") -> dict | None:
    try:
        return collect(cfg, path, sid, cwd) if path else None
    except Exception as error:
        _error(cfg, error)
        return None


def _roots(cfg: C.Config) -> list[Path]:
    if cfg.host == "codex":
        home = Path(C.env("NOWITKNOWS_CODEX_HOME", cfg.env) or cfg.env["CODEX_HOME"])
        return [home / "sessions", home / "archived_sessions"]
    if cfg.host == "cursor":
        projects = (cfg.cursor_paths or C.resolve_cursor_paths(cfg.env)).transcript_projects
        return [cfg.transcripts, *sorted(projects.glob("*/agent-transcripts"))]
    return [cfg.transcripts]


def _history_files(roots: list[Path]):
    for root in roots:
        if root.is_symlink():
            continue
        for folder, directories, names in os.walk(root):
            directories[:] = sorted(d for d in directories if not (Path(folder) / d).is_symlink())
            for name in sorted(names):
                path = Path(folder) / name
                if not path.is_symlink() and path.is_file():
                    yield path.absolute()


def backfill(cfg: C.Config, *, foreground: bool = False) -> dict:
    """Scan changed files in path order, stopping only between complete source lineages."""
    result = dict(
        processed=0,
        complete=0,
        failed=0,
        unchanged=0,
        unsupported=0,
        skipped_compressed=0,
        pending=0,
        done=False,
        file=str(cfg.data / C.METRICS_FILE),
    )
    if not cfg.metrics or cfg.child:
        return {**result, "off": not cfg.metrics, "done": True}
    try:
        with file_lock(cfg.data / C.METRICS_STATE_FILE, wait_s=0):
            cache = _cache(cfg)
            records = cache["files"]
            paths = list(_history_files(_roots(cfg)))
            result["unsupported"] = sum(cfg.host == "cursor" and p.name.endswith((".txt", ".txt.gz")) for p in paths)
            paths = {str(p): p for p in paths if p.name.endswith((".jsonl", ".jsonl.gz", ".jsonl.zst"))}
            if cfg.host == "cursor":
                preferred = {p.stem for p in paths.values() if p.is_relative_to(cfg.transcripts)}
                paths = {k: p for k, p in paths.items() if p.is_relative_to(cfg.transcripts) or p.stem not in preferred}
            dirty = set()
            for name, record in list(records.items()):
                if record["host"] != cfg.host:
                    continue
                if Path(name).is_file() and (
                    cfg.host != "cursor"
                    or Path(name).stem not in preferred
                    or Path(name).is_relative_to(cfg.transcripts)
                ):
                    paths[name] = Path(name)
                    continue
                dirty |= _lineage(record)
                del records[name]
            unreadable = {name for name in paths if name.endswith(".jsonl.zst") and sessions.zstd_reader() is None}
            result["skipped_compressed"] = len(unreadable)
            for record in records.values():
                if record["host"] == cfg.host and dirty & _lineage(record):
                    record["mtime"] = -1
            names = sorted(paths)
            progress = cache["hosts"].setdefault(cfg.host, {})
            after = "" if foreground else progress.get("after", "")
            names = [n for n in names if n > after] + [n for n in names if n <= after]
            deadline = time.monotonic() + C.METRICS_BACKFILL_SECONDS
            done = set()
            for name in names:
                if name in done or name in unreadable:
                    continue
                path, old = paths[name], records.get(name, {})
                info = path.stat()
                changed = (old.get("size"), old.get("mtime")) != (info.st_size, info.st_mtime_ns) or bool(
                    dirty & _lineage(old)
                )
                if not changed:
                    result["unchanged"] += 1
                    continue
                if not foreground and (result["processed"] or result["failed"]) and time.monotonic() >= deadline:
                    break
                if not old and len(records) >= C.METRICS_MAX_FILES:
                    result["limited"] = True
                    continue
                if (
                    not foreground
                    and old
                    and info.st_size >= max(C.METRICS_LARGE_FILE_BYTES, old["size"])
                    and (
                        time.time() - old["scanned"] < C.METRICS_LARGE_FILE_INTERVAL_S
                        and time.time() - info.st_mtime < C.METRICS_LARGE_FILE_INTERVAL_S
                    )
                ):
                    continue
                try:
                    done |= _rebuild(cfg, cache, path)
                    result["processed"] += 1
                    result["complete"] += 1
                except Exception as error:
                    result["failed"] += 1
                    _error(cfg, error)
                progress["after"] = name
                if foreground and time.monotonic() >= deadline:
                    print(
                        f"metrics backfill: {result['processed']} changed files scanned / {len(paths)} files",
                        flush=True,
                    )
                    deadline = time.monotonic() + C.METRICS_BACKFILL_SECONDS
            for name, path in paths.items():
                current = records.get(name, {})
                try:
                    info = path.stat()
                    matches = name not in unreadable and (current.get("size"), current.get("mtime")) == (
                        info.st_size,
                        info.st_mtime_ns,
                    )
                    result["pending"] += not matches
                    if current:
                        current["current"] = matches
                except FileNotFoundError:
                    result["pending"] += 1
                    if current:
                        current["current"] = False
            result.update(done=result["pending"] == 0 and not result["failed"], files=len(paths))
            progress.update(result)
            if result["done"]:
                progress["after"] = ""
            _save(cfg, cache)
    except TimeoutError:
        result["busy"] = True
    except Exception as error:
        _error(cfg, error)
        result["failed"] += 1
    return result


def after_job(cfg: C.Config, job: dict) -> None:
    """Launch a detached collector only after an end/stop job releases the review lock."""
    if cfg.child or not cfg.metrics or job.get("reason") not in ("end", "stop"):
        return
    try:
        if lock_held(cfg.data / C.METRICS_STATE_FILE):
            return
        env = {
            **cfg.env,
            "NOWITKNOWS_DATA_DIR": str(cfg.data),
            "NOWITKNOWS_TRANSCRIPTS_DIR": str(cfg.transcripts),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        }
        launch = dict(env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        python = sys.executable
        if os.name == "nt":
            from .hooks import _windows_detach

            # A Windows venv launcher can lose creation flags when it starts the base interpreter.
            base = getattr(sys, "_base_executable", "")
            python = base if base and os.path.isfile(base) else python
            _windows_detach([python, "-m", "nowitknows.session_metrics"], launch, C.METRICS_WINDOWS_PRIORITY)
        else:
            subprocess.Popen([python, "-m", "nowitknows.session_metrics"], **launch, start_new_session=True)
    except Exception as error:
        _error(cfg, error)


def _background_pass(cfg: C.Config) -> None:
    try:
        if os.name != "nt":
            with contextlib.suppress(OSError, AttributeError):
                os.nice(max(0, C.METRICS_NICE - os.nice(0)))
        if cfg.metrics:
            usage_coverage(cfg, time.time(), time.time(), record=True)
            activity_coverage(cfg, time.time(), time.time(), record=True)
        backfill(cfg)
    except Exception as error:
        _error(cfg, error)


def backfill_now(cfg: C.Config) -> dict:
    result = backfill(cfg, foreground=True)
    if result.get("skipped_compressed"):
        print(
            f"metrics backfill: skipped {result['skipped_compressed']} compressed transcripts; "
            "no zstd reader is available in this Python or on PATH.",
            flush=True,
        )
    print(
        f"metrics backfill: {result['complete']} changed files scanned; {result['unchanged']} unchanged; "
        f"{result['pending']} pending",
        flush=True,
    )
    return result


def activity_coverage(cfg: C.Config, lo: float, hi: float, *, record: bool = False) -> bool:
    return usage_coverage(cfg, lo, hi, record=record, activity=True)


def usage_coverage(cfg: C.Config, lo: float, hi: float, *, record: bool = False, activity: bool = False) -> bool:
    """Require an observed append-only usage generation covering the requested window."""
    path = cfg.data / ("activity.jsonl" if activity else "usage.jsonl")
    mark_path = cfg.data / (C.METRICS_ACTIVITY_COVERAGE_FILE if activity else C.METRICS_COVERAGE_FILE)
    mark = _load_state(mark_path)
    try:
        with path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            if stat.st_size > C.METRICS_MAX_BYTES:
                return False
            data = stream.read(C.METRICS_MAX_BYTES + 1)
        same = (
            mark.get("identity") == [stat.st_dev, stat.st_ino]
            and len(data) >= mark.get("size", 0)
            and hashlib.sha256(data[: mark.get("size", 0)]).hexdigest() == mark.get("digest")
        )
        start = mark["since"] if same else hi
        if record:
            atomic_write(
                mark_path,
                json.dumps(
                    {
                        "identity": [stat.st_dev, stat.st_ino],
                        "size": len(data),
                        "digest": hashlib.sha256(data).hexdigest(),
                        "since": start,
                        "through": hi,
                    }
                ),
            )
        return same and start <= lo and (mark.get("through", 0) >= hi if activity else hi >= mark.get("through", hi))
    except OSError:
        return False


if __name__ == "__main__":
    _background_pass(C.for_command(create=False))
