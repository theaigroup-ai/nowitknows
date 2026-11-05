from __future__ import annotations

import difflib
import json

from . import claude, model
from . import config as C
from .safety import redact
from .skills import size_label

PROMPTS = C.ROOT / "prompts"
SEVERITIES = ("critical", "major", "minor")
FINDING = {
    "type": "object",
    "additionalProperties": False,
    "required": ["severity", "issue", "fix"],
    "properties": {
        "severity": {"type": "string", "enum": list(SEVERITIES)},
        "issue": {"type": "string"},
        "fix": {"type": "string"},
    },
}
VERDICT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "findings": {"type": "array", "items": FINDING},
    },
}
REVISION = {
    "type": "object",
    "additionalProperties": False,
    "required": ["content"],
    "properties": {"content": {"type": "string"}},
}


def _findings(verdict: dict, label: str) -> list[dict]:
    """The findings of a verdict shaped like VERDICT; any other shape is a judge failure, never a pass."""
    findings = verdict.get("findings")
    if verdict.get("verdict") not in ("pass", "fail") or not isinstance(findings, list):
        raise claude.ClaudeError(f"{label}: malformed verdict")
    for f in findings:
        if not (
            isinstance(f, dict)
            and f.get("severity") in SEVERITIES
            and all(isinstance(f.get(k), str) for k in ("issue", "fix"))
        ):
            raise claude.ClaudeError(f"{label}: malformed finding")
    return findings


def blocking(findings: list[dict]) -> bool:
    return any(f.get("severity") in ("critical", "major") for f in findings)


def _system(prompt: str, evidence: str) -> str:
    # Evidence sits in the system prompt so every judge call of one run shares a cacheable prefix.
    tail = redact(evidence)[-C.DIGEST_MAX_CHARS :]
    return f"{(PROMPTS / prompt).read_text(encoding='utf-8')}\n\n<session_evidence>\n{tail}\n</session_evidence>"


def judge_request(cfg: C.Config, change: dict, diff: str, current: str, evidence: str, existing: list[str]) -> dict:
    case = (
        f"<change action='{change['action']}' skill='{change['name']}'>\n{diff}\n</change>\n\n"
        f"<current_skill>\n{current or '(new skill)'}\n</current_skill>\n\n"
        f"<other_skills>\n{chr(10).join(existing) or '(none)'}\n</other_skills>"
    )
    if change.get("support_files"):
        case += (
            "\n\n<staged_support_files>\n"
            + "\n".join(
                f"<file path={json.dumps(item['file_path'])}>\n{item['content']}\n</file>"
                for item in change["support_files"]
            )
            + "\n</staged_support_files>"
        )
    return {
        "prompt": redact(case),
        "system": _system("judge.md", evidence),
        "model": cfg.judge_model,
        "schema": VERDICT,
        "timeout": C.JUDGE_TIMEOUT_S,
        "label": f"judge:{change['name']}",
        "run_id": change.get("run_id"),
    }


def note_request(
    cfg: C.Config, change: dict, before: str | None, after: str | None, others: list[str], evidence: str
) -> dict:
    """The judge's request for one project note change: `after` is None for a removal, `before` for a new note."""
    name = change["name"]
    verb = "remove" if after is None else "replace" if before is not None else "add"
    diff = "".join(
        difflib.unified_diff(
            (before or "").splitlines(True), (after or "").splitlines(True), f"a/{name}.md", f"b/{name}.md"
        )
    )
    case = (
        f"<change action='{verb}' note='{name}'>\n{diff}\n</change>\n\n"
        f"<current_note>\n{before or '(new note)'}\n</current_note>\n\n"
        f"<other_notes>\n{chr(10).join(others) or '(none)'}\n</other_notes>"
    )
    return {
        "prompt": redact(case),
        "system": _system("judge_notes.md", evidence),
        "model": cfg.judge_model,
        "schema": VERDICT,
        "timeout": C.JUDGE_TIMEOUT_S,
        "label": f"judge-note:{name}",
        "run_id": change.get("run_id"),
    }


def judge_answer(out: dict, label: str) -> dict:
    answer = claude.structured(out)
    findings = _findings(answer, label)
    # A fail stands even when its findings look harmless: a self-contradictory answer must not apply unattended.
    failed = answer["verdict"] == "fail" or blocking(findings)
    return {"verdict": "fail" if failed else "pass", "findings": findings}


def revision_request(cfg: C.Config, change: dict, current: str, findings: list[dict], evidence: str) -> dict:
    issues = "\n".join(f"- [{f['severity']}] {f['issue']} -> {f['fix']}" for f in findings)
    prompt = (
        f"<change action='{change['action']}' skill='{change['name']}'/>\n\n<findings>\n{issues}\n</findings>\n\n"
        f"{size_label(current, change.get('current_chars'))}\n<current_skill>\n"
        f"{current or '(new skill)'}\n</current_skill>\n\n"
        f"{size_label(change.get('content') or '')}\n<proposed_file>\n{change.get('content') or ''}\n</proposed_file>"
    )
    return {
        "prompt": redact(prompt),
        "system": _system("reviser.md", evidence),
        "model": cfg.reviewer_model,
        "schema": REVISION,
        "timeout": C.JUDGE_TIMEOUT_S,
        "label": f"revise:{change['name']}",
        "run_id": change.get("run_id"),
    }


def revision_answer(out: dict, label: str) -> str:
    content = claude.structured(out).get("content")
    if not isinstance(content, str) or not content.strip():
        raise claude.ClaudeError(f"{label}: no revised content")
    return content


def revision_tokens(change: dict, current: str) -> int:
    size = max(
        len(json.dumps(text, ensure_ascii=False).encode("utf-8")) for text in (current, change.get("content") or "")
    )
    estimate = (size + C.MOD_REVISE_CHARS_PER_TOKEN - 1) // C.MOD_REVISE_CHARS_PER_TOKEN
    return min(C.MOD_REVISE_MAX_TOKENS, max(C.MOD_MAX_TOKENS, estimate) + C.MOD_REVISE_OVERHEAD_TOKENS)


def judge(cfg: C.Config, change: dict, diff: str, current: str, evidence: str, existing: list[str]) -> dict:
    request = judge_request(cfg, change, diff, current, evidence, existing)
    return judge_answer(model.run(cfg, **request), request["label"])


def revise(cfg: C.Config, change: dict, current: str, findings: list[dict], evidence: str) -> str:
    request = revision_request(cfg, change, current, findings, evidence)
    return revision_answer(model.run(cfg, **request), request["label"])
