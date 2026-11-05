"""Normalize native Cursor transcripts into NowItKnows's internal transcript schema."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from . import config as C
from . import native, safety, sessions
from .state import atomic_write, file_lock

LOGGER = logging.getLogger(__name__)
USER_QUERY_RE = re.compile(r"<user_query>\s*([\s\S]*?)\s*</user_query>", re.I)
UNCLOSED_USER_QUERY_RE = re.compile(r"<user_query>\s*([\s\S]*)\Z", re.I)
XML_TAG_RE = re.compile(r"<(?P<close>/)?(?P<tag>[a-z_][\w:-]*)(?:\s[^<>]*?)?\s*(?P<self>/)?>", re.I)
LEADING_TIMESTAMP_RE = re.compile(r"\A(?:<timestamp>[^\n]*</timestamp>\s*)+", re.I)

# Scaffold XML blocks Cursor injects into user messages or into the first turn of a transcript.
SCAFFOLD_BLOCK_RE = re.compile(
    r"\A(?:\s*<(?P<tag>"
    r"user_info|project_layout|rules|always_applied_workspace_rules|agent_requestable_workspace_rules|"
    r"user_rules|agent_skills|available_skills|available_subagent_types|available_subagent_models|"
    r"cloud_instructions|cloud_task_instructions|open_and_recently_viewed_files|system_reminder|"
    r"system-reminder|instructions_update|mcp_instructions|mcp_file_system|mcp_file_system_servers|"
    r"mcp_meta_tools|mcp_server_catalog|mcp_meta_tool_servers|dynamic_tool_catalog|dynamic_tool_namespaces|"
    r"git_status|agent_transcripts|cursor_rules_context|attached_files|uploaded_documents|image_files|"
    r"external_links|hooks_context|automation_instructions|user_profile|manually_attached_skills|"
    r"tone_and_style|tool_calling|maximize_parallel_tool_calls|maximize_context_understanding|no_reverts|"
    r"citing_code|inline_line_numbers|terminal_files_information|ask_question_guidance|dynamic_tools|"
    r"system-communication|timestamp"
    r")(?:\s[^>]*)?>[\s\S]*?</(?P=tag)>\s*)+\Z",
    re.I,
)
LEADING_SCAFFOLD_TAG_RE = re.compile(
    r"\A\s*<(?:"
    r"user_info|project_layout|rules|always_applied_workspace_rules|agent_requestable_workspace_rules|"
    r"user_rules|agent_skills|available_skills|available_subagent_types|available_subagent_models|"
    r"cloud_instructions|cloud_task_instructions|open_and_recently_viewed_files|system_reminder|"
    r"system-reminder|instructions_update|mcp_instructions|mcp_file_system|mcp_file_system_servers|"
    r"mcp_meta_tools|mcp_server_catalog|mcp_meta_tool_servers|dynamic_tool_catalog|dynamic_tool_namespaces|"
    r"git_status|agent_transcripts|cursor_rules_context|attached_files|uploaded_documents|image_files|"
    r"external_links|hooks_context|automation_instructions|user_profile|manually_attached_skills"
    r")(?:\s[^>]*)?>",
    re.I,
)
STRIP_PREFIX_BLOCKS_RE = re.compile(
    r"\s*<(?P<tag>"
    r"timestamp|attached_files|uploaded_documents|image_files|external_links|open_and_recently_viewed_files|"
    r"system_reminder|system-reminder|git_status|agent_transcripts|agent_skills|available_subagent_types|"
    r"available_subagent_models|user_info|rules|hooks_context"
    r")(?:\s[^>]*)?>[\s\S]*?</(?P=tag)>\s*",
    re.I,
)
MCP_FALLBACK_NOTICE = (
    "If the available MCP tools do not fully support what the user asked you to do, complete the work you can with "
    "the current tool set."
)
NOTICE_BLOCK_RE = re.compile(
    r"\A\s*<(?P<kind>task_notification|task-notification|subagent_notification|subagent-notification|"
    r"system_notification|agent_notification)(?:\s[^>]*)?>(?P<body>[\s\S]*?)</(?P=kind)>\s*\Z",
    re.I,
)
THINK_TAG_RE = re.compile(r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>", re.I)
BINARY_OMITTED_RE = re.compile(r"\A\[(?:Oversize transcript blob|Binary data) omitted[^\]]*\]\Z")

TEXT_ROLE_RE = re.compile(r"\A(user|assistant):\n([\s\S]*)\Z")
TEXT_TOOL_CALL_RE = re.compile(r"^\[Tool call\]\s+(\S+)(?:\n((?:  .*\n?)*))?", re.M)
TEXT_ARG_LINE_RE = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*(.*)$")
TURN_ENDED_TEXT_RE = re.compile(r"\ATurn ended:\s*(success|error|aborted)(?::\s*(.*))?\.\Z", re.S)


def slugify_path(path: str | Path) -> str:
    """Cursor's project folder slug (`slugifyPath` in `@anysphere/utils`): non-alphanumeric runs become `-`, trimmed."""
    text = str(path or "").strip()
    if not text:
        return ""
    return re.sub(r"^-+|-+$", "", re.sub(r"-+", "-", re.sub(r"[^a-zA-Z0-9]", "-", text)))


def project_key(cwd: str | Path) -> str:
    text = str(cwd or "").strip()
    return native.project_key(Path(text)) if text else ""


def _bump(counts: dict[str, int], key: str, amount: int = 1) -> None:
    counts[key] = counts.get(key, 0) + amount


def _clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.encode("utf-8", "replace").decode("utf-8")


def _redact_text(value: object) -> str:
    return safety.redact(_clean_text(value))


