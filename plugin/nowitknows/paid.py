from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from . import config as C
from .state import durable_write, jsonl_rows

_CAPTURE = ContextVar("paid_reply", default=None)


def spent(usage: dict | None) -> bool:
    return isinstance(usage, dict) and any(
        type(usage.get(key)) is int and usage[key] > 0
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )


def replied(output: dict) -> bool:
    return bool(output) and (
        not output.get("is_error") or output.get("subtype") == "error_secret" or spent(output.get("usage"))
    )


@contextmanager
def capture(cfg: C.Config, context: dict):
    """Bind settlement context without changing the public model request or judge interfaces."""
    from .modreview import _settings

    value = {"id": context.get("step") or uuid.uuid4().hex, "host": cfg.host, "env": _settings(cfg), "context": context}
    token = _CAPTURE.set(value)
    try:
        yield value
    finally:
        _CAPTURE.reset(token)


def receive(
    cfg: C.Config,
    label: str,
    model: str,
    started: float,
    output: dict,
    run_id: str | None,
    *,
    receipt: dict | None = None,
    pending: bool = False,
    record: bool = True,
) -> dict:
    """Persist a paid response before acquiring any shared accounting lock."""
    from . import claude, modreview

    value = receipt or _CAPTURE.get() or {"id": uuid.uuid4().hex, "host": cfg.host}
    routing = None
    if cfg.host == "cursor":
        from .cursor_runner import ROUTE

        routing = ROUTE.get()
    if routing is not None and (pending or output.get("is_error")):
        # A failed call accounts spend independently and can never replay effects.
        value = routing.setdefault("receipt", {"id": uuid.uuid4().hex, "host": cfg.host})
    prior = (routing or {}).get("receipt") or receipt
    if not pending and not output.get("is_error") and prior:
        # Keep the accounting receipt until its validated replacement is durable.
        value = _CAPTURE.get() or prior
        if value["id"] != prior["id"]:
            value["replaces"] = prior["id"]
    text = (
        json.dumps(output["structured_output"])
        if isinstance(output.get("structured_output"), dict)
        else str(output.get("result") or "")
    )
    safe = modreview._safe_payload({"text": text})
    saved = {
        k: output[k] for k in ("usage", "modelUsage", "subtype", "num_turns", "is_error", "session_id") if k in output
    }
    if routing is not None and output.get("is_error"):
        safe = {"text": ""}
        saved = {
            "usage": {
                k: v
                for k, v in (output.get("usage") or {}).items()
                if k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
                and type(v) is int
                and v >= 0
            },
            "is_error": True,
            "subtype": "error_cursor",
            "num_turns": 1,
        }
    if saved.get("subtype") == "error_max_turns":
        saved["is_error"] = False
    saved["result"] = safe.get("text", safe.get("error", ""))
    if "error" in safe:
        saved.update(is_error=True, subtype="error_secret")
    saved["step_id"] = value["id"]
    if routing is not None:
        route = {k: routing[k] for k in ("job", "runner")}
        saved.update(route)
        value.update(route)
        context = (_CAPTURE.get() or {}).get("context", {})
        if context.get("kind") == "module":
            value["module_step"] = context["step"]  # Accounting link only; failed attempts retain no replay context.
    value.update(output=saved, label=label, model=model, started=started, run_id=run_id)
    durable_write(cfg.path("paid-replies", value["id"] + ".json"), json.dumps(value))
    if value.get("replaces"):
        cfg.path("paid-replies", value["replaces"] + ".json").unlink(missing_ok=True)
    if pending:
        return value  # Recovery counts this sanitized failure if validation is interrupted.
    if not record:
        return value  # The modular caller accounts its answer; recovery retains the validated reply.
    claude._log_usage(cfg, label, model, started, saved, run_id)
    if not value.get("context"):
        complete(cfg, value)
    return value


def complete(cfg: C.Config, value: dict) -> None:
    if "output" in value:
        durable_write(cfg.path("paid-replies", value["id"] + ".json"), json.dumps({**value, "done": True}))


def recover(cfg: C.Config) -> None:
    """Reconcile spend and effects under the worker lock without starting a model."""
    from . import claude, modreview, worker

    receipts = [
        value
        for path in sorted((cfg.data / "paid-replies").glob("*.json"))
        if (value := json.loads(path.read_text(encoding="utf-8"))).get("host") == cfg.host
    ]
    replaced = {value["replaces"] for value in receipts if value.get("replaces")}
    accounted = None
    for value in receipts:
        if value["id"] in replaced:
            continue
        if accounted is None:
            accounted = {
                row.get("step_id")
                for row in jsonl_rows(cfg.data / "usage.jsonl")
                if row.get("host", cfg.legacy_host) == cfg.host
            }
        if value["id"] not in accounted and (not value.get("replaces") or value["replaces"] not in accounted):
            claude._log_usage(
                cfg, value["label"], value["model"], value["started"], value["output"], value.get("run_id")
            )
            accounted.add(value["id"])
        if value.get("done"):
            if value.get("replaces"):
                cfg.path("paid-replies", value["replaces"] + ".json").unlink(missing_ok=True)
            continue
        context = value.get("context", {})
        saved = worker._queued_cfg(cfg, value["env"]) if context else cfg
        text = value["output"]["result"]
        if context.get("kind") == "reviewer":
            if value["output"].get("is_error"):
                worker.review_failed(saved, context["plan"], claude.ClaudeError(text))
            else:
                worker.finish_review(saved, context["plan"], text)
            worker.memory_notice(saved, context["plan"]["run_id"], context["plan"])
        elif context.get("kind") == "module":
            payload = {"usage": value["output"].get("usage", {})}
            if value["output"].get("is_error") and replied(value["output"]):
                payload["paid_reply"] = True
            payload["error" if value["output"].get("is_error") else "text"] = text
            modreview._receive(saved, context["step"], payload)
        elif context.get("kind") == "triggers" and not value["output"].get("is_error"):
            try:
                worker.finish_triggers(saved, context["batch"], value["output"])
            except claude.ClaudeError as error:
                value["effect_error"] = str(error)
        complete(cfg, value)
        if value.get("replaces"):
            cfg.path("paid-replies", value["replaces"] + ".json").unlink(missing_ok=True)
