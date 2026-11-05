"""Tools-free Cursor plan calls through the separately installed Node helper."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from contextvars import ContextVar
from pathlib import Path

from . import claude
from . import config as C
from .safety import redact
from .state import atomic_write

ROUTE = ContextVar("cursor_run", default=None)

# No keys, provider overrides, Node injection, or inherited host configuration reach the SDK.
CHILD_ENV = frozenset(
    (
        "PATH",
        "HOME",
        "USERPROFILE",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "APPDATA",
        "LOCALAPPDATA",
        "PATHEXT",
    )
)


def child_env(env: dict) -> dict:
    return {key: value for key, value in env.items() if key in CHILD_ENV}


def install_env(env: dict, node: str) -> dict:
    network = {
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
        "no_proxy",
        "NODE_EXTRA_CA_CERTS",
    }
    clean = {**child_env(env), **{k: v for k, v in env.items() if k in network}}
    clean["PATH"] = str(Path(node).parent) + os.pathsep + clean.get("PATH", "")
    return clean


def helper(root: Path | None = None) -> Path:
    return (root or C.ROOT) / "cursor/sdk_runner/runner.mjs"


def prerequisites(env: dict, *, root: Path | None = None, installing: bool = False) -> tuple[str, str]:
    node = None
    if not installing:
        try:
            saved = json.loads((helper(root).parent / "node-path.json").read_text(encoding="utf-8"))
            candidate = saved.get("node") if isinstance(saved, dict) else None
            if isinstance(candidate, str) and Path(candidate).is_absolute() and Path(candidate).is_file():
                node = candidate
        except (OSError, ValueError):
            pass
    node = node or shutil.which("node", path=env.get("PATH"))
    if not node:
        raise claude.AuthError("Cursor SDK paused: Node 22.13 or later is required; Node was not found.")
    try:
        out = subprocess.run(
            [node, "--version"],
            env=child_env(env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=C.CLI_CHECK_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise claude.AuthError("Cursor SDK paused: could not check Node version.") from exc
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)\s*", out.stdout)
    if out.returncode or not match or tuple(map(int, match.groups())) < C.CURSOR_NODE_MINIMUM:
        raise claude.AuthError("Cursor SDK paused: Node 22.13 or later is required.")
    npm = shutil.which("npm", path=env.get("PATH")) if installing else ""
    if installing and not npm:
        raise claude.AuthError("Cursor SDK paused: npm is required; npm was not found.")
    return node, npm


def _login_status(node: str, root: Path | None, env: dict) -> bool:
    proc = subprocess.run(
        [node, str(helper(root)), "status"],
        env=child_env(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=C.CLI_CHECK_TIMEOUT_S,
        check=False,
    )
    reply = json.loads(proc.stdout)
    if proc.returncode or not isinstance(reply, dict) or reply.get("status") not in ("logged-in", "logged-out"):
        raise claude.ClaudeError("Cursor SDK helper unavailable; repeat setup.")
    return reply["status"] == "logged-in"


def check(cfg: C.Config, *, root: Path | None = None) -> tuple[bool, str]:
    try:
        node, _ = prerequisites(cfg.env, root=root) if root is not None else prerequisites(cfg.env)
        if not _login_status(node, root, cfg.env):
            return False, pause_line("login", root=root, node=node)
        return True, "Cursor SDK logged in"
    except (claude.ClaudeError, OSError, ValueError, subprocess.SubprocessError):
        return False, pause_line("login", root=root)


def install(root: Path, env: dict) -> str:
    try:
        node, npm = prerequisites(env, root=root, installing=True)
    except claude.AuthError:
        return pause_line("login", root=root)
    atomic_write(helper(root).parent / "node-path.json", json.dumps({"node": str(Path(node).absolute())}))
    try:
        proc = subprocess.run(
            [npm, "ci", "--ignore-scripts"],
            cwd=helper(root).parent,
            env=install_env(env, node),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=C.SETUP_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise claude.AuthError("Cursor SDK setup failed: npm ci --ignore-scripts could not run.") from exc
    if proc.returncode:
        tail = redact(proc.stderr)[-C.TOOL_ERROR_CHARS :].strip()
        raise claude.AuthError(f"Cursor SDK setup failed: npm ci --ignore-scripts failed. {tail}")
    try:
        logged_in = _login_status(node, root, env)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise claude.AuthError("Cursor SDK helper unavailable; repeat setup.") from exc
    return "" if logged_in else pause_line("login", root=root, node=node)


def model_ids(cfg: C.Config) -> list[str]:
    """Worker/setup only: the helper returns IDs, never model/account metadata."""
    node, _ = prerequisites(cfg.env)
    proc = subprocess.run(
        [node, str(helper()), "models"],
        env=child_env(cfg.env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=C.CLI_CHECK_TIMEOUT_S,
        check=False,
    )
    ids = json.loads(proc.stdout)
    if proc.returncode or not isinstance(ids, list) or any(not isinstance(mid, str) for mid in ids):
        raise claude.ClaudeError("Cursor model list unavailable")
    return ids


def validate(value, schema: dict) -> None:
    """Validate the JSON Schema subset used by the shared review and judge contracts."""
    if "anyOf" in schema:
        for choice in schema["anyOf"]:
            try:
                validate(value, choice)
                return
            except ValueError:
                pass
        raise ValueError("result does not match any allowed shape")
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int, "null": type(None)}
    if kind not in types or type(value) is not types[kind]:
        raise ValueError("result has an invalid type")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("result has an invalid value")
    if kind == "object":
        props = schema.get("properties", {})
        if set(schema.get("required", [])) - value.keys():
            raise ValueError("result is missing required fields")
        if schema.get("additionalProperties") is False and value.keys() - props.keys():
            raise ValueError("result has unexpected fields")
        for key in value.keys() & props.keys():
            validate(value[key], props[key])
    elif kind == "array":
        for item in value:
            validate(item, schema["items"])


def token_usage(value) -> dict | None:
    keys = {
        "inputTokens": "input_tokens",
        "outputTokens": "output_tokens",
        "cacheReadTokens": "cache_read_input_tokens",
        "cacheWriteTokens": "cache_creation_input_tokens",
    }
    if not isinstance(value, dict) or any(type(value.get(k)) is not int or value[k] < 0 for k in keys):
        return None
    return {target: value[key] for key, target in keys.items()}


def parse_result(text: str) -> dict:
    """Accept a single terminal object, optionally preceded by unambiguous prose."""
    if not isinstance(text, str):
        raise ValueError("Cursor result must be JSON object text")
    start = text.find("{")
    if start < 0:
        raise ValueError("Cursor result has no JSON object")
    prefix = text[:start].strip()
    if prefix:
        if prefix.startswith('"') or "```" in prefix or any(char in prefix for char in "[]}"):
            raise ValueError("Cursor result prefix is not unambiguous prose")
        try:
            json.JSONDecoder().raw_decode(prefix)
        except ValueError:
            pass
        else:
            raise ValueError("Cursor result contains another JSON value")
    value, end = json.JSONDecoder().raw_decode(text, start)
    if not isinstance(value, dict) or text[end:].strip():
        raise ValueError("Cursor result must end with exactly one JSON object")
    return value


class CursorAdmission(claude.AuthError):
    pass


class CursorRejected(claude.ClaudeError):
    pass


class CursorTimeout(claude.ClaudeError):
    pass


def role_for(label: str) -> str:
    name = label.split(":", 1)[0]
    if name in ("revise", "reviser"):
        return "reviser"
    return (
        "judge"
        if name in ("judge", "judge-note", "judge-tidy", "eval")
        else "cheap"
        if name in ("triggers", "cheap")
        else "reviewer"
    )


def pause_line(kind: str = "login", *, root: Path | None = None, node: str = "node") -> str:
    if kind == "call":
        return (
            "Cursor learning paused: Grok call failed or returned an invalid reply; "
            "check Grok plan availability and /nowitknows-status."
        )
    login = shlex.join([node, str(helper(root)), "login"])
    return (
        "Cursor learning paused: SDK not logged in or unavailable; install Node 22.13+ and npm, "
        f"run /nowitknows-setup, then `{login}` for SDK login and check Grok plan availability."
    )


def status_version() -> str:
    return json.loads((C.ROOT / ".cursor-plugin/plugin.json").read_text(encoding="utf-8"))["version"]


def _status(cfg: C.Config, label: str, model: str, state: str, kind: str = "") -> None:
    atomic_write(
        cfg.path("cursor-status.json"),
        json.dumps(
            {
                "job": label,
                "runner": "cursor",
                "model": model,
                "state": state,
                "reason": pause_line(kind) if kind else "",
                "reason_kind": kind,
                "version": status_version(),
                "ts": time.time(),
            }
        ),
    )


def background(cfg: C.Config, **request) -> dict:
    from . import cursor_models

    target = cursor_models.target(cfg)
    label = request.get("label", "run")
    token = ROUTE.set({"job": label, "runner": "cursor", "model": target})
    try:
        ok, detail = check(cfg)
        if not ok:
            raise claude.AuthError(detail)
        output = run(cfg, **{**request, "model": target})
        if output.get("is_error"):
            raise claude.ClaudeError("Grok refused the job")
    except CursorAdmission:
        raise
    except CursorTimeout as exc:
        _status(cfg, label, target, "retry")
        failure = CursorTimeout("Cursor Grok call timed out; learning will retry after backoff.")
        failure.paid_reply = getattr(exc, "paid_reply", False)
        raise failure from exc
    except claude.ClaudeError as exc:
        kind = "login" if isinstance(exc, claude.AuthError) else "call"
        line = pause_line(kind)
        _status(cfg, label, target, "paused", kind)
        error = claude.AuthError if kind == "login" else type(exc)
        failure = error(line)
        failure.paid_reply = getattr(exc, "paid_reply", False)
        raise failure from exc
    finally:
        ROUTE.reset(token)
    _status(cfg, label, target, "ready")
    return output


def run(cfg: C.Config, **request) -> dict:
    for attempt in range(2):
        routing = ROUTE.get()
        if routing is not None:
            routing.pop("receipt", None)
        try:
            return _run_once(cfg, **request)
        except CursorTimeout as error:
            settlement = request.get("label", "").startswith(("judge:", "revise:"))
            if attempt == 1 or (settlement and getattr(error, "paid_reply", False)):
                raise
    raise AssertionError("unreachable")


def _run_once(
    cfg: C.Config,
    *,
    prompt: str,
    system: str,
    model: str,
    schema: dict | None = None,
    timeout: int | None = None,
    label: str = "run",
    run_id: str | None = None,
    tools: str = "",
    allowed=None,
    mcp=None,
    max_turns=None,
    record: bool = True,
    params: tuple | None = None,
) -> dict:
    from . import paid, worker
    from .codex_client import AdmissionError

    if cfg.host != "cursor" or tools or allowed or mcp or max_turns not in (None, 1):
        raise claude.ClaudeError("Cursor SDK requires a Cursor tools-free request.")
    from . import cursor_models

    if cursor_models.version(model) is None:
        raise claude.AuthError("Cursor background requests require a Grok model.")
    if schema is None:
        raise claude.ClaudeError("Cursor SDK requires a result schema.")
    guard = worker.spending_guard(cfg, worker.guarded(cfg).allow_extra_usage)
    try:
        guard()
    except AdmissionError as exc:
        raise CursorAdmission(str(exc)) from exc
    node, _ = prerequisites(cfg.env)
    role = role_for(label)
    params = cursor_models.parameters(role)
    if timeout is None or timeout == C.JUDGE_TIMEOUT_S:
        timeout = C.CURSOR_JUDGE_TIMEOUT_S if role == "judge" else C.CURSOR_REVIEWER_TIMEOUT_S
    selection = {"id": model, "params": [dict(p) for p in params]}
    if role != "judge":
        guidance = C.prompt("cursor_models.md").split("## reviewer\n", 1)[1].split("\n## ", 1)[0].strip()
        system += "\n\n" + guidance
    body = "\nReturn only JSON matching this schema:\n" + json.dumps(schema) if schema else ""
    request = {"prompt": system + body + "\n\n" + prompt, "model": selection, "timeout": timeout}
    started = time.time()
    usage = None
    receipt = None
    rejected = False
    failure = {"subtype": "error_cursor", "is_error": True, "num_turns": 1}
    try:
        proc = claude.run_child(
            [node, str(helper())],
            input=json.dumps(request),
            timeout=timeout + C.CHILD_STOP_GRACE_S,
            env=child_env(cfg.env),
            cwd=str(helper().parent),
        )
        reply = json.loads(proc.stdout)
        timed_out = (
            isinstance(reply, dict)
            and reply.get("status") == "error"
            and (reply.get("message") == "Cursor run timed out.")
        )
        if timed_out:
            failure["subtype"] = "error_timeout"
            # A retry must not overwrite the enclosing modular/reviewer step receipt.
            receipt = {"id": uuid.uuid4().hex, "host": cfg.host}
        # Capture spend before validating status, reply text or its schema.
        if isinstance(reply, dict):
            usage = token_usage(reply.get("usage"))
        if usage is not None:
            failure["usage"] = usage
            receipt = paid.receive(
                cfg,
                label,
                model,
                started,
                failure,
                run_id,
                receipt=receipt,
                pending=True,
            )
        if timed_out:
            raise subprocess.TimeoutExpired([node, str(helper())], timeout)
        if isinstance(reply, dict) and reply.get("message") == "Cursor run refused: model identity is not proven Grok.":
            rejected = True
            returned = reply.get("model")
            if (
                receipt is not None
                and isinstance(returned, dict)
                and isinstance(returned.get("id"), str)
                and cursor_models.version(returned["id"]) is None
            ):
                receipt["returned_model"] = redact(returned["id"])
        if isinstance(reply, dict) and reply.get("message") in (
            "Cursor run refused: a tool-call event appeared.",
            "Cursor run refused: Max Mode is enabled.",
        ):
            rejected = True
        if not isinstance(reply, dict) or proc.returncode or reply.get("status") != "finished":
            raise ValueError("Cursor SDK run failed; check setup, login and plan limits.")
        rejected = True  # A returned model response, rather than an unavailable helper/login.
        if reply.get("maxMode") is not False or not isinstance(reply.get("model"), dict):
            raise ValueError("Cursor SDK run refused: Max Mode or model status was not safe.")
        if reply["model"].get("maxMode") is True:
            raise ValueError("Cursor SDK run refused: Max Mode is enabled.")
        if not isinstance(reply["model"].get("id"), str) or not reply["model"]["id"]:
            raise ValueError("Cursor SDK run failed: result model is missing.")
        if cursor_models.version(reply["model"]["id"]) is None:
            if receipt is not None:
                receipt["returned_model"] = redact(reply["model"]["id"])
            raise ValueError("Cursor SDK run refused: result model is outside the Grok family.")
        text = reply["result"]
        value = parse_result(text)
        validate(value, schema)
        if usage is None and worker.guarded(cfg).review_daily_tokens > 0:
            raise ValueError("Cursor SDK run failed: token usage is missing for a budgeted run.")
    except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            failure["subtype"] = "error_timeout"
            receipt = receipt or {"id": uuid.uuid4().hex, "host": cfg.host}
        if usage is not None:
            # Failed modular calls also use record=False. They never reach
            # modreview.answer, so settle their spend here in either mode.
            # Only the redacted model ID from a family refusal enters the receipt.
            paid.receive(
                cfg,
                label,
                model,
                started,
                failure,
                run_id,
                receipt=receipt,
            )
        error = (
            CursorTimeout
            if isinstance(exc, subprocess.TimeoutExpired)
            else CursorRejected
            if rejected
            else claude.ClaudeError
        )
        failure_error = error(f"{label}: {exc}")
        failure_error.paid_reply = rejected or paid.spent(usage)
        raise failure_error from exc
    output = {
        "result": text,
        "structured_output": value,
        "usage": usage or {},
        "subtype": "success",
        "is_error": False,
        "num_turns": 1,
        "model": reply["model"]["id"],
    }
    paid.receive(cfg, label, output["model"], started, output, run_id, receipt=receipt, record=record)
    return output