def _timestamp(raw: dict, text: str) -> dict:
    value = raw.get("timestamp")
    if value is None:
        value = raw.get("message", {}).get("timestamp")
    if value is None and (match := re.search(r"<timestamp>([^<]+)</timestamp>", text, re.I)):
        value = match[1].strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            value = datetime.fromtimestamp(value / 1000 if value > 10**11 else value, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return {}
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return {"timestamp": (stamp.astimezone(timezone.utc) if stamp.tzinfo else stamp).isoformat()}
        except ValueError:
            for form in ("%A, %b %d, %Y", "%A, %B %d, %Y"):
                try:
                    return {"timestamp": datetime.strptime(value, form).date().isoformat()}
                except ValueError:
                    continue
    return {}


def _row_id(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[: C.CURSOR_ROW_ID_CHARS]


def _strip_scaffold_subtrees(text: str) -> str:
    parts: list[str] = []
    stack: list[str] = []
    end = 0
    for match in XML_TAG_RE.finditer(text):
        tag = match["tag"].lower()
        if not stack:
            if match["close"] or not SCAFFOLD_BLOCK_RE.fullmatch(f"<{tag}></{tag}>"):
                continue
            parts.append(text[end : match.start()])
        if match["close"]:
            if tag in stack:
                del stack[len(stack) - 1 - stack[::-1].index(tag) :]
        elif not match["self"]:
            stack.append(tag)
        end = match.end()
    if not stack:
        parts.append(text[end:])
    return "".join(parts)


def _top_level_queries(text: str) -> list[str]:
    queries: list[str] = []
    stack: list[str] = []
    start: int | None = None
    for match in XML_TAG_RE.finditer(text):
        tag = match["tag"].lower()
        if match["close"]:
            if tag == "user_query" and start is not None:
                queries.append(text[start : match.start()].strip())
                start = None
            if tag in stack:
                del stack[len(stack) - 1 - stack[::-1].index(tag) :]
        elif not match["self"]:
            if tag == "user_query" and not stack:
                start = match.end()
            stack.append(tag)
    if start is not None:
        queries.append(text[start:].strip())
    return [query for query in queries if query]


def _extract_user_text(raw: str) -> tuple[str, str | None] | None:
    """Return `(clean_text, notice_subkind)` for a genuine user message or notice, or None for Cursor scaffold."""
    text = _clean_text(raw).strip()
    if not text or BINARY_OMITTED_RE.fullmatch(text):
        return None
    if m := NOTICE_BLOCK_RE.fullmatch(text):
        body = _redact_text(m.group("body").strip())
        return (body, "subagent_notification") if body else None
    text = _strip_scaffold_subtrees(text).strip()
    queries = _top_level_queries(text)
    if queries:
        return _redact_text("\n\n".join(queries)), None
    if USER_QUERY_RE.search(text) or UNCLOSED_USER_QUERY_RE.search(text):
        return None
    if text.startswith(MCP_FALLBACK_NOTICE) or SCAFFOLD_BLOCK_RE.fullmatch(text):
        return None
    stripped = STRIP_PREFIX_BLOCKS_RE.sub("", LEADING_TIMESTAMP_RE.sub("", text)).strip()
    if not stripped or LEADING_SCAFFOLD_TAG_RE.match(stripped) or stripped.startswith(MCP_FALLBACK_NOTICE):
        return None
    if m := NOTICE_BLOCK_RE.fullmatch(stripped):
        body = _redact_text(m.group("body").strip())
        return (body, "subagent_notification") if body else None
    return _redact_text(stripped), None


def latest_user_message(path: Path) -> str:
    """Read the latest genuine user message in a bounded native JSONL tail, ignoring incomplete records."""
    with path.open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        start = max(0, size - C.CURSOR_PROMPT_TAIL_BYTES)
        stream.seek(start)
        tail = stream.read(C.CURSOR_PROMPT_TAIL_BYTES)
    lines = tail.split(b"\n")
    if start:
        lines = lines[1:]
    for line in reversed(lines):
        try:
            raw = json.loads(line.decode("utf-8").lstrip("\ufeff"))
        except (ValueError, UnicodeError):
            continue
        if not isinstance(raw, dict) or raw.get("role") != "user" or not isinstance(raw.get("message"), dict):
            continue
        content = raw["message"].get("content")
        items = [{"type": "text", "text": content}] if isinstance(content, str) else content
        if not isinstance(items, list):
            continue
        parts = []
        for item in items:
            if isinstance(item, dict) and item.get("type") == "text":
                extracted = _extract_user_text(item.get("text", ""))
                if extracted and extracted[1] is None:
                    parts.append(extracted[0])
        if parts:
            return "\n\n".join(parts)
    return ""


def _normalize_mcp_name(server: str, tool: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]", "_", server.strip()) or "mcp"
    t = re.sub(r"[^a-zA-Z0-9_-]", "_", tool.strip()) or "call"
    for prefix in ("plugin-nowitknows-nowitknows", "user-nowitknows", "nowitknows"):
        if s == prefix or s.endswith("-" + prefix):
            s = "nowitknows"
            break
    return f"mcp__{s}__{t}"


def _normalize_tool_call(name: str, raw_input: object, *, identity: bool = False) -> tuple[str, dict]:
    """Map Cursor tool names and arguments onto the safe digest keys `sessions.tool_line` expects."""
    from .outcome import command_text

    tool = _clean_text(name).strip() or "unknown"
    args = raw_input if isinstance(raw_input, dict) else {}
    if tool in ("CallMcpTool", "CallDynamicTool"):
        server = _clean_text(args.get("server") or args.get("namespace") or "mcp")
        inner = _clean_text(args.get("toolName") or "call")
        nested = args.get("arguments")
        nested = nested if isinstance(nested, dict) else {}
        tool = _normalize_mcp_name(server, inner)
        args = nested
    elif tool.startswith("MCP:"):
        inner = tool.partition(":")[2].strip() or "call"
        safe_inner = re.sub(r"[^a-zA-Z0-9_-]", "_", inner)
        if safe_inner in ("memory", "session_search", "skill_view", "skill_manage"):
            tool = f"mcp__nowitknows__{safe_inner}"
        else:
            tool = f"mcp__cursor__{safe_inner}"
    elif tool in ("GetMcpTools", "GetDynamicTools"):
        server = _clean_text(args.get("server") or args.get("namespace") or "")
        inner = _clean_text(args.get("toolName") or args.get("pattern") or args.get("query") or "")
        what = "/".join(p for p in (server, inner) if p)
        return tool, ({"query": _redact_text(what)} if what else {})
    safe: dict = {}
    for k, v in args.items():
        key = _clean_text(k)
        if not key:
            continue
        if key == "cmd" and "command" not in args:
            target_key = "command"
        elif key == "path" and "file_path" not in args:
            target_key = "file_path"
        elif key in ("glob_pattern", "search_term") and "pattern" not in args:
            target_key = "pattern"
        else:
            target_key = key
        if identity:
            safe[target_key] = v
        elif target_key in (*sessions.TOOL_ARG_KEYS, "cwd", "workdir") and isinstance(v, str) and v.strip():
            safe[target_key] = command_text(v) if target_key == "command" else _redact_text(v)
        else:
            safe[key] = None
    if not identity and _shell_background(tool, args):
        safe["run_in_background"] = True
    return tool, safe


def _shell_background(name: str, args: dict) -> bool:
    return name in ("Shell", "Bash") and any(
        args.get(key) is True or (isinstance(args.get(key), str) and args[key].lower() == "true")
        for key in ("is_background", "isBackground", "run_in_background")
    )


def _is_subagent_path(path: Path) -> bool:
    return "subagents" in path.parts


def _session_id_from_path(path: Path) -> str:
    name = path.name
    for suffix in (".jsonl.gz", ".jsonl.zst", ".jsonl", ".txt.gz", ".txt"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _tool_key(name: str, *, per_tool: bool = False) -> str:
    name, _ = _normalize_tool_call(name, {})
    bare = C.tool_name(name)
    aliases = {"Bash": "Shell"}
    if per_tool:
        aliases.update({"StrReplace": "Write", "Glob": "Grep"})
    return f"mcp__nowitknows__{bare}" if bare is not None else aliases.get(name, name)


def _input_key(safe_input: dict) -> str:
    return hashlib.sha256(
        safety.redact(json.dumps({k: v for k, v in safe_input.items() if v is not None}, sort_keys=True)).encode(
            "utf-8"
        )
    ).hexdigest()


def _hook_shell_command(command: str) -> str:
    """Remove the observed Cursor trailer only from a single plain git commit invocation."""
    literal = 'git commit --trailer "Co-authored-by: Cursor <cursoragent@cursor.com>"'
    if not command.startswith(literal) or any(part in command for part in ("\n", "\r", "<<", "$", "`")):
        return command
    word = r"""(?:[^\s;&|<>()'"\\`$#]|\\[^\r\n]|'[^'\r\n]*'|"(?:\\[^\r\n]|[^"\\\r\n])*")+"""
    suffix = command[len(literal) :]
    if not re.fullmatch(rf"(?:[ \t]+{word})*[ \t]*", suffix):
        return command
    return "git commit" + suffix


def _identity_fields(name: str, raw_input: object, *, hook: bool = False) -> dict | None:
    """Select fields shared by Cursor's hook and transcript serializers, before display truncation."""
    tool, args = _normalize_tool_call(name, raw_input, identity=True)
    family = _tool_key(tool, per_tool=True)
    if family == "Shell":
        command = args.get("command")
        if hook and isinstance(command, str):
            command = _hook_shell_command(command)
        identity = {"command": command}
    elif family in ("Write", "Read"):
        identity = {"file_path": args.get("file_path")}
    elif family == "Grep":
        path = args.get("file_path", args.get("target_directory"))
        if tool == "Glob":
            pattern = args.get("pattern")
            # Cursor expands a basename glob to a recursive glob before calling its Grep hook.
            if isinstance(pattern, str) and pattern and "/" not in pattern:
                pattern = "**/" + pattern
            identity = {"glob": pattern, "file_path": path}
        else:
            identity = {"pattern": args.get("pattern"), "file_path": path}
            if args.get("glob"):
                identity["glob"] = args["glob"]
                if not identity["pattern"]:
                    del identity["pattern"]
    else:
        return args
    if not all(isinstance(value, str) and value.strip() for value in identity.values()):
        return None
    return identity


def _identity_key(name: str, raw_input: object, *, hook: bool = False) -> str | None:
    fields = _identity_fields(name, raw_input, hook=hook)
    return _input_key(fields) if fields is not None else None


def _identity_masked(name: str, raw_input: object) -> bool:
    fields = _identity_fields(name, raw_input)
    return fields is not None and "[redacted]" in safety.redact(json.dumps(fields, sort_keys=True))


def _matches_input(call: dict, item: dict) -> bool:
    per_tool = "identity_key" in item
    if _tool_key(str(item.get("tool") or ""), per_tool=per_tool) != _tool_key(call["name"], per_tool=per_tool):
        return False
    if "identity_key" in item:
        return bool(item["identity_key"]) and item["identity_key"] == call.get("cursor_identity_key")
    if item.get("input_key"):
        return item["input_key"] == call.get("cursor_input_key", _input_key(call["input"]))
    arg = item.get("arg")
    if not isinstance(arg, str) or not arg:
        return True
    return any(isinstance(value, str) and value.strip()[: C.TOOL_ARG_CHARS] == arg for value in call["input"].values())


def _convert_txt(
    path: Path,
    sid: str,
    project: str,
    sidechain: bool,
    outcomes: list[dict],
    counts: dict[str, int],
) -> list[dict]:
    from . import outcome

    try:
        with sessions.open_history(path) as stream:
            raw = stream.read().decode("utf-8", "replace").lstrip("\ufeff")
    except OSError:
        _bump(counts, "read_errors")
        return []
    rows: list[dict] = []
    separator = (
        r"(?:\r?\n){2,}(?=(?:user|assistant):\r?\n|Turn ended:)"
        if C.WINDOWS
        else r"\n{2,}(?=(?:user|assistant):\n|Turn ended:)"
    )
    blocks = [b.strip() for b in re.split(separator, raw) if b.strip()]
    turn_idx = 0
    position = 0
    for block_idx, block in enumerate(blocks):
        position = raw.index(block, position) + len(block)
        if C.WINDOWS:
            block = block.replace("\r\n", "\n")
        if m := TURN_ENDED_TEXT_RE.fullmatch(block):
            turn_idx += 1
            continue
        m = TEXT_ROLE_RE.fullmatch(block)
        if not m:
            _bump(counts, "unsupported_records")
            continue
        role, body = m.group(1), m.group(2)
        meta = {
            "source_line": block_idx + 1,
            "turn_id": f"turn-{turn_idx}",
            "format": "txt",
            "source_offset": len(raw[:position].encode("utf-8")),
            **_timestamp({}, body),
        }
        if role == "user":
            extracted = _extract_user_text(body)
            if extracted is None:
                continue
            text, subkind = extracted
            row = {
                "type": "user",
                "uuid": _row_id(f"{sid}:txt:u:{block_idx}:{text[: C.CURSOR_PLAN_SNIPPET_CHARS]}"),
                "sessionId": sid,
                "cwd": project,
                "isSidechain": sidechain,
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
                "cursor": meta,
            }
            if subkind:
                row["origin"] = {"kind": C.NOTICE_ORIGIN, "subkind": subkind}
            else:
                _bump(counts, "human_messages")
            rows.append(row)
        else:
            content_blocks: list[dict] = []
            cleaned = THINK_TAG_RE.sub("", body)
            narration_lines = []
            pos = 0
            for tm in TEXT_TOOL_CALL_RE.finditer(cleaned):
                before = cleaned[pos : tm.start()].strip()
                if before:
                    narration_lines.append(before)
                pos = tm.end()
                tname = tm.group(1)
                raw_args: dict = {}
                for line in (tm.group(2) or "").splitlines():
                    if am := TEXT_ARG_LINE_RE.match(line):
                        raw_args[am.group(1)] = am.group(2).strip()
                norm_name, safe_in = _normalize_tool_call(tname, raw_args)
                call_id = _row_id(f"{sid}:txt:tc:{block_idx}:{len(content_blocks)}:{norm_name}")
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": norm_name,
                        "input": safe_in,
                        "cursor_input_key": _input_key(_normalize_tool_call(tname, raw_args, identity=True)[1]),
                        "cursor_identity_key": _identity_key(tname, raw_args),
                        "cursor_identity_masked": _identity_masked(tname, raw_args),
                        "outcome_success_output": outcome.success_output_allowed(
                            raw_args.get("command", raw_args.get("cmd", "")), project
                        ),
                    }
                )
            tail = cleaned[pos:].strip()
            if tail and not tail.startswith("[Tool result]"):
                narration_lines.append(tail)
            narration = "\n".join(
                ln
                for ln in "\n".join(narration_lines).splitlines()
                if not ln.strip().startswith(("[Tool result]", "[Thinking]"))
            ).strip()
            if narration:
                content_blocks.insert(0, {"type": "text", "text": _redact_text(narration)})
                _bump(counts, "assistant_messages")
            if not content_blocks:
                continue
            rows.append(
                {
                    "type": "assistant",
                    "uuid": _row_id(f"{sid}:txt:a:{block_idx}"),
                    "sessionId": sid,
                    "cwd": project,
                    "isSidechain": sidechain,
                    "message": {"role": "assistant", "content": content_blocks},
                    "cursor": meta,
                }
            )
            for b in content_blocks:
                if b.get("type") == "tool_use":
                    _bump(counts, "tool_calls")
                    rows.append(_tool_result_row(sid, project, sidechain, b["id"], None, meta))
                    _bump(counts, "tool_results")
    return _dated(_bind_outcomes(rows, outcomes))


def _tool_result_row(
    sid: str,
    project: str,
    sidechain: bool,
    call_id: str,
    outcome: dict | None,
    meta: dict,
) -> dict:
    from .outcome import output_text

    ambiguous = bool(outcome and outcome.get("status") == "ambiguous")
    background = bool(outcome and outcome.get("background"))
    incomplete = background or ambiguous or bool(outcome and outcome.get("status") == "unknown")
    is_error = bool(
        outcome and (outcome.get("status") in ("failed", "denied", "rejected") or outcome.get("ambiguous_failure"))
    )
    text = _redact_text(outcome.get("text") or ("error" if is_error else "ok")) if outcome else "outcome unknown"
    text = text[: C.TOOL_ERROR_CHARS]
    cursor_meta = {
        **meta,
        "outcome": "background" if background else outcome.get("status", "unknown") if outcome else "unknown",
        "ran": None if outcome is None or incomplete else outcome.get("status") not in ("denied", "rejected"),
    }
    row = {
        "type": "user",
        "uuid": _row_id(f"{sid}:tr:{call_id}"),
        "sessionId": sid,
        "cwd": project,
        "isSidechain": sidechain,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result" if outcome is not None else "cursor_tool_outcome",
                    "tool_use_id": call_id,
                    "content": text,
                    **(
                        {"outcome_output": output_text(str(outcome.get("output_tail", outcome.get("text")) or ""))}
                        if outcome
                        else {}
                    ),
                    **({"is_error": is_error} if outcome is not None else {}),
                    **({"outcome_complete": False} if incomplete else {}),
                    **({"outcome_running": True} if background else {}),
                    **({"cursor_identity_ambiguous": True} if ambiguous else {}),
                    **({"outcome_exit_code": outcome["exit_code"]} if outcome and "exit_code" in outcome else {}),
                }
            ],
        },
        "cursor": cursor_meta,
    }
    if outcome and outcome.get("status") == "denied":
        row["permissionDecision"] = {"decision": "reject", "reasonType": "mode"}
    return row


