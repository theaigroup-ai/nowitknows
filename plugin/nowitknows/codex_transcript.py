"""Read persisted Codex rollouts without modifying history or guessing human authorship."""

from __future__ import annotations

import hashlib
import heapq
import itertools
import json
import logging
import os
import re
import shlex
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import config as C
from . import native, outcome, safety, sessions
from .state import atomic_write, file_lock

_LOG = logging.getLogger(__name__)
_TEXT_TYPES = {"text", "Text", "input_text", "output_text"}
# A memory tool may inject its own tagged block into the prompt; those are not the user's words.
_INJECTION = re.compile(
    r"^\s*(?:# AGENTS\.md instructions\b|<(?:environment_context|permissions[ >]|skills_instructions|"
    r"app-context|developer_instructions|user_instructions|system_reminder|system-reminder|"
    r"context|instructions|plugin_context|available_tools|available_skills)\b|\[[\w.-]+ (?:keyword|worldview) ns=)",
    re.I,
)
_NOTICE = re.compile(r"^\s*<(?:task-notification|subagent_notification|subagent-notification)\b", re.I)
_EXIT = re.compile(r"(?:^|\n)(?:Process exited with code|Exit code:)\s*(-?\d+)\b")
_COMMANDS = {"exec_command", "shell", "shell_command", "local_shell", "Codex.command"}
_IGNORED_RECORDS = {"world_state", "token_usage_record"}
_IGNORED_EVENTS = {
    "task_started",
    "task_complete",
    "task_completed",
    "turn_started",
    "turn_complete",
    "turn_completed",
    "token_count",
    "thread_settings_applied",
    "agent_message_delta",
    "agent_reasoning_delta",
    "agent_reasoning",
    "agent_reasoning_raw_content",
    "agent_reasoning_raw_content_delta",
    "item_started",
    "item_updated",
    "exec_command_output_delta",
    "mcp_tool_call_begin",
}
_IGNORED_ITEMS = {"Reasoning", "reasoning"}
_IGNORED_RESPONSES = {"reasoning", "compaction", "web_search_call"}


def _string(value) -> str:
    return value if isinstance(value, str) else ""


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        b["text"]
        for b in content
        if isinstance(b, dict) and b.get("type") in _TEXT_TYPES and isinstance(b.get("text"), str)
    )


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            return value
    return value


def _clean(value):
    if isinstance(value, str):
        return safety.redact(value).encode("utf-8", "replace").decode("utf-8")
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {_clean(k): _clean(v) for k, v in value.items()}
    return value


@dataclass
class _Entry:
    line: int
    row: dict
    turn: str = ""
    family: str = ""
    priority: int = 0
    identity: str = ""
    text: str = ""
    eligible: bool = True
    removed: bool = False
    results: list = field(default_factory=list)


