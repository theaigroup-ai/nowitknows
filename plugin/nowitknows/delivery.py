from __future__ import annotations

import time
from contextlib import suppress

from . import config as C
from .safety import _PATTERNS, lines, redact
from .state import log_row


def finish_delivery(text: str, masked: bool, cfg: C.Config | None = None) -> str:
    if not masked:
        return text
    if cfg is not None:
        with suppress(OSError, TimeoutError):
            log_row(
                cfg, "activity.jsonl", {"ts": time.time(), "kind": "delivery_masked"}, wait_s=C.ACTIVITY_LOCK_WAIT_S
            )
    return text + "\n" + C.DELIVERY_MASK_NOTE


def masked_delivery(text: str, cfg: C.Config | None = None) -> str:
    clean = redact(text)
    return finish_delivery(clean, clean != text, cfg)


def screened_delivery(text: str, cfg: C.Config | None = None) -> str:
    clean = redact(text)
    parts = lines(clean)
    checks = [rx for label, rx in _PATTERNS if label != "a secret-shaped value"]
    kept = [line for line in parts if not any(rx.search(line) for rx in checks)]
    return finish_delivery(clean if len(kept) == len(parts) else "\n".join(kept), clean != text, cfg)