def convert(
    source: Path,
    cwd: str | Path | None = None,
    *,
    expected_session: str | None = None,
    outcomes: list[dict] | None = None,
    diagnostics: dict[str, int] | None = None,
) -> list[dict]:
    """Convert a native Cursor `.jsonl` (or `.txt` fallback) transcript into normalized rows."""
    from . import outcome

    counts = diagnostics if diagnostics is not None else {}
    for key in (
        "lines",
        "blank_lines",
        "malformed_records",
        "oversized_records",
        "unsupported_records",
        "human_messages",
        "assistant_messages",
        "tool_calls",
        "tool_results",
        "read_errors",
    ):
        counts.setdefault(key, 0)

    path = Path(source)
    sid = (
        expected_session.strip()
        if isinstance(expected_session, str) and expected_session.strip()
        else _session_id_from_path(path)
    )
    project = _clean_text(str(cwd)) if cwd else ""
    sidechain = _is_subagent_path(path)
    counts["session_id"] = sid
    counts["session_ids"] = [sid] if sid else []
    recorded_outcomes = list(outcomes or [])

    if path.name.endswith((".txt", ".txt.gz")):
        return _convert_txt(path, sid, project, sidechain, recorded_outcomes, counts)

    rows: list[dict] = []
    turn_idx = 0
    source_offset = 0
    pending_tool_results: list[tuple[str, dict]] = []
    turn_failures: dict[str, str] = {}

    def flush_pending_tools() -> None:
        nonlocal pending_tool_results
        for call_id, meta in pending_tool_results:
            rows.append(_tool_result_row(sid, project, sidechain, call_id, None, meta))
            _bump(counts, "tool_results")
        pending_tool_results = []

    try:
        with sessions.open_history(path) as stream:
            for line_number, text in enumerate(sessions.history_lines(stream, counts, text=True), 1):
                _bump(counts, "lines")
                source_offset = (
                    source_offset + len(text.encode("utf-8"))
                    if text is not None and source_offset is not None
                    else None
                )
                if text is None:
                    continue
                if line_number == 1:
                    text = text.lstrip("\ufeff")
                if not text.strip():
                    _bump(counts, "blank_lines")
                    continue
                if "\ufffd" in text:
                    _bump(counts, "malformed_records")
                    continue
                try:
                    raw = json.loads(text)
                except ValueError:
                    _bump(counts, "malformed_records")
                    continue
                if not isinstance(raw, dict):
                    _bump(counts, "malformed_records")
                    continue

                record_type = raw.get("type")
                if record_type == "metadata":
                    continue
                if record_type == "turn_ended":
                    status = raw.get("status")
                    error_msg = _redact_text(raw.get("error") or "")
                    if status in ("error", "aborted") and pending_tool_results:
                        call_id, meta = pending_tool_results[-1]
                        turn_failures[call_id] = error_msg or f"turn {status}"
                    flush_pending_tools()
                    turn_idx += 1
                    continue

                role = raw.get("role")
                msg = raw.get("message")
                if role not in ("user", "assistant") or not isinstance(msg, dict):
                    _bump(counts, "unsupported_records")
                    continue

                content = msg.get("content")
                if isinstance(content, str):
                    items = [{"type": "text", "text": content}]
                elif isinstance(content, list):
                    items = [it for it in content if isinstance(it, dict)]
                else:
                    _bump(counts, "malformed_records")
                    continue

                meta = {
                    "source_line": line_number,
                    "source_offset": source_offset,
                    "turn_id": f"turn-{turn_idx}",
                    **_timestamp(raw, "\n".join(str(item.get("text", "")) for item in items)),
                }
                if role == "user":
                    flush_pending_tools()
                    parts: list[str] = []
                    notice_subkind: str | None = None
                    for item in items:
                        if item.get("type") == "text":
                            extracted = _extract_user_text(item.get("text", ""))
                            if extracted is not None:
                                piece, subkind = extracted
                                parts.append(piece)
                                notice_subkind = notice_subkind or subkind
                    joined = "\n\n".join(p for p in parts if p).strip()
                    if not joined:
                        continue
                    row = {
                        "type": "user",
                        "uuid": _row_id(f"{sid}:u:{line_number}:{joined[: C.CURSOR_PLAN_SNIPPET_CHARS]}"),
                        "sessionId": sid,
                        "cwd": project,
                        "isSidechain": sidechain,
                        "message": {"role": "user", "content": [{"type": "text", "text": joined}]},
                        "cursor": meta,
                    }
                    if notice_subkind:
                        row["origin"] = {"kind": C.NOTICE_ORIGIN, "subkind": notice_subkind}
                    else:
                        _bump(counts, "human_messages")
                    rows.append(row)
                else:
                    flush_pending_tools()
                    out_blocks: list[dict] = []
                    text_parts: list[str] = []
                    for item in items:
                        itype = item.get("type")
                        if itype == "text":
                            cleaned = THINK_TAG_RE.sub("", _clean_text(item.get("text", ""))).strip()
                            if cleaned and not BINARY_OMITTED_RE.fullmatch(cleaned):
                                text_parts.append(_redact_text(cleaned))
                        elif itype == "tool_use":
                            norm_name, safe_in = _normalize_tool_call(item.get("name", ""), item.get("input"))
                            seed = f"{sid}:tc:{line_number}:{len(out_blocks)}"
                            call_id = _clean_text(item.get("id")) or _row_id(seed)
                            raw_args = item.get("input") if isinstance(item.get("input"), dict) else {}
                            command = raw_args.get("command", raw_args.get("cmd", ""))
                            out_blocks.append(
                                {
                                    "type": "tool_use",
                                    "id": call_id,
                                    "name": norm_name,
                                    "input": safe_in,
                                    "cursor_input_key": _input_key(
                                        _normalize_tool_call(item.get("name", ""), raw_args, identity=True)[1]
                                    ),
                                    "cursor_identity_key": _identity_key(item.get("name", ""), raw_args),
                                    "cursor_identity_masked": _identity_masked(item.get("name", ""), raw_args),
                                    "outcome_success_output": isinstance(command, str)
                                    and outcome.success_output_allowed(command, project),
                                }
                            )
                            pending_tool_results.append((call_id, meta))
                            _bump(counts, "tool_calls")
                    if text_parts:
                        out_blocks.insert(0, {"type": "text", "text": "\n\n".join(text_parts)})
                        _bump(counts, "assistant_messages")
                    if not out_blocks:
                        continue
                    rows.append(
                        {
                            "type": "assistant",
                            "uuid": _row_id(f"{sid}:a:{line_number}"),
                            "sessionId": sid,
                            "cwd": project,
                            "isSidechain": sidechain,
                            "message": {"role": "assistant", "content": out_blocks},
                            "cursor": meta,
                        }
                    )
        flush_pending_tools()
    except OSError:
        _bump(counts, "read_errors")
        LOGGER.warning("Cursor transcript could not be read")
        return []
    rows = _bind_outcomes(rows, recorded_outcomes)
    for index, row in enumerate(rows):
        for block in row.get("message", {}).get("content", []):
            if block.get("type") == "cursor_tool_outcome" and (error := turn_failures.get(block["tool_use_id"])):
                rows[index] = _tool_result_row(
                    sid, project, sidechain, block["tool_use_id"], {"status": "failed", "text": error}, row["cursor"]
                )
    return _dated(rows)