class _Reader:
    def __init__(self, path: Path, cwd: str):
        self.path, self.cwd = path, cwd
        self.counts = Counter()
        self.entries: list[_Entry] = []
        self.meta: dict = {}
        self.turn = ""
        self.sid = ""
        self.session_ids: set[str] = set()
        self.record_session_ids: set[str] = set()
        self.sidechain = False
        self.meta_timestamp = ""
        self.tool_names: dict[str, str] = {}
        self.patch_calls: set[str] = set()
        self.secret_outputs: set[str] = set()
        self.success_outputs: set[str] = set()
        self.running_commands: dict[str, str] = {}
        self.poll_calls: dict[str, str] = {}
        self.prior_results: dict[str, dict] = {}

    def resume(self, rows: list[dict]) -> None:
        commands = {
            block.get("id"): (block, row.get("cwd"))
            for row in rows
            for block in row.get("message", {}).get("content", [])
            if isinstance(block, dict) and block.get("name") == "Codex.command"
        }
        for row in rows:
            for block in row.get("message", {}).get("content", []):
                if not isinstance(block, dict) or block.get("outcome_session_id") is None:
                    continue
                identity = block.get("tool_use_id")
                if identity not in commands:
                    continue
                command, cwd = commands[identity]
                text = _string(command.get("outcome_command", _mapping(command.get("input")).get("command")))
                self.running_commands[str(block["outcome_session_id"])] = identity
                self.prior_results[identity] = row
                self.tool_names[identity] = "Codex.command"
                if command.get("outcome_success_output", outcome.success_output_allowed(text, cwd)):
                    self.success_outputs.add(identity)
                if safety.secret_output(text):
                    self.secret_outputs.add(identity)

    def changed(self, raw: dict, payload: dict, line: int) -> None:
        """Record explicit successful edits, never infer an edit from a shell command or exit code."""
        if self.sidechain or raw.get("isSidechain") or self.meta.get("source") != "exec":
            return
        kind, typ = raw.get("type"), payload.get("type")
        item = _mapping(payload.get("item"))
        changed = (
            kind == "event_msg"
            and typ == "item_completed"
            and item.get("type") in {"FileChange", "fileChange"}
            and item.get("status") == "completed"
            and bool(_mapping(item.get("changes")))
        ) or (
            kind == "event_msg"
            and typ == "patch_apply_end"
            and payload.get("success") is True
            and bool(_mapping(payload.get("changes")))
        )
        if kind == "response_item":
            call = _string(payload.get("call_id"))
            if typ in {"custom_tool_call", "function_call"} and payload.get("name") in {
                "apply_patch",
                "functions.apply_patch",
            }:
                patch = payload.get("input", payload.get("arguments"))
                if typ == "function_call":
                    patch = _json(patch)
                    if isinstance(patch, dict):
                        patch = patch.get("patch", patch.get("input"))
                if (
                    call
                    and isinstance(patch, str)
                    and re.search(r"^\*\*\* (?:Add|Update|Delete) File: .+", patch, re.M)
                ):
                    self.patch_calls.add(call)
            elif typ in {"custom_tool_call_output", "function_call_output"} and call in self.patch_calls:
                self.patch_calls.discard(call)
                output = _json(payload.get("output"))
                text = output if isinstance(output, str) else _string(_mapping(output).get("output"))
                changed = bool(re.fullmatch(r"Success\. Updated the following files:\n(?:[AMD] [^\n]+\n?)+", text))
                if isinstance(output, dict):
                    changed &= not output.get("is_error") and not output.get("isError") and not output.get("error")
                    code = output.get("exit_code", _mapping(output.get("metadata")).get("exit_code", 0))
                    changed &= code == 0 and output.get("status", "completed") == "completed"
        if changed:
            row = self.row("system", line, raw)
            row.update(subtype="codex_file_change")
            row["codex"]["successful_file_change"] = True
            self.entries.append(_Entry(line, row, self.turn))

    def row(self, kind: str, line: int, raw: dict, identity: str = "") -> dict:
        stamp = raw.get("timestamp")
        payload = _mapping(raw.get("payload"))
        passthrough = _mapping(payload.get("internal_chat_message_metadata_passthrough"))
        stamp = _string(stamp) or _string(passthrough.get("create_time"))
        # A line identity stays stable when an active rollout grows, even without native ids or timestamps.
        digest = hashlib.sha256(
            f"{self.sid or self.path.name}\0{line}\0{kind}".encode("utf-8", "surrogatepass")
        ).hexdigest()
        row = {
            "type": kind,
            "uuid": identity or f"codex:{digest}",
            "timestamp": stamp or None,
            "sessionId": self.sid,
            "cwd": self.cwd,
            "codex": {"source_line": line, "turn_id": self.turn},
        }
        explicit = _string(raw.get("session_id")) or _string(raw.get("sessionId"))
        thread = _string(payload.get("thread_id")) or _string(payload.get("threadId"))
        if explicit:
            row["sessionId"] = explicit
        foreign = self.sid and ((thread and thread != self.sid) or (explicit and explicit != self.sid))
        if self.sidechain or raw.get("isSidechain") is True or foreign:
            row["isSidechain"] = True
        return row

    def message(self, raw: dict, payload: dict, line: int, role: str, priority: int, schema: str):
        content = payload.get("content", payload.get("message", payload.get("text")))
        metadata = _mapping(payload.get("internal_chat_message_metadata_passthrough"))
        kinds = metadata.get("content_item_kinds")
        eligible = priority > 0
        notice = False
        if role == "user" and kinds is not None:
            if not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds):
                self.counts["unsupported_user_metadata"] += 1
                return
            notice = any(k.startswith("multi_agent.") or k == "task-notification" for k in kinds)
            if not notice and kinds != ["user.text"]:
                if "user.text" in kinds and isinstance(content, list) and len(content) == len(kinds):
                    content = [b for b, k in zip(content, kinds, strict=True) if k == "user.text"]
                else:
                    self.counts["excluded_context"] += 1
                    return
            eligible = True
        text = _text(content).strip()
        if not text:
            self.counts["empty_messages"] += 1
            return
        if role == "user":
            origin = _mapping(payload.get("origin"))
            notice |= origin.get("kind") in {"task-notification", "subagent", "subagent_notification"}
            notice |= bool(_NOTICE.match(text))
            if not notice:
                if isinstance(content, list):
                    text = "\n".join(t for b in content if (t := _text([b]).strip()) and not _INJECTION.match(t))
                elif _INJECTION.match(text):
                    text = ""
            if not text:
                self.counts["excluded_context"] += 1
                return
        identity = _string(payload.get("id")) or _string(payload.get("message_id"))
        row = self.row(role, line, raw, identity)
        row["message"] = {"role": role, "content": [{"type": "text", "text": text}]}
        row["codex"]["schema"] = schema
        if notice:
            row["origin"] = {"kind": "task-notification", "subkind": "session-inbox"}
            eligible = True
            self.counts["notices"] += 1
        self.entries.append(_Entry(line, row, self.turn, role, priority, identity, text, eligible))

    def tool(self, raw: dict, line: int, identity: str, name: str, args, priority: int, schema: str) -> _Entry:
        args = _json(args)
        if not isinstance(args, dict):
            args = {"input": None} if args else {}
        if name.removeprefix("functions.") == "write_stdin" and (
            original := self.running_commands.get(str(args.get("session_id")))
        ):
            self.poll_calls[identity] = original
        command_cwd = ""
        if name.removeprefix("functions.") in _COMMANDS:
            command_cwd = next((args[k] for k in ("workdir", "cwd", "working_directory") if k in args), self.cwd)
            command_cwd = _command_directory(command_cwd)
            command = args.get("command", args.get("cmd"))
            if isinstance(command, list) and all(isinstance(v, str) for v in command):
                shell = len(command) == 3 and Path(command[0]).name in {"sh", "bash", "zsh", "dash", "ksh"}
                command = command[2] if shell and command[1] in {"-c", "-lc", "-ic", "-lic"} else shlex.join(command)
            args = {"command": command} if isinstance(command, str) else {}
            name = "Codex.command"
        name = safety.redact(name)[: C.TOOL_ARG_CHARS]
        # Store exactly the shared parser's safe argument summary, never arbitrary MCP form values or output.
        selected = next((k for k in sessions.TOOL_ARG_KEYS if isinstance(args.get(k), str) and args[k].strip()), None)
        if selected:
            what = sessions.tool_line({"name": name, "input": args}).partition(f"TOOL {name}: ")[2]
            safe_args = {selected: what}
        else:
            safe_args = {safety.redact(str(k))[: C.TOOL_ARG_CHARS]: None for k in args}
        if C.tool_name(name) == "skill_view" and safe_args.get("file_path") and isinstance(args.get("name"), str):
            safe_args["name"] = safety.redact(args["name"])[: C.TOOL_ARG_CHARS]
        row = self.row("assistant", line, raw)
        block = {"type": "tool_use", "id": identity or row["uuid"], "name": name, "input": safe_args}
        if name == "Codex.command" and isinstance(args.get("command"), str):
            block["command_cwd"] = command_cwd
            block["outcome_command"] = outcome.command_text(args["command"])
            block["outcome_success_output"] = outcome.success_output_allowed(args["command"], self.cwd)
            if block["outcome_success_output"]:
                self.success_outputs.add(block["id"])
            if safety.secret_output(args["command"]):
                self.secret_outputs.add(block["id"])
        self.tool_names[block["id"]] = name
        row["message"] = {"role": "assistant", "content": [block]}
        row["codex"]["schema"] = schema
        fingerprint = json.dumps([name, safe_args], sort_keys=True)
        entry = _Entry(line, row, self.turn, "tool", priority, identity, fingerprint)
        self.entries.append(entry)
        return entry

    def result(self, raw: dict, line: int, identity: str, value, *, failed=None, rejected=False):
        identity = self.poll_calls.get(identity, identity)
        value = _json(value)
        obj = _mapping(value)
        error = obj.get("isError", obj.get("is_error"))
        status = obj.get("status")
        exit_code = obj.get("exit_code", obj.get("exitCode", _mapping(obj.get("metadata")).get("exit_code")))
        if any(key in obj and type(obj[key]) is not bool for key in ("isError", "is_error")) or (
            exit_code is not None and type(exit_code) is not int
        ):
            self.counts["unsupported_tool_results"] += 1
            return None
        text = _text(obj.get("content")) or _string(obj.get("output")) or _string(value)
        if isinstance(obj.get("error"), dict):
            text = _string(obj["error"].get("message")) or text
        else:
            text = _string(obj.get("error")) or text
        if exit_code is None and (m := _EXIT.search(text.partition("Output:")[0])):
            exit_code = int(m.group(1))
        if exit_code is not None:
            failed = exit_code != 0
        if error is True or obj.get("error") or status in {"failed", "declined", "rejected", "cancelled", "canceled"}:
            failed = True
        elif failed is None and (error is False or status == "completed"):
            failed = False
        if not failed and (problem := sessions.skill_tool_error(self.tool_names.get(identity, ""), text)) is not None:
            failed, text = True, problem  # nowitknows's skill tools report their own failures inside a finished call
        if failed is None:  # a refusal by the approval policy is bare text: the call never ran, so nothing exited
            failed = text.startswith(("Error:", "error:", "Execution failed", "Command failed")) or bool(
                sessions.CODEX_DENIAL.match(text)
            )
        approved = ""
        running_match = re.search(
            r"(?:^|\n)(?:Process|Script) running with (?:session|cell) ID\s+(\S+)", text.partition("Output:")[0]
        )
        session_id = obj.get("session_id", running_match[1] if running_match else None)
        running = session_id is not None and exit_code is None and status != "completed" and not failed
        if running and self.tool_names.get(identity) == "Codex.command":
            self.running_commands[str(session_id)] = identity
        completed = exit_code is not None or status == "completed" or failed is True or rejected
        output_tail = (
            outcome.output_text(text, "printenv SECRET" if identity in self.secret_outputs else "")
            if completed and identity in self.success_outputs
            else ""
        )
        if not failed and text.startswith(sessions.APPROVED_HEAD) and (match := sessions.APPROVED.search(text)):
            approved = text[: match.end()] + safety.redact(text[match.end() :])[: C.PLAN_MAX_CHARS]
        # Only an explicit host attribution can turn a result into user feedback. Quoted tool output cannot.
        if rejected:
            failed = True
            feedback = _string(obj.get("feedback"))
            text = sessions.REJECTED_HEAD + (f"\n{sessions.REJECTED} {feedback}" if feedback else "")
        elif failed:
            text = (
                "[redacted]"
                if identity in self.secret_outputs
                else safety.redact(" ".join(text.split()))[: C.TOOL_ERROR_CHARS]
            )
            if text.startswith(sessions.REJECTED_HEAD):
                text = "Tool error: " + text
        else:
            text = ""
        row = self.row("user", line, raw)
        row["message"] = {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": identity,
                    "is_error": bool(failed),
                    "content": text,
                    **(
                        {
                            "outcome_output": output_tail,
                            "outcome_complete": completed,
                            "outcome_running": running,
                            "outcome_exit_code": exit_code,
                            **({"outcome_session_id": session_id} if session_id is not None else {}),
                        }
                        if self.tool_names.get(identity) == "Codex.command"
                        else {}
                    ),
                }
            ],
        }
        if rejected:
            row["toolDenialKind"] = "user-rejected"
        elif exit_code is not None or status == "completed":  # it ran, so its output never reads as a refusal
            row["codex"].update(ran=True, exit_code=exit_code)
        return _Entry(line, row, self.turn, "result", identity=identity, text=approved)

    def typed(self, raw: dict, payload: dict, line: int):
        item = _mapping(payload.get("item"))
        kind = item.get("type")
        schema = "item_completed"
        if kind in {"UserMessage", "userMessage"}:
            self.message(raw, item, line, "user", 2, schema)
        elif kind in {"AgentMessage", "agentMessage"}:
            self.message(raw, item, line, "assistant", 2, schema)
        elif kind in {"CommandExecution", "commandExecution", "McpToolCall", "mcpToolCall", "FileChange", "fileChange"}:
            identity = _string(item.get("id"))
            if kind in {"CommandExecution", "commandExecution"}:
                name, args = "Codex.command", {"command": item.get("command")}
                if "cwd" in item:
                    args["cwd"] = item["cwd"]
                output = {
                    "output": item.get("aggregated_output", item.get("aggregatedOutput")),
                    "exit_code": item.get("exit_code", item.get("exitCode")),
                    "status": item.get("status"),
                }
            elif kind in {"McpToolCall", "mcpToolCall"}:
                name = f"mcp__{_string(item.get('server'))}__{_string(item.get('tool'))}"
                args = item.get("arguments")
                output = {**_mapping(item.get("result")), "status": item.get("status")}
                if item.get("error"):
                    output.update(isError=True, error=item["error"])
            else:
                changes = item.get("changes")
                paths = list(changes) if isinstance(changes, dict) else []
                name, args = "Codex.file_change", {"file_path": ", ".join(paths)} if paths else {"changes": None}
                output = {"output": item.get("stderr") or item.get("stdout"), "status": item.get("status")}
            entry = self.tool(raw, line, identity, name, args, 2, schema)
            status = item.get("status")
            missing_exit = (
                name == "Codex.command" and status == "completed" and type(output.get("exit_code")) is not int
            )
            if not missing_exit and status in {"completed", "failed", "declined", "rejected", "cancelled", "canceled"}:
                if result := self.result(raw, line, entry.row["message"]["content"][0]["id"], output):
                    entry.results.append(result)
            else:
                self.counts["incomplete_tools"] += 1
        elif kind == "CollabAgentToolCall":
            tool = _string(item.get("tool"))
            if not tool:
                self.counts["unsupported_items"] += 1
                return
            entry = self.tool(raw, line, _string(item.get("id")), f"Codex.collab.{tool}", {"agents": None}, 2, schema)
            if item.get("status") in {"completed", "failed"} and (
                result := self.result(raw, line, entry.row["message"]["content"][0]["id"], {"status": item["status"]})
            ):
                entry.results.append(result)
        elif kind == "Extension":
            self.tool(
                raw,
                line,
                _string(item.get("id")),
                "Codex.extension",
                {"query": item.get("query")} if isinstance(item.get("query"), str) else {},
                2,
                schema,
            )
            self.counts["unknown_tool_outcomes"] += 1
        elif kind in {"ContextCompaction", "contextCompaction"}:
            self.counts["compaction_markers"] += 1
        elif kind not in _IGNORED_ITEMS:
            self.counts["unsupported_items"] += 1

    def parse(self, raw: dict, line: int):
        kind, payload = raw.get("type"), _mapping(raw.get("payload"))
        for value in (raw.get("session_id"), raw.get("sessionId"), payload.get("thread_id"), payload.get("threadId")):
            if isinstance(value, str) and value:
                self.record_session_ids.add(value)
            elif value is not None:
                self.counts["invalid_record_identity"] += 1
        if kind == "session_meta":
            self.counts["session_metadata_records"] += 1
            identity = payload.get("id")
            valid = isinstance(identity, str) and bool(identity.strip()) and identity == identity.strip()
            if valid:
                try:
                    identity.encode("utf-8")
                except UnicodeError:
                    valid = False
            if valid:
                self.session_ids.add(identity)
                self.sid = identity
            else:
                self.counts["invalid_session_metadata"] += 1
        if not payload:
            self.counts["unsupported_records"] += 1
            return
        metadata = _mapping(payload.get("internal_chat_message_metadata_passthrough"))
        self.turn = (
            _string(payload.get("turn_id", payload.get("turnId"))) or _string(metadata.get("turn_id")) or self.turn
        )
        self.changed(raw, payload, line)
        if kind == "session_meta":
            self.cwd = self.cwd or _string(payload.get("cwd"))
            source = payload.get("source")
            self.sidechain = source == "subagent" or (isinstance(source, dict) and "subagent" in source)
            self.meta_timestamp = _string(payload.get("timestamp")) or _string(raw.get("timestamp"))
            self.meta = {
                key: payload[key]
                for key in ("cli_version", "originator", "source", "model_provider")
                if isinstance(payload.get(key), str)
            }
            if self.sidechain:
                self.meta["source"] = "subagent"
        elif kind == "turn_context":
            self.cwd = self.cwd or _string(payload.get("cwd"))
        elif kind == "event_msg":
            typ = payload.get("type")
            if typ == "item_completed":
                self.typed(raw, payload, line)
            elif typ in {"user_message", "agent_message"}:
                self.message(raw, payload, line, "user" if typ == "user_message" else "assistant", 1, typ)
            elif typ not in _IGNORED_EVENTS:
                self.counts["unsupported_events"] += 1
        elif kind == "response_item":
            typ = payload.get("type")
            if typ == "message" and payload.get("role") in {"user", "assistant"}:
                self.message(raw, payload, line, payload["role"], 0, "response_item")
                if self.entries and self.entries[-1].line == line and payload["role"] == "assistant":
                    self.entries[-1].eligible = True
            elif typ in {"function_call", "custom_tool_call", "local_shell_call"}:
                name = "Codex.command" if typ == "local_shell_call" else _string(payload.get("name"))
                args = payload.get("arguments", payload.get("input", payload.get("action")))
                if name:
                    self.tool(
                        raw, line, _string(payload.get("call_id")) or _string(payload.get("id")), name, args, 0, typ
                    )
                else:
                    self.counts["unsupported_items"] += 1
            elif typ in {"function_call_output", "custom_tool_call_output", "local_shell_call_output"}:
                rejected = payload.get("toolDenialKind") == "user-rejected"
                result = self.result(
                    raw, line, _string(payload.get("call_id")), payload.get("output"), rejected=rejected
                )
                if result:
                    self.entries.append(result)
            elif typ == "message" and payload.get("role") in {"developer", "system"}:
                self.counts["excluded_context"] += 1
            elif typ not in _IGNORED_RESPONSES:
                self.counts["unsupported_items"] += 1
        elif kind == "compacted":
            text = _text(payload.get("message"))
            row = self.row("system", line, raw)
            row.update(
                subtype="compact_boundary",
                isCompactSummary=True,
                message={"role": "system", "content": [{"type": "text", "text": text}]},
            )
            self.entries.append(_Entry(line, row, self.turn))
        elif kind not in _IGNORED_RECORDS:
            self.counts["unsupported_records"] += 1

    def reconcile(self) -> list[dict]:
        by_id: dict[tuple, _Entry] = {}
        for e in self.entries:
            if not e.identity or e.family not in {"user", "assistant", "tool"}:
                continue
            key = e.family, e.priority, e.identity, bool(e.row.get("isSidechain"))
            if old := by_id.get(key):
                old.removed = True
                e.line = old.line
                self.counts["duplicates"] += 1
            by_id[key] = e
        identities = defaultdict(list)
        for e in self.entries:
            if not e.removed and e.identity and e.family in {"user", "assistant", "tool"}:
                identities[e.family, e.identity, bool(e.row.get("origin")), bool(e.row.get("isSidechain"))].append(e)
        for group in identities.values():
            chosen = max(group, key=lambda e: e.priority)
            for mirror in group:
                if mirror is chosen or mirror.priority == chosen.priority:
                    continue
                mirror.removed = True
                chosen.line = min(chosen.line, mirror.line)
                chosen.results.extend(mirror.results)
                self.counts["mirrors"] += 1
        groups = defaultdict(list)
        for e in self.entries:
            if not e.removed and e.family in {"user", "assistant", "tool"}:
                groups[e.family, e.text, bool(e.row.get("origin")), bool(e.row.get("isSidechain"))].append(e)
        for group in groups.values():
            for chosen in sorted(group, key=lambda e: (-e.priority, e.line)):
                if chosen.removed:
                    continue
                for priority in range(chosen.priority):
                    matches = [
                        e
                        for e in group
                        if not e.removed
                        and e.priority == priority
                        and (not e.turn or not chosen.turn or e.turn == chosen.turn)
                    ]
                    if not matches:
                        continue
                    mirror = min(matches, key=lambda e: abs(e.line - chosen.line))
                    mirror.removed = True
                    chosen.line = min(chosen.line, mirror.line)
                    if chosen.family == "tool":
                        chosen.results.extend(mirror.results)
                        if mirror.identity:
                            chosen.row["codex"].setdefault("mirrored_call_ids", []).append(mirror.identity)
                    self.counts["mirrors"] += 1
        tools = {}
        for e in self.entries:
            if e.removed or e.family != "tool":
                continue
            block = e.row["message"]["content"][0]
            tools[block["id"]] = e
            for alias in e.row["codex"].get("mirrored_call_ids", []):
                tools[alias] = e
        for e in self.entries:
            if e.family == "result":
                if e.identity and (tool := tools.get(e.identity)):
                    tool.results.append(e)
                elif prior := self.prior_results.get(e.identity):
                    e.row["uuid"] = prior["uuid"]
                    e.row["timestamp"] = prior.get("timestamp")
                    continue
                else:
                    self.counts["orphan_results"] += 1
                e.removed = True
        rows = []
        for e in sorted(self.entries, key=lambda e: e.line):
            if e.removed:
                continue
            if not e.eligible:
                self.counts["unclassified_user_messages"] += 1
                continue
            rows.append((e.line, 0, e.row))
            if e.family == "tool" and e.results:
                # Typed outcomes carry exit codes and MCP error flags that textual mirrors can lose.
                result = e.results[0]
                if e.row["message"]["content"][0]["name"] == "Codex.command" and any(
                    r.row["message"]["content"][0].get("outcome_running") for r in e.results
                ):
                    result = max(e.results, key=lambda r: r.line)
                    result.row["uuid"] = e.results[0].row["uuid"]
                    result.row["timestamp"] = e.results[0].row["timestamp"]
                block = result.row["message"]["content"][0]
                block["tool_use_id"] = e.row["message"]["content"][0]["id"]
                if result.text and e.row["message"]["content"][0]["name"] == "ExitPlanMode":
                    block["content"] = result.text
                # The shared parser associates outcomes with the current exchange, so a late result follows its call.
                rows.append((e.line, 1, result.row))
        out = [row for _, _, row in sorted(rows, key=lambda r: (r[0], r[1]))]
        for row in out:
            if not row["sessionId"] and len(self.session_ids) == 1:
                row["sessionId"] = self.sid
            row["cwd"] = self.cwd
        if self.sid or self.meta:
            meta = self.row("system", 0, {})
            meta.update(subtype="codex_session", codex={"session": self.meta}, timestamp=self.meta_timestamp or None)
            out.insert(0, meta)
        self.counts["output_rows"] = len(out)
        self.counts["human_messages"] = sum(
            e["type"] == "user" and sessions.prompt_text(e) is not None and sessions.prompt_text(e)[1] is None
            for e in out
        )
        self.counts["notice_messages"] = sum(bool(e.get("origin")) for e in out)
        return _clean(out)


