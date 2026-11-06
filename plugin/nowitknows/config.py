from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

from . import legacy as L

IDENTIFIERS = L.IDENTIFIERS
ENV_VARS = {
    name.removeprefix("NOWITKNOWS_").lower(): name
    for name in IDENTIFIERS
    if name.startswith("NOWITKNOWS_") and name != "NOWITKNOWS_"
}
aliases = L.aliases
canonical = L.canonical
is_ours = L.is_ours
env = L.env
dual_env = L.dual_env
preferred_path = L.preferred_path
tool_name = L.tool_name
owner_fields = L.owner_fields
owner = L.owner

ROOT = Path(__file__).resolve().parent.parent
WINDOWS = os.name == "nt"
CHILD_TOOLS = "mcp__nowitknows__"
SURFACE_ENV = {  # what SessionStart records per session (state.json sessions.<id>.surface)
    "entrypoint": "CLAUDE_CODE_ENTRYPOINT",
    "attended": "CLAUDE_CODE_SESSION_ATTENDED",
    "remote": "CLAUDE_CODE_REMOTE",
    "remote_environment": "CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE",
    "environment_kind": "CLAUDE_CODE_ENVIRONMENT_KIND",  # bridge: Remote Control, e.g. a Projects "Work locally" thread
    "session_kind": "CLAUDE_CODE_SESSION_KIND",
}
SURFACE_FLAGS = {  # recorded as present or not; the values are ids or long text
    "claude_project": "CLAUDE_PROJECT_UUID",
    "projects_session": "CLAUDE_CODE_PROJECTS_SESSION",
    "remote_control": "CLAUDE_CODE_BRIDGE_SESSION_ID",  # a session sharing itself by Remote Control; not a bridge child
    "hosted_memory": "CLAUDE_CODE_POST_TURN_MEMORY",
}
REMOTE_CONTROL_KIND = "bridge"  # someone drives the session from claude.ai or the phone, so it counts as working
RUNNER_KIND = "byoc"  # a self-hosted cloud runner, whose login is the runner's
NOTICE_PROMPT = "<task-notification>"  # a background-task notice the host sends as a prompt (Claude Code's own test)
NOTICE_ORIGIN = "task-notification"  # transcript origin.kind of those prompts
# Notice subkinds that carry the user's words: a Projects coordinator's relay, a scheduled task's stored prompt
USER_NOTICES = ("projects-relay", "scheduled-trigger")
SESSION_NOTICES = ("peer-send-message", "session-inbox")  # another Claude session writing to this one
CODEX_IMPORT_TURN = re.compile(r"external-import-turn-\d+")
CODEX_COPY_TURN = re.compile(r"rollout-\d+")
CODEX_IMPORT_RECORDS = ("external_agent_session_imports.json", "claude-cowork-import-history.json")

