"""Subscription-only native Codex app-server transport, using the standard library."""

from __future__ import annotations

import json
import math
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from os.path import realpath
from pathlib import Path

from . import config as C

# The native temporary structured-thread path uses these switches to remove tools and ambient context.
# Source: openai/codex core config schema; installed CLI 0.162.0-alpha.17.2, generated experimental schema.
REVIEW_CONFIG = {
    "approval_policy": "never",
    "approvals_reviewer": "user",
    "sandbox_mode": "read-only",
    "web_search": "disabled",
    "project_doc_max_bytes": 0,
    "skills.include_instructions": False,
    "cloud.skills.enabled": False,
    "tools.experimental_request_user_input.enabled": False,
    "tools.update_plan.enabled": False,
    **{
        "features." + name: False
        for name in (
            "apps",
            "plugins",
            "remote_plugin",
            "code_mode",
            "code_mode_only",
            "context_management",
            "current_time_reminder",
            "deferred_executor",
            "enable_fanout",
            "goals",
            "image_generation",
            "memories",
            "multi_agent",
            "multi_agent_v2",
            "request_permissions_tool",
            "shell_snapshot",
            "shell_tool",
            "standalone_web_search",
            "token_budget",
            "tool_suggest",
            "unified_exec",
            "view_image",
            "skill_mcp_dependency_install",
        )
    },
}
SAFE_ENV = (
    "PATH",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "APPDATA",
    "LOCALAPPDATA",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "CODEX_CA_CERTIFICATE",
    "__CF_USER_TEXT_ENCODING",
)
READ_METHODS = frozenset(
    (
        "account/read",
        "account/rateLimits/read",
        "skills/list",
        "hooks/list",
        "config/read",
        "configRequirements/read",
        "model/list",
    )
)
STRICT_GAPS = (
    "The native turn/start schema has no verified per-turn credit prohibition.",
    "A usage snapshot cannot reserve included allowance against this turn or concurrent sessions.",
    "normalModelSlug describes a quota alias, not an exhaustive model entitlement mapping.",
)
TURN_ERROR_CODES = frozenset(
    (
        "contextWindowExceeded",
        "sessionBudgetExceeded",
        "usageLimitExceeded",
        "rateLimitExceeded",
        "flexUnavailable",
        "serverOverloaded",
        "cyberPolicy",
        "misalignmentPolicyViolation",
        "tooManyDenials",
        "internalServerError",
        "unauthorized",
        "badRequest",
        "threadRollbackFailed",
        "sandboxError",
        "other",
        "httpConnectionFailed",
        "responseStreamConnectionFailed",
        "responseStreamDisconnected",
        "responseTooManyFailedAttempts",
        "activeTurnNotSteerable",
    )
)


def _same_path(left, right) -> bool:
    if not C.WINDOWS or not all(isinstance(value, (str, os.PathLike)) for value in (left, right)):
        return left == right
    if not Path(left).is_absolute() or not Path(right).is_absolute():
        return left == right
    import ntpath

    def identity(value):
        value = ntpath.normcase(realpath(value))
        return "\\\\" + value[8:] if value.startswith("\\\\?\\unc\\") else value.removeprefix("\\\\?\\")

    return identity(left) == identity(right)


class CodexError(RuntimeError):
    """A bounded native transport failure; usage already observed remains available."""

    def __init__(self, message: str, *, usage: dict | None = None, status: dict | None = None, started: bool = False):
        super().__init__(message)
        self.started = started
        self.usage = dict(usage or {})
        self.status = dict(status or {})


class AdmissionError(CodexError):
    """Learning is paused before a model turn, without changing account settings."""


def child_environment(env: dict) -> dict:
    """Keep OS connectivity, discard provider/session credentials, and pin the native profile explicitly."""
    home = Path.home()
    profile = Path(env.get("CODEX_HOME", home / ".codex"))
    if not profile.is_absolute():
        raise AdmissionError("CODEX_HOME must name an absolute native profile path")
    result = {k: env[k] for k in SAFE_ENV if k in env}
    result.update(HOME=str(home), CODEX_HOME=str(profile.resolve()), NOWITKNOWS_CHILD="1")
    if os.name == "nt":
        result["USERPROFILE"] = str(home)
    return result


def _finite(value) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except (ValueError, OverflowError):
        return False