def convert(
    path: str | Path, cwd: str = "", *, diagnostics: dict | None = None, previous: list[dict] | None = None
) -> list[dict]:
    """Return shared-session rows; unsupported or ambiguous input is skipped, with content-free counts."""
    reader, failure = _Reader(Path(path), str(cwd)), ""
    reader.resume(previous or [])
    try:
        with sessions.open_history(reader.path) as source:
            for line, data in enumerate(sessions.history_lines(source, reader.counts), 1):
                reader.counts["lines"] += 1
                if data is None:  # past the line limit, counted as oversized
                    continue
                if not data.strip():
                    reader.counts["blank_lines"] += 1
                    continue
                try:
                    raw = json.loads(data.decode("utf-8-sig" if line == 1 else "utf-8"))
                    if not isinstance(raw, dict):
                        raise ValueError("non-object")
                    reader.parse(raw, line)
                except (ValueError, UnicodeError, RecursionError, TypeError):
                    reader.counts["malformed_records"] += 1
    except sessions.HistoryTooLarge:
        reader.counts["too_large"] += 1
    except (OSError, EOFError) as error:
        reader.counts["read_errors"] += 1
        failure = str(error)
    rows = reader.reconcile()
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update(reader.counts)
        if failure:
            diagnostics["read_error"] = failure
        diagnostics.update(
            session_id=reader.sid,
            session_ids=sorted(reader.session_ids),
            record_session_ids=sorted(reader.record_session_ids),
        )
    dropped = sum(
        v
        for k, v in reader.counts.items()
        if k.startswith("unsupported_")
        or k in {"malformed_records", "oversized_records", "read_errors", "too_large", "unclassified_user_messages"}
    )
    if dropped:
        _LOG.warning("Codex transcript skipped records: %s", json.dumps(dict(reader.counts), sort_keys=True))
    return rows


