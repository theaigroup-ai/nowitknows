from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import config as C
from .state import log_row

# A child keeps no Claude Code variable of the session it was spawned from except these, which it needs to log in,
# connect, and stay under the org's policy and telemetry the way the user's own CLI does. Sessions export their own
# (the desktop app about 20: host-refreshed auth, account and org ids, a host session id, effort, SDK switches), and
# a child is a separate headless run.
CHILD_KEEP = (
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_SECURESTORAGE_CONFIG_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_GIT_BASH_PATH",
    "CLAUDE_CODE_CLIENT_CERT",
    "CLAUDE_CODE_CLIENT_KEY",
    "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
    "CLAUDE_CODE_CERT_STORE",
    "CLAUDE_CODE_HTTPS_PROXY",
    "CLAUDE_CODE_HTTP_PROXY",
    "CLAUDE_CODE_PROXY_RESOLVES_HOSTS",
    "CLAUDE_CODE_ENABLE_PROXY_AUTH_HELPER",
    "CLAUDE_CODE_PROXY_AUTH_HELPER_TTL_MS",
    "CLAUDE_CODE_FORCE_WINDOWS_CREDMAN",
    "CLAUDE_CODE_MANAGED_SETTINGS_PATH",
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "CLAUDE_CODE_TMPDIR",
    "CLAUDE_TMPDIR",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
)
CHILD_KEEP_PREFIXES = ("CLAUDE_CODE_OTEL_",)  # an org's telemetry export covers background runs too
STRIP_ENV = ("TRACEPARENT",)
# Anything that would point a child at an API key, a gateway, or a cloud provider instead of the user's Claude login,
# plus login overrides a hosted or remote session hands its own CLI: a child uses the stored login or a setup-token.
API_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")
API_ENV = (
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
    "CLAUDE_CODE_API_BASE_URL",
    "CLAUDE_CODE_CUSTOM_OAUTH_URL",
    "CLAUDE_CODE_GATEWAY_TOKEN",
    "CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_OAUTH_SCOPES",
    "CLAUDE_CODE_OAUTH_CLIENT_ID",
    "CLAUDE_CODE_SUBSCRIPTION_TYPE",
    "CLAUDE_CODE_RATE_LIMIT_TIER",
    "CLAUDE_CODE_SESSION_ACCESS_TOKEN",
    "CLAUDE_BG_AUTH_SNAPSHOT_PATH",
    "CLAUDE_SESSION_INGRESS_TOKEN_FILE",
    "USE_STAGING_OAUTH",
    "USE_LOCAL_OAUTH",
)
# A subscription, as `claude auth status` shows it: JSON authMethod claude.ai with one of these plans and a text
# "Login method: <plan name> account" line, or authMethod oauth_token with exactly "Auth token: <SETUP_TOKEN>"
# (`claude setup-token` requires a subscription). Plan names come from the server, so only the no-plan label fails.
PLANS = ("pro", "max", "team", "enterprise")
NO_PLAN = "Claude API account"
SETUP_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
# Text lines that say nothing about who pays. Any other line (a new CLI label included) refuses: fail closed.
NEUTRAL_LINES = (
    "Organization",
    "Email",
    "Proxy",
    "Additional CA cert(s)",
    "mTLS client cert",
    "mTLS client key",
    "Allowed providers",
)
SHOWN_LINES = ("Login", "API key", "API provider", "Anthropic base URL", "Profile", "apiKeyHelper")
FIRST_PARTY_URL = "https://api.anthropic.com"
EXTRA_USAGE_OFF = "(allow_extra_usage is off)"
OFF_FIX = "Or turn background learning off with /plugin configure nowitknows@nowitknows."
LOGIN_FIX = (
    "Background learning uses the CLI's own login, not the desktop app's: run `claude auth login` once in a terminal "
    "(if `claude` is not found there, install it first: curl -fsSL https://claude.ai/install.sh | bash, or on "
    "Windows in PowerShell: irm https://claude.ai/install.ps1 | iex). Skills, "
    "session search, and memory keep working meanwhile."
)
EXPIRED = "Login: Expired"  # what `auth status --text` shows once the CLI has blanked its stored login
OUTDATED = "outdated claude CLI"
# How a build is updated, by the folder its binary resolves into; the native installer's own command is the default
UPDATES = (
    ("/node_modules/", "with `npm install -g @anthropic-ai/claude-code@latest`"),
    ("/Caskroom/", "with `brew upgrade claude-code`"),
    ("/Cellar/", "with `brew upgrade claude-code`"),
    ("/Claude/claude-code/", "by updating the Claude desktop app, which brings this build"),
)
NATIVE = ("/.local/share/claude/", "/.local/bin/", "/.claude/local/")
VERSION = re.compile(r"(?<![\w.])(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z][0-9A-Za-z.]*)?(?![\w.-])")
EXTRA_USAGE_UNSEEN = "no cached account details show whether extra usage is on for this login"
# Settings files whose `env` block the CLI applies even with --setting-sources "" (the global config, verified live)
# or always (managed settings). Registry, MDM, and server-delivered managed settings are not read.
MANAGED_DIRS = {"darwin": "/Library/Application Support/ClaudeCode", "win32": "C:/Program Files/ClaudeCode"}
MODEL_TOKEN_KEYS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
CREATE_NO_WINDOW = 0x08000000