def _dated(rows: list[dict]) -> list[dict]:
    _bounded_commands(rows)
    _display_outcomes(rows)
    for row in rows:
        if stamp := row.get("cursor", {}).get("timestamp"):
            row["timestamp"] = stamp
    return rows


def _display_outcomes(rows: list[dict]) -> None:
    evidence = {
        block["tool_use_id"]: "Cursor outcome evidence: " + block["content"]
        for row in rows
        for block in row.get("message", {}).get("content", [])
        if isinstance(block, dict)
        and (block.get("cursor_identity_ambiguous") or block.get("outcome_running"))
        and block.get("is_error")
    }
    unknown = {
        block["tool_use_id"]
        for row in rows
        for block in row.get("message", {}).get("content", [])
        if isinstance(block, dict) and block.get("type") == "cursor_tool_outcome"
    }
    for row in rows:
        if row.get("cursor", {}).get("unbound_outcome"):
            continue
        content = row.get("message", {}).get("content", [])
        content[:] = [
            block for block in content if not isinstance(block, dict) or not block.get("cursor_outcome_evidence")
        ]
        for block in list(content):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if detail := evidence.get(block.get("id")):
                note = {"type": "text", "text": detail, "cursor_outcome_evidence": True}
                if note not in content:
                    content.append(note)
            original = block.pop("cursor_tool_name", block["name"])
            block["name"] = original
            if block.get("id") in unknown:
                # The shared digest calls every non-error tool_result a success; absence is no result.
                block["cursor_tool_name"] = original
                block["name"] = original + " -> outcome unknown"