def _merge_content(before, after):
    if before == after:
        return before
    if isinstance(before, str) and isinstance(after, str):
        if before in after:
            return after
        return before if after in before else before + "\n" + after
    if isinstance(before, list) and isinstance(after, list):
        merged = []
        for block in [*before, *after]:
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
                if (
                    block["type"] == "tool_result"
                    and old.get("outcome_complete") is True
                    and block.get("outcome_complete") is False
                ):
                    continue
                if block["type"] == "tool_use":
                    merged[match] = block
                    continue
                key = "text" if block["type"] == "text" else "content"
                merged[match] = {**old, **block, key: _merge_content(old.get(key, ""), block.get(key, ""))}
                if block["type"] == "tool_result":
                    merged[match]["is_error"] = old.get("is_error", False) or block.get("is_error", False)
        return merged
    return before or after


def _command_directory(value) -> str:
    """Keep a known absolute command directory, including native file URIs, outside the rendered arguments."""
    if not isinstance(value, str):
        return ""
    if value.startswith("file:"):
        parsed = urlsplit(value)
        if parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
            return ""
        value = unquote(parsed.path)
        if C.WINDOWS and re.match(r"^/[A-Za-z]:/", value):
            value = value[1:]
    return safety.redact(str(Path(value))) if Path(value).is_absolute() and len(value) <= C.TOOL_ARG_CHARS else ""


