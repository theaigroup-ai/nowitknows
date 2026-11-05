from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from . import claude, judge, memorytidy, model, native, notify
from . import config as C
from .memory import (
    MASKED_WRITE_ERROR,
    TopicStore,
    apply_operations,
    note_refusals,
    nowitknows_note,
    parse_topic,
    project_store,
    refuse,
    render_topic,
)
from .state import atomic_write, durable_write, log_row

UNEXPLAINED = {"severity": "major", "issue": "the judge failed the change without a blocking finding", "fix": "-"}
VERBS = {"write": "added", "remove": "removed"}
BAD_OPERATION = "a project operation writes or removes a named note"


def _rejection(cfg: C.Config, effect: str) -> Path:
    return cfg.data / "rejected-notes" / (hashlib.sha256(effect.encode()).hexdigest() + ".json")


def _dir(cfg: C.Config) -> Path:
    return cfg.data / C.STAGED_NOTES


def _path(cfg: C.Config, cid: str) -> Path:
    return _dir(cfg) / f"{cid}.json"


def _read(path: Path, *, strict: bool = False) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    except OSError:
        if strict:
            raise
        return {}
    return value if isinstance(value, dict) else {}


def _rejection_reason(cfg: C.Config, effect: str) -> str | None:
    try:
        rejected = _read(_rejection(cfg, effect), strict=True)
    except OSError as error:
        return f"the terminal rejection record could not be read ({error})"
    return "this effect was already rejected: " + rejected["closing"]["reason"] if rejected else None


def staged(cfg: C.Config, run_id: str | None = None) -> list[dict]:
    """This app's staged changes, oldest first: one run's, or every leftover."""
    found = [c for p in _dir(cfg).glob("*.json") if (c := _read(p)).get("id") == p.stem]
    mine = [c for c in found if c.get("host", cfg.legacy_host) == cfg.host and run_id in (None, c.get("run_id"))]
    return sorted(mine, key=lambda c: (c.get("ts", 0), c["id"]))


def waiting(cfg: C.Config) -> int:
    """How many changes wait for a judge, of both apps."""
    return sum(1 for _ in _dir(cfg).glob("*.json"))


def drop(cfg: C.Config, cid: str) -> None:
    _path(cfg, cid).unlink(missing_ok=True)


def _well_formed(op: object) -> bool:
    return (
        isinstance(op, dict)
        and isinstance(op.get("action"), str)
        and op["action"] in VERBS
        and isinstance(op.get("name"), str)
        and all(isinstance(op.get(k, ""), str) for k in ("description", "type", "body"))
    )


def stage(
    cfg: C.Config,
    modes: native.Modes,
    start: Path,
    operations: list,
    run_id: str | None,
    effect_id: str,
) -> list[dict]:
    """Queue the operations of one `project` memory call for the judge; each result says whether its operation is
    queued, and one that is not is recorded as a note that was not saved. Where the note lands is decided when it is
    judged, as the settings are then."""
    results, queued = [], {c.get("effect_id") for c in staged(cfg)}
    for i, op in enumerate(operations):
        effect = f"{effect_id}:{i}"
        if not _well_formed(op):
            name = " ".join(str(op.get("name") or "").split())[: C.NOTE_NAME_MAX] if isinstance(op, dict) else ""
            refuse(cfg, native.current(cfg, modes, start).fallback_dir, name or "(unnamed)", BAD_OPERATION, run_id)
            results.append({"ok": False, "error": BAD_OPERATION})
        elif op["action"] == "write" and any("[redacted]" in op.get(k, "") for k in ("description", "body")):
            results.append({"ok": False, "error": MASKED_WRITE_ERROR})
        elif reason := _rejection_reason(cfg, _effect({"effect_id": effect})):
            refuse(cfg, native.current(cfg, modes, start).fallback_dir, op["name"], reason, run_id)
            results.append({"ok": False, "error": reason})
        elif effect in queued:
            results.append({"ok": True, "replayed": True})
        elif not cfg.judge_notes:  # the switch is off: the note is written at once, as the memory tool does
            now = native.current(cfg, modes, start)
            result = apply_operations(
                cfg,
                now.fallback_dir,
                "project",
                [op],
                "background",
                run_id,
                effect_id=effect,
                shared=now.shared,
                project=now.key,
            )
            note_refusals(cfg, now.fallback_dir, [op], result, run_id)
            results.append(result["results"][0])
        else:
            cid = uuid.uuid4().hex[: C.STAGED_ID_CHARS]
            change = {"id": cid, "ts": time.time(), "host": cfg.host, "run_id": run_id, "effect_id": effect}
            change.update(op=op, name=op["name"], modes=modes.to_json(), start=str(start), attempts=0)
            durable_write(_path(cfg, cid), json.dumps(change))
            results.append({"ok": True, "staged": cid})
    return results