def _ambiguous_outcome(observed: list[dict], *, masked: bool = False) -> dict | None:
    failed = any(item.get("status") in ("failed", "denied", "rejected") for item in observed)
    if not failed and not masked:
        return None
    observed = sorted(observed, key=lambda item: item.get("status") not in ("failed", "denied", "rejected"))
    detail = "; ".join(f"{item.get('status', 'unknown')}: {item.get('text', '')}" for item in observed)
    return {
        "status": "ambiguous",
        "text": "Outcome identity is ambiguous; observed outcomes: " + detail,
        "ambiguous_failure": failed,
    }


def _bind_outcomes(rows: list[dict], outcomes: list[dict]) -> list[dict]:
    """Align the complete transcript with recorded completions, independent of serializer flush timing."""
    calls = [
        block
        for row in rows
        if row.get("type") == "assistant"
        for block in row["message"]["content"]
        if block.get("type") == "tool_use"
    ]
    call_ids = {call["id"] for call in calls}
    background = {call["id"] for call in calls if _shell_background(call["name"], call["input"])}
    messages = {
        call["id"]: index
        for index, row in enumerate(rows)
        for call in row.get("message", {}).get("content", [])
        if call.get("type") == "tool_use"
    }
    bound: dict[str, dict | None] = {}
    exact: dict[str, list[dict]] = {}
    for item in outcomes:
        if item.get("call_id") in call_ids:
            exact.setdefault(item["call_id"], []).append(item)
    for identity, observed in exact.items():
        if any(item.get("background") for item in observed):
            background.add(identity)
        bound[identity] = next(
            (item for item in observed if item.get("status") in ("failed", "denied", "rejected")), observed[0]
        )

    remaining = [call for call in calls if call["id"] not in exact]
    pending = [item for item in outcomes if item.get("call_id") not in exact and not item.get("identity_missing")]
    candidates = {
        index: {call["id"] for call in remaining if _matches_input(call, item)} for index, item in enumerate(pending)
    }
    unbound = [
        pending[index]
        for index, identities in candidates.items()
        if not identities and not any(_matches_input(call, pending[index]) for call in calls)
    ]
    # Connected candidates isolate uncertainty to calls that share the recorded identity.
    while candidates:
        index, identities = candidates.popitem()
        if not identities:
            continue
        indexes = {index}
        while True:
            connected = [i for i, ids in candidates.items() if ids & identities]
            if not connected:
                break
            for i in connected:
                identities.update(candidates.pop(i))
                indexes.add(i)
        group = [call for call in remaining if call["id"] in identities]
        observed = [pending[i] for i in sorted(indexes)]
        if not identities & background and any(item.get("background") for item in observed):
            background.update(identities)
        inputs = {
            call.get("cursor_identity_key") or call.get("cursor_input_key", _input_key(call["input"])) for call in group
        }
        sequential = len({messages[call["id"]] for call in group}) == len(group)
        masked = any(call.get("cursor_identity_masked") for call in group) or any(
            item.get("identity_masked") for item in observed
        )
        # Background completions can arrive after a later call, so their order cannot bind a repeat group.
        if (
            not masked
            and not identities & background
            and sequential
            and len(inputs) == 1
            and len(group) == len(observed)
        ):
            bound.update((call["id"], item) for call, item in zip(group, observed, strict=True))
        else:
            ambiguous = _ambiguous_outcome(observed, masked=masked)
            bound.update((call["id"], ambiguous) for call in group)
    recorded = {identity for identity, item in bound.items() if item is not None}
    for identity in background:
        bound[identity] = {**(bound.get(identity) or {"status": "unknown"}), "background": True}
    for index, row in enumerate(rows):
        for block in row.get("message", {}).get("content", []):
            if block.get("type") in ("tool_result", "cursor_tool_outcome"):
                identity = block["tool_use_id"]
                rows[index] = _tool_result_row(
                    row["sessionId"], row["cwd"], row["isSidechain"], identity, bound.get(identity), row["cursor"]
                )
                if identity in recorded:
                    rows[index]["cursor"]["outcome_binding"] = "complete"
                    rows[index]["cursor"]["outcome_exact"] = identity in exact
    for item in unbound:
        if item.get("status") not in ("failed", "denied", "rejected"):
            continue
        anchor = next((row for row in reversed(rows) if row.get("type") == "assistant"), None)
        if anchor is None:
            continue
        # Unmatched failures are session evidence, never proof of any particular call's status.
        detail = _redact_text(_ambiguous_outcome([item])["text"])[: C.TOOL_ERROR_CHARS]
        rows.append(
            {
                **anchor,
                "uuid": _row_id(f"{anchor['sessionId']}:unbound:{json.dumps(item, sort_keys=True)}"),
                "cursor": {**anchor["cursor"], "unbound_outcome": True},
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Cursor outcome evidence: " + detail, "cursor_outcome_evidence": True}
                    ],
                },
            }
        )
    return rows