def _bounded_commands(rows: list[dict]) -> None:
    commands = {}
    allowed = set()
    for row in rows:
        content = row.get("message", {}).get("content", [])
        if "message" not in row or not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") != "Codex.command":
                continue
            args = _mapping(block.get("input"))
            command = _string(block.get("outcome_command", args.get("command")))
            commands[block.get("id")] = command
            keep_output = block.get("outcome_success_output")
            if keep_output is None:
                keep_output = outcome.success_output_allowed(command, row.get("cwd"))
            if keep_output:
                allowed.add(block.get("id"))
            block.pop("outcome_parts", None)
            block.pop("outcome_cwd", None)
            if command:
                block["outcome_command"] = outcome.command_text(command)
            if isinstance(args.get("command"), str) and args["command"]:
                block["input"] = {
                    "command": sessions.tool_line({"name": "Codex.command", "input": args}).partition(
                        "TOOL Codex.command: "
                    )[2]
                }
        row["message"]["content"] = _merge_content([], content)
    for row in rows:
        content = row.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            command = commands.get(block.get("tool_use_id"), "")
            block.pop("outcome_check_output", None)
            if block.get("tool_use_id") in commands:
                block["content"] = (
                    (
                        "[redacted]"
                        if safety.secret_output(command)
                        else safety.redact(" ".join(_string(block.get("content")).split()))[: C.TOOL_ERROR_CHARS]
                    )
                    if block.get("is_error")
                    else ""
                )
            if "outcome_output" in block:
                block["outcome_output"] = (
                    outcome.output_text(_string(block["outcome_output"]), command)
                    if block.get("outcome_complete") is not False and block.get("tool_use_id") in allowed
                    else ""
                )