# Router note: each suggested skill's description is cut to this, enough to say what it is when Claude Code's own
# over-budget skill listing shows that skill by name only.
NOTE_DESCRIPTION_CHARS = 80
RECALL_REQUEST_MAX_CHARS = 3_000  # field replay keeps the short past-work requests and drops long untagged material
RECALL_REQUEST = re.compile(
    r"\b(?:remember\s+when|earlier\s+sessions?)\b"
    r"|\b(?:like|as|from)\s+(?:last\s+time|the\s+other\s+day)\b"
    r"|\blike\s+(?:before|we\s+did\s+(?:before|last\s+time|yesterday))\b"
    r"|(?:^\s*|[^\w\s]\s*|\b(?!such\b)\w+\s+)as\s+before\b"
    r"|\b(?:we|you|i)\s+(?:previously|yesterday|last\s+time)\b"
    r"|\b(?:we|you|i)\s+(?:(?:already|actually)\s+)?"
    r"(?:(?!(?:need|feed|embed|speed|breed|heed|seed|bleed|proceed|exceed|succeed|shred)\b)\w+ed"
    r"|had|did|built|made|ran|set|wrote|got|hit)\b(?:[^.!?\n]|\.(?=\w)){0,60}?"
    r"\b(?:previously|yesterday|last\s+time|the\s+other\s+day)\b"
    r"|\bour\s+(?:work|sessions?|chat|fix|change|discussion|approach|decision|build|setup|workaround)\b"
    r"\s+(?:(?:was|were|worked|used|ran)\b(?:[^.!?\n]|\.(?=\w)){0,60}?)?"
    r"\b(?:previously|yesterday|last\s+time|the\s+other\s+day)\b"
    r"|\b(?:previously|yesterday|last\s+time)[ ,:]+(?:we|you|i|our)\b"
    r"|\byesterday['\u2019]s\s+(?:work|session|chat|fix|change)\b"
    r"|\b(?:how|where)(?:\s+did|['\u2019]d)\s+(?:we|i|you)\b"
    r"|\bwhat(?:\s+did|['\u2019]d)\s+(?:we|i|you)\s+decide\b"
    r"|\bdid(?:n['\u2019]t)?\s+(?:we|i|you)\s+already\b"
    r"|\bwe\s+(?:fixed|did|decided|discussed)\b(?:[^.!?\n]|\.(?=\w)){0,160}?\bbefore\b"
    r"(?=[^\S\n]*(?:[^\w\s]|\n|\Z)|\s+(?:in|on|when|and|but|with|without)\b)"
    r"|\b(?:what|which|where)\s+(?:was|were)\b[^.!?\n]{0,160}\bagain\b",
    re.I,
)  # bare "again" is usually a retry, not a request for past work; gaps stay within one short sentence
NOTICE_SUMMARY_CHARS = 200  # a notice's text kept in search and review digests
PROSE_CLAUSE_CHARS = 80  # how far redaction looks back for the verb before `credentials:`, so it stays linear
REDACT_CODE_AUTH_HEADERS = ("authorization", "proxy-authorization")
REDACT_CODE_VALUE_HEADERS = ("x-api-key", "api-key", "x-auth-token")
# Entrypoints Claude Code never marks attended (2.1.296), for CLIs that do not export CLAUDE_CODE_SESSION_ATTENDED
HEADLESS_ENTRYPOINTS = (
    "sdk-cli",
    "sdk-ts",
    "sdk-py",
    "mcp",
    "bench",
    "claude-code-github-action",
    "claude-security",
    "remote_trigger",
    "remote_cowork_trigger",
    "remote_baku",
)
SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
PLUGIN_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.]+)?")
PROCESS_MEMORY_DAYS = 7  # a Claude Code process idle this long counts again when it starts a session
USER_CAP = 2750
GLOBAL_MEMORY_CAP = 2500
MEMORY_TIDY_AT = 0.85
MEMORY_ENTRY_CHARS = 150
MEMORY_HOOK_CHARS = 100
MEMORY_NOTE_SUFFIX = " (details in note)"
GLOBAL_NOTE_MAX_BYTES = 16_384  # a full legacy block fits even with four-byte Unicode and temporal metadata
GLOBAL_NOTE_ID_CHARS = 12
MEMORY_WRITING_RULES = (
    f"Write one short fact per entry, about {MEMORY_ENTRY_CHARS} characters at most. "
    "USER.md holds who the user is and how they want to be worked with; MEMORY.md holds environment facts and "
    "standing conventions. Procedures and multi-step how-tos go in skills, never memory. No session history, no "
    "examples. A request the user made once is a preference to verify, not a permanent rule: where a fact can be "
    "checked or measured, the agent checks it."
)
HYBRID_MEMORY_RULES = (
    f"New memory >{MEMORY_ENTRY_CHARS} chars: content=body; hook=situation "
    f"(one line, <={MEMORY_HOOK_CHARS - len(MEMORY_NOTE_SUFFIX)} chars); "
    f"NowItKnows adds '{MEMORY_NOTE_SUFFIX}'. source_exchange=N for supporting [exchange N], "
    "not automatically last. Verify scope there: repo facts=project; cross-repo environment=memory. "
    'Read: target=memory, operations=[{"action":"read","name":"<note>"}].'
)
INLINE_LIMIT = 10_000
TOPIC_FILE_MAX = 4096
INDEX_LINE_MAX = 200
INDEX_MAX_LINES = 200
INDEX_RESERVE_LINES = 180  # in a folder shared with Claude Code nowitknows adds index lines only up to here
INDEX_EDIT_TRIES = 5  # reads of a shared MEMORY.md that changed under an edit before the edit fails closed
SHARE_PROJECT_NOTES_DEFAULT = True
SHARE_NOTES_ENV = "NOWITKNOWS_SHARE_PROJECT_NOTES"
NOTES_FOLDER_ENV = ("HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR", "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE")
NOTES_FOLDER_MIN_CHARS = 3  # Claude Code drops a memory folder path shorter than this (2.1.296)
NOTE_NAME_MAX = 64  # characters in a project note's name, the stem of its file
NOTE_COPY_STEM = 40  # a note kept beside Claude Code's note of the same name keeps this much of its name
FINGERPRINT_CHARS = 12  # hex digits of an import's key or a note's digest, in file names and messages
LOCK_NAME_CHARS = 32  # hex digits of the hash that names a notes folder's lock file
WEEK_S = 7 * 86400  # "the last 7 days" in status, reports and the list of notes not saved
INDEX_MAX_BYTES = 25_000
INDEX_RESERVE_BYTES = 22_500  # in a folder shared with Claude Code nowitknows grows MEMORY.md only up to here
REFUSALS_LOG = "memory_refusals.jsonl"  # project notes background writes could not save at an index limit
DELIVERY_MASK_NOTE = (
    "[redacted] marks a value NowItKnows masked; never reconstruct it, and when a command needs it, "
    "use the real value from the user's environment."
)
NOTE_POINTER_CHARS = 100  # description shown for a shared note that is pointed to rather than delivered whole
SKILL_DESC_MAX = 1024
SKILL_DESC_SOFT = 250
SKILL_MAX_CHARS = 30_000
SUPPORT_NEW_PER_SKILL = 1
SUPPORT_NEW_WINDOW_DAYS = 7
SKILL_BODY_MAX = 100_000
DIGEST_MAX_CHARS = 30_000
OUTCOME_MAX_CHARS = 1_200
OUTCOME_COMMAND_CHARS = 600
OUTCOME_OUTPUT_CHARS = 240
OUTCOME_SCAN_CHARS = 8_192
OUTCOME_COMMANDS = 3
OUTCOME_CHECKS = 8
OUTCOME_FILE_BYTES = 65_536
OUTCOME_DIRECTORY_ENTRIES = 256
EXCHANGE_MAX_CHARS = 8_000
EXCHANGE_MIN_CHARS = 1_200  # each exchange's share when a long stretch must fit one digest
PLAN_MAX_CHARS = 2_000
SEARCH_RESULTS = 3
SEARCH_MAX_RESULTS = 10
SEARCH_PER_SESSION = 3
SEARCH_CANDIDATES = 300  # best-ranked exchanges read per search before they are grouped by session
SEARCH_SNIPPET_TOKENS = 16
SCROLL_WINDOW = 3  # exchanges shown on each side of around_uuid
SQLITE_TIMEOUT_S = 5
ARCHIVE_GZIP_LEVEL = 6
# Every uv launch requests this floor so a UV_PYTHON pin cannot select an unsupported interpreter.
PYTHON_FLOOR = ">=3.14"
UV_MINIMUM = "0.9.5"  # the oldest uv checked to install Python 3.14 (0.8.22 lists no 3.14 build)
# New sessions skip a plugin's MCP server whose start failed for this long (probed on 2.1.311: failed at +2 min,
# starting again at +16; Claude Code's docs say 15 minutes); the setup notice says so, and the notice that a setup
# finished is dropped unsaid once this long has passed since
MCP_RETRY_MINUTES = 15
# Claude Code's connect timeout for an MCP server (MCP_TIMEOUT's default); 2.1.308 starts a server that exits during it
# once more within it, so the MCP start's waits below count from the first start of one Claude Code process
MCP_CONNECT_TIMEOUT_S = 30
# The MCP start waits until this long after that first start for a background setup that runs, so a first session on
# a fast link gets its tools and a slower one fails with the reason before the connect timeout, and this long for a
# session start to begin one where none runs yet; it reads files only, never running uv (bin/nowitknows.sh repeats all
# three)
MCP_SETUP_WAIT_S = 25
MCP_SETUP_START_S = 5
# A hook leaves the notice that a setup finished alone this long after it did: an MCP start waiting for that setup,
# polling each second, removes it first, since its session's tools then start
SETUP_NOTICE_SETTLE_S = 3
# A background setup that failed, or stopped unfinished twice in a row, is not started again for this long; its notice
# shows the failure meanwhile
BOOTSTRAP_RETRY_S = 600
# An attended Claude Code session tells that Codex lacks nowitknows at most this often, and never once it has it
CODEX_LATER_DAYS = 30
# An attended Codex session tells that Claude Code lacks nowitknows at most this often, and never once it has it
CLAUDE_LATER_DAYS = 30
CURSOR_LATER_DAYS = 30
# Keep environments outside the plugin cache because Codex prunes old versions on update.
CODEX_RUNTIME = "nowitknows-runtime"
WINDOWS_RELEASE_DIGEST_CHARS = 16
WINDOWS_RUNTIME_PATH_LIMIT = 260
# Longest installed dependency path, including bytecode, in the pinned Python 3.14/3.15 runtime.
WINDOWS_VENV_DEEPEST_PATH = (
    "Lib/site-packages/cryptography/hazmat/primitives/ciphers/__pycache__/algorithms.cpython-315.pyc"
)
START_MISSED_MAX_AGE_S = 7 * 86400  # a start-missed note no prompt took by then is an ended session's
SKILL_GIT_TIMEOUT_S = 60
SKILL_GIT_STOP_GRACE_S = 0.5
SKILL_BACKUP_REPO = "nowitknows-skills"
SKILL_BACKUP_TIMEOUT_S = 180
SKILL_BACKUP_DEADLINE_S = 300
SKILL_BACKUP_RETRY_S = 900
SKILL_BACKUP_FETCH_S = 900
SKILL_BACKUP_SCAN_BYTES = 64 * 1024 * 1024
SKILL_BACKUP_SHA_CHARS = 12