def _entry_key(row: dict) -> tuple[str | None, str | None]:
    return row.get("type"), row.get("uuid")


def _merge_content(before: object, after: object, *, newest_first: bool = False) -> object:
    if before == after:
        return before
    if isinstance(before, str) and isinstance(after, str):
        if newest_first:
            before, after = after, before
        if before == "ok":
            return after
        if after == "ok":
            return before
        if before in after:
            return after
        return before if after in before else before + "\n" + after
    if isinstance(before, list) and isinstance(after, list):
        # Derived evidence is rebuilt from retained results, so it cannot merge into assistant narration.
        merged = [block for block in before if not isinstance(block, dict) or not block.get("cursor_outcome_evidence")]
        for block in (
            block for block in after if not isinstance(block, dict) or not block.get("cursor_outcome_evidence")
        ):
            if block in merged:
                continue
            match = next(
                (
                    i
                    for i, old in enumerate(merged)
                    if isinstance(old, dict)
                    and isinstance(block, dict)
                    and old.get("type") == block.get("type")
                    and (
                        old.get("type") == "text"
                        or (old.get("tool_use_id") == block.get("tool_use_id") and old.get("type") == "tool_result")
                        or (old.get("id") == block.get("id") and old.get("type") == "tool_use")
                    )
                ),
                None,
            )
            if match is None:
                merged.append(block)
            else:
                old = merged[match]
                key = "text" if block["type"] == "text" else "content"
                merged_blk = {**old, **block}
                if key in old or key in block:
                    merged_blk[key] = _merge_content(old.get(key, ""), block.get(key, ""), newest_first=newest_first)
                if block["type"] == "tool_result":
                    merged_blk["is_error"] = bool(old.get("is_error")) or bool(block.get("is_error"))
                merged[match] = merged_blk
        return merged
    return after if after is not None else before