def _distinct(rows: list[dict], held: dict, source: str | Path) -> list[dict]:
    """A row whose line-numbered id the transcript holds for another moment and message gets an id from its file."""
    page = Path(source).name.split(".jsonl")[0]
    out = []
    for row in rows:
        uuid, stamp = row.get("uuid"), row.get("timestamp")
        when, message = held.get((row.get("type"), uuid), (None, None))
        # A thread's later pages and its subagents' files carry its session id, and their line numbers start again;
        # a fork's copy of a row keeps the message under a new time, and stays one row.
        another = stamp and when and when != stamp and message != row.get("message")
        if page and isinstance(uuid, str) and uuid.startswith("codex:") and another:
            digest = hashlib.sha256(f"{uuid}\0{page}".encode("utf-8", "surrogatepass")).hexdigest()
            row = {**row, "uuid": f"codex:{digest}"}
        out.append(row)
    return out


def merge_normalized(target: Path, rows: list[dict], source: str | Path = "") -> None:
    """Union native history versions before replacing their normalized transcript; `source` is the file read."""
    with file_lock(target):
        old = (
            # Not splitlines: it also breaks at U+2028, U+2029 and U+0085, which json.dumps leaves raw here.
            [json.loads(line) for line in target.read_text(encoding="utf-8").split("\n") if line.strip()]
            if target.exists()
            else []
        )
        held = {(row.get("type"), row.get("uuid")): (row.get("timestamp"), row.get("message")) for row in old}
        rows = _distinct(rows, held, source)
        merged = {}
        for row in [*old, *rows]:
            key = row.get("type"), row.get("uuid")
            previous = merged.get(key)
            if previous is None:
                merged[key] = row
            elif "message" in row:
                message = {**previous.get("message", {}), **row["message"]}
                message["content"] = _merge_content(
                    previous.get("message", {}).get("content", ""), message.get("content", "")
                )
                merged[key] = {**previous, **row, "message": message}
            elif row.get("subtype") == "codex_session" and not previous.get("codex", {}).get("session"):
                merged[key] = row
        # A later page updates the yielded result in place, before its new polling call.
        linked_results = {
            (row.get("type"), row.get("uuid"))
            for row in old
            for block in row.get("message", {}).get("content", [])
            if isinstance(block, dict) and block.get("outcome_session_id") is not None
        }
        incoming = [key for row in rows if (key := (row.get("type"), row.get("uuid"))) not in linked_results]
        order = merged_order([(row.get("type"), row.get("uuid")) for row in old], incoming)
        order = dated(order, incoming, {key: row.get("timestamp") for key, row in merged.items()})
        result = [merged[key] for key in order]
        _bounded_commands(result)
        atomic_write(target, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in result))


