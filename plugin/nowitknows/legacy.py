"""Identifiers retained while installed older processes can still use the shared store."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

IDENTIFIERS = {
    "nowitknows": "learnloop",
    "nowitknows-claude": "learnloop-claude",
    "nowitknows-codex": "learnloop-codex",
    "NOWITKNOWS_": "LEARNLOOP_",
    "nowitknows-ready": "learnloop-ready",
    "mcp__nowitknows__": "mcp__learnloop__",
    "mcp__plugin_nowitknows_nowitknows__": "mcp__plugin_learnloop_learnloop__",
    "nowitknows@nowitknows": "learnloop@learnloop",
    "nowitknows-codex@nowitknows": "learnloop-codex@learnloop",
    "nowitknows-codex-local": "learnloop-codex-local",
    "nowitknows-codex@nowitknows-codex-local": "learnloop-codex@learnloop-codex-local",
    "nowitknows-installer": "learnloop-installer",
    "nowitknows-runtime": "learnloop-runtime",
    "plugins/cache/nowitknows/nowitknows": "plugins/cache/learnloop/learnloop",
    "plugins/cache/nowitknows/nowitknows-codex": "plugins/cache/learnloop/learnloop-codex",
    "plugins/cache/nowitknows-codex-local/nowitknows-codex": "plugins/cache/learnloop-codex-local/learnloop-codex",
    ".nowitknows": ".learnloop",
    ".nowitknows-migrated-": ".learnloop-migrated-",
    "nowitknows/__init__.py": "learnloop/__init__.py",
    "nowitknows/codex_transcript.py": "learnloop/codex_transcript.py",
    "bin/nowitknows.py": "bin/learnloop.py",
    "bin/nowitknows_codex.py": "bin/learnloop_codex.py",
    "bin/nowitknows.sh": "bin/learnloop.sh",
    "nowitknows router:": "learnloop router:",
    "nowitknows_skill": "learnloop_skill",
    "nowitknows_owner": "learnloop_owner",
}
IDENTIFIERS.update(
    {
        "NOWITKNOWS_" + suffix: "LEARNLOOP_" + suffix
        for suffix in (
            "ALLOW_EXTRA_USAGE",
            "ARCHIVE_MAX_MB",
            "BOUNDED_REVIEW_SKILLS",
            "CHEAP_MODEL",
            "CHILD",
            "CLAUDE_BIN",
            "CLAUDE_SKILLS_DIR",
            "CLAUDE_TRANSCRIPTS_DIR",
            "CODEX_BIN",
            "CODEX_DATA_DIR",
            "CODEX_HOME",
            "CODEX_SKILLS_DIR",
            "CURSOR_DATA_DIR",
            "CURSOR_DEFER_WORKER",
            "CURSOR_HOME",
            "CURSOR_SKILLS_DIR",
            "CONFIG_DIR_BASE",
            "CONFIG_DIR_INJECTED",
            "DATA_DIR",
            "DEBUG",
            "DRY_RUN",
            "HOST",
            "IN_SESSION",
            "JUDGE_MODEL",
            "JUDGE_NOTES",
            "LEGACY_HOST",
            "LEGACY_STORE",
            "LIVE_MODEL",
            "LIVE_SINCE",
            "LIVE_SKIP_CHILDREN",
            "METRICS",
            "MEMORY_INTERVAL",
            "MEMORY_TARGETS",
            "MODES",
            "OS_NOTIFY",
            "PROJECT",
            "PROJECT_KEY",
            "PROJECT_NAME",
            "REVIEW",
            "REVIEWER_MODEL",
            "REVIEW_DAILY_TOKENS",
            "REVIEW_MIN_DIGEST_CHARS",
            "REVIEW_MIN_GAP_S",
            "REVIEW_SIGNALS",
            "REVIEW_STOP_DIGEST_CHARS",
            "REVIEW_UNATTENDED",
            "ROUTER_THRESHOLD",
            "RUN_ID",
            "SHARED_DATA_DIR",
            "SHARE_PROJECT_NOTES",
            "SKILLS_DIR",
            "SKILL_INTERVAL",
            "SKILL_SCOPE",
            "START",
            "TRANSCRIPTS_DIR",
            "TRANSCRIPT_FOLDER",
            "VENV",
            "WRITE_MODE",
        )
    }
)

TOOL_PREFIXES = ("mcp__nowitknows__", "mcp__plugin_nowitknows_nowitknows__")


def canonical(value: str) -> str:
    for new, old in IDENTIFIERS.items():
        if value == old:
            return new
    for new in ("NOWITKNOWS_", *TOOL_PREFIXES, ".nowitknows-migrated-"):
        old = IDENTIFIERS[new]
        if value.startswith(old):
            return new + value[len(old) :]
    return value


def aliases(value: str) -> tuple[str, ...]:
    value = canonical(value)
    if value in IDENTIFIERS:
        return value, IDENTIFIERS[value]
    for new in ("NOWITKNOWS_", *TOOL_PREFIXES, ".nowitknows-migrated-"):
        if value.startswith(new):
            return value, IDENTIFIERS[new] + value[len(new) :]
    return (value,)


def is_ours(value: object, identity: str | None = None) -> bool:
    if not isinstance(value, str):
        return False
    return canonical(value) == canonical(identity) if identity is not None else canonical(value) in IDENTIFIERS


def env(name: str, environ: Mapping | None = None, default=None):
    source = os.environ if environ is None else environ
    return next((source[key] for key in aliases(name) if key in source), default)


def environment(environ: Mapping | None = None) -> dict:
    source = os.environ if environ is None else environ
    out = dict(source)
    for key in source:
        if key.startswith(IDENTIFIERS["NOWITKNOWS_"]):
            out.setdefault(canonical(key), source[key])
            out.pop(key)
    return out


def dual_env(environ: Mapping) -> dict:
    out = environment(environ)
    for key, value in tuple(out.items()):
        if key.startswith("NOWITKNOWS_"):
            for alias in aliases(key):
                out[alias] = value
    return out


def preferred_path(folder: Path, identity: str) -> Path:
    new, old = aliases(identity)
    return folder / old if (folder / old).exists() else folder / new


def tool_name(value: str) -> str | None:
    value = canonical(value)
    return next((value[len(prefix) :] for prefix in TOOL_PREFIXES if value.startswith(prefix)), None)


def owner_fields(owner: str) -> dict:
    # The unprefixed field is consumed by workers that only understand the original owner spelling.
    return {
        "owner": IDENTIFIERS.get(owner, owner),
        "nowitknows_owner": owner,
        "learnloop_owner": IDENTIFIERS.get(owner, owner),
    }


def owner(record: Mapping) -> str | None:
    # Old workers update only the unprefixed field, so it remains authoritative during an upgrade.
    return next((record[key] for key in ("owner", *aliases("nowitknows_owner")) if key in record), None)