def _merge_row(old: dict, new: dict) -> dict:
    if old.get("cursor", {}).get("outcome") == new.get("cursor", {}).get("outcome") == "background" and not new.get(
        "cursor", {}
    ).get("outcome_binding"):
        return old
    if old.get("cursor", {}).get("outcome") == "ambiguous" and new.get("cursor", {}).get("outcome_exact"):
        return new
    if (
        old.get("cursor", {}).get("outcome") == "ok"
        and not old.get("cursor", {}).get("outcome_binding")
        and not any(
            block.get("type") == "tool_result" and block.get("is_error")
            for block in old.get("message", {}).get("content", [])
            if isinstance(block, dict)
        )
    ):
        # Legacy offsets could assign another tool's success to this call.
        return new
    legacy_ambiguity = [
        block.get("content", "")
        for block in old.get("message", {}).get("content", [])
        if isinstance(block, dict)
        and block.get("type") == "tool_result"
        and str(block.get("content", "")).startswith("Outcome identity is ambiguous; observed outcomes: ")
    ]
    if (
        legacy_ambiguity
        and new.get("cursor", {}).get("outcome_binding")
        and new.get("cursor", {}).get("outcome") not in (None, "unknown", "ambiguous", "background")
    ):
        return new
    if legacy_ambiguity and not old.get("cursor", {}).get("outcome_binding"):
        all_ok = all(
            not re.search(r"(?:^|; )(?:failed|denied|rejected):", text.partition("observed outcomes: ")[2])
            for text in legacy_ambiguity
        )
        if all_ok:
            return new
    if new.get("cursor", {}).get("outcome") == "unknown":
        observed = old.get("cursor", {}).get("outcome") not in (None, "unknown") or any(
            block.get("type") == "tool_result"
            and (
                block.get("outcome_complete") is False
                or block.get("is_error")
                or block.get("content") not in (None, "", "ok")
            )
            for block in old.get("message", {}).get("content", [])
            if isinstance(block, dict)
        )
        return old if observed else new
    if old.get("cursor", {}).get("outcome") == "unknown":
        return new
    if "message" not in old or "message" not in new:
        return {**old, **new}
    old_msg = old.get("message") if isinstance(old.get("message"), dict) else {}
    new_msg = new.get("message") if isinstance(new.get("message"), dict) else {}
    message = {**old_msg, **new_msg}
    message["content"] = _merge_content(
        old_msg.get("content", ""),
        new_msg.get("content", ""),
        newest_first=new.get("cursor", {}).get("outcome") == "background",
    )
    merged = {**old, **new, "message": message}
    old_res = old.get("toolUseResult") if isinstance(old.get("toolUseResult"), dict) else {}
    new_res = new.get("toolUseResult") if isinstance(new.get("toolUseResult"), dict) else {}
    if old_res or new_res:
        merged["toolUseResult"] = {**old_res, **new_res}
    return merged