def dated(order: list, page: list, when: dict) -> list:
    """A page's rows, in page order, moved before the first other message dated after all of them when one sits after
    it: an older page read late, or so merged by an older version. The session's own row stays first."""
    block = [key for key in dict.fromkeys(page) if key[0] != "system"]
    times = [t for key in block if isinstance(t := when.get(key), str) and t]
    if not times:
        return order
    newest, members = max(times), set(block)
    anchor = next(
        (i for i, k in enumerate(order) if k not in members and k[0] != "system" and (when.get(k) or "") > newest),
        None,
    )
    if anchor is None or all(k not in members for k in order[anchor:]):
        return order
    present, rest = set(order), [k for k in order if k not in members]
    at = rest.index(order[anchor])
    return [*rest[:at], *(k for k in block if k in present), *rest[at:]]


def merged_order(old: list, incoming: list) -> list:
    """Old keys keep their order; each new key goes just before the next later incoming key already placed, else
    last. A linked list and a heap of placed positions keep this near linear for long histories."""
    head, end = object(), object()
    chain = [head, *dict.fromkeys(old), end]
    following = dict(itertools.pairwise(chain))
    preceding = {b: a for a, b in following.items()}
    placed = set(chain[1:-1])
    first = {}
    for j, key in enumerate(incoming):
        first.setdefault(key, j)
    ready = [j for j, key in enumerate(incoming) if key in placed]  # ascending, so already a heap
    repeats = defaultdict(list)  # a new key's later positions count as placed once its first one is
    for j, key in enumerate(incoming):
        if key not in placed and first[key] != j:
            repeats[first[key]].append(j)
    for i, key in enumerate(incoming):
        while ready and ready[0] <= i:
            heapq.heappop(ready)
        if key not in placed:
            anchor = incoming[ready[0]] if ready else end
            before = preceding[anchor]
            following[before], preceding[key], following[key], preceding[anchor] = key, before, anchor, key
            placed.add(key)
        for j in repeats.pop(i, ()):
            heapq.heappush(ready, j)
    order, node = [], following[head]
    while node is not end:
        order.append(node)
        node = following[node]
    return order