def _modes(cfg: C.Config, change: dict) -> native.Modes:
    return native.current(cfg, native.Modes.from_json(change["modes"]), Path(change["start"]))


def _store(cfg: C.Config, modes: native.Modes) -> TopicStore:
    return project_store(cfg, modes.fallback_dir, modes.shared, modes.key)


def _others(modes: native.Modes, name: str) -> list[str]:
    lines = []
    for path in TopicStore(modes.fallback_dir).topics():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if path.stem != name:
            lines.append(f"- {path.stem}: {parse_topic(text).get('description', '')[: C.NOTE_JUDGE_DESCRIPTION_CHARS]}")
    return lines[: C.NOTE_JUDGE_OTHERS]


def _effect(change: dict) -> str:
    return f"{change['effect_id']}:0"  # the ledger's id for the only operation of a change


def _already(cfg: C.Config, modes: native.Modes, change: dict, announce: bool = True) -> dict | None:
    """A change whose write is in the ledger is settled whatever any judge says now (a crash, or a folder that could
    not be written, came between its write and its removal from the queue)."""
    if (row := _store(cfg, modes).landed(_effect(change))) is None:
        return None
    drop(cfg, change["id"])
    if announce:
        _announce(cfg, change, modes, row["path"])
    return {"outcome": "applied", "name": change["name"]}


def _announce(cfg: C.Config, change: dict, modes: native.Modes, path: str) -> None:
    counts, origin = {VERBS[change["op"]["action"]]: 1}, (modes.key, native.project_name(Path(change["start"])))
    notify.push(cfg, notify.memory_line(counts, path), tell=notify.memory_form(counts, path), project=origin)


def _close(
    cfg: C.Config,
    change: dict,
    modes: native.Modes,
    outcome: str,
    reason: str,
    record: str | None = None,
    limit: str | None = None,
) -> dict:
    """Settle a change without writing its note. A rejection's closing record stays keyed by effect after the staged
    change is removed; other closings stay in the staged file until they are carried out."""
    change = {**change, "closing": {"outcome": outcome, "reason": reason, "record": record, "limit": limit}}
    path = _rejection(cfg, _effect(change)) if outcome == "rejected" else _path(cfg, change["id"])
    atomic_write(path, json.dumps(change))
    return _closed(cfg, change, modes)


def _closed(cfg: C.Config, change: dict, modes: native.Modes) -> dict:
    """The rest of closing a change: its record, the note that it was not saved, and its removal from the queue."""
    closing, where = change["closing"], None
    if closing.get("record"):
        where = cfg.data / "rejected" / f"note-{change['id']}.md"
        atomic_write(where, closing["record"])
    limit = closing.get("limit")
    refuse(cfg, modes.fallback_dir, change["name"], closing["reason"], change.get("run_id"), limit, where, change["id"])
    drop(cfg, change["id"])
    return {"outcome": closing["outcome"], "name": change["name"]}


def _decided(cfg: C.Config, change: dict) -> dict | None:
    """The end of a change that was closed and not yet removed from the queue when the process died."""
    terminal = _rejection(cfg, _effect(change))
    if not (live := _read(terminal, strict=True)):
        live = _read(_path(cfg, change["id"]))
        if (live.get("closing") or {}).get("outcome") == "rejected":
            # 0.10.7 kept the rejection in staging; retain its effect before removing it.
            atomic_write(terminal, json.dumps(live))
    if live.get("closing"):
        return _closed(cfg, live, _modes(cfg, live))
    return None


def _live(cfg: C.Config, context: dict) -> dict | None:
    """The staged change this judgment is for; None once it is settled or a later judgment replaced this one."""
    live, token = _read(_path(cfg, context["proposed"]["id"])), (context.get("judged") or {}).get("token")
    return live if token and (live.get("judged") or {}).get("token") == token else None


def _keep(cfg: C.Config, change: dict, why: str) -> dict:
    """Leave the change queued, count the try, and say why once."""
    path = _path(cfg, change["id"])
    if live := _read(path):  # a change settled meanwhile is not brought back
        attempts = live.get("attempts", 0) + 1
        atomic_write(path, json.dumps({**live, "attempts": attempts}))
        if attempts == 1:
            line = f"{why}; project note {change['name']} stays queued and is retried on the next run"
            notify.push(cfg, f"{notify.PREFIX} {line}")
    return {"outcome": "deferred", "name": change["name"]}