GITHUB_REPO = "theaigroup-ai/nowitknows"
CLAUDE_INSTALL = (
    f"claude plugin marketplace add {GITHUB_REPO} --sparse .claude-plugin plugin; "
    "claude plugin install nowitknows@nowitknows"
)
CODEX_INSTALL = f"codex plugin marketplace add {GITHUB_REPO}; codex plugin install nowitknows-codex@nowitknows"
LEGACY_CODEX_ENV = tuple(
    L.aliases("NOWITKNOWS_" + name)[1]
    for name in ("CODEX_HOME", "SHARED_DATA_DIR", "DATA_DIR", "CODEX_DATA_DIR", "CODEX_SKILLS_DIR", "CODEX_BIN")
)
CODEX_SESSION_END_TIMEOUT_S = 10
CODEX_MANAGED_MARKET = "nowitknows-codex-local"
BOOTSTRAP_PEEK_S = 1  # how long SessionStart waits on a setup it started before saying how it went
BOOTSTRAP_ERROR_CHARS = 300  # characters of uv's error line a setup notice quotes
# A slow background setup must not hold up session start.
MOD_START_WAIT_MS = 1000
# Bound decompressed reads because a small .zst or .gz file can expand to gigabytes.
HISTORY_LINE_MAX_BYTES = 32 * 1024 * 1024
HISTORY_FILE_MAX_BYTES = 512 * 1024 * 1024
ZSTD_WAIT_S = 60  # how long reading one file may wait, in all, on the zstd command before it is stopped
ZSTD_KILL_GRACE_S = 2  # from terminating a zstd command to killing it
ZSTD_PIPE_CHUNKS, ZSTD_CHUNK_BYTES = 16, 65_536  # how far the command's output is read ahead of the reader
REVIEW_MAX_TURNS = 16
REVIEW_MIN_GAP_S = 15 * 60  # mid-session reviews of one session at least this far apart (precompact and end excepted)
REVIEW_MIN_DIGEST_CHARS = 1500  # ordinary final floor; shorter digests need a signal and the absolute minimum
REVIEW_SIGNAL_MIN_CHARS = 200  # final reviews can retain short teaching, but not tiny acknowledgements
REVIEW_STOP_DIGEST_CHARS = 20_000  # mid-session reviews wait for this much, so each one reads a nearly full digest
REVIEW_FIXED_TOKENS = 8_000  # a review's prompt and tools, for the budget's look-ahead
REVIEW_JUDGE_TOKENS = 15_000  # a judgment of one staged change, with its revision and re-judgment
TRIGGER_TOKENS = 12_000  # one trigger backfill run
SIGNAL_SCAN_CHARS = 1_000_000  # the signal check reads each exchange whole
REVIEW_SKILL_LIST = 30  # skills named to a reviewer or judge: the session's and those most like the text
REPO_SKILL_COUNT = 20
REPO_SKILL_DESCRIPTION_CHARS = 200
REPO_SKILL_CHARS = 4_000  # escaped metadata including its read-only label
REPO_SKILL_HEADER_CHARS = 8_192
REPO_SKILL_SCAN_ENTRIES = 1_000
REPO_SKILL_DEPTH = 3
REPO_SKILL_SOURCE_CHARS = 6_000
REPO_SKILL_SOURCE_COUNT = 2
SIGNAL_LONG_TASK_TOOLS = 12  # tool calls in one exchange that make it a multi-step task worth reviewing
REVIEW_DAILY_TOKENS = 300_000  # background tokens per local day before reviews and trigger runs wait for tomorrow
CODEX_DEV_BUDGET_SHARE = 0.5  # automatic exec development reviews share the Codex daily ceiling
REVIEW_EMPTY_STREAK = 3  # after this many reviews in a row that changed nothing, intervals double per further one
REVIEW_BACKOFF_MAX = 4  # up to this factor; a review that changes something resets it
TOOL_ARG_CHARS = 200  # a tool call's command, path, or pattern in a digest line
TOOL_ERROR_CHARS = 160  # the start of a failed tool call's error
MOD_MAX_TOKENS = 8192
MOD_REVISE_MAX_TOKENS = 65_536
MOD_REVISE_CHARS_PER_TOKEN = 2  # conservative estimate for a whole skill encoded as JSON
MOD_REVISE_OVERHEAD_TOKENS = 4096  # room for fixes and JSON framing
MOD_SKILL_CHARS = 60_000  # tools-free reviews need bounded complete bodies before reference detail
MOD_SUPPORT_FILES = 20
MOD_SUPPORT_CHARS = 2000
SESSION_SKILLS = 50  # skills a session keeps as loaded, most recent last
MOD_MEMORY_CHARS = 30_000  # current writable memory, global entries before project topics
MOD_QUEUE_DAYS = 7
MOD_LEASE_S = 30 * 60  # a lost session cannot reserve background work forever
MOD_REPLY_TRIES = 3  # times a call applies a model reply that raises, before the reply is given up
CODEX_MODEL = "gpt-6-luna"  # trigger generation, the cheap role
CODEX_REVIEW_MODEL = "gpt-6.1-sol"
CODEX_REVIEW_STEPS = 12
CODEX_JOB_LIMIT = 8
CODEX_TOOL_HISTORY = 256
CODEX_DELIVERY_HISTORY = 256
CODEX_CONTEXT_CHARS = 30_000
CURSOR_CONTEXT_CHARS = 9_500  # Cursor Desktop inline additional_context limit is 10,000 chars before file spill
CURSOR_CONTEXT_RESERVE_CHARS = 80
CURSOR_CONTEXT_MIN_BODY_CHARS = 200
CURSOR_PROMPT_TAIL_BYTES = 256 * 1024  # prompt routing reads only a bounded tail of the native JSONL
CURSOR_TOOL_HISTORY = 256
CURSOR_SESSION_ID_MAX = 128
CURSOR_JOB_LIMIT = 8
CURSOR_ROW_ID_CHARS = 24
CURSOR_PLAN_SNIPPET_CHARS = 120
# Rollout originators of sessions a person works in (Codex 0.165 binary: codex_exec, the SDKs, the MCP and app
# server clients and the ambiguous default codex_cli_rs are not), and how much of a rollout's first line a hook reads
CODEX_ATTENDED_ORIGINATORS = ("Codex Desktop", "codex_desktop", "codex_work_desktop", "codex-tui", "codex_vscode")
CODEX_META_MAX_BYTES = 1 << 20
CODEX_CATALOG_TIMEOUT_S = 2
TRIGGER_DESCRIPTION_CHARS = 400
CODEX_RPC_TIMEOUT_S = 30
CODEX_SESSION_ID_MAX = 128
CODEX_ENV = (
    "NOWITKNOWS_SHARED_DATA_DIR",
    "NOWITKNOWS_CODEX_HOME",
    "NOWITKNOWS_CODEX_DATA_DIR",
    "NOWITKNOWS_CODEX_SKILLS_DIR",
    "NOWITKNOWS_CODEX_BIN",
)
CURSOR_NODE_MINIMUM = (22, 13, 0)
CURSOR_GROK_PATTERN = r"grok-(\d+(?:[.-]\d+)*)"
CURSOR_MODEL = "grok-4.7"
CURSOR_MODEL_CACHE_S = 24 * 60 * 60
CURSOR_REVIEWER_PARAMS = (
    {"id": "context", "value": "256k"},
    {"id": "reasoning_effort", "value": "medium"},
    {"id": "fast", "value": "true"},
)
CURSOR_PARAMS = CURSOR_REVIEWER_PARAMS
CURSOR_JUDGE_PARAMS = (
    {"id": "context", "value": "256k"},
    {"id": "reasoning_effort", "value": "low"},
    {"id": "fast", "value": "true"},
)
# Bumped whenever metadata masking changes, so caches written by older masking are rebuilt.
CURSOR_META_MASK_VERSION = 3
# Metadata fields whose name has one of these parts are masked whole, whatever their value looks like.
CURSOR_META_SECRET_PARTS = (
    "apikey",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "credentials",
    "key",
    "passphrase",
    "passwd",
    "password",
    "pwd",
    "secret",
    "token",
)
CURSOR_ENV = (
    "NOWITKNOWS_SHARED_DATA_DIR",
    "NOWITKNOWS_CURSOR_HOME",
    "NOWITKNOWS_CURSOR_DATA_DIR",
    "NOWITKNOWS_CURSOR_SKILLS_DIR",
    "CURSOR_CONFIG_DIR",
    "CURSOR_DATA_DIR",
)
CURSOR_REVIEWER_TIMEOUT_S = 600
CURSOR_JUDGE_TIMEOUT_S = 600
JUDGE_TIMEOUT_S = 300
JUDGE_REVISIONS = 1
STAGED_MAX_DAYS = 7
STAGED_MAX_PAID_TRIES = 2
STAGED_LEGACY_ATTEMPTS_LIMIT = 10
NOTE_JUDGMENTS_LOG = "note_judgments.jsonl"  # one row per verdict on a project note: run, note, pass or fail
STAGED_NOTES = "staged-notes"  # project note changes a background review proposed, until the judge settles each
NOTE_JUDGE_OTHERS = 80  # other notes of the folder (name and description) the note judge compares a change with
NOTE_JUDGE_DESCRIPTION_CHARS = 200  # of each other note's description the note judge is shown
NOTE_REFUSAL_REASON_CHARS = 300  # of a judge's first blocking finding kept as the reason a note was not saved
STAGED_ID_CHARS = 12  # of a uuid: the id of a staged change
CHANGES_SHOWN = 20  # changes `changes` lists unless asked for more
CURATOR_EVERY_DAYS = 7
CURATOR_IDLE_HOURS = 2
ARCHIVE_DAYS = 30
ARCHIVE_SESSIONS = 20
ARCHIVE_MAX_DAYS = 180  # backstop for skills whose projects went quiet
ATTENTION_LOADS = 5  # a skill changed after ATTENTION_CHANGES of its last ATTENTION_LOADS reviewed loads needs a look
ATTENTION_CHANGES = 3
REPORT_WEEKS = 4
METRICS_FILE = "session_metrics.jsonl"
METRICS_STATE_FILE = "session_metrics_state.json"
METRICS_SCHEMA = 3
METRICS_TOKEN_WEEKS = 4
METRICS_INTERACTIVE_ENTRYPOINTS = (
    "cli",
    "claude-desktop",
    "claude-desktop-3p",
    "claude-vscode",
    "local-agent",
    "remote",
    "remote_desktop",
    "remote_mobile",
    "remote_projects",
    "remote_cowork",
    "ssh-remote",
)
METRICS_HEADLESS_SESSION_KINDS = ("bg", "daemon", "daemon-worker")
METRICS_GAP_CAP_S = 300
METRICS_MIN_GROUP = 5
METRICS_COMPARABILITY_FACTOR = 3
METRICS_MAX_FILES = 100_000
METRICS_LARGE_FILE_BYTES = 16 * 1024 * 1024
METRICS_LARGE_FILE_INTERVAL_S = 300
METRICS_MAX_BYTES = 32 * 1024 * 1024
METRICS_LINE_BYTES = 2 * 1024 * 1024
METRICS_BACKFILL_SECONDS = 2
METRICS_NICE = 10
METRICS_WINDOWS_PRIORITY = 0x00004000  # BELOW_NORMAL_PRIORITY_CLASS
CODEX_WINDOWS_HOOK_TIMEOUT_S = 5
METRICS_ACTIVITY_COVERAGE_FILE = "session_metrics_activity_coverage.json"
METRICS_COVERAGE_FILE = "session_metrics_coverage.json"
METRICS_EXCLUDED_PARTS = ("bench", "benchmark", "benchmarks", "lab", "*-lab", "tmp", "temp", "tmp-*", "pytest-*")
METRICS_EXCLUDED_PATHS = ("/private/var/folders/*/*/t/*", "/var/folders/*/*/t/*")
METRICS_BENCH_MARKERS = tuple(f"{prefix}BENCH" for prefix in ("NOWITKNOWS_", "LEARNLOOP_"))
METRICS_NIK_DATA_PARTS = (".learnloop", ".nowitknows")
METRICS_NIK_CODEX_PARTS = ("learnloop", "nowitknows")
METRICS_MEMORY_SOURCES = ("memory_consolidation", "memory_generation", "memory")
# Synthetic benchmark savings are not measured in user sessions, so reports label this an estimate.
EST_TOKENS_SAVED_PER_LOAD = 40_000
PROMPT_VERSION_CHARS = 12  # of a sha256 over the shipped prompt files: the prompt-version tag on each run
UI_TOKEN_BYTES = 32
UI_REQUEST_TIMEOUT_S = 5
UI_POLL_INTERVAL_S = 0.2
UI_RECORDS_SHOWN = 200  # newest records per receipt list and skill history; older ones are counted, not rendered
UI_FILE_MAX_BYTES = 1_000_000  # a larger skill, note, findings or backup file is not read and shows as omitted for size
# Each view's share of the page, in rendered bytes: 6,000,000 together, so a full page stays under UI_PAGE_MAX_BYTES
UI_OVERVIEW_BYTES = 250_000  # report tables, then skill outcome rows
UI_SKILLS_BYTES = 1_250_000  # skill cards with their current text
UI_HISTORY_BYTES = 1_500_000  # version history changes with their diffs
UI_RECEIPTS_BYTES = 1_500_000  # learning receipt cards and records
UI_REJECTIONS_BYTES = 750_000  # judge rejection cards with their findings
UI_NOTES_BYTES = 750_000  # global memory entries, project notes and note backups
UI_DIFF_MAX_PAIRS = 1_000_000  # changed old lines times changed new lines one diff may compare (difflib's time)
UI_LEDGERS_MAX_BYTES = 64_000_000  # every aggregate holds all ledger rows, so larger ledgers are refused
UI_PAGE_MAX_BYTES = 8_000_000  # a larger page is refused, not served; the shares plus fixed text stay below it
ROUTER_COMPACT_MIN_DOCS = 1000
ROUTER_PRIOR_DOCS = 30
ROUTER_THRESHOLD = 12.0
ROUTER_SKILL_THRESHOLD = {
    "claude": 12.0,
    "codex": 15.0,
    "cursor": 12.0,
}  # Codex pastes whole bodies, so its routing threshold is higher.
ROUTER_PLAIN_RATIO = 0.7
ROUTER_K = 3
ROUTER_REL = 0.5
ROUTER_LONG_TOKENS = 20  # distinct words past which the threshold grows; eval requests have at most 18
ROUTER_EXCLUDED_SOURCES = ("plugin", "system", "unknown")  # hosts advertise managed skills; unknown origins fail closed
SKILL_SCOPE_DEFAULT = True
SKILL_SCOPE_FILE = "skill_scope.json"  # in the data folder, keyed like skill_triggers.json
SCOPE_READ_MAX_BYTES = 1_048_576  # parsing a larger file can exhaust the prompt hook's latency budget
SCOPE_REFERENCE_CHARS = 60_000  # reference detail must not make a scope refresh read an unbounded library
REPO_ID_GIT_TIMEOUT_S = 2  # identity discovery runs only at session start or in the worker
REPO_IDENTITIES_FILE = "repo_identities.jsonl"
SCOPE_REPO_NAME_MIN = 4  # a repo name shorter than this, letters and digits only, marks no skill as that repo's
SCOPE_GENERIC_REPO_NAMES = ("repo", "src", "app", "project", "work", "workspace", "code", "home")  # too common to mark
SCOPE_LOCK_WAIT_S = 0.2  # a hook adding a repo to a skill's scope waits this long for the file, else skips the add
SCOPE_CWD_ROWS = 50  # transcript rows the scope backfill reads for the folder a repo key names
SCOPE_WRITE_WAIT_S = 5  # a skill write or the scope backfill waits this long for the scope file
SCOPE_TRACE_DEPTH = 2  # migrations the scope backfill follows back from a creation row to the copies it brought
TRIGGERS_MAX = 12
TRIGGER_BATCH = 12
TRIGGER_RUN_LIMIT = 24
TRIGGER_TRIES = 3
NOTICE_MAX_LINES = 8
NOTICE_REPEAT_S = 6 * 3600
NOTICE_TOLD_CHARS = 200  # one notice line as the model is told it
NOTICE_TELL_OTHERS = 3
# A notice from one project's work waits this long for a session in that project before any session may tell it
NOTICE_PROJECT_WAIT_S = 3600
BACKOFF_BASE_S = 900
BACKOFF_MAX_S = 6 * 3600
AUTH_REUSE_S = 300  # a passing `claude auth status` pair, reused this long within one background process
# The oldest claude CLI background runs start: 2.1.296 and earlier blank the stored login when a short command such as
# `claude auth status` exits during a login refresh (fixed in Claude Code 2.1.301)
CLI_FLOOR = "2.1.301"
CLI_CHECK_TIMEOUT_S = 60  # each `claude --version` and `claude auth status` launch
CHILD_STOP_GRACE_S = 10  # from asking a claude child past its time to stop (SIGTERM) to killing it
CHILD_STOP_POLL_S = 0.1  # how often a child asked to stop is checked for having exited
# Set to "1" where nowitknows, not the session, put CLAUDE_CONFIG_DIR in an env (the Codex host, jobs 0.8.0 and 0.8.1
# saved); children drop such a value, since set at all it sends the macOS CLI to another Keychain login
CONFIG_DIR_INJECTED_ENV = "NOWITKNOWS_CONFIG_DIR_INJECTED"
CONFIG_DIR_BASE_ENV = "NOWITKNOWS_CONFIG_DIR_BASE"  # where a session's relative CLAUDE_CONFIG_DIR is resolved from
CONFIG_READ_TRIES = 3  # the CLI rewrites ~/.claude.json often; a torn read is retried before failing closed
CONFIG_READ_RETRY_S = 0.05
REPLACE_TRIES = 5  # Windows refuses to replace a file while another process has it open; readers close quickly
REPLACE_RETRY_S = 0.025
OPTION_PREFIX = "CLAUDE_PLUGIN_OPTION_"
SHARE_NOTES_OPTION = OPTION_PREFIX + "SHARE_PROJECT_NOTES"
OPTIONS_FILE = "plugin_options.json"  # options as the last hook saw them; mtime dates the snapshot for commands
CURSOR_SETUP_TIMEOUT_S = 1200
SETUP_TIMEOUT_S = 300
MIGRATION_VERSION = 4  # 4 adds notes actions to 3's action kinds
MIGRATION_COMPATIBLE = (3, 4)  # pending journals this version replays as they are
MIGRATE_QUIET_MINUTES = 10
MIGRATE_CWD_ROWS = 50  # transcript rows read looking for the folder a session ran in
NOTES_GATE_FILLINGS = 32  # ways the Codex notes gate fills in one path's variables before it stops trying more
NOTES_GATE_FOLDERS = 32  # folders the Codex notes gate follows a command's cd moves through; past it, any folder
NOTES_GATE_NESTING = 4  # levels the Codex notes gate follows into ${VAR:-default}, shell scripts and substitutions
NOTES_GATE_MAX_CHARS = 60_000
NOTES_GATE_DEADLINE_S = 0.4  # the full check gives way to cheap mode after this long: Codex cuts its hook off at 1 s
NOTES_GATE_RECORDED = 100  # notes folders of the newest sessions the Codex notes gate follows besides the layout's
NOTES_GATE_WORD_MAX = 4096  # a word or run of text longer than this is no path
NOTES_GATE_HEADERS = 2000  # patch headers cheap mode judges one by one, links followed; the rest only by their text
ACTIVITY_LOCK_WAIT_S = 0.02
NOTICE_LOCK_WAIT_S = 0.02
HOOK_STATE_LOCK_WAIT_S = 0.02


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


