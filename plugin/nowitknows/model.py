"""Dispatch shared model requests without changing the Claude transport contract."""

from __future__ import annotations

import time

from . import claude
from . import config as C

CODEX_REQUEST_KEYS = frozenset(
    (
        "prompt",
        "system",
        "model",
        "tools",
        "allowed",
        "mcp",
        "schema",
        "max_turns",
        "timeout",
        "label",
        "run_id",
    )
)


def run(cfg: C.Config, **request) -> dict:
    """Claude requests pass through verbatim; native Codex requests use one tools-free turn."""
    host = C.env("NOWITKNOWS_HOST", cfg.env)
    if host == "cursor":
        from . import cursor_runner

        return cursor_runner.background(cfg, **request)
    return native_run(cfg, host=host, **request)


def native_run(cfg: C.Config, *, host: str, **request) -> dict:
    from . import paid

    if host != "codex":
        return claude.run(cfg, **request)
    if (
        set(request) - CODEX_REQUEST_KEYS
        or any(request.get(k) for k in ("tools", "allowed", "mcp"))
        or request.get("max_turns") not in (None, 1)
    ):
        raise claude.ClaudeError("Codex shared calls require one tools-free turn; use the structured review loop")
    from . import worker  # it imports this module
    from .codex_client import Client, CodexError

    label, target, run_id = request.get("label", "run"), request["model"], request.get("run_id")
    allowed = worker.guarded(cfg).allow_extra_usage  # settings saved since this worker loaded its own may be stricter
    guard = worker.spending_guard(cfg, allowed)  # checked now, and by the client before admission and the turn
    started_at = time.time()
    try:
        guard()
        with Client(cfg.env, cfg.data, allow_extra_usage=allowed, guard=guard) as client:
            response = client.complete(
                request["system"],
                request["prompt"],
                request.get("schema"),
                target,
                timeout=request.get("timeout", C.JUDGE_TIMEOUT_S),
            )
    except CodexError as error:
        usage = dict(error.usage)
        if error.started:
            paid.receive(
                cfg,
                label,
                target,
                started_at,
                {"subtype": "error_codex", "is_error": True, "result": str(error), "usage": usage, "num_turns": 1},
                run_id,
            )
        failure = (claude.ClaudeError if error.started else claude.AuthError)(f"{label}: {error}")
        failure.started, failure.usage, failure.status = error.started, usage, dict(error.status)
        raise failure from error
    output = {**response, "result": response["text"], "subtype": "success", "is_error": False, "num_turns": 1}
    paid.receive(cfg, label, response.get("model", target), started_at, output, run_id)
    return output
