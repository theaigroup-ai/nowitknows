"""Cursor Grok catalog; refresh only in workers/setup."""

from __future__ import annotations

import json
import math
import re
import subprocess
import time

from . import claude, cursor_runner
from . import config as C
from .state import atomic_write, file_lock


def version(mid: str) -> tuple[int, ...] | None:
    match = re.fullmatch(C.CURSOR_GROK_PATTERN, mid)
    if not match:
        return None
    components = re.split(r"[.-]", match[1])
    if any(len(p) > 2 or (len(p) > 1 and p.startswith("0")) for p in components[1:]):
        return None
    parts = [int(p) for p in components]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def newest(ids: list[str]) -> dict[str, str]:
    return {
        "grok": max(
            (mid for mid in ids if version(mid) is not None),
            key=lambda mid: (version(mid), mid),
            default=C.CURSOR_MODEL,
        )
    }


def _read(path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def resolved(cfg: C.Config) -> dict[str, str]:
    """Read-only snapshot: never probes SDK, even on cache expiry."""
    ids = _read(cfg.data / "cursor-models.json").get("ids", {})
    mid = ids.get("grok") if isinstance(ids, dict) else None
    return {"grok": mid if isinstance(mid, str) and version(mid) is not None else C.CURSOR_MODEL}


def target(cfg: C.Config) -> str:
    return resolved(cfg)["grok"]


def parameters(role: str) -> tuple:
    return tuple(dict(p) for p in (C.CURSOR_JUDGE_PARAMS if role == "judge" else C.CURSOR_PARAMS))


def refresh(cfg: C.Config) -> dict[str, str]:
    """Refresh once per 24 hours; retain the last valid cache when discovery fails."""
    path = cfg.path("cursor-models.json")
    with file_lock(path):
        cache = _read(path)
        ts = cache.get("ts")
        age = time.time() - ts if type(ts) in (int, float) and math.isfinite(ts) else -1
        ids = cache.get("ids", {})
        valid = isinstance(ids, dict) and isinstance(ids.get("grok"), str) and version(ids["grok"]) is not None
        if valid and 0 <= age < C.CURSOR_MODEL_CACHE_S:
            return resolved(cfg)
        try:
            listed = cursor_runner.model_ids(cfg)
            if not any(version(mid) is not None for mid in listed):
                return resolved(cfg)
            ids = newest(listed)
        except (claude.ClaudeError, OSError, ValueError, subprocess.SubprocessError):
            return resolved(cfg)
        atomic_write(path, json.dumps({"ts": time.time(), "ids": ids}))
        return ids