def _package_version() -> str:
    try:
        version = json.loads((C.ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8")).get("version")
        return version if isinstance(version, str) and version else "unknown"
    except (OSError, ValueError, AttributeError):
        return "unknown"


def find_codex(env: dict) -> str | None:
    selected = C.env("NOWITKNOWS_CODEX_BIN", C.environment(env))
    if selected:
        return shutil.which(str(selected), path=env.get("PATH"))
    found = shutil.which("codex", path=env.get("PATH"))
    if found or sys.platform != "darwin":
        return found
    relative = Path("ChatGPT.app/Contents/Resources/codex-cli/bin/codex")
    for applications in (Path("/Applications"), Path.home() / "Applications"):
        found = shutil.which(str(applications / relative), path=env.get("PATH"))
        if found:
            return found
    return None


def allowance(account: dict, rate: dict, model: str, allow_extra_usage: bool = True) -> dict:
    """Evaluate native read results; percentages and credits never establish strict entitlement."""
    reasons, windows, credits = [], [], []
    status = {
        "admit": False,
        "policy": "subscription_with_extra_usage" if allow_extra_usage else "included_only",
        "allow_extra_usage": allow_extra_usage,
        "model": model,
        "ordinary_usage_allowed": None,
        "strict_credit_prevention": False,
        "model_applicability_proven": False,
        "capability_gaps": list(STRICT_GAPS),
        "reasons": reasons,
        "windows": windows,
        "source": "native account/read and account/rateLimits/read",
    }
    if type(allow_extra_usage) is not bool:
        reasons.append("allow_extra_usage must be a boolean")
    if type(model) is not str or not model.strip():
        reasons.append("An explicit target model is required")
    identity = account.get("account") if type(account) is dict else None
    if type(identity) is not dict or identity.get("type") != "chatgpt" or account.get("requiresOpenaiAuth") is not True:
        reasons.append("Native account/read must report the user's own ChatGPT login")
    if type(rate) is not dict or "error" in rate:
        rate = {}
        reasons.append("Native allowance read failed or returned malformed data")
    ordinary = rate.get("ordinaryUsageAllowed")
    if type(ordinary) is not bool:
        reasons.append("Native ordinaryUsageAllowed is unavailable or not a boolean")
    else:
        status["ordinary_usage_allowed"] = ordinary
    buckets = rate.get("rateLimitsByLimitId")
    if type(buckets) is not dict or not buckets:
        reasons.append("Native metered quota inventory is unavailable")
        buckets = {}
    legacy = rate.get("rateLimits")
    if (
        type(legacy) is not dict
        or type(legacy.get("limitId")) is not str
        or legacy["limitId"] not in buckets
        or buckets.get(legacy["limitId"]) != legacy
    ):
        reasons.append("Native legacy and metered quota views disagree or are missing")
    for key, bucket in buckets.items():
        if type(bucket) is not dict or type(key) is not str or not key or bucket.get("limitId") != key:
            reasons.append("Malformed native quota identifier")
            continue
        if bucket.get("spendControlReached") is not False:
            reasons.append(f"{key}: native spend control reached or unavailable")
        reached = bucket.get("rateLimitReachedType")
        if reached not in (None, "rate_limit_reached"):
            reasons.append(f"{key}: native usage or credit limit reached, or unknown reached state")
        if "rateLimitReachedType" not in bucket:
            reasons.append(f"{key}: native reached state unavailable")
        seen = False
        for name in ("primary", "secondary"):
            window = bucket.get(name)
            if window is None:
                continue
            if type(window) is not dict:
                reasons.append(f"{key}.{name}: malformed quota window")
                continue
            used = window.get("usedPercent")
            if not _finite(used) or not 0 <= used <= 100:
                reasons.append(f"{key}.{name}: quota usage must be finite and between zero and 100")
                continue
            for field in ("windowDurationMins", "resetsAt"):
                value = window.get(field)
                if type(value) is not int or value <= 0:
                    reasons.append(f"{key}.{name}: quota {field} must be a positive integer")
            seen = True
            windows.append({"limit_id": key, "window": name, "used_percent": used})
            if ordinary is True and (used >= 100 or reached is not None):
                reasons.append(f"{key}: included permission conflicts with exhausted quota")
        if not seen:
            reasons.append(f"{key}: native quota windows unavailable")
        individual = bucket.get("individualLimit")
        if individual is not None:
            remaining = individual.get("remainingPercent") if type(individual) is dict else None
            if not _finite(remaining) or not 0 < remaining <= 100:
                reasons.append(f"{key}: individual spend allowance exhausted or malformed")
        credit = bucket.get("credits")
        if credit is not None:
            if (
                type(credit) is not dict
                or type(credit.get("hasCredits")) is not bool
                or type(credit.get("unlimited")) is not bool
            ):
                reasons.append(f"{key}: malformed native credit flags")
            else:
                credits.append(credit["hasCredits"] or credit["unlimited"])
        elif ordinary is False:
            reasons.append(f"{key}: extra usage allowance unavailable")
    if ordinary is False and (not credits or not all(credits)):
        reasons.append("Included usage is unavailable and native credit allowance is exhausted or unknown")
    if allow_extra_usage is False:
        reasons.append("Included-only learning paused: native per-turn credit prevention is not proven")
    status["admit"] = not reasons
    status["state"] = "ready" if status["admit"] else "paused"
    return status


def normalize_usage(value: dict) -> dict:
    """Split native cached input from inclusive input; reasoning is already part of output."""
    if type(value) is not dict:
        raise CodexError("Native token usage is missing")
    fields = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens")
    if any(type(value.get(k)) is not int or value[k] < 0 for k in fields):
        raise CodexError("Native token usage contains missing, negative, or non-integer counts")
    write = value.get("cacheWriteInputTokens", 0)
    if (
        type(write) is not int
        or write < 0
        or value["cachedInputTokens"] + write > value["inputTokens"]
        or value["reasoningOutputTokens"] > value["outputTokens"]
        or value["totalTokens"] != value["inputTokens"] + value["outputTokens"]
    ):
        raise CodexError("Native token usage counts are inconsistent")
    return {
        "input_tokens": value["inputTokens"] - value["cachedInputTokens"] - write,
        "cache_read_input_tokens": value["cachedInputTokens"],
        "cache_creation_input_tokens": write,
        "output_tokens": value["outputTokens"],
        "reasoning_output_tokens": value["reasoningOutputTokens"],
        "total_tokens": value["totalTokens"],
    }


class Client:
    """One serialized app-server connection; catalog reads never create model threads."""

    def __init__(
        self, env: dict, cwd: Path | str, allow_extra_usage: bool = True, *, timeout=C.JUDGE_TIMEOUT_S, guard=None
    ):
        if type(allow_extra_usage) is not bool:
            raise AdmissionError("allow_extra_usage must be a boolean")
        self.env = child_environment(env)
        self.cwd = Path(cwd).resolve()
        self.allow_extra_usage = allow_extra_usage
        self.guard = guard  # the caller's check of its settings as saved now; raises AdmissionError to refuse
        self.timeout = self._timeout(timeout)
        self.exe = find_codex(
            {**self.env, "NOWITKNOWS_CODEX_BIN": C.env("NOWITKNOWS_CODEX_BIN", C.environment(env), "")}
        )
        if not self.exe:
            raise AdmissionError("The user's own Codex CLI was not found; set NOWITKNOWS_CODEX_BIN")
        self.status = {
            "state": "unchecked",
            "allow_extra_usage": allow_extra_usage,
            "policy": "subscription_with_extra_usage" if allow_extra_usage else "included_only",
        }
        self.usage: dict = {}
        self._proc = None
        self._tmp = None
        self._seq = 0
        self._queue: queue.Queue = queue.Queue()
        self._pending: deque = deque()
        self._lock = threading.RLock()
        self._active = None
        self._submitted_thread = None
        self._model = None
        self._last_method = "initialize"
        self.started = False

    @staticmethod
    def _timeout(value):
        if not _finite(value) or value <= 0:
            raise ValueError("timeout must be finite and positive")
        return value

    def __enter__(self):
        with self._lock:
            if self._proc is not None:
                raise CodexError("Client is already open")
            self._tmp = tempfile.TemporaryDirectory(prefix="nowitknows-codex-")
            # The CLI reads authentication itself. No auth file, token, or login helper is read by nowitknows.
            argv = [
                self.exe,
                "--no-daemon",
                "-c",
                'forced_login_method="chatgpt"',
                "-c",
                'model_provider="openai"',
                "-c",
                "features.hooks=false",
            ]
            try:
                self._proc = subprocess.Popen(
                    [*argv, "app-server", "--stdio"],
                    env=self.env,
                    cwd=self.cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    bufsize=1,
                    start_new_session=os.name != "nt",
                )
                self._reader = threading.Thread(target=self._read, args=(self._proc,), daemon=True)
                self._reader.start()
                initialized = self._result(
                    self._rpc(
                        "initialize",
                        {
                            "clientInfo": {"name": "nowitknows", "version": _package_version()},
                            "capabilities": {"experimentalApi": True},
                        },
                    )
                )
                if not _same_path(initialized.get("codexHome"), self.env["CODEX_HOME"]):
                    raise AdmissionError("Native app-server profile identity differs from explicit CODEX_HOME")
                self.status["native_user_agent"] = initialized.get("userAgent")
                self._send({"method": "initialized"})
            except (OSError, UnicodeError) as exc:
                self.close()
                raise CodexError("Could not launch the user's native Codex app-server") from exc
            except BaseException:
                self.close()
                raise
            return self

    def __exit__(self, *_):
        self.close()

    def _read(self, proc):
        try:
            for line in proc.stdout:
                try:
                    obj = json.loads(line, parse_constant=lambda _: None)
                    if type(obj) is not dict:
                        raise ValueError
                    self._queue.put(obj)
                except (ValueError, UnicodeError):
                    self._queue.put(CodexError("Malformed native JSON-RPC output"))
                    return
        except (OSError, UnicodeError):
            self._queue.put(CodexError("Could not read native app-server output"))
        finally:
            self._queue.put(CodexError("Native app-server exited before completing the request"))

    def _send(self, obj):
        if self._proc is None:
            raise CodexError("Client context is not open")
        try:
            self._proc.stdin.write(json.dumps(obj, allow_nan=False) + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise CodexError("Could not send native app-server request", usage=self.usage) from exc

    def _next(self, deadline, stage):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodexError(f"Native app-server timed out waiting for {stage}", usage=self.usage)
        try:
            obj = self._queue.get(timeout=remaining)
        except queue.Empty:
            raise CodexError(f"Native app-server timed out waiting for {stage}", usage=self.usage) from None
        if isinstance(obj, CodexError):
            obj.usage = dict(self.usage)
            raise obj
        if "method" in obj and "id" in obj:
            self._send(
                {"id": obj["id"], "error": {"code": -32601, "message": "nowitknows grants no tools or approvals"}}
            )
            raise CodexError("Native child requested a tool, approval, or credential callback", usage=self.usage)
        self._capture_usage(obj)
        return obj

    def _capture_usage(self, obj):
        params = obj.get("params")
        if type(params) is not dict:
            return
        if (
            self.started
            and self._active is None
            and obj.get("method") == "turn/started"
            and params.get("threadId") == self._submitted_thread
        ):
            turn = params.get("turn")
            if type(turn) is dict and type(turn.get("id")) is str and turn["id"]:
                self._active = (self._submitted_thread, turn["id"])
        if not self._active:
            return
        if (
            obj.get("method") == "model/rerouted"
            and (params.get("threadId"), params.get("turnId")) == self._active
            and params.get("toModel") != self._model
        ):
            raise CodexError("Native model turn rerouted away from the requested model", usage=self.usage)
        if (
            obj.get("method") == "thread/tokenUsage/updated"
            and (params.get("threadId"), params.get("turnId")) == self._active
        ):
            usage = params.get("tokenUsage")
            self.usage = normalize_usage(usage.get("total") if type(usage) is dict else None)

    def _rpc(self, method, params, *, deadline=None):
        self._last_method = method
        deadline = deadline or time.monotonic() + min(self.timeout, C.CODEX_RPC_TIMEOUT_S)
        self._seq += 1
        rid = self._seq
        if method == "turn/start":
            # Submission is a conservative billing boundary, even if its reply is lost.
            self.started = True
        self._send({"id": rid, "method": method, "params": params})
        while True:
            obj = self._next(deadline, f"RPC {method}")
            if obj.get("id") == rid and type(obj.get("id")) is int and ("result" in obj or "error" in obj):
                return obj
            if "method" in obj:
                self._pending.append(obj)
            else:
                raise CodexError("Native RPC response identity mismatch", usage=self.usage)

    def _result(self, envelope):
        if "error" in envelope or type(envelope.get("result")) is not dict:
            code = (envelope.get("error") or {}).get("code") if type(envelope.get("error")) is dict else None
            detail = f" (code {code})" if type(code) is int else ""
            error = CodexError(f"Native RPC failed for {self._last_method}{detail}", usage=self.usage)
            error.rpc_error = envelope.get("error")
            raise error
        return envelope["result"]

    def call(self, method: str, params: dict | None = None, *, timeout=None) -> dict:
        """Return an unchanged JSON-RPC read envelope; model and account mutations are private."""
        if method not in READ_METHODS:
            raise CodexError("Public Client.call permits native read operations only")
        with self._lock:
            try:
                duration = min(self.timeout, C.CODEX_RPC_TIMEOUT_S) if timeout is None else timeout
                deadline = time.monotonic() + self._timeout(duration)
                envelope = self._rpc(method, params or {}, deadline=deadline)
                self._result(envelope)
                self._pending.clear()
                return envelope
            except CodexError:
                self.close()
                raise

    def _preflight(self, model):
        if self.guard is not None:  # settings saved while this client started may have turned the call off
            self.guard()
        config = self._result(self.call("config/read", {"includeLayers": False})).get("config")
        if (
            type(config) is not dict
            or config.get("forced_login_method") != "chatgpt"
            or config.get("model_provider") != "openai"
        ):
            raise AdmissionError("Native effective config did not retain forced ChatGPT login and OpenAI provider")
        providers = config.get("model_providers", {})
        if (
            type(providers) is not dict
            or "openai" in providers
            or any(config.get(k) for k in ("openai_base_url", "model_catalog_json"))
            or config.get("chatgpt_base_url")
            not in (None, "https://chatgpt.com/backend-api", "https://chatgpt.com/backend-api/")
        ):
            raise AdmissionError("Native OpenAI provider or catalog has an unverified override")
        hooks = self._result(self.call("hooks/list", {"cwds": [str(self.cwd)]})).get("data")
        if type(hooks) is not list or not hooks:
            raise AdmissionError("Native hook inventory is unavailable")
        hooks_off = (config.get("features") or {}).get("hooks") is False
        for group in hooks:
            if type(group) is not dict or group.get("errors") or type(group.get("hooks")) is not list:
                raise AdmissionError("Native hook inventory has errors")
            for hook in group["hooks"]:
                if type(hook) is not dict or type(hook.get("enabled")) is not bool:
                    raise AdmissionError("Native hook enablement is unknown")
                if (
                    not hooks_off
                    and hook["enabled"]
                    and not (hook.get("isManaged") is True and hook.get("trustStatus") == "managed")
                ):
                    raise AdmissionError("Ambient nonmanaged hooks remain enabled; no hook trust bypass is permitted")
        account = self._result(self.call("account/read", {"refreshToken": False}))
        rate = self._result(self.call("account/rateLimits/read", {"excludeResetCreditDetails": True}))
        self.status.update(allowance(account, rate, model, self.allow_extra_usage))
        if not self.status["admit"]:
            raise AdmissionError("; ".join(self.status["reasons"]), status=self.status)
        return config

    def _review_config(self, config, system):
        overrides = dict(REVIEW_CONFIG)
        instructions = Path(self._tmp.name) / "instructions.txt"
        instructions.write_text(system, encoding="utf-8")
        overrides["model_instructions_file"] = str(instructions)
        servers = config.get("mcp_servers", {})
        if type(servers) is not dict:
            raise AdmissionError("Native MCP configuration is malformed")
        # RPC override keys split dots literally; nested values preserve arbitrary server names.
        overrides["mcp_servers"] = {name: {"enabled": False} for name in servers}
        groups = self._result(
            self.call(
                "skills/list",
                {"cwds": [str(self.cwd)], "forceReload": True},
            )
        ).get("data")
        if type(groups) is not list or not groups:
            raise AdmissionError("Native skill inventory is unavailable")
        disabled = []
        for group in groups:
            if type(group) is not dict or group.get("errors") or type(group.get("skills")) is not list:
                raise AdmissionError("Native skill inventory has errors")
            for skill in group["skills"]:
                if (
                    type(skill) is not dict
                    or type(skill.get("enabled")) is not bool
                    or type(skill.get("path")) is not str
                    or not Path(skill["path"]).is_absolute()
                ):
                    raise AdmissionError("Native skill metadata is malformed")
                disabled.append({"path": skill["path"], "enabled": False})
        overrides["skills.config"] = disabled
        return overrides

    def _start_review(self, system, model):
        config = self._preflight(model)
        overrides = self._review_config(config, system)
        start = self._result(
            self._rpc(
                "thread/start",
                {
                    "model": model,
                    "modelProvider": "openai",
                    "cwd": str(self.cwd),
                    "ephemeral": True,
                    "approvalPolicy": "never",
                    "approvalsReviewer": "user",
                    "sandbox": "read-only",
                    "baseInstructions": system,
                    "developerInstructions": "Do not use tools. Return the requested answer.",
                    "config": overrides,
                    "dynamicTools": [],
                    "environments": [],
                    "serviceTier": "default",
                    "allowProviderModelFallback": False,
                },
            )
        )
        sandbox = start.get("sandbox")
        if (
            start.get("model") != model
            or start.get("modelProvider") != "openai"
            or start.get("approvalPolicy") != "never"
            or start.get("approvalsReviewer") != "user"
            or type(sandbox) is not dict
            or sandbox.get("type") != "readOnly"
            or sandbox.get("networkAccess", False) is not False
            or not _same_path(start.get("cwd"), str(self.cwd))
        ):
            raise AdmissionError("Native review thread changed model, provider, or read-only/no-approval policy")
        thread = start.get("thread")
        tid = thread.get("id") if type(thread) is dict else None
        if type(tid) is not str or not tid:
            raise CodexError("Native review thread has no identity")
        self._pending.clear()
        return tid

    def check_review(self, model: str) -> dict:
        """Validate an isolated reviewer thread without submitting a model turn."""
        if type(model) is not str or not model.strip():
            raise ValueError("explicit model must be a nonempty string")
        with self._lock:
            self.usage = {}
            self.started = False
            try:
                self._start_review("Do not use tools.", model)
                return {"state": "ready", "model": model}
            except (OSError, UnicodeError) as error:
                self.close()
                raise CodexError(
                    "Native review could not read its own temporary instructions",
                    usage=self.usage,
                    status=self.status,
                    started=self.started,
                ) from error
            except CodexError as exc:
                exc.started = self.started
                self.status.update(state="paused", reason=str(exc))
                exc.status = dict(self.status)
                self.close()
                raise

    def complete(self, system: str, prompt: str, schema: dict | None, model: str, *, timeout=None) -> dict:
        """Run one ephemeral no-tools review; return text and Claude-style usage or an error carrying usage."""
        if type(system) is not str or type(prompt) is not str or type(model) is not str or not model.strip():
            raise ValueError("system, prompt, and explicit model must be strings")
        if schema is not None and type(schema) is not dict:
            raise ValueError("schema must be a JSON object or None")
        json.dumps(schema, allow_nan=False)
        duration = self._timeout(self.timeout if timeout is None else timeout)
        with self._lock:
            self.usage = {}
            self.started = False
            try:
                tid = self._start_review(system, model)
                # Re-read allowance and account immediately before each paid turn, including on a reused Client.
                self._preflight(model)
                deadline = time.monotonic() + duration
                self._submitted_thread = tid
                self._model = model
                if self.guard is not None:  # and while it was admitted
                    self.guard()
                turn = self._result(
                    self._rpc(
                        "turn/start",
                        {
                            "threadId": tid,
                            "model": model,
                            "input": [{"type": "text", "text": prompt}],
                            "outputSchema": schema,
                            "approvalPolicy": "never",
                            "approvalsReviewer": "user",
                            "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                            "environments": [],
                            "serviceTierForTurn": "default",
                        },
                        deadline=deadline,
                    )
                ).get("turn")
                turn_id = turn.get("id") if type(turn) is dict else None
                if type(turn_id) is not str or not turn_id:
                    raise CodexError("Native started turn has no identity")
                if self._active is not None and self._active != (tid, turn_id):
                    raise CodexError("Native turn/start reply disagrees with turn/started identity", usage=self.usage)
                self._active = (tid, turn_id)
                items = {}
                completed = None
                while completed is None or not self.usage or self._pending:
                    stage = "thread/tokenUsage/updated after turn/completed" if completed else "turn/completed"
                    try:
                        msg = self._pending.popleft() if self._pending else self._next(deadline, stage)
                    except CodexError as error:
                        if completed and (completed.get("status") != "completed" or completed.get("error") is not None):
                            raise self._turn_error(completed) from error
                        raise
                    self._capture_usage(msg)
                    params = msg.get("params", {})
                    if type(params) is not dict or params.get("threadId") != tid:
                        continue
                    if msg.get("method") == "item/completed" and params.get("turnId") == turn_id:
                        item = params.get("item")
                        if type(item) is dict:
                            self._item(items, item)
                    if msg.get("method") == "turn/completed":
                        candidate = params.get("turn")
                        if type(candidate) is dict and candidate.get("id") == turn_id:
                            completed = candidate
                            if not self.usage:
                                deadline = min(deadline, time.monotonic() + C.CODEX_RPC_TIMEOUT_S)
                            for item in candidate.get("items", []):
                                self._item(items, item)
                if completed.get("status") != "completed" or completed.get("error") is not None:
                    raise self._turn_error(completed)
                final = [
                    item["text"]
                    for item in items.values()
                    if item.get("type") == "agentMessage" and item.get("phase") in (None, "final_answer")
                ]
                if not final:
                    raise CodexError("Native completed turn supplied no final response", usage=self.usage)
                return {
                    "text": "\n".join(final),
                    "usage": dict(self.usage),
                    "model": model,
                    "thread_id": tid,
                    "turn_id": turn_id,
                    "admission": dict(self.status),
                }
            except (OSError, UnicodeError) as error:
                self.close()
                raise CodexError(
                    "Native review could not read its own temporary instructions",
                    usage=self.usage,
                    status=self.status,
                    started=self.started,
                ) from error
            except CodexError as exc:
                exc.started = self.started
                if self.usage:
                    exc.usage = dict(self.usage)
                self.status.update(state="error" if self.started else "paused", reason=str(exc))
                exc.status = dict(self.status)
                self.close()
                raise
            finally:
                self._active = None
                self._submitted_thread = None
                self._model = None

    def _turn_error(self, turn):
        status = turn.get("status")
        status = status if status in ("completed", "failed", "interrupted") else "invalid status"
        error = turn.get("error")
        info = error.get("codexErrorInfo") if type(error) is dict else None
        code = info if type(info) is str else next(iter(info), None) if type(info) is dict and len(info) == 1 else None
        code = code if code in TURN_ERROR_CODES else "unclassified"
        detail = info.get(code) if type(info) is dict else None
        http = detail.get("httpStatusCode") if type(detail) is dict else None
        http = f", HTTP {http}" if type(http) is int and 100 <= http <= 599 else ""
        usage = "" if self.usage else "; token usage unavailable"
        # Native error text can contain request contents; retain only protocol-defined categories.
        return CodexError(f"Native turn/completed: {status} ({code}{http}){usage}", usage=self.usage)

    def _item(self, items, item):
        if type(item) is not dict or type(item.get("id")) is not str:
            raise CodexError("Malformed native completed item", usage=self.usage)
        if item.get("type") not in ("agentMessage", "userMessage", "reasoning"):
            raise CodexError("Native no-tools turn emitted a tool or unexpected item", usage=self.usage)
        if item.get("type") == "agentMessage" and type(item.get("text")) is not str:
            raise CodexError("Malformed native assistant response", usage=self.usage)
        previous = items.get(item["id"])
        if previous is not None and previous != item:
            raise CodexError("Conflicting native completed item identity", usage=self.usage)
        items[item["id"]] = item

    def close(self):
        """Stop only this child process group; never change or remove native profile data."""
        proc, self._proc = self._proc, None
        if proc is not None:
            if proc.poll() is None:
                try:
                    if os.name != "nt":
                        os.killpg(proc.pid, signal.SIGTERM)
                    else:
                        proc.terminate()
                    proc.wait(timeout=C.SQLITE_TIMEOUT_S)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    if proc.poll() is None:
                        if os.name != "nt":
                            os.killpg(proc.pid, signal.SIGKILL)
                        else:
                            proc.kill()
                        proc.wait(timeout=C.SQLITE_TIMEOUT_S)
            for stream in (proc.stdin, proc.stdout):
                stream.close()
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None


def inventory_skills(env: dict, cwds: list[Path | str], *, timeout=C.CODEX_CATALOG_TIMEOUT_S) -> dict:
    """Read native enabled/path/name/scope metadata without a thread, MCP connection, or model turn."""
    if not cwds:
        raise ValueError("At least one catalog directory is required")
    directories = [str(Path(cwd).resolve()) for cwd in cwds]
    with Client(env, directories[0], timeout=timeout) as client:
        return client.call("skills/list", {"cwds": directories, "forceReload": True}, timeout=timeout)