def begin(cfg: C.Config, change: dict, evidence: str, announce: bool = True) -> dict:
    """A serializable judgment context, or the outcome of a change that cannot apply any more. The context's `judged`
    is what the judge is asked about: only a verdict on it, while it is the change's latest judgment and the note
    and its folder are as they were, writes the note."""
    if change.get("tidy"):
        return memorytidy.begin(cfg, change)
    try:
        modes, op, name = _modes(cfg, change), change["op"], change["name"]
        if finished := _decided(cfg, change) or _already(cfg, modes, change, announce):
            return finished
        store, remove = _store(cfg, modes), op["action"] == "remove"
        if error := store.problem(
            name, op.get("description", ""), op.get("type", ""), op.get("body", ""), remove=remove
        ):
            return _close(cfg, change, modes, "conflict", error)
        destination = name if remove else store.destination(name)
        before, version = store.snapshot(destination)
        if remove and before is None:
            return _close(cfg, change, modes, "skipped", f"no topic {name!r}")
        if modes.shared and before is not None and not nowitknows_note(before):
            return _close(
                cfg, change, modes, "conflict", "that note is Claude Code's own, which nowitknows never changes"
            )
        description = " ".join(op.get("description", "").split())
        after = None if remove else render_topic(destination, description, op["type"], op.get("body", ""), cfg.host)
        judged = {
            "token": uuid.uuid4().hex,  # also the id of the step that asks the judge
            "folder": str(modes.fallback_dir),
            "shared": modes.shared,
            "destination": destination,
            "version": version,
        }
        if not (live := _read(_path(cfg, change["id"]))):
            return {"outcome": "skipped", "name": name}
        atomic_write(_path(cfg, change["id"]), json.dumps({**live, "judged": judged}))
        if isinstance(earlier := (live.get("judged") or {}).get("token"), str):
            from . import modreview

            modreview.supersede(cfg, earlier)
        request = judge.note_request(
            cfg, {**change, "name": destination}, before, after, _others(modes, destination), evidence
        )
    except OSError as error:
        return _keep(cfg, change, f"the notes folder could not be read ({error})")
    return {"kind": "note", "proposed": {**live, "judged": judged}, "judged": judged, "request": request}


def defer(cfg: C.Config, context: dict, error: Exception | str) -> dict:
    """Keep the change queued when the judge could not answer; one notice says so."""
    if context["proposed"].get("tidy"):
        return memorytidy.defer(cfg, context, error)
    name = context["proposed"]["name"]
    if (live := _live(cfg, context)) is None:
        return {"outcome": "void", "name": name}
    return _keep(cfg, live, f"judge unavailable ({error})")


def expire(cfg: C.Config, change: dict, announce: bool = True) -> dict | None:
    """Finish a change that waited longer than STAGED_MAX_DAYS for a judge, before either runner asks one."""
    if change.get("tidy"):
        return memorytidy.expire(cfg, change)
    if time.time() - change.get("ts", 0) <= C.STAGED_MAX_DAYS * 86400:
        return None
    try:
        modes = _modes(cfg, change)
        return (
            _decided(cfg, change)
            or _already(cfg, modes, change, announce)
            or _close(cfg, change, modes, "expired", f"no judge answered for {C.STAGED_MAX_DAYS} days")
        )
    except OSError as error:
        return _keep(cfg, change, f"the notes folder could not be read ({error})")


def _reject(cfg: C.Config, change: dict, modes: native.Modes, verdict: dict) -> dict:
    name, op = change["name"], change["op"]
    findings = verdict["findings"]
    blocking = [f for f in findings if f["severity"] != "minor"]
    if not blocking:  # a fail the judge did not explain still stands
        findings, blocking = [*findings, UNEXPLAINED], [UNEXPLAINED]
    lines = [f"# Rejected {op['action']} of project note {name}", ""]
    lines += [f"- [{f['severity']}] {f['issue']} -> {f['fix']}" for f in findings]
    lines += ["", "## Proposed operation", "```json", json.dumps(op, indent=1), "```"]
    count = f"{len(blocking)} blocking finding" + ("" if len(blocking) == 1 else "s")
    reason = f"the judge dropped it ({count}): {blocking[0]['issue']}"[: C.NOTE_REFUSAL_REASON_CHARS]
    return _close(cfg, change, modes, "rejected", reason, "\n".join(lines) + "\n")