class ClaudeError(RuntimeError):
    pass


class AuthError(ClaudeError):
    """The CLI would not run children on the user's Claude subscription, so none runs."""


def hidden() -> dict:
    """subprocess flags for a console child: on Windows the detached worker has no console, so each would open one."""
    return {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def is_api_env(name: str) -> bool:
    return name in API_ENV or name.startswith(API_ENV_PREFIXES)


def cli_env(env: dict) -> dict:
    """env for the user's own CLI: a CLAUDE_CONFIG_DIR the session set stays as it was; one nowitknows set goes, since
    set at all it sends the macOS CLI to another Keychain entry (`Claude Code-credentials-<hash>`)."""
    env = C.environment(env)
    out = {k: v for k, v in env.items() if k not in (C.CONFIG_DIR_INJECTED_ENV, C.CONFIG_DIR_BASE_ENV)}
    if env.get(C.CONFIG_DIR_INJECTED_ENV) == "1":
        out.pop("CLAUDE_CONFIG_DIR", None)
    return out


def child_env(cfg: C.Config) -> dict:
    """Headless children run only on the user's Claude subscription login, never on an API key or provider."""
    source = cfg.env
    if cfg.cursor_paths is not None and cfg.cursor_paths.isolated:
        # An isolated child must keep its explicit profile instead of using the native home fallback.
        source = {**source, C.CONFIG_DIR_INJECTED_ENV: "0"}
    env = {
        k: v
        for k, v in cli_env(source).items()
        if k not in STRIP_ENV
        and not is_api_env(k)
        and (k in CHILD_KEEP or k.startswith(CHILD_KEEP_PREFIXES) or not k.startswith("CLAUDE"))
    }
    env.update(NOWITKNOWS_CHILD="1", CLAUDE_CODE_DISABLE_AUTO_MEMORY="1", ENABLE_CLAUDEAI_MCP_SERVERS="false")
    return C.dual_env(env)


def default_config_dir(env: dict) -> bool:
    """Whether env sets CLAUDE_CONFIG_DIR to the folder the CLI uses without it."""
    try:
        value = env.get("CLAUDE_CONFIG_DIR")
        return bool(value) and Path(value).expanduser().resolve() == (C.home_dir(env) / ".claude").resolve()
    except (OSError, RuntimeError):  # an unresolvable path is no default folder
        return False


def relative_base(cfg: C.Config) -> Path | None:
    """The CLI resolves relative config paths from its cwd and names its Keychain entry from the text.
    Children keep both so they use the same login."""
    value = child_env(cfg).get("CLAUDE_CONFIG_DIR")
    return C.config_base(cfg.env) if value and not os.path.isabs(value) else None


def child_cwd(cfg: C.Config) -> Path:
    """Where a child starts: nowitknows's data folder, or a relative CLAUDE_CONFIG_DIR's own base."""
    if base := relative_base(cfg):
        return base
    cfg.data.mkdir(parents=True, exist_ok=True)
    return cfg.data


def auth_check(cfg: C.Config) -> tuple[bool, str]:
    """(ok, detail) for display: whether children would run on the user's Claude subscription right now."""
    try:
        return subscription(cfg)
    except ClaudeError as e:
        return False, str(e)


def runner_session(cfg: C.Config) -> bool:
    """A cloud or self-hosted runner session: its login belongs to the runner, and nothing learned there lasts."""
    return cfg.remote or cfg.env.get("CLAUDE_CODE_ENVIRONMENT_KIND") == C.RUNNER_KIND


_PASSED: dict[tuple, tuple[float, float, tuple, dict, str]] = {}  # a passing `auth status` pair, reused briefly
_VERSIONS: dict[tuple, str] = {}  # each CLI binary's version, read once per process


def _login_fingerprint(cfg: C.Config, exe: str) -> tuple:
    """What changes with the stored login or the CLI, read from files: the account and API-key fields of the
    configs the CLI reads, its credentials file (the store on Linux and Windows), and the CLI binary."""
    parts: list = []
    for path in _global_configs(cfg):
        data = _read_config(path) or {}
        acct = data.get("oauthAccount") if isinstance(data.get("oauthAccount"), dict) else {}
        keys = tuple(k for k in ("primaryApiKey", "customApiKeyResponses", "apiKeyHelper") if k in data)
        parts.append((acct.get("accountUuid"), acct.get("organizationUuid"), acct.get("emailAddress"), keys))
    env = child_env(cfg)
    config_dir = (relative_base(cfg) or Path()) / Path(env.get("CLAUDE_CONFIG_DIR") or C.home_dir(env) / ".claude")
    for f in (config_dir / ".credentials.json", Path(exe)):
        try:
            st = f.stat()
            parts.append((st.st_mtime_ns, st.st_size, st.st_ino))
        except OSError:
            parts.append(None)
    return tuple(parts)


def version_key(version: str) -> tuple[int, ...]:
    """A version as a sortable tuple: a pre-release (`2.1.301-beta.1`) comes before its release (`2.1.301`)."""
    main, _, pre = version.partition("-")
    return (*(int(n) for n in main.split(".")), 0 if pre else 1)


def cli_version(exe: str, env: dict, cwd: Path) -> str:
    """The CLI's version from `claude --version`, which reads no login, once per process for each binary (path,
    time, size and inode, so an update in place is read again). Raises ClaudeError when it shows none."""
    try:
        st = os.stat(exe)
        key: tuple = (exe, st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        key = (exe,)
    if key not in _VERSIONS:
        try:
            proc = run_child([exe, "--version"], timeout=C.CLI_CHECK_TIMEOUT_S, env=env, cwd=str(cwd))
        except (OSError, subprocess.SubprocessError) as e:
            raise ClaudeError(f"could not run `claude --version`: {e}") from e
        if proc.returncode:
            raise ClaudeError(f"`claude --version` failed (rc={proc.returncode}): {proc.stderr[-300:]}")
        # The first version on any line: a wrapper may print a line of its own first
        found = next((m.group(0) for line in proc.stdout.splitlines() if (m := VERSION.search(line))), None)
        if not found:
            raise ClaudeError(f"`claude --version` showed no version: {proc.stdout.strip()[:120]}")
        _VERSIONS[key] = found
    return _VERSIONS[key]


def subscription(cfg: C.Config) -> tuple[bool, str]:
    """Ask the CLI itself, under the exact child env, before every child (a pass is reused for AUTH_REUSE_S, since
    each launch can refresh the stored login). Raises ClaudeError when it cannot run."""
    if cfg.env.get("ANTHROPIC_UNIX_SOCKET"):
        return False, refusal(["a `claude ssh` session, which runs on the login of the machine it came from"])
    if runner_session(cfg):
        return False, "background learning runs on your own machine, not in a cloud or self-hosted runner session"
    exe = find_claude(cfg.env)
    if not exe:
        raise ClaudeError("claude CLI not found (set NOWITKNOWS_CLAUDE_BIN)")
    env, cwd = child_env(cfg), child_cwd(cfg)
    # Asked before any `auth status`, the very kind of short command that makes an older CLI lose its stored login,
    # which a setup-token login does not have
    version = None if env.get("CLAUDE_CODE_OAUTH_TOKEN") else cli_version(exe, env, cwd)
    if version and version_key(version) < version_key(C.CLI_FLOOR):
        return False, refusal([f"{OUTDATED} {version} at {exe}"])
    key = (exe, tuple(sorted(env.items())))
    try:
        login = _login_fingerprint(cfg, exe)
    except ClaudeError:  # unreadable now: no reuse, and the check below reports what it finds first
        login = None
    hit = _PASSED.get(key)
    fresh = (
        hit
        and login is not None
        and hit[2] == login
        and all(
            0 <= now - then < C.AUTH_REUSE_S
            for now, then in (
                (time.time(), hit[0]),
                (time.monotonic(), hit[1]),  # wall time too: the monotonic clock stops in sleep
            )
        )
    )
    if fresh:
        st, detail = hit[3], hit[4]
    else:
        try:
            st = json.loads(_auth_status(exe, env, cwd, "--json"))
        except ValueError as e:
            raise ClaudeError("`claude auth status --json` printed no JSON") from e
        if not isinstance(st, dict):
            raise ClaudeError("`claude auth status --json` printed no JSON object")
        ok, detail = judge_auth(st, _auth_status(exe, env, cwd, "--text"), env.get("CLAUDE_CONFIG_DIR"))
        if not ok:  # a clean refusal stays one, even if a config file is also unreadable; it is never reused
            _PASSED.pop(key, None)
            return ok, detail
        _PASSED[key] = (time.time(), time.monotonic(), login, st, detail)
    # The files the CLI applies on its own change without a launch, so they are read again before every child.
    problems = config_problems(cfg) + extra_usage_problems(cfg, st.get("authMethod"))
    return (False, refusal(problems)) if problems else (True, detail)


def _auth_status(exe: str, env: dict, cwd: Path, fmt: str) -> str:
    try:
        proc = run_child(
            [exe, "--setting-sources", "", "auth", "status", fmt], timeout=C.CLI_CHECK_TIMEOUT_S, env=env, cwd=str(cwd)
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise ClaudeError(f"could not run `claude auth status`: {e}") from e
    if proc.returncode not in (0, 1) or not proc.stdout.strip():  # 1 is a clean "not logged in"
        raise ClaudeError(f"`claude auth status {fmt}` failed (rc={proc.returncode}): {proc.stderr[-300:]}")
    return proc.stdout


def run_child(
    argv: list[str], *, timeout: float, input: str | None = None, capture: bool = True, **popen
) -> subprocess.CompletedProcess:
    """subprocess.run with text output captured (or, `capture` off, left on the terminal), except that a child still
    running at its timeout is stopped gently (`stop`) before TimeoutExpired is raised."""
    with subprocess.Popen(
        argv,
        stdin=None if input is None else subprocess.PIPE,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        encoding="utf-8",
        errors="replace",
        **hidden(),
        **popen,
    ) as proc:
        try:
            out, err = proc.communicate(input, timeout=timeout)
        except BaseException:
            stop(proc)
            raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def stop(proc: subprocess.Popen) -> None:
    """SIGTERM, then up to CHILD_STOP_GRACE_S for the child to exit, its output drained meanwhile, and only then a
    kill: a claude killed outright can lose a login it has just refreshed. Windows has no SIGTERM, so no grace there."""
    try:
        if proc.poll() is None:
            proc.terminate()
            deadline = time.monotonic() + C.CHILD_STOP_GRACE_S
            # Its exit is awaited, not the end of its output: something it started may hold the pipes open for long
            while proc.poll() is None and (left := deadline - time.monotonic()) > 0:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.communicate(timeout=min(left, C.CHILD_STOP_POLL_S))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def judge_auth(st: dict, text: str, config_dir: str | None = None) -> tuple[bool, str]:
    """Pass only a first-party subscription login with no API key and no text line that could route elsewhere."""
    method, provider, plan = st.get("authMethod"), st.get("apiProvider"), str(st.get("subscriptionType") or "")
    if st.get("loggedIn") is not True and provider in ("firstParty", None) and not st.get("apiKeySource"):
        # A CLI with no login (2.1.296: authMethod "none" and one "Not logged in." line) needs a login, nothing else
        expired = [ln.strip()[:120] for ln in text.splitlines() if ln.strip().startswith("Login: ")]
        return False, refusal(["not logged in", *expired], config_dir)
    problems, logins, tokens = [], [], []
    if st.get("loggedIn") is not True:
        problems.append("not logged in")
    if provider != "firstParty":
        problems.append(f"provider {provider}")
    if st.get("apiKeySource"):
        problems.append(f"API key from {st['apiKeySource']}")
    for line in filter(None, (raw.strip() for raw in text.splitlines())):
        label, _, value = line.partition(": ")
        if label == "Login method":
            logins.append(value)
        elif label == "Auth token":
            tokens.append(value)
        elif label in NEUTRAL_LINES or (label == "Anthropic base URL" and value.rstrip("/") == FIRST_PARTY_URL):
            continue
        else:
            problems.append(line[:120] if label in SHOWN_LINES else f"unrecognized line '{label[:40]}'")
    if method == "claude.ai":
        if plan not in PLANS:
            problems.append("no Claude subscription plan on this login")
        if tokens or len(logins) != 1 or logins[0] == NO_PLAN or not logins[0].endswith(" account"):
            problems += [f"Login method: {v}" for v in logins or ["none"]] + [f"Auth token: {v}" for v in tokens]
    elif method == "oauth_token":
        if logins or tokens != [SETUP_TOKEN]:
            problems += [f"Login method: {v}" for v in logins] + [f"Auth token: {v}" for v in tokens or ["none"]]
    else:
        problems.append(f"auth method {method}")
    if problems:
        return False, refusal(problems, config_dir)
    return True, logins[0] if method == "claude.ai" else f"setup-token ({SETUP_TOKEN})"


def config_problems(cfg: C.Config) -> list[str]:
    """API settings in env blocks the CLI applies on its own."""
    problems = []
    for path, data in _cli_configs(cfg):
        env = data.get("env")
        if names := sorted(k for k in env if is_api_env(k)) if isinstance(env, dict) else []:
            problems.append(f"{path} sets {', '.join(names)} in its env block")
    return problems


def extra_usage_problems(cfg: C.Config, method: str | None) -> list[str]:
    """Detached runs fail closed when paid extra usage is enabled or the account flag is unknown."""
    if cfg.allow_extra_usage:
        return []
    # A setup-token login caches no account details, and any cached ones may belong to an earlier /login.
    flag = extra_usage_flag(cfg) if method == "claude.ai" else None
    if flag is None:
        return [f"{EXTRA_USAGE_UNSEEN} {EXTRA_USAGE_OFF}"]
    if flag:
        return [
            f"extra usage is on for this Claude account, so runs past your plan's limits are billed {EXTRA_USAGE_OFF}"
        ]
    return []


def extra_usage_flag(cfg: C.Config) -> bool | None:
    """Claude Code's cached `hasExtraUsageEnabled`: True if any config says on, False only if the config the CLI
    reads caches a boolean False, else None (not cached, or not a boolean)."""
    configs = _cli_configs(cfg)
    accounts = {path: d["oauthAccount"] for path, d in configs if isinstance(d.get("oauthAccount"), dict)}
    if any(a.get("hasExtraUsageEnabled") is True for a in accounts.values()):
        return True
    active = _global_configs(cfg)
    used = active[0] if active[0].exists() else active[1]
    return False if accounts.get(used, {}).get("hasExtraUsageEnabled") is False else None


def extra_usage(cfg: C.Config) -> bool:
    """Whether Claude Code's cached account details say paid extra usage is on (a setup-token login caches none)."""
    return extra_usage_flag(cfg) is True


def _global_configs(cfg: C.Config) -> list[Path]:
    """The global configs as the child's CLI finds them: from its env, a relative folder from its own base."""
    files = C.global_config_files(child_env(cfg))
    return [base / f for f in files] if (base := relative_base(cfg)) else files


def _cli_configs(cfg: C.Config) -> list[tuple[Path, dict]]:
    """Both global configs (whichever the CLI picks is covered) and the managed settings, as JSON objects."""
    files = _global_configs(cfg)
    # The platform's managed-settings folder, and the one a host can hand the CLI instead (read as policy too).
    for managed in {Path(MANAGED_DIRS.get(sys.platform, "/etc/claude-code")), *_handed_policy(cfg)}:
        files += [managed / "managed-settings.json"]
        files += sorted(managed.glob("managed-settings.d/*.json")) if managed.is_dir() else []
    return [(path, data) for path in files if (data := _read_config(path)) is not None]


def _handed_policy(cfg: C.Config) -> list[Path]:
    return (
        [Path(cfg.env["CLAUDE_CODE_MANAGED_SETTINGS_PATH"])] if cfg.env.get("CLAUDE_CODE_MANAGED_SETTINGS_PATH") else []
    )


def _read_config(path: Path) -> dict | None:
    """None when the file is missing. The CLI rewrites its config often, so a torn read is retried before this fails
    closed: an unreadable config could hide an env block or the extra usage flag."""
    for attempt in range(C.CONFIG_READ_TRIES):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            if attempt + 1 == C.CONFIG_READ_TRIES:
                raise ClaudeError(f"could not read {path}: {e}") from e
            time.sleep(C.CONFIG_READ_RETRY_S)
            continue
        if not isinstance(data, dict):
            raise ClaudeError(f"{path} is not a JSON object")
        return data
    return None


def _login_problem(problem: str) -> bool:
    return problem == "not logged in" or problem.startswith("Login: ")


def update_fix(exe: str) -> str:
    """How the build at `exe` is updated, by the install its binary resolves into."""
    where = os.path.realpath(exe).replace("\\", "/")
    if how := next((how for folder, how in UPDATES if folder in where), None):
        return how
    if any(folder in where for folder in NATIVE):
        return "with `claude update` in a terminal"
    return "the way it was installed (`claude update` for the native installer)"


def refusal(problems: list[str], config_dir: str | None = None) -> str:
    if outdated := next((p.removeprefix(OUTDATED).strip() for p in problems if p.startswith(OUTDATED)), None):
        version, _, exe = outdated.partition(" at ")
        return (
            f"your `claude` CLI ({version} at {exe}) is older than {C.CLI_FLOOR}, the oldest that background learning "
            "runs on: an older build can sign you out when a short command such as `claude auth status` exits during "
            f"a login refresh. Update it first, {update_fix(exe)} (a new login would not last on this build), then "
            f"check `/nowitknows:status`. {OFF_FIX}"
        )
    if problems and all(_login_problem(p) for p in problems):
        if any(p.startswith(EXPIRED) for p in problems):
            state = "'s stored login was invalidated (`claude auth status` shows Login: Expired)"
        else:
            detail = "; ".join(p for p in problems if p != "not logged in")
            state = f" is not logged in{f' ({detail})' if detail else ''}"
        own = (  # the CLI keeps a login apart for the folder the session names, the default one spelled out included
            f" Your session sets CLAUDE_CONFIG_DIR, and the CLI keeps a separate login for it: log in with it set, "
            f"`CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} claude auth login`."
            if config_dir
            else ""
        )
        return f"your `claude` CLI{state}. {LOGIN_FIX}{own} {OFF_FIX}"
    fixes = []
    if any(_login_problem(p) for p in problems):
        fixes.append("run `claude auth login` in a terminal")
    if any(EXTRA_USAGE_OFF not in p and not _login_problem(p) for p in problems):
        fixes.append(
            "check with `claude auth status --text` (a stored API key goes with `claude auth logout`, then "
            "`claude auth login`)"
        )
    if any(EXTRA_USAGE_OFF in p and EXTRA_USAGE_UNSEEN not in p for p in problems):
        fixes.append(
            "turn extra usage off for your Claude account, or turn allow_extra_usage on with "
            "`/plugin configure nowitknows@nowitknows`, then check `/nowitknows:status`"
        )
    if any(EXTRA_USAGE_UNSEEN in p for p in problems):
        fixes.append(
            "log in with `claude auth login` so Claude Code caches your account details, or turn allow_extra_usage on "
            "with `/plugin configure nowitknows@nowitknows`, then check `/nowitknows:status`"
        )
    return (
        "runs only on a Claude subscription login, with nothing that could bill or route elsewhere; found: "
        f"{'; '.join(problems)}. Fix it, then {'; or '.join(fixes)}. {OFF_FIX}"
    )


def _exe_of_pid(pid: str) -> str | None:
    try:
        if sys.platform.startswith("linux"):
            return os.readlink(f"/proc/{int(pid)}/exe")
        if sys.platform == "darwin":
            out = subprocess.run(["ps", "-o", "comm=", "-p", str(int(pid))], capture_output=True, text=True, timeout=2)
            return out.stdout.strip() or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def _runnable(cmd: str, env: dict) -> str | None:
    """The file a process can start for `cmd`. CreateProcess adds no extension, so on Windows a path without one
    needs its PATHEXT form, which shutil.which looks for only from Python 3.12 (it always does for a bare name)."""
    forms = [cmd]
    if sys.platform == "win32" and os.path.dirname(cmd) and not os.path.splitext(cmd)[1]:
        forms = [cmd + ext for ext in (env.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";") if ext] + forms
    return next((found for form in forms if (found := shutil.which(form, path=env.get("PATH")))), None)


def find_claude(env: dict) -> str | None:
    """The user's own claude CLI: explicit override, PATH, the running Claude Code binary, then default installs."""
    override = C.env("NOWITKNOWS_CLAUDE_BIN", C.environment(env))
    # Queued jobs carry the path their session resolved, and an app update removes the desktop app's versioned CLI.
    if override and (found := _runnable(override, env) or (override if os.path.exists(override) else None)):
        return found
    if exe := shutil.which("claude", path=env.get("PATH")):
        return exe
    running = _exe_of_pid(env["CLAUDE_PID"]) if env.get("CLAUDE_PID") else None
    if running and Path(running).name.lower().startswith("claude"):
        return running
    home = C.home_dir(env)
    for p in (
        home / ".claude" / "local" / "claude",
        home / ".local" / "bin" / "claude",
        home / ".local" / "bin" / "claude.exe",
    ):
        if p.exists():
            return str(p)
    return None


def run(
    cfg: C.Config,
    *,
    prompt: str,
    system: str,
    model: str,
    tools: str = "",
    allowed: list[str] | None = None,
    mcp: dict | None = None,
    schema: dict | None = None,
    max_turns: int | None = None,
    timeout: int = 600,
    label: str = "run",
    run_id: str | None = None,
) -> dict:
    from . import paid

    exe = find_claude(cfg.env)
    if not exe:
        raise ClaudeError("claude CLI not found (set NOWITKNOWS_CLAUDE_BIN)")
    ok, detail = subscription(cfg)
    if not ok:
        raise AuthError(detail)
    tmp = cfg.path("tmp", f"{uuid.uuid4().hex[:12]}")
    tmp.mkdir()
    (tmp / "system.txt").write_text(system, encoding="utf-8")
    argv = [exe, "-p", "--model", model, "--output-format", "json", "--no-session-persistence", "--setting-sources", ""]
    argv += ["--system-prompt-file", str(tmp / "system.txt"), "--tools", tools, "--permission-mode", "dontAsk"]
    if allowed:
        argv += ["--allowedTools", ",".join(allowed)]
    (tmp / "mcp.json").write_text(json.dumps({"mcpServers": mcp or {}}), encoding="utf-8")
    argv += ["--mcp-config", str(tmp / "mcp.json"), "--strict-mcp-config"]
    if schema:
        argv += ["--json-schema", json.dumps(schema)]
    if max_turns:
        argv += ["--max-turns", str(max_turns)]
    t0 = time.time()
    try:
        proc = run_child(argv, input=prompt, timeout=timeout, env=child_env(cfg), cwd=str(child_cwd(cfg)))
    except subprocess.TimeoutExpired as e:
        paid.receive(
            cfg, label, model, t0, {"subtype": "error_timeout", "is_error": True, "result": "model timed out"}, run_id
        )
        raise ClaudeError(f"{label}: timed out after {timeout}s") from e
    except (OSError, UnicodeError) as e:
        raise ClaudeError(f"{label}: could not run {exe}: {e}") from e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        out = None
    if not isinstance(out, dict):
        paid.receive(
            cfg, label, model, t0, {"subtype": "error_no_result", "is_error": True, "result": "no JSON result"}, run_id
        )
        raise ClaudeError(f"{label}: no JSON result (rc={proc.returncode}): {proc.stderr[-400:]}")
    paid.receive(cfg, label, model, t0, out, run_id)
    if out.get("is_error") and out.get("subtype") != "error_max_turns":
        raise ClaudeError(f"{label}: {out.get('subtype')}: {str(out.get('result'))[:400]}")
    return out


def _log_usage(cfg: C.Config, label: str, model: str, t0: float, out: dict, run_id: str | None = None) -> None:
    """One usage.jsonl row per child that ran, also when it timed out or printed no result (then with no tokens);
    `run_id` joins it to runs.jsonl and the ledger, for tokens per applied change."""
    usage = out.get("usage") or {}
    record = {
        "ts": int(t0),
        "label": label,
        "run_id": run_id,
        "model": model,
        "secs": round(time.time() - t0, 1),
        "subtype": out.get("subtype"),
        "turns": out.get("num_turns"),
        "input": usage.get("input_tokens", 0),
        "cache_write": usage.get("cache_creation_input_tokens", 0),
        "cache_read": usage.get("cache_read_input_tokens", 0),
        "output": usage.get("output_tokens", 0),
        "prompt_version": C.prompt_version(),
    }
    # Plan usage, not dollars: tokens per model that actually ran (an alias can resolve to any version).
    record["models"] = {
        m: sum(v for k, v in u.items() if k in MODEL_TOKEN_KEYS and isinstance(v, int))
        for m, u in (out.get("modelUsage") or {}).items()
        if isinstance(u, dict)
    } or {model: record["input"] + record["cache_write"] + record["cache_read"] + record["output"]}
    if isinstance(out.get("session_id"), str):
        record["child_session"] = out["session_id"]
    if out.get("step_id"):
        record["step_id"] = out["step_id"]
    for key in ("job", "runner", "chain_step", "fallbacks"):
        if key in out:
            record[key] = out[key]
    log_row(cfg, "usage.jsonl", record)


def structured(out: dict) -> dict:
    """The child's first JSON object, even when fenced or followed by prose; no object is a failed child."""
    data = out.get("structured_output")
    if not isinstance(data, dict):
        result = out.get("result")
        data = None
        if isinstance(result, str):
            try:
                data, _ = json.JSONDecoder().raw_decode(result, result.index("{"))
            except ValueError:
                pass
    if not isinstance(data, dict):
        raise ClaudeError("expected structured output")
    return data


def mcp_spec(cfg: C.Config, env_extra: dict | None = None) -> dict:
    """The server as the plugin manifest starts it, through bin/nowitknows.sh, which never runs uv; Windows cannot start
    a sh script, so there the environment's own Python, the one that script would run, starts it."""
    root = C.ROOT
    cmd = [str(root / "bin" / "nowitknows.sh"), "mcp"]
    if C.WINDOWS:
        cmd = [str(root / ".venv" / "Scripts" / "python.exe"), str(root / "bin" / "nowitknows.py"), "mcp"]
    extra = dict(env_extra or {})
    if cfg.cursor_paths is not None:
        # Resolved Cursor inputs are authoritative. Generic store overrides become
        # false production boundaries when an isolated MCP server resolves again.
        for key in ("NOWITKNOWS_DATA_DIR", "NOWITKNOWS_SKILLS_DIR", "NOWITKNOWS_TRANSCRIPTS_DIR"):
            extra.pop(key, None)
        extra.update({k: v for k, v in cfg.cursor_paths.env.items() if k in C.CURSOR_PATH_ENV})
        extra[C.CONFIG_DIR_INJECTED_ENV] = cfg.cursor_paths.env.get(C.CONFIG_DIR_INJECTED_ENV, "0")
        extra["NOWITKNOWS_HOST"] = "cursor"
    return {"nowitknows": {"command": cmd[0], "args": cmd[1:], "env": extra}}