CURSOR_PATH_ENV = frozenset(
    (
        "HOME",
        "USERPROFILE",
        "NOWITKNOWS_CURSOR_HOME",
        "CURSOR_CONFIG_DIR",
        "CURSOR_DATA_DIR",
        "NOWITKNOWS_DATA_DIR",
        "NOWITKNOWS_SHARED_DATA_DIR",
        "NOWITKNOWS_CURSOR_DATA_DIR",
        "NOWITKNOWS_SKILLS_DIR",
        "NOWITKNOWS_CURSOR_SKILLS_DIR",
        "NOWITKNOWS_TRANSCRIPTS_DIR",
        "NOWITKNOWS_MODES",
        "NOWITKNOWS_PROJECT",
        "NOWITKNOWS_START",
        "CURSOR_TRANSCRIPT_PATH",
        "CURSOR_PROJECT_DIR",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE",
        "CLAUDE_CODE_REMOTE_MEMORY_DIR",
        "CLAUDE_PROJECT_DIR",
        "NOWITKNOWS_CONFIG_DIR_BASE",
        "NOWITKNOWS_CODEX_HOME",
        "CODEX_HOME",
        "NOWITKNOWS_CODEX_DATA_DIR",
        "NOWITKNOWS_CODEX_SKILLS_DIR",
        "NOWITKNOWS_CLAUDE_SKILLS_DIR",
        "NOWITKNOWS_CLAUDE_TRANSCRIPTS_DIR",
        "NOWITKNOWS_LEGACY_STORE",
        "NOWITKNOWS_CODEX_BIN",
        "NOWITKNOWS_CLAUDE_BIN",
        "CLAUDE_PLUGIN_OPTION_DATA_DIR",
        "CLAUDE_PLUGIN_OPTION_SKILLS_DIR",
        "CLAUDE_PLUGIN_OPTION_TRANSCRIPTS_DIR",
    )
)