def normalize(
    path: str | Path,
    target: str | Path,
    cwd: str = "",
    *,
    expected_session: str | None = None,
    diagnostics: dict | None = None,
) -> Path:
    """Atomically write a UTF-8 shared-session transcript, refusing aliases of the source file."""
    source, target = Path(path), Path(target)
    if source.resolve() == target.resolve() or (
        source.exists() and target.exists() and os.path.samefile(source, target)
    ):
        raise ValueError("normalized transcript must be outside the original file")
    counts = {}
    previous = list(sessions.read_jsonl(target)) if target.exists() else []
    rows = convert(source, cwd, diagnostics=counts, previous=previous)
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update(counts)
    if counts.get("read_errors") or counts.get("too_large"):
        raise OSError("Codex transcript could not be read completely")
    if expected_session is not None:
        compatible = (
            isinstance(expected_session, str)
            and bool(expected_session.strip())
            and counts.get("session_ids") == [expected_session]
            and not counts.get("invalid_session_metadata")
            and not counts.get("invalid_record_identity")
            and set(counts.get("record_session_ids", [])) <= {expected_session}
            and all(row.get("sessionId") == expected_session for row in rows)
        )
        if not compatible:
            raise ValueError("Codex transcript session identity is missing or incompatible")
    merge_normalized(target, rows, source)
    return target


def project_key(cwd: str | Path) -> str:
    """Use the shared folder-name encoding without probing git or another host's memory settings."""
    return native.folder_name(str(cwd)) if cwd else ""