def merged_order(old: list[tuple[str | None, str | None]], incoming: list[tuple[str | None, str | None]]) -> list:
    old = list(dict.fromkeys(old))
    old_set = set(old)
    before: dict[tuple[str | None, str | None] | None, list[tuple[str | None, str | None]]] = {}
    anchor: tuple[str | None, str | None] | None = None
    seen_new: set[tuple[str | None, str | None]] = set()
    for key in reversed(incoming):
        if key in old_set:
            anchor = key
        elif key not in seen_new:
            seen_new.add(key)
            before.setdefault(anchor, []).append(key)
    order: list[tuple[str | None, str | None]] = []
    for key in old:
        if key in before:
            order.extend(reversed(before[key]))
        order.append(key)
    if None in before:
        order.extend(reversed(before[None]))
    return order


def merge_normalized(target: Path, incoming: list[dict]) -> list[dict]:
    with file_lock(target):
        existing: dict[tuple[str | None, str | None], dict] = {}
        old_keys: list[tuple[str | None, str | None]] = []
        if target.exists():
            for line in target.read_text(encoding="utf-8", errors="replace").split("\n"):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    key = _entry_key(row)
                    old_keys.append(key)
                    existing[key] = _merge_row(existing[key], row) if key in existing else row
        _source_identities(list(existing.values()), incoming)
        migrated = {}
        for row in existing.values():
            key = _entry_key(row)
            migrated[key] = _merge_row(migrated[key], row) if key in migrated else row
        existing = migrated
        old_keys = list(existing)
        incoming_by_key: dict[tuple[str | None, str | None], dict] = {}
        incoming_keys: list[tuple[str | None, str | None]] = []
        for row in incoming:
            key = _entry_key(row)
            incoming_keys.append(key)
            incoming_by_key[key] = _merge_row(incoming_by_key[key], row) if key in incoming_by_key else row
        order = merged_order(old_keys, incoming_keys)
        merged: list[dict] = []
        for key in order:
            if key in existing and key in incoming_by_key:
                merged.append(_merge_row(existing[key], incoming_by_key[key]))
            elif key in incoming_by_key:
                merged.append(incoming_by_key[key])
            else:
                merged.append(existing[key])
        _bounded_commands(merged)
        _display_outcomes(merged)
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in merged)
        atomic_write(target, body)
        return merged


def _source_identities(retained: list[dict], incoming: list[dict]) -> None:
    """Migrate content-derived fallback ids by their source row and tool position, preserving observed results."""
    old_rows = {_entry_key(row): row for row in retained}
    aliases = {}
    for row in incoming:
        old = old_rows.get(_entry_key(row), {})
        tools = [b for b in row.get("message", {}).get("content", []) if b.get("type") == "tool_use"]
        for index, block in enumerate(tools):
            for before in old.get("message", {}).get("content", []):
                if before.get("type") != "tool_use":
                    continue
                name = before.get("cursor_tool_name", before.get("name", ""))
                encoded = json.dumps(before.get("input", {}), sort_keys=True)
                seed = f"{row.get('sessionId')}:tc:{row.get('cursor', {}).get('source_line')}:{index}:{name}:{encoded}"
                if before.get("id") == _row_id(seed):
                    aliases[before["id"]] = block["id"]
    for row in retained:
        for block in row.get("message", {}).get("content", []):
            if block.get("type") == "tool_use" and block.get("id") in aliases:
                block["id"] = aliases[block["id"]]
            elif block.get("tool_use_id") in aliases:
                block["tool_use_id"] = aliases[block["tool_use_id"]]
                row["uuid"] = _row_id(f"{row.get('sessionId')}:tr:{block['tool_use_id']}")


def _bounded_commands(rows: list[dict]) -> None:
    from . import outcome

    calls = {}
    for row in rows:
        if row.get("cursor", {}).get("unbound_outcome"):
            continue
        blocks = row.get("message", {}).get("content", [])
        if "message" not in row or not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("cursor_tool_name", block.get("name", ""))
            args = block.get("input") if isinstance(block.get("input"), dict) else {}
            command = args.get("command", args.get("cmd", ""))
            if name in outcome.COMMAND_TOOLS and isinstance(command, str):
                allowed = block.get("outcome_success_output")
                if allowed is None:
                    allowed = outcome.success_output_allowed(command, row.get("cwd"))
                calls[block.get("id")] = command, allowed
            _, block["input"] = _normalize_tool_call(name, args)
            for key in ("outcome_command", "outcome_parts", "outcome_cwd"):
                block.pop(key, None)
        row["message"]["content"] = _merge_content([], blocks)
    for row in rows:
        blocks = row.get("message", {}).get("content", [])
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            command, allowed = calls.get(block.get("tool_use_id"), ("", False))
            failed = block.get("is_error")
            text = block.get("outcome_output", block.get("content"))
            block.pop("outcome_check_output", None)
            if failed:
                block["content"] = (
                    "[redacted]"
                    if safety.secret_output(command)
                    else _redact_text(block.get("content") or "")[: C.TOOL_ERROR_CHARS]
                )
            else:
                block["content"] = ""
            block["outcome_output"] = outcome.output_text(str(text or ""), command) if allowed else ""


def _same_destination(source: Path, target: Path) -> bool:
    try:
        if source.resolve() == target.resolve():
            return True
    except OSError:
        pass
    try:
        return os.path.samestat(source.stat(), target.lstat())
    except OSError:
        return False


def normalize(
    source: Path,
    target: Path,
    cwd: str | Path | None = None,
    *,
    expected_session: str | None = None,
    outcomes: list[dict] | None = None,
    diagnostics: dict[str, int] | None = None,
) -> Path:
    """Convert a native Cursor transcript into a NowItKnows JSONL file outside the source path."""
    source, target = Path(source), Path(target)
    if target.is_symlink() or _same_destination(source, target):
        raise ValueError("Normalized Cursor transcript target must be a distinct file outside the original")
    counts = diagnostics if diagnostics is not None else {}
    rows = convert(
        source,
        cwd,
        expected_session=expected_session,
        outcomes=outcomes,
        diagnostics=counts,
    )
    if counts.get("read_errors"):
        raise OSError("Native Cursor transcript could not be read completely")
    merge_normalized(target, rows)
    return target