@dataclass(frozen=True)
class CursorPaths:
    """All profile paths, resolved before any Cursor I/O. `env` is safe to pass to shared code/jobs."""

    home: Path
    profile: Path
    data: Path
    skills: Path
    skills_library: Path
    skill_roots: tuple[Path, ...]
    links: tuple[Path, ...]
    transcripts: Path
    transcript_source: Path | None
    transcript_projects: Path
    memory: Path
    notes: Path
    logs: Path
    state: Path
    jobs: Path
    project: Path
    claude: Path
    shared: Path
    isolated: bool
    protected_profiles: tuple[Path, ...]
    env: dict = field(compare=False, repr=False)

    def transcript_path(self, given: object) -> Path | None:
        if not isinstance(given, (str, Path)) or not str(given).strip():
            return None
        candidate = Path(str(given).strip()).resolve()
        if self.isolated and not candidate.is_relative_to(self.transcript_projects.resolve()):
            return None
        return candidate


def resolve_cursor_paths(
    env: dict | None = None, *, profile: Path | None = None, default_profile: Path | None = None
) -> CursorPaths:
    """Resolve every Cursor store together; an explicit profile is the launcher's isolation boundary.

    A non-default profile never inherits another host's stores. Explicit Cursor destinations outside that
    profile remain supported, except when they overlap a production store (including symlink aliases).
    Resolving an isolated profile never reads production store contents.
    """
    source = environment(env)
    home = Path(source.get("USERPROFILE" if WINDOWS else "HOME") or Path.home()).resolve()

    def path(value) -> Path:
        text = str(value)
        if text == "~" or text.startswith("~/") or text.startswith("~\\"):
            text = str(home) + text[1:]
        return Path(text).resolve()

    production = [
        path(home / name) for name in (".nowitknows", ".learnloop", ".claude", ".codex", ".cursor", ".agents")
    ]
    production.extend(
        path(source[k])
        for k in (
            "NOWITKNOWS_SHARED_DATA_DIR",
            "NOWITKNOWS_DATA_DIR",
            "NOWITKNOWS_SKILLS_DIR",
            "NOWITKNOWS_TRANSCRIPTS_DIR",
            "CLAUDE_CONFIG_DIR",
            "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE",
            "NOWITKNOWS_CODEX_HOME",
            "CODEX_HOME",
            "NOWITKNOWS_CODEX_DATA_DIR",
            "NOWITKNOWS_CODEX_SKILLS_DIR",
            "NOWITKNOWS_CLAUDE_SKILLS_DIR",
            "NOWITKNOWS_CLAUDE_TRANSCRIPTS_DIR",
            "NOWITKNOWS_LEGACY_STORE",
        )
        if source.get(k)
        and not (
            k == "CLAUDE_CONFIG_DIR"
            and source.get(CONFIG_DIR_INJECTED_ENV) == "1"
            and L.env("NOWITKNOWS_CURSOR_DATA_DIR", source)
            and path(source[k]) == path(source["NOWITKNOWS_CURSOR_DATA_DIR"]) / "host"
        )
    )

    production_profile = path(home / ".cursor")
    if profile is None:
        candidates = [
            path(source[k]) for k in ("NOWITKNOWS_CURSOR_HOME", "CURSOR_CONFIG_DIR", "CURSOR_DATA_DIR") if source.get(k)
        ]
        separate = [
            p
            for p in candidates
            if p != production_profile
            and not any(p.is_relative_to(store) or store.is_relative_to(p) for store in production)
        ]
        if separate:
            profile = separate[0]
        elif any(p != production_profile for p in candidates):
            raise ValueError("Cursor profile selects a production store")
        else:
            profile = default_profile or production_profile
    profile = path(profile)
    separate_profile = profile != production_profile
    shared_raw = L.env("NOWITKNOWS_SHARED_DATA_DIR", source) or L.env("NOWITKNOWS_DATA_DIR", source)
    if not shared_raw and not separate_profile:
        shared_raw = source.get("CLAUDE_PLUGIN_OPTION_DATA_DIR") or preferred_path(home, ".nowitknows")
    shared = path(shared_raw or home / ".nowitknows")

    def destination(value, fallback: Path) -> Path:
        candidate = path(value or fallback)
        if separate_profile and (
            candidate == home or any(candidate.is_relative_to(p) or p.is_relative_to(candidate) for p in production)
        ):
            fallback = path(fallback)
            if fallback == home or any(fallback.is_relative_to(p) or p.is_relative_to(fallback) for p in production):
                raise ValueError("isolated Cursor path overlaps a production store")
            return fallback
        return candidate

    data = (
        destination(L.env("NOWITKNOWS_CURSOR_DATA_DIR", source), profile / "nowitknows")
        if (separate_profile or L.env("NOWITKNOWS_CURSOR_DATA_DIR", source))
        else shared
    )
    isolated = separate_profile or data != shared
    # A data-only override is an isolation boundary too, including native input folders.
    if isolated and not separate_profile:
        profile = data / "cursor"
        separate_profile = True  # Apply the same production-store exclusions to this derived profile.
    skills = destination(
        L.env("NOWITKNOWS_CURSOR_SKILLS_DIR", source) or (None if isolated else L.env("NOWITKNOWS_SKILLS_DIR", source)),
        profile / "skills",
    )
    transcripts = destination(
        None if isolated else L.env("NOWITKNOWS_TRANSCRIPTS_DIR", source), data / "transcripts/cursor"
    )
    claude = data / "host" if isolated else path(source.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    # Older queued records used data/host as an injected default, not an explicit Claude profile.
    if not isolated and claude == data / "host":
        claude = path(home / ".claude")
    codex = path(L.env("NOWITKNOWS_CODEX_HOME", source) or source.get("CODEX_HOME") or home / ".codex")
    roots = [skills, profile / "skills"]
    links = [skills]
    if not isolated:
        roots.extend((home / ".agents/skills", claude / "skills", data / "skills"))
        links.append(path(L.env("NOWITKNOWS_CLAUDE_SKILLS_DIR", source) or home / ".claude/skills"))
        codex_data = L.env("NOWITKNOWS_CODEX_DATA_DIR", source)
        if codex == path(home / ".codex") and (not codex_data or path(codex_data) == shared):
            links.append(path(L.env("NOWITKNOWS_CODEX_SKILLS_DIR", source) or codex / "skills"))
    transcript_source = source.get("CURSOR_TRANSCRIPT_PATH")
    if transcript_source:
        transcript_source = path(transcript_source)
        if isolated and not transcript_source.is_relative_to((profile / "projects").resolve()):
            transcript_source = None
    project = destination(
        source.get("CURSOR_PROJECT_DIR") or source.get("CLAUDE_PROJECT_DIR"),
        profile / "workspace" if isolated else Path.cwd(),
    )
    mapped = {k: v for k, v in source.items() if not k.startswith("CLAUDE_")}
    if isolated:
        for k in CURSOR_PATH_ENV:
            mapped.pop(k, None)
        for k in ("NOWITKNOWS_PROJECT", "NOWITKNOWS_START"):
            if value := L.env(k, source):
                mapped[k] = str(destination(value, project))
        # Background MCP servers consume both pinned directories without detecting modes again.
        # Preserve a pin only when every directory (and the key used to rebuild it) stays in this store.
        if pinned := L.env("NOWITKNOWS_MODES", source):
            try:
                modes = json.loads(pinned)
                key = modes["key"]
                directories = ("native_dir", "fallback_dir")
                if (
                    isinstance(key, str)
                    and (not key or (key not in (".", "..") and Path(key).name == key and "\\" not in key))
                    and all(isinstance(modes[k], str) and modes[k].strip() for k in directories)
                ):
                    resolved = {k: path(modes[k]) for k in directories}
                    if all(
                        p.is_relative_to(path(data / "projects")) and destination(p, profile) == p
                        for p in (*resolved.values(), path(data / "projects" / key / "memory"))
                    ):
                        mapped["NOWITKNOWS_MODES"] = json.dumps(modes | {k: str(p) for k, p in resolved.items()})
            except (KeyError, TypeError, ValueError, OSError, RuntimeError):
                pass  # An invalid inherited pin falls back to detection inside the isolated store.
        # Keep command overrides only outside stores; CLI executables are not profile data.
        for k in ("NOWITKNOWS_CLAUDE_BIN", "NOWITKNOWS_CODEX_BIN"):
            if source.get(k) and destination(source[k], profile) != profile:
                mapped[k] = source[k]
    elif source.get("CLAUDE_COWORK_MEMORY_PATH_OVERRIDE"):
        mapped["CLAUDE_COWORK_MEMORY_PATH_OVERRIDE"] = source["CLAUDE_COWORK_MEMORY_PATH_OVERRIDE"]
    runtime_home = profile / "home" if isolated else home
    mapped.update(
        HOME=str(home),
        USERPROFILE=str(home),
        NOWITKNOWS_HOST="cursor",
        NOWITKNOWS_CURSOR_HOME=str(profile),
        NOWITKNOWS_CURSOR_DATA_DIR=str(data),
        NOWITKNOWS_SHARED_DATA_DIR=str(shared),
        NOWITKNOWS_DATA_DIR=str(data),
        NOWITKNOWS_SKILLS_DIR=str(skills),
        NOWITKNOWS_TRANSCRIPTS_DIR=str(transcripts),
        CLAUDE_CONFIG_DIR=str(claude),
        CLAUDE_CODE_SESSION_ATTENDED="1",
        CURSOR_PROJECT_DIR=str(project),
    )
    if transcript_source is not None:
        mapped["CURSOR_TRANSCRIPT_PATH"] = str(transcript_source)
    if isolated:
        # Shared helpers dispatch to these resolved paths; do not reintroduce generic destinations as inputs
        # when a worker or installed hook resolves this environment again.
        for k in ("NOWITKNOWS_DATA_DIR", "NOWITKNOWS_SKILLS_DIR", "NOWITKNOWS_TRANSCRIPTS_DIR"):
            mapped.pop(k, None)
        mapped["NOWITKNOWS_CURSOR_SKILLS_DIR"] = str(skills)
    if isolated or not source.get("CLAUDE_CONFIG_DIR"):
        mapped[CONFIG_DIR_INJECTED_ENV] = "1"
    protected_profiles = tuple(
        dict.fromkeys(
            (
                *(path(home / name) for name in (".cursor", ".claude", ".codex")),
                profile,
                claude,
                codex,
                *(
                    path(source[k])
                    for k in (
                        "CURSOR_CONFIG_DIR",
                        "CURSOR_DATA_DIR",
                        "NOWITKNOWS_CURSOR_HOME",
                        "CLAUDE_CONFIG_DIR",
                        "CODEX_HOME",
                        "NOWITKNOWS_CODEX_HOME",
                    )
                    if source.get(k)
                ),
            )
        )
    )
    return CursorPaths(
        runtime_home,
        profile,
        data,
        skills,
        skills if isolated else data / "skills",
        tuple(dict.fromkeys(roots)),
        tuple(dict.fromkeys(links)),
        transcripts,
        transcript_source,
        profile / "projects",
        data / "memory",
        data / "projects",
        data,
        data / "state.json",
        data / "cursor-jobs",
        project,
        claude,
        shared,
        isolated,
        protected_profiles,
        mapped,
    )


@dataclass(frozen=True)
class Config:
    data: Path
    skills_home: Path
    claude_home: Path
    transcripts: Path
    reviewer_model: str
    judge_model: str
    cheap_model: str
    memory_interval: int
    skill_interval: int
    router_threshold: float
    archive_max_mb: int
    review: bool
    review_unattended: bool
    judge_notes: bool
    review_min_gap_s: int
    review_min_digest_chars: int
    review_stop_digest_chars: int
    review_daily_tokens: int
    review_signals: bool
    bounded_review_skills: bool
    os_notify: bool
    allow_extra_usage: bool
    metrics: bool = True
    share_project_notes: bool = SHARE_PROJECT_NOTES_DEFAULT
    skill_scope: bool = SKILL_SCOPE_DEFAULT
    env: dict = field(default_factory=dict, compare=False, repr=False)
    options_snapshot_at: float | None = field(default=None, compare=False, repr=False)
    cursor_paths: CursorPaths | None = field(default=None, compare=False, repr=False)

    @property
    def host(self) -> str:
        host = L.env("NOWITKNOWS_HOST", self.env)
        return host if host in ("codex", "cursor") else "claude"

    @property
    def skill_threshold(self) -> float:
        """Skill routing's per-host default; a configured or replaced router_threshold applies to skills and notes."""
        names = (f"{OPTION_PREFIX}ROUTER_THRESHOLD", "NOWITKNOWS_ROUTER_THRESHOLD")
        if self.router_threshold != ROUTER_THRESHOLD or any(L.env(name, self.env) not in (None, "") for name in names):
            return self.router_threshold
        return ROUTER_SKILL_THRESHOLD[self.host]

    @property
    def legacy_host(self) -> str:
        return L.env("NOWITKNOWS_LEGACY_HOST", self.env, "claude")

    @property
    def skills_library(self) -> Path:
        if self.cursor_paths is not None:
            return self.cursor_paths.skills_library
        if L.env("NOWITKNOWS_LEGACY_STORE", self.env) == str(self.data):
            return self.skills_home
        if self.host == "codex":
            home = home_dir(self.env)
            profile = Path(L.env("NOWITKNOWS_CODEX_HOME", self.env) or self.env.get("CODEX_HOME") or home / ".codex")
            shared = Path(L.env("NOWITKNOWS_SHARED_DATA_DIR", self.env) or data_dir(self.env))
            if (
                profile.expanduser().resolve() != (home / ".codex").resolve()
                or self.data != shared.expanduser().resolve()
            ):
                return self.skills_home
        if self.host == "cursor" and self.isolated_cursor:
            return self.skills_home
        return self.data / "skills"

    @property
    def isolated_codex(self) -> bool:
        home = home_dir(self.env)
        profile = Path(L.env("NOWITKNOWS_CODEX_HOME", self.env) or self.env.get("CODEX_HOME") or home / ".codex")
        separate = L.env("NOWITKNOWS_CODEX_DATA_DIR", self.env)
        shared = Path(L.env("NOWITKNOWS_SHARED_DATA_DIR", self.env) or self.data)
        return profile.expanduser().resolve() != (home / ".codex").resolve() or bool(
            separate and Path(separate).expanduser().resolve() != shared.expanduser().resolve()
        )

    @property
    def isolated_cursor(self) -> bool:
        return (self.cursor_paths or resolve_cursor_paths(self.env)).isolated

    @property
    def project_notes_shared(self) -> bool:
        return (
            self.share_project_notes
            and not (self.host == "codex" and self.isolated_codex)
            and not (self.host == "cursor" and self.isolated_cursor)
        )

    @property
    def skill_link_homes(self) -> tuple[Path, ...]:
        if self.cursor_paths is not None:
            return self.cursor_paths.links
        home = home_dir(self.env)
        codex = Path(L.env("NOWITKNOWS_CODEX_HOME", self.env) or self.env.get("CODEX_HOME") or home / ".codex")
        if self.host != "cursor":
            if self.isolated_codex:
                return (self.skills_home,)
            peer = (
                Path(L.env("NOWITKNOWS_CLAUDE_SKILLS_DIR", self.env) or home / ".claude/skills")
                if self.host == "codex"
                else Path(L.env("NOWITKNOWS_CODEX_SKILLS_DIR", self.env) or codex / "skills")
            )
            peers = [self.skills_home.expanduser().resolve(), peer.expanduser().resolve()]
            registry = self.data / "cursor-profiles.json"
            try:
                profiles = json.loads(registry.read_text(encoding="utf-8"))
                if isinstance(profiles, list):
                    peers.extend(Path(p) for p in profiles if isinstance(p, str) and Path(p).is_absolute())
            except (OSError, ValueError):
                pass
            return tuple(dict.fromkeys(peers))
        return resolve_cursor_paths(self.env).links

    @property
    def remote(self) -> bool:
        if self.host == "cursor" and _truthy(self.env.get("CURSOR_CODE_REMOTE")):
            return True
        return _truthy(self.env.get("CLAUDE_CODE_REMOTE"))

    @property
    def child(self) -> bool:
        return L.env("NOWITKNOWS_CHILD", self.env) == "1"

    def path(self, *parts: str) -> Path:
        p = self.data.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


def python_env(env: dict) -> dict:
    """Python children use the UTF-8 wire format on Windows, including redirected diagnostics."""
    return {**env, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"} if WINDOWS else env


def environment(env: dict | None = None) -> dict:
    return L.environment(env)


def codex_runtime(profile: Path) -> Path:
    return preferred_path(profile, CODEX_RUNTIME)


def _opt(env: dict, key: str, default):
    """Plugin option, then NOWITKNOWS_<KEY>, then the default; an unparsable number falls back to the default."""
    env = environment(env)
    raw = env.get(f"{OPTION_PREFIX}{key.upper()}") or L.env(ENV_VARS[key], env)
    if raw in (None, ""):
        return default
    if isinstance(default, bool):
        return _truthy(raw)
    try:
        return type(default)(raw)
    except ValueError:
        return default


def _all_on(env: dict, key: str, default: bool) -> bool:
    """An opt-out sticks: off in either the plugin option or NOWITKNOWS_<KEY> wins over on in the other."""
    env = environment(env)
    raw = [env.get(f"{OPTION_PREFIX}{key.upper()}"), L.env(ENV_VARS[key], env)]
    return all(_truthy(v) for v in raw if v not in (None, "")) if any(v not in (None, "") for v in raw) else default


def home_dir(env: dict) -> Path:
    if L.env("NOWITKNOWS_HOST", env) == "cursor":
        return resolve_cursor_paths(env).home
    return _home_dir(env)


def _home_dir(env: dict) -> Path:
    """The home Claude Code uses: on Windows, USERPROFILE, else the profile folder Windows reports (Node's
    os.homedir()); never HOME there, which Git Bash or MSYS may set."""
    if WINDOWS:
        return Path(env.get("USERPROFILE") or Path.home())
    return Path(env.get("HOME") or Path.home()).expanduser().resolve()


def data_dir(env: dict) -> Path:
    if L.env("NOWITKNOWS_HOST", env) == "cursor":
        return resolve_cursor_paths(env).data
    return _data_dir(env)


def _data_dir(env: dict) -> Path:
    return Path(_opt(env, "data_dir", str(preferred_path(_home_dir(env), ".nowitknows")))).expanduser().resolve()


def config_base(env: dict) -> Path:
    """Where a relative CLAUDE_CONFIG_DIR is resolved from, as the session's CLI did: the folder saved with a queued
    job, else the one the CLI started in (CLAUDE_PROJECT_DIR), else this process's."""
    if L.env("NOWITKNOWS_HOST", env) == "cursor":
        return resolve_cursor_paths(env).project
    return Path(L.env(CONFIG_DIR_BASE_ENV, env) or env.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def global_config_files(env: dict) -> list[Path]:
    """The CLI reads <config dir or ~/.claude>/.config.json when it exists, else <config dir or ~>/.claude.json."""
    if L.env("NOWITKNOWS_HOST", env) == "cursor":
        from .claude import cli_env

        return _global_config_files(_home_dir(env), cli_env(env))
    return _global_config_files(home_dir(env), env)


def _global_config_files(home: Path, env: dict) -> list[Path]:
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    return [Path(config_dir or home / ".claude") / ".config.json", Path(config_dir or home) / ".claude.json"]


def load(env: dict | None = None, *, create: bool = True, cursor_paths: CursorPaths | None = None) -> Config:
    env = environment(env)
    if L.env("NOWITKNOWS_HOST", env) == "cursor":
        cursor_paths = cursor_paths or resolve_cursor_paths(env)
        env = cursor_paths.env | {k: v for k, v in env.items() if k not in CURSOR_PATH_ENV}
    if cursor_paths is not None:
        data, claude_home, skills_home, transcripts = (
            cursor_paths.data,
            cursor_paths.claude,
            cursor_paths.skills,
            cursor_paths.transcripts,
        )
    else:
        home = home_dir(env)
        claude_home = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude").expanduser()
        if not claude_home.is_absolute():  # from where the session's CLI resolved it, not where this process runs
            claude_home = config_base(env) / claude_home
        claude_home = claude_home.resolve()
        data = data_dir(env)
        skills_home = Path(_opt(env, "skills_dir", str(claude_home / "skills"))).expanduser().resolve()
        transcripts = Path(_opt(env, "transcripts_dir", str(claude_home / "projects"))).expanduser().resolve()
    if create:
        data.mkdir(parents=True, exist_ok=True)
    cursor_host = L.env("NOWITKNOWS_HOST", env) == "cursor"
    return Config(
        data=data,
        skills_home=skills_home,
        claude_home=claude_home,
        transcripts=transcripts,
        reviewer_model=CURSOR_GROK_PATTERN if cursor_host else _opt(env, "reviewer_model", "sonnet"),
        judge_model=CURSOR_GROK_PATTERN if cursor_host else _opt(env, "judge_model", "opus"),
        cheap_model=CURSOR_GROK_PATTERN if cursor_host else _opt(env, "cheap_model", "haiku"),
        memory_interval=_opt(env, "memory_interval", 10),
        skill_interval=_opt(env, "skill_interval", 10),
        router_threshold=_opt(env, "router_threshold", ROUTER_THRESHOLD),
        archive_max_mb=_opt(env, "archive_max_mb", 1024),
        review=_opt(env, "review", True),
        metrics=_all_on(env, "metrics", True),
        review_unattended=_opt(env, "review_unattended", False),  # scripted `claude -p` and SDK sessions
        judge_notes=_opt(env, "judge_notes", True),  # off: background project notes are written without the judge
        review_min_gap_s=_opt(env, "review_min_gap_s", REVIEW_MIN_GAP_S),
        review_min_digest_chars=_opt(env, "review_min_digest_chars", REVIEW_MIN_DIGEST_CHARS),
        review_stop_digest_chars=_opt(env, "review_stop_digest_chars", REVIEW_STOP_DIGEST_CHARS),
        review_daily_tokens=_opt(env, "review_daily_tokens", REVIEW_DAILY_TOKENS),  # 0: no daily limit
        review_signals=_opt(env, "review_signals", True),  # off: review whatever is due, signal or not
        # on: a tools-free review (Claude in-session, Codex) inlines only loaded skills and those it may change
        bounded_review_skills=_opt(env, "bounded_review_skills", True),
        os_notify=_opt(env, "os_notify", False),
        allow_extra_usage=_all_on(env, "allow_extra_usage", True),
        share_project_notes=_all_on(env, "share_project_notes", SHARE_PROJECT_NOTES_DEFAULT),
        skill_scope=_opt(env, "skill_scope", SKILL_SCOPE_DEFAULT),  # off: learned skills route in every repo
        env=env,
        cursor_paths=cursor_paths,
    )


@lru_cache(maxsize=1)
def get() -> Config:
    host = L.env("NOWITKNOWS_HOST", environment())
    if host == "codex":
        from .codex_host import configuration

        return configuration()
    if host == "cursor":
        from .cursor_host import configuration as cursor_configuration

        return cursor_configuration()
    return load()


def _json_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def for_command(*, create: bool = True, env: dict | None = None) -> Config:
    """Claude supplies plugin options only to hooks, so commands reuse their last snapshot."""
    source = environment(env)
    host = L.env("NOWITKNOWS_HOST", source)
    if host == "codex":
        from .codex_host import configuration

        return configuration(source, create=create)
    if host == "cursor":
        from .cursor_host import configuration as cursor_configuration

        return cursor_configuration(source, create=create)
    cfg = (get() if create else load(create=False)) if env is None else load(source, create=create)
    options = plugin_options(_json_object(cfg.data / OPTIONS_FILE))
    merged = {**({} if plugin_options(cfg.env) else options), **cfg.env, **sharing_opt_outs(options)}
    settings_env = _json_object(cfg.claude_home / "settings.json").get("env")
    saved = environment(settings_env).get(SHARE_NOTES_ENV) if isinstance(settings_env, dict) else None
    if isinstance(saved, str) and (SHARE_NOTES_ENV not in cfg.env or not _truthy(saved)):
        merged[SHARE_NOTES_ENV] = saved
    result = load(merged, create=create) if merged != cfg.env else cfg
    if options and not plugin_options(cfg.env):
        try:
            result = replace(result, options_snapshot_at=(cfg.data / OPTIONS_FILE).stat().st_mtime)
        except OSError:
            pass
    return result


@lru_cache(maxsize=1)
def option_names() -> frozenset[str]:
    """This plugin's own option variables (from plugin.json userConfig), so another plugin's are never saved."""
    try:
        keys = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["userConfig"]
    except (OSError, ValueError, KeyError):
        return frozenset()
    return frozenset(OPTION_PREFIX + re.sub(r"[^A-Za-z0-9_]", "_", k).upper() for k in keys)


def plugin_options(env: dict) -> dict:
    return {k: str(v) for k, v in env.items() if k in option_names()}


def sharing_opt_outs(env: dict) -> dict:
    """A current notes opt-out must win over stale settings retained by a queued job."""
    env = environment(env)
    return {
        k: str(env[k])
        for k in (SHARE_NOTES_OPTION, SHARE_NOTES_ENV)
        if str(env.get(k) or "").strip() and not _truthy(str(env[k]))
    }


def session_tools() -> str:
    host = L.env("NOWITKNOWS_HOST", environment())
    if host in ("codex", "cursor"):
        return "mcp__nowitknows__"
    return _claude_tools()


@lru_cache(maxsize=1)
def _claude_tools() -> str:
    """Prefix Claude Code gives this plugin's MCP tools in a session: mcp__plugin_<plugin>_<server>__."""
    try:
        name = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["name"]
    except (OSError, ValueError, KeyError):
        name = "nowitknows"
    return f"mcp__plugin_{re.sub(r'[^A-Za-z0-9_-]', '_', name)}_nowitknows__"


@lru_cache(maxsize=1)
def prompt_version() -> str:
    """Tag of the shipped prompt files a run used, so tuning can compare variants."""
    h = hashlib.sha256()
    try:
        for path in sorted((ROOT / "prompts").glob("*.md")):
            h.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    except OSError:
        return "unknown"  # never cost a usage or run row its accounting
    return h.hexdigest()[:PROMPT_VERSION_CHARS]


def prompt(name: str) -> str:
    return (
        (ROOT / "prompts" / name)
        .read_text(encoding="utf-8")
        .strip()
        .replace("{mcp}", session_tools())
        .replace("{memory_rules}", MEMORY_WRITING_RULES)
        .replace("{skill_max_chars}", f"{SKILL_MAX_CHARS:,}")
    )