def _finish(cfg: C.Config, context: dict, verdict: dict) -> dict:
    name = context["proposed"]["name"]
    if (change := _live(cfg, context)) is None:
        return {"outcome": "void", "name": name}
    judged, op, run_id = context["judged"], change["op"], change.get("run_id")
    try:
        modes = _modes(cfg, change)
        if finished := _decided(cfg, change) or _already(cfg, modes, change, context.get("announce", True)):
            return finished
        store = _store(cfg, modes)
        destination = name if op["action"] == "remove" else store.destination(name)
        now = {
            "folder": str(modes.fallback_dir),
            "shared": modes.shared,
            "destination": destination,
            "version": store.snapshot(destination)[1],
        }
        if now != {k: judged.get(k) for k in now}:  # the judge saw another note, or another folder
            return {"outcome": "void", "name": name}
        verdict_row = {"run_id": run_id, "name": name, "verdict": verdict["verdict"], "step_id": judged["token"]}
        log_row(cfg, C.NOTE_JUDGMENTS_LOG, {"ts": time.time(), **verdict_row})  # once, whatever replays the reply
        if verdict["verdict"] != "pass":
            return _reject(cfg, change, modes, verdict)
        try:
            result = apply_operations(
                cfg,
                modes.fallback_dir,
                "project",
                [op],
                "background",
                run_id,
                effect_id=change["effect_id"],
                shared=modes.shared,
                project=modes.key,
                expect={name: judged["version"]},
                destinations={name: judged["destination"]},
            )
        except (TypeError, ValueError, AttributeError) as error:
            return _close(cfg, change, modes, "conflict", str(error))
        [written] = result["results"]
        if written.get("stale"):
            return {"outcome": "void", "name": name}
        if not written.get("ok"):
            why = str(written["error"])
            return _close(cfg, change, modes, "conflict", why, limit=written.get("refused"))
        drop(cfg, change["id"])
    except OSError as error:
        return _keep(cfg, change, f"the notes folder could not be written ({error})")
    if context.get("announce", True):
        _announce(cfg, change, modes, written["path"])
    return {"outcome": "applied", "name": name}


def answer(cfg: C.Config, context: dict, text: str) -> dict:
    """Advance one judgment without running a model; an answer that is not a clean verdict never applies."""
    if context["proposed"].get("tidy"):
        return memorytidy.answer(cfg, context, text)
    try:
        verdict = judge.judge_answer({"result": text}, context["request"]["label"])
    except claude.ClaudeError as error:
        return defer(cfg, context, error)
    return _finish(cfg, context, verdict)


def settle(cfg: C.Config, change: dict, evidence: str, announce: bool = True) -> str:
    """Judge one change through the CLI and return how it ended; `announce` False leaves the notice of a note that
    landed to the caller's summary of the run."""
    from . import modreview, paid

    context = begin(cfg, change, evidence, announce)
    if "request" not in context:
        return context["outcome"]
    context = {**context, "announce": announce}
    flow = {"owner": "cli:" + uuid.uuid4().hex, "visited": [], "waiting": [], "run_id": change.get("run_id")}
    step_id = context["judged"]["token"]
    modreview._step(cfg, flow, context, reserve=False, identity=step_id)
    step = modreview._read(cfg.path("mod", "steps", step_id + ".json"))
    with paid.capture(cfg, {"kind": "module", "step": step_id}) as receipt:
        try:
            output = model.run(cfg, **context["request"])
        except claude.AuthError:
            modreview._complete(cfg, step)
            raise  # the change stays staged; the job reports the cause once
        except claude.ClaudeError as error:
            outcome = defer(cfg, context, error)["outcome"]
        else:
            structured = output.get("structured_output")
            text = json.dumps(structured) if isinstance(structured, dict) else str(output.get("result"))
            outcome = answer(cfg, context, text)["outcome"]
        modreview._complete(cfg, step)
        paid.complete(cfg, receipt)
    return outcome


def settle_pending(cfg: C.Config, run_id: str | None = None, pending: frozenset | set = frozenset()) -> list[str]:
    """Judge staged changes in order: one run's, or (run_id None) every leftover from earlier runs."""
    from . import worker
    from .skills import SkillStore

    store, outcomes = SkillStore(cfg), []
    for change in staged(cfg, run_id):
        if change["id"] in pending:
            outcomes.append("deferred")
        elif run_id is None and (finished := expire(cfg, change)):
            outcomes.append(finished["outcome"])
        elif worker.over_budget(cfg, C.REVIEW_JUDGE_TOKENS):
            outcomes.append("deferred")
        else:
            outcomes.append(settle(cfg, change, store.evidence(change.get("run_id")), announce=run_id is None))
        store.drop_evidence_if_done(change.get("run_id"))
    return outcomes
