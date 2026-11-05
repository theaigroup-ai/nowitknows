from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import uuid
from contextlib import suppress
from dataclasses import fields
from pathlib import Path

from . import claude, native, notejudge, notify, router, worker
from . import config as C
from .delivery import finish_delivery, masked_delivery
from .memory import apply_operations
from .safety import redact, scan, secret_shaped
from .skills import SkillStore, norm_file, sha, size_label
from .state import State, durable_write, file_lock, jsonl_rows, noted, session

DONE = {"action": "done"}
CONTRACT = (
    """
You have no tools. Return only one JSON object, with arrays `skills` and `memory` (empty if nothing to save).
Your entire reply must be the JSON object only: no Markdown fences, preamble, or prose summary before or after it.
skills entries use the skill_manage shapes: {"action":"create|patch|edit|write_file|archive","name":"...",
"content":"full file for create/edit/write_file","old_string":"unique text for patch","new_string":"replacement",
"file_path":"support file path, only for write_file","triggers":["example requests"]}.
"""
    f"SKILL.md and each support file: {C.SKILL_MAX_CHARS:,}-character limit, never a target; "
    "over it, move detail needed only sometimes into references/ in the same change.\n"
    """memory entries: {"target":"user|memory|project","operations":[...]}.
For user/memory, operations are {"action":"add|replace|remove","content":"...","old_text":"..."}.
Memory hooks contain only the situation; NowItKnows adds the note suffix.
For a supplied <memory_tidy> block, return only {"action":"tidy","content":"full rewritten block"} for that target.
Tidy operations are always staged for an adversarial lossless judgment; ordinary global writes apply immediately.
Tidy may include notes:[{content:exact entry,hook}] to convert long inline facts.
For project, operations are {"action":"write","name":"...","description":"...","type":"user|feedback|project|reference",
"body":"..."} or {"action":"remove","name":"..."}. Only write the focus and memory targets named above.
The full skill files below substitute for skill_view. Only patch/edit a file supplied in full; no overwriting
existing support files because their contents are not supplied. Never archive a skill owned by the user.
For create/edit, content is the ENTIRE SKILL.md, beginning with YAML frontmatter:
---
name: exactly-the-entry-name
description: Use when the reusable task or trigger applies.
---
followed by the procedure. The name must match the entry's kebab-case name (at most 64 characters).
Never return a heading-only body: without this frontmatter the store refuses the proposal.
Current writable memory is supplied below, capped; use its exact old_text for replacements and removals.
Omitted memory is unknown, not empty. Do not overwrite an existing project topic unless its full text is supplied.
Skill proposals and project notes are staged and judged. User and memory writes apply immediately through the same
safety checks as the tool.
When skills are in focus, always save a taught or successfully discovered multi-step procedure for a recurring
repository task as a skill, such as ferrylog-release, whose description names the repository and when to use it.
A one-line instruction about recurring work is a standing convention:
save it in that task's skill (create one if needed).
A plain repo fact, such as a single command with no procedure around it, may stay in target='project' when offered;
otherwise recurring commands or paths go in that repository task's skill.
One-off task narratives stay excluded.
Learned skills are shared by Claude Code and Codex. Write procedures as shell commands, file paths and plain
instructions. Host-specific tool names (Skill tool, Bash tool, apply_patch, any mcp__ id) belong only under a
Platform notes heading naming Claude Code or Codex. User-authored skills are read-only.
"""
)


def _shape(required: list[str], properties: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": required, "properties": properties}


def _action(actions: list[str], required: list[str], fields: tuple[str, ...]) -> dict:
    return _shape(
        ["action", *required],
        {
            "action": {"type": "string", "enum": actions},
            **{name: {"type": "string"} for name in fields},
        },
    )


# The tools-free review contract above, before any proposal or memory operation is applied.
REVIEW_SCHEMA = _shape(
    ["skills", "memory"],
    {
        "skills": {
            "type": "array",
            "items": {
                "anyOf": [
                    _shape(
                        ["action", "name", "content"],
                        {
                            "action": {"type": "string", "enum": ["create", "edit", "write_file"]},
                            **{key: {"type": "string"} for key in ("name", "content", "file_path")},
                            "triggers": {"type": "array", "items": {"type": "string"}},
                        },
                    ),
                    _action(["patch"], ["name", "old_string", "new_string"], ("name", "old_string", "new_string")),
                    _action(["archive"], ["name"], ("name",)),
                ]
            },
        },
        "memory": {
            "type": "array",
            "items": _shape(
                ["target", "operations"],
                {
                    "target": {"type": "string", "enum": ["user", "memory", "project"]},
                    "operations": {
                        "type": "array",
                        "items": {
                            "anyOf": [
                                _shape(
                                    ["action", "content"],
                                    {
                                        "action": {"type": "string", "enum": ["add"]},
                                        **{key: {"type": "string"} for key in ("content", "hook")},
                                        "source_exchange": {"type": "integer"},
                                    },
                                ),
                                _shape(
                                    ["action", "content", "old_text"],
                                    {
                                        "action": {"type": "string", "enum": ["replace"]},
                                        **{key: {"type": "string"} for key in ("content", "old_text", "hook")},
                                        "source_exchange": {"type": "integer"},
                                    },
                                ),
                                _shape(
                                    ["action", "content"],
                                    {
                                        "action": {"type": "string", "enum": ["tidy"]},
                                        "content": {"type": "string"},
                                        "notes": {
                                            "type": "array",
                                            "items": _shape(
                                                ["content", "hook"],
                                                {key: {"type": "string"} for key in ("content", "hook")},
                                            ),
                                        },
                                    },
                                ),
                                _action(["remove"], ["old_text"], ("old_text",)),
                                _action(["remove"], ["name"], ("name",)),
                                _action(
                                    ["write"],
                                    ["name", "description", "type", "body"],
                                    ("name", "description", "type", "body"),
                                ),
                            ]
                        },
                    },
                },
            ),
        },
    },
)


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, value: dict) -> None:
    durable_write(path, json.dumps(value))


def _settings(cfg: C.Config) -> dict:
    names = {f.name for f in fields(C.Config)} - {
        "env",
        "data",
        "skills_home",
        "claude_home",
        "transcripts",
        "cursor_paths",
    }
    names |= {"data_dir", "skills_dir", "transcripts_dir", "child", "claude_bin", "dry_run", "debug"}
    keys = {C.ENV_VARS[name] for name in names if name in C.ENV_VARS} | C.option_names()
    keys |= {name for name in worker._JOB_ENV if not name.endswith("_")}
    keys.update(C.CODEX_ENV)
    keys.update(C.CURSOR_ENV)
    keys.update(("NOWITKNOWS_CLAUDE_SKILLS_DIR", "NOWITKNOWS_CLAUDE_TRANSCRIPTS_DIR", "NOWITKNOWS_LEGACY_HOST"))
    result = {k: v for k, v in cfg.env.items() if k in keys}
    if cfg.cursor_paths is not None:
        paths = cfg.cursor_paths
        return C.dual_env(
            {
                **result,
                **{k: v for k, v in paths.env.items() if k in C.CURSOR_PATH_ENV},
                C.CONFIG_DIR_INJECTED_ENV: paths.env.get(C.CONFIG_DIR_INJECTED_ENV, "0"),
                "NOWITKNOWS_HOST": "cursor",
            }
        )
    home = C.home_dir(cfg.env)
    codex = Path(C.env("NOWITKNOWS_CODEX_HOME", cfg.env) or cfg.env.get("CODEX_HOME") or home / ".codex")
    cursor = Path(C.env("NOWITKNOWS_CURSOR_HOME", cfg.env) or cfg.env.get("CURSOR_CONFIG_DIR") or home / ".cursor")
    result.update(
        HOME=str(home),
        USERPROFILE=str(home),
        NOWITKNOWS_HOST=cfg.host,
        NOWITKNOWS_DATA_DIR=str(cfg.data),
        NOWITKNOWS_SKILLS_DIR=str(cfg.skills_home),
        NOWITKNOWS_TRANSCRIPTS_DIR=str(cfg.transcripts),
        CODEX_HOME=str(codex.expanduser().resolve()),
        NOWITKNOWS_CODEX_HOME=str(codex.expanduser().resolve()),
        NOWITKNOWS_CURSOR_HOME=str(cursor.expanduser().resolve()),
    )
    # CLAUDE_CONFIG_DIR travels as the session had it (_JOB_ENV), with whether nowitknows set it, which a record of
    # 0.8.0 or 0.8.1 does not say, and, when relative, the folder it is resolved from (claude.cli_env, child_cwd)
    result[C.CONFIG_DIR_INJECTED_ENV] = "1" if C.env(C.CONFIG_DIR_INJECTED_ENV, cfg.env) == "1" else "0"
    if (value := cfg.env.get("CLAUDE_CONFIG_DIR")) and not os.path.isabs(value):
        result[C.CONFIG_DIR_BASE_ENV] = str(C.config_base(cfg.env))
    for key in (
        "NOWITKNOWS_SHARED_DATA_DIR",
        "NOWITKNOWS_CODEX_DATA_DIR",
        "NOWITKNOWS_CODEX_SKILLS_DIR",
        "NOWITKNOWS_CURSOR_DATA_DIR",
        "NOWITKNOWS_CURSOR_SKILLS_DIR",
        "NOWITKNOWS_CLAUDE_SKILLS_DIR",
        "NOWITKNOWS_CLAUDE_TRANSCRIPTS_DIR",
    ):
        if result.get(key):
            result[key] = str(Path(result[key]).expanduser().resolve())
    return C.dual_env(result)


def _enabled(cfg: C.Config, sid: str) -> bool:
    from .hooks import _learns

    surface = (
        State(cfg.path("state.json"), cfg.host, cfg.legacy_host).read()["sessions"].get(sid, {}).get("surface", {})
    )
    env = {C.SURFACE_ENV[k]: v for k, v in surface.items() if k in C.SURFACE_ENV and v is not None}
    cfg = C.load({**cfg.env, **env})
    return cfg.review and not cfg.remote and not cfg.child and _learns(cfg)


def queued(cfg: C.Config) -> list[dict]:
    with file_lock(cfg.path("mod", "queue")):
        return _queued(cfg)


def _queued(cfg: C.Config) -> list[dict]:
    result = []
    reservation = active(cfg)
    held = _read(cfg.path("mod", "steps", reservation["step"] + ".json")) if reservation.get("step") else {}
    for path in sorted((cfg.data / "mod" / "queue").glob("*.json")):
        item = _read(path)
        stamp, env = item.get("ts"), item.get("env", {})
        if isinstance(env, dict) and C.env("NOWITKNOWS_HOST", C.environment(env), "claude") != cfg.host:
            continue
        valid = (
            all(isinstance(item.get(k), str) and item[k] for k in ("session", "transcript", "cwd", "reason"))
            and type(stamp) in (int, float)
            and math.isfinite(stamp)
            and isinstance(env, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
        )
        if not valid:
            _retire_queue(path, ".bad")
        elif time.time() - stamp > C.MOD_QUEUE_DAYS * 86400 and held.get("queue_file") != path.name:
            _retire_queue(path, ".expired")
        else:
            result.append({**item, "queue_file": path.name})
    return result


def _retire_queue(path: Path, suffix: str) -> None:
    with suppress(FileNotFoundError):
        path.replace(path.with_suffix(suffix))
        noted(path.with_suffix(suffix))  # the migration's settlement retires queued work too (state.own_writes)


def retire_queue(cfg: C.Config, name: str) -> None:
    with file_lock(cfg.path("mod", "queue")):
        _retire_queue(cfg.path("mod", "queue", name), ".done")


def enqueue(cfg: C.Config, args: dict) -> str:
    with file_lock(cfg.path("mod", "queue")):
        for item in _queued(cfg):
            if item["session"] == args["session"] and item["transcript"] == args["transcript"]:
                return item["queue_file"]
        stamp = time.time_ns()
        sid = re.sub(r"[^a-zA-Z0-9_-]", "_", args["session"])
        name = f"{stamp}-{sid}.json"
        _save(cfg.path("mod", "queue", name), {**args, "ts": time.time(), "env": _settings(cfg)})
        return name


def start(cfg: C.Config, sid: str, cwd: str) -> dict:
    if cfg.remote or cfg.child:
        return {"queued": [], "allow_extra_usage": cfg.allow_extra_usage}
    with State(cfg.path("state.json"), cfg.host, cfg.legacy_host).edit() as data:
        session(data, sid).update(in_session=True, cwd=cwd)
    return {"queued": queued(cfg), "allow_extra_usage": cfg.allow_extra_usage}


def active(cfg: C.Config) -> dict:
    record = _read(cfg.path("mod", cfg.host, "active.json"))
    stamp = record.get("ts")
    if (
        type(stamp) not in (int, float)
        or not math.isfinite(stamp)
        or not isinstance(record.get("owner"), str)
        or not record["owner"]
        or not isinstance(record.get("step"), str)
        or not re.fullmatch(r"[a-f0-9]{32}", record["step"])
    ):
        return {}
    return record if record.get("host", cfg.legacy_host) == cfg.host and time.time() - stamp < C.MOD_LEASE_S else {}


def release(cfg: C.Config, sid: str | None = None) -> None:
    if sid is None or active(cfg).get("owner") == sid:
        cfg.path("mod", cfg.host, "active.json").unlink(missing_ok=True)


def _owns_step(cfg: C.Config, step: dict) -> bool:
    return (
        isinstance(step.get("env", {}), dict)
        and step.get("host", cfg.host) == cfg.host
        and C.env("NOWITKNOWS_HOST", C.environment(step.get("env", {})), cfg.legacy_host) == cfg.host
    )


def _request(cfg: C.Config, step: dict) -> dict:
    if not _owns_step(cfg, step):
        return DONE
    if step.get("kind") in ("judge", "reviser") and worker._settle_exhausted(cfg, SkillStore(cfg), step["proposed"]):
        _complete(cfg, step)
        _release_step(cfg, step["id"])
        return DONE
    queued = worker._queued_cfg(cfg, step["env"])  # its guards, the caller's and those saved now: the strictest
    if (
        not queued.review
        or worker.over_budget(cfg)
        or worker.over_budget(queued)
        or worker.dev_request_blocked(cfg, step["request"])
        or worker.dev_request_blocked(queued, step["request"])
    ):
        release(cfg)
        return DONE
    request = step["request"]
    system = request["system"]
    if request.get("schema"):
        system += "\nReturn only a JSON object matching this schema:\n" + json.dumps(request["schema"])
    return {
        "action": "model",
        "step": step["id"],
        "model": request["model"],
        "system": system,
        "prompt": request["prompt"],
        "maxTokens": worker.judge.revision_tokens(step["change"], step["current"])
        if step.get("kind") == "reviser"
        else C.MOD_MAX_TOKENS,
        "allow_extra_usage": cfg.allow_extra_usage and queued.allow_extra_usage,
        "timeoutMs": C.JUDGE_TIMEOUT_S * 1000,
    }


def _step(cfg: C.Config, flow: dict, context: dict, *, reserve: bool = True, identity: str | None = None) -> dict:
    value = {
        **context,
        "host": cfg.host,
        "id": identity or uuid.uuid4().hex,
        "ts": time.time(),
        "flow": flow,
        "env": _settings(cfg),
    }
    path = cfg.path("mod", "steps", value["id"] + ".json")
    value = _read(path) or value
    _save(path, value)
    if not reserve:
        _save(
            cfg.path("mod", "continuations", value["id"] + ".json"),
            {"step": value["id"], "owner": flow["owner"], "host": cfg.host},
        )
    if reserve:
        _save(
            cfg.path("mod", cfg.host, "active.json"),
            {"step": value["id"], "host": cfg.host, "owner": flow["owner"], "ts": value["ts"]},
        )
    return _request(cfg, value) if reserve else DONE


CODEX_COMMAND_OK = re.compile(r"^TOOL Codex\.command: (.*) -> ok$", re.M)
SKILL_FILE = re.compile(rf"(?<![\w.-])({C.SKILL_NAME.pattern})/SKILL\.md\b")


def keep_command_reads(cfg: C.Config, plan: dict) -> None:
    """Codex reads a SKILL.md with a shell command when its hook could supply only the path, a load no hook records:
    each read that worked counts as loaded for this review and, kept in the session's state, for its later ones."""
    read = [name for command in CODEX_COMMAND_OK.findall(plan["body"]) for name in SKILL_FILE.findall(command)]
    if not read:
        return
    plan["s"]["codex_read_skills"] = _latest([*plan["s"].get("codex_read_skills", []), *read])
    with State(cfg.path("state.json"), cfg.host, cfg.legacy_host).edit() as data:
        s = session(data, plan["sid"])
        s["codex_read_skills"] = _latest([*s.get("codex_read_skills", []), *read])


def _latest(names: list[str]) -> list[str]:
    """Each name once, in the order of its last appearance, and only the newest SESSION_SKILLS."""
    return list(reversed(dict.fromkeys(reversed(names))))[-C.SESSION_SKILLS :]


def support_context(store: SkillStore, folder: Path) -> str:
    lines, size = [], 0
    omitted = "(more support files omitted)"
    header = "\n<support_files>\nExisting support files cannot be replaced; use a new path.\n"
    for directory in ("references", "templates", "scripts"):
        for path in sorted((folder / directory).rglob("*")):
            relative = path.relative_to(folder).as_posix()
            try:
                if not path.is_file() or store.file(folder.name, relative, folder=folder) is None:
                    continue
                line = f"- {json.dumps(redact(relative))}: {path.stat().st_size} bytes"
            except (OSError, RuntimeError):
                continue
            if len(lines) >= C.MOD_SUPPORT_FILES or size + len(line) + len(omitted) + 1 > C.MOD_SUPPORT_CHARS:
                return header + "\n".join([*lines, omitted]) + "\n</support_files>"
            lines.append(line)
            size += len(line) + 1
    return header + "\n".join(lines) + "\n</support_files>" if lines else ""


def skill_context(cfg: C.Config, plan: dict, cwd: str) -> tuple[str, dict]:
    """Full SKILL.md text for the reviewer, loaded skills first. With bounded skills, on either host, it inlines only
    the skills the session loaded and those background runs may change; the rest keep their <user_skills> line."""
    store, texts, viewed, size = SkillStore(cfg), [], {}, 0
    loaded = {*plan["s"].get("skills", []), *plan["s"].get("codex_read_skills", [])}
    chain = native.settings_chain(Path(cwd), cfg.claude_home)
    paths = {}
    if C.env("NOWITKNOWS_HOST", cfg.env) == "codex":
        from .codex_host import skill_files

        files = [(path, "") for path in skill_files(cfg, Path(cwd))]
    elif C.env("NOWITKNOWS_HOST", cfg.env) == "cursor":
        from .cursor_host import skill_files as cursor_skill_files

        files = [(path, "") for path in cursor_skill_files(cfg, Path(cwd))]
    else:
        files = router.skill_files([cfg.skills_home, Path(cwd) / ".claude" / "skills"], cfg.claude_home, chain)
    names = list(
        dict.fromkeys(
            [*sorted(loaded), *[line[2:].split(":", 1)[0] for line in plan["listing"] if line.startswith("- ")]]
        )
    )
    for path, prefix in files:
        meta, _ = router.frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        paths[prefix + str(meta.get("name") or path.parent.name)] = path
    for name in names:
        if cfg.bounded_review_skills and name not in loaded and store.writable(name) is not None:
            continue
        path = paths.get(name)
        try:
            own_text = store.read(name)
            if (
                own_text is not None
                and store.writable(name) is None
                and path
                and router.home_skill(path, router.home_skill_roots(cfg))
                and store.skill_md(name).is_file()
                and path.resolve() != store.skill_md(name).resolve()
            ):
                path = store.file(name)
            text = path.read_text(encoding="utf-8-sig") if path else own_text
        except (OSError, UnicodeError):
            continue
        if text is None:
            continue
        body = masked_delivery(text, cfg)
        body += support_context(store, path.parent if path else store.folder(name))
        if size + len(body) > C.MOD_SKILL_CHARS:
            continue
        texts.append(f"<skill name={json.dumps(redact(name))}>\n{size_label(text)}\n{body}\n</skill>")
        size += len(body)
        if own_text == text:
            viewed[name] = sha(text)
    return "\n\n".join(texts), viewed


def memory_context(cfg: C.Config, plan: dict, targets: list[str] | None = None) -> str:
    modes = native.Modes.from_json(plan["modes"])
    targets = worker.memory_targets(plan["do_memory"], modes, plan.get("caller", False)) if targets is None else targets
    paths = [
        (target, cfg.data / "memory" / ("USER.md" if target == "user" else "MEMORY.md"))
        for target in targets
        if target != "project"
    ]
    if "memory" in targets:
        from .global_notes import LINK
        from .memory import EntryStore

        entries = EntryStore(cfg.data / "memory", create=False).entries("memory")
        paths += [
            ("memory", cfg.data / "memory" / "global" / f"{match[1]}.md") for e in entries if (match := LINK.search(e))
        ]
    if "project" in targets:
        paths += [("project", path) for path in sorted(modes.fallback_dir.glob("*.md"))]
    texts, size = [], 0
    for target, path in paths:
        if target == "memory" and path.parent.name == "global" and path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        wrapped = f"<memory target={json.dumps(target)} file={json.dumps(path.name)}>\n{text}\n</memory>"
        clean = redact(wrapped)
        header, _, body = clean.partition("\n")
        text = finish_delivery(body.removesuffix("\n</memory>"), clean != wrapped, cfg)
        if size + len(text) > C.MOD_MEMORY_CHARS or scan(text):
            continue
        texts.append(f"{header}\n{text}\n</memory>")
        size += len(text)
    return "\n\n".join(texts)


def _review(cfg: C.Config, flow: dict, sid: str, transcript: str, cwd: str, reason: str, queue_file: str = "") -> dict:
    snapshot_at = time.time()
    plan = worker.prepare_review(cfg, sid, Path(transcript), cwd, reason)
    if not plan["ran"]:
        return plan
    if C.env("NOWITKNOWS_HOST", cfg.env) == "codex":
        keep_command_reads(cfg, plan)
    text, viewed = skill_context(cfg, plan, cwd)
    memory = memory_context(cfg, plan)
    system = plan["system"]
    if C.env("NOWITKNOWS_HOST", cfg.env) in ("codex", "cursor"):
        system = tools_free_system(system)
        plan["s"]["codex_evidence_ts"] = snapshot_at
    extra = "\n\n<skill_files>\n" + text + "\n</skill_files>\n\n<current_memory>\n" + memory + "\n</current_memory>"
    cost = (
        C.REVIEW_FIXED_TOKENS + (plan.get("budget_chars", len(plan["body"])) + len(extra)) // 4 + C.REVIEW_JUDGE_TOKENS
    )
    if plan.get("caller") and worker.over_dev_budget(cfg, cost):
        SkillStore(cfg).drop_evidence_if_done(plan["run_id"])
        return {"ran": False, "reason": "Codex development budget reached"}
    if worker.over_budget(cfg, cost):
        SkillStore(cfg).drop_evidence_if_done(plan["run_id"])
        return {"ran": False, "reason": "daily budget reached"}
    return _step(
        cfg,
        flow,
        {
            "kind": "reviewer",
            "plan": plan,
            "viewed": viewed,
            "queue_file": queue_file,
            "request": {
                "model": cfg.reviewer_model,
                "system": system + CONTRACT,
                "prompt": plan["prompt"] + extra,
                "label": "reflector",
                "run_id": plan["run_id"],
                **({"schema": REVIEW_SCHEMA} if cfg.host == "cursor" else {}),
            },
        },
    )


SKILL_FILES = (
    "## Skill files\n\nThe loaded skills, and the listed skills nowitknows may change while room allows, are supplied "
    "in full in <skill_files>. Build every patch or edit from that text.\n\n"
)


def tools_free_system(system: str) -> str:
    """The reflector for a reviewer that has no tools and answers in JSON: its Tools section becomes a pointer to the
    supplied skill files, and its Final line section, a prose answer, is dropped."""
    system = re.sub(r"^## Tools\n.*?(?=^## )", lambda _: SKILL_FILES, system, count=1, flags=re.S | re.M)
    return re.sub(r"\n*^## Final line\n.*?(?=^## |\Z)", "\n", system, count=1, flags=re.S | re.M)


def _settled(cfg: C.Config, flow: dict, context: dict, result: dict) -> None:
    change, store = context["proposed"], SkillStore(cfg)
    flow["visited"].append(change["id"])
    if result["outcome"] == "deferred":
        flow["waiting"].append(result["name"])
    elif result["outcome"] == "applied" and result["name"] != change["name"]:
        worker._follow_rename(store, change.get("run_id"), change["name"], result["name"])
    store.drop_evidence_if_done(change.get("run_id"))


def _pending(cfg: C.Config, flow: dict, run_id: str | None = None) -> dict:
    store = SkillStore(cfg)
    for change in store.staged_changes():
        if change.get("host", cfg.legacy_host) != cfg.host:
            continue
        if run_id is not None and change.get("run_id") != run_id:
            flow["waiting"].append(change["name"])
            continue
        if change["id"] in flow["visited"] or change["name"] in flow["waiting"]:
            continue
        result = worker.pending_change(cfg, store, change, expire=run_id is None)
        context = {"proposed": change}
        if result is None:
            if worker.over_budget(cfg, C.REVIEW_JUDGE_TOKENS):
                release(cfg)
                return DONE
            context = worker.settle_begin(cfg, store, change, store.evidence(change.get("run_id")))
            if "request" in context:
                if worker.dev_request_blocked(cfg, context["request"]):
                    continue
                return _step(cfg, {**flow, "run_id": run_id}, context)
            result = context
        _settled(cfg, flow, {"proposed": change}, result)
    for note in notejudge.staged(cfg, run_id):
        if note["id"] in flow["visited"]:
            continue
        flow["visited"].append(note["id"])
        result = notejudge.expire(cfg, note) if run_id is None else None
        if result is None:
            if worker.over_budget(cfg, C.REVIEW_JUDGE_TOKENS):
                release(cfg)
                return DONE
            context = notejudge.begin(cfg, note, store.evidence(note.get("run_id")))
            if "request" in context:
                if worker.dev_request_blocked(cfg, context["request"]):
                    continue
                return _step(cfg, {**flow, "run_id": run_id}, context, identity=context["judged"]["token"])
        store.drop_evidence_if_done(note.get("run_id"))
    if run_id:
        store.drop_evidence_if_done(run_id)
    release(cfg)
    return DONE


def plan(cfg: C.Config, sid: str, transcript: str, cwd: str) -> dict:
    with file_lock(cfg.path("worker"), wait_s=0):
        resumed = resume(cfg, sid)
        if resumed is not None:
            return resumed
        if not _enabled(cfg, sid):
            return DONE
        if record := active(cfg):
            if record["owner"] != sid:
                return DONE
            step = _read(cfg.path("mod", "steps", record["step"] + ".json"))
            if step and not step.get("answered"):
                return _request(cfg, step)
            release(cfg)
        flow = {"owner": sid, "visited": [], "waiting": []}
        for item in queued(cfg):
            saved = worker._queued_cfg(cfg, item.get("env", {}))
            if not saved.review:
                continue
            source = Path(item["transcript"])
            if not source.exists():
                source = cfg.data / "archive" / saved.host / source.parent.name / f"{source.stem}.jsonl.gz"
                if not source.exists():
                    continue
            out = _review(saved, flow, item["session"], str(source), item["cwd"], item["reason"], item["queue_file"])
            if out.get("action") == "model":
                out["allow_extra_usage"] = cfg.allow_extra_usage and saved.allow_extra_usage
                return out
            if out.get("reason") in ("daily budget reached", "backing off after a failed run"):
                return DONE
            if out.get("reason") == "Codex development budget reached":
                continue
            retire_queue(cfg, item["queue_file"])
        if not worker.over_budget(cfg, C.REVIEW_JUDGE_TOKENS):
            pending = _pending(cfg, flow)
            if pending["action"] == "model":
                return pending
        if not transcript:
            return DONE
        current = State(cfg.path("state.json"), cfg.host, cfg.legacy_host).read()["sessions"].get(sid, {})
        out = _review(cfg, flow, sid, transcript, cwd, "precompact" if current.get("mod_precompact") else "stop")
        if current.get("mod_precompact") and out.get("reason") in (
            "not due",
            "no new exchanges",
            "too little new conversation",
            "nothing to learn from",
        ):
            with State(cfg.path("state.json"), cfg.host, cfg.legacy_host).edit() as data:
                latest = session(data, sid)
                if latest.get("mod_precompact") == current["mod_precompact"]:
                    latest.pop("mod_precompact", None)
        return out if out.get("action") == "model" else DONE


def _apply(cfg: C.Config, step: dict, text: str) -> None:
    try:
        payload = claude.structured({"result": text})
    except claude.ClaudeError:
        return
    plan = step["plan"]
    start = Path(plan.get("start") or plan.get("s", {}).get("cwd") or ".")
    modes = native.Modes.from_json(plan["modes"])  # a project note is judged with the settings of that day
    targets = worker.memory_targets(plan["do_memory"], modes, plan.get("caller", False))
    for i, call in enumerate(payload.get("memory", []) if isinstance(payload.get("memory"), list) else []):
        if not isinstance(call, dict) or call.get("target") not in targets:
            continue
        ops = call.get("operations")
        if not isinstance(ops, list):
            continue
        if call["target"] == "project":  # a note is judged before it is written
            notejudge.stage(
                cfg,
                modes,
                start,
                ops,
                plan["run_id"],
                step["id"] + ":note:" + str(i),
            )
            continue
        if any(not isinstance(op, dict) for op in ops):
            continue
        try:
            apply_operations(
                cfg,
                modes.fallback_dir,
                call["target"],
                ops,
                "background",
                plan["run_id"],
                effect_id=step["id"] + ":memory:" + str(i),
                shared=modes.shared,
                project=modes.key,
            )
        except (TypeError, ValueError, AttributeError):
            continue
    store = SkillStore(cfg, overlay_run=plan["run_id"])
    changes = payload.get("skills", []) if plan["do_skills"] and isinstance(payload.get("skills"), list) else []
    refused = set()
    for i, change in enumerate(changes):
        effect_id = step["id"] + ":skill:" + str(i)
        existing = next((c for c in store.staged_changes(plan["run_id"]) if c.get("effect_id") == effect_id), None)
        if existing:
            if existing["action"] != "archive" and not existing.get("file_path"):
                step["viewed"][existing["name"]] = sha(existing["content"])
            continue
        if not isinstance(change, dict) or not all(
            isinstance(change.get(k, ""), str)
            for k in ("name", "action", "content", "old_string", "new_string", "file_path")
        ):
            continue
        name, action = change.get("name", ""), change.get("action")
        fp = norm_file(change.get("file_path"))
        if action in ("patch", "edit") and (fp or step["viewed"].get(name) != sha(store.read(name))):
            continue
        if name in refused:
            continue
        if action == "write_file" and store.read(name, fp) is not None:
            refused.add(name)
            continue
        if action == "archive" and name not in store.owned():
            continue
        prepared = store.prepare(change)
        if action == "write_file" and not prepared.get("ok"):
            refused.add(name)
        if f"SKILL.md over {C.SKILL_BODY_MAX} chars" in prepared.get("errors", []):
            notify.push(cfg, notify.oversized_line(name, store.keep_oversized(change, plan["run_id"])))
        if prepared.get("ok"):
            triggers = change.get("triggers")
            store.stage(
                prepared,
                {
                    "source": "background",
                    "effect_id": effect_id,
                    "run_id": plan["run_id"],
                    "project": plan["project"],
                    "where": plan.get("where") or "",
                    "triggers": [t for t in triggers if isinstance(t, str)] if isinstance(triggers, list) else [],
                },
            )
            if action != "archive" and not prepared.get("file_path"):
                step["viewed"][name] = sha(prepared["content"])
    for i, change in enumerate(changes):
        if not isinstance(change, dict) or change.get("name") not in refused:
            continue
        effect_id = step["id"] + ":skill:" + str(i)
        staged = next((c for c in store.staged_changes(plan["run_id"]) if c.get("effect_id") == effect_id), None)
        record_id = uuid.uuid5(uuid.NAMESPACE_URL, effect_id).hex
        if not (store.rejected / f"{record_id}.md").exists():
            record = {**change, "id": record_id, "run_id": plan["run_id"]}
            finding = {"severity": "major", "issue": "support write refused", "fix": "use a new support-file path"}
            path = store.reject(record, [[finding]], reason="support write refused", related=False)
            notify.push(
                cfg,
                f"{notify.PREFIX} skipped change to skill {change['name']}: "
                f"support write refused; proposal kept {notify.link(path)}",
                key=record_id,
            )
        if staged:
            store.drop_staged(staged["id"])


def _load_step(cfg: C.Config, sid: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", sid):
        raise ValueError("invalid model step id")
    step = _read(cfg.path("mod", "steps", sid + ".json"))
    return (
        step if step and _owns_step(cfg, step) and not step.get("answered") and active(cfg).get("step") == sid else {}
    )


def _usage(cfg: C.Config, step: dict, answer: dict) -> None:
    usage = answer.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    usage = {k: v for k, v in usage.items() if isinstance(v, int) and v >= 0}
    req = step["request"]
    claude._log_usage(
        cfg,
        req["label"],
        req["model"],
        step["ts"],
        {
            "usage": usage,
            "subtype": "error_mod" if "error" in answer else "success",
            "num_turns": 1,
            "step_id": step["id"],
        },
        req.get("run_id"),
    )


def _receive(cfg: C.Config, sid: str, payload: dict) -> bool:
    if not re.fullmatch(r"[a-f0-9]{32}", sid):
        raise ValueError("invalid model step id")
    path = cfg.path("mod", "answers", sid + ".json")
    with file_lock(path):
        step = _read(cfg.path("mod", "steps", sid + ".json"))
        if not step or not _owns_step(cfg, step):
            return False
        if path.exists():
            payload = _read(path)
        else:
            if step.get("answered"):
                return False
            payload = _safe_payload(payload)
            _save(path, payload)
        if "error" not in payload or "usage" in payload:
            _usage(cfg, step, payload)
    return True


def resume(cfg: C.Config, owner: str, *, claim: bool = True) -> dict | None:
    """Consume a durable answer under the worker lock, including before a session-end handoff."""
    from . import paid

    paid.recover(cfg)
    result = None
    accounted = {
        r.get("step_id") for r in jsonl_rows(cfg.data / "usage.jsonl") if r.get("host", cfg.legacy_host) == cfg.host
    }
    for path in sorted((cfg.data / "mod/steps").glob("*.json")):
        step = _read(path)
        if step.get("id") != path.stem or not _owns_step(cfg, step):
            continue
        reply = cfg.data / "mod/answers" / path.name
        if reply.exists():
            payload = _read(reply)
            if step["id"] not in accounted and ("error" not in payload or "usage" in payload):
                _usage(cfg, step, payload)
                accounted.add(step["id"])
            if not step.get("answered"):
                reply_result = _answer(
                    cfg,
                    step["id"],
                    payload,
                    recovered=active(cfg).get("step") != step["id"] or step.get("flow", {}).get("owner") != owner,
                )
                if result is None or reply_result.get("action") == "model":
                    result = reply_result
    if result is not None:
        return result
    record = active(cfg)
    for path in sorted((cfg.data / "mod/continuations").glob("*.json")):
        item = _read(path)
        if item.get("host") != cfg.host:
            continue
        step = _read(cfg.path("mod", "steps", item["step"] + ".json"))
        if step.get("answered"):
            path.unlink(missing_ok=True)
        elif claim and step and not record and _enabled(cfg, owner):
            queued = worker._queued_cfg(cfg, step["env"])
            if worker.dev_request_blocked(cfg, step["request"]) or worker.dev_request_blocked(queued, step["request"]):
                continue
            step["flow"]["owner"] = owner
            _save(cfg.path("mod", "steps", step["id"] + ".json"), step)
            _save(
                cfg.path("mod", cfg.host, "active.json"),
                {"step": step["id"], "host": cfg.host, "owner": owner, "ts": time.time()},
            )
            return _request(cfg, step)
    if record.get("owner") != owner:
        return None
    path = cfg.path("mod", "answers", record["step"] + ".json")
    if path.exists():
        return _answer(cfg, record["step"], _read(path))
    return None


def answer(cfg: C.Config, sid: str, payload: dict) -> dict:
    if not _receive(cfg, sid, payload):
        return DONE
    try:
        with file_lock(cfg.path("worker"), wait_s=0):
            return _answer(cfg, sid, _read(cfg.path("mod", "answers", sid + ".json")))
    except TimeoutError:
        return DONE


def drain_cli(cfg: C.Config) -> None:
    """Finish queued settlement steps in a worker already authorized to start models."""
    from . import model, paid

    if any(
        _owns_step(cfg, step := _read(path))
        and not step.get("answered")
        and (cfg.data / "mod/answers" / path.name).exists()
        for path in (cfg.data / "mod/steps").glob("*.json")
    ):
        resume(cfg, "cli-recovery", claim=False)
    for _ in range(C.CODEX_REVIEW_STEPS):
        candidates = [
            _read(cfg.data / "mod/steps" / path.name)
            for path in sorted((cfg.data / "mod/continuations").glob("*.json"))
            if _read(path).get("host") == cfg.host
        ]
        step = next((s for s in candidates if _owns_step(cfg, s) and not s.get("answered")), None)
        if not step or not cfg.review or active(cfg):
            return
        saved = worker._queued_cfg(cfg, step["env"])
        if not saved.review or _request(saved, step).get("action") != "model":
            return
        with paid.capture(saved, {"kind": "module", "step": step["id"]}) as receipt:
            try:
                output = model.run(saved, **step["request"])
            except claude.AuthError:
                raise
            except claude.ClaudeError as error:
                worker.settle_defer(
                    saved,
                    SkillStore(saved),
                    step,
                    error,
                    paid_reply=getattr(error, "paid_reply", False) or paid.replied(receipt.get("output", {})),
                )
                _complete(saved, step)
                paid.complete(saved, receipt)
                continue
            text = (
                json.dumps(output["structured_output"])
                if isinstance(output.get("structured_output"), dict)
                else str(output.get("result") or "")
            )
            _receive(saved, step["id"], {"text": text, "usage": output.get("usage", {})})
            _answer(saved, step["id"], _read(saved.data / "mod/answers" / (step["id"] + ".json")), recovered=True)
            paid.complete(saved, receipt)


def pending_ids(cfg: C.Config) -> set[str]:
    return {
        step["proposed"]["id"]
        for path in (cfg.data / "mod/steps").glob("*.json")
        if (step := _read(path))
        and _owns_step(cfg, step)
        and not step.get("answered")
        and "proposed" in step
        and (
            (cfg.data / "mod/continuations" / path.name).exists()
            or (cfg.data / "mod/answers" / path.name).exists()
            or active(cfg).get("step") == path.stem
        )
    }


def _release_step(cfg: C.Config, sid: str) -> None:
    path = cfg.path("mod", cfg.host, "active.json")
    if _read(path).get("step") == sid:
        path.unlink(missing_ok=True)


def _complete(cfg: C.Config, step: dict) -> None:
    _save(cfg.path("mod", "steps", step["id"] + ".json"), {**step, "answered": True})
    cfg.path("mod", "continuations", step["id"] + ".json").unlink(missing_ok=True)


def _failed(cfg: C.Config, step: dict, error: Exception) -> None:
    """A reply that could not be applied is tried again by a later call, MOD_REPLY_TRIES times in all and then done
    with, so that one it can never apply does not hold up every review after it."""
    log_error(cfg, str(error))
    failures = step.get("failures", 0) + 1
    if failures < C.MOD_REPLY_TRIES:
        _save(cfg.path("mod", "steps", step["id"] + ".json"), {**step, "failures": failures})
    else:
        if step["kind"] == "reviewer" and step["queue_file"]:
            retire_queue(cfg, step["queue_file"])
        elif step["kind"] != "reviewer":
            worker.settle_defer(cfg, SkillStore(cfg), step, error, paid_reply=True)
        _complete(cfg, step)
    _release_step(cfg, step["id"])


def supersede(cfg: C.Config, sid: str) -> None:
    """A newer judgment replaces this step: it is answered, so a reply that comes late is not applied."""
    step = _read(cfg.path("mod", "steps", sid + ".json")) if re.fullmatch(r"[a-f0-9]{32}", sid) else {}
    if step and _owns_step(cfg, step) and not step.get("answered"):
        _complete(cfg, step)
        _release_step(cfg, sid)


def _answer(cfg: C.Config, sid: str, payload: dict, *, recovered: bool = False) -> dict:
    step = _read(cfg.path("mod", "steps", sid + ".json")) if recovered else _load_step(cfg, sid)
    if recovered and (not _owns_step(cfg, step) or step.get("answered")):
        return DONE
    if not step:
        return DONE
    if "error" not in payload or "usage" in payload:
        _usage(cfg, step, payload)
    current = cfg
    cfg = worker._queued_cfg(cfg, step["env"])
    if (
        payload.get("cancelled")
        or not current.review
        or not cfg.review
        or (not recovered and not _enabled(current, step["flow"]["owner"]))
    ):
        if step["kind"] == "reviewer":
            plan = step["plan"]
            if worker._reviewer_ran(cfg, plan["run_id"]):
                worker.log_run(
                    cfg, plan["run_id"], plan["sid"], plan["project"], plan["s"], "failed", reason=plan.get("trigger")
                )
            SkillStore(cfg).drop_evidence_if_done(plan["run_id"])
        elif payload.get("paid_reply"):
            worker.settle_defer(cfg, SkillStore(cfg), step, "paid reply discarded while review paused", paid_reply=True)
        _complete(cfg, step)
        _release_step(cfg, sid)
        return DONE
    if "error" in payload:
        if step["kind"] == "reviewer":
            worker.review_failed(cfg, step["plan"], claude.ClaudeError(str(payload["error"])))
        else:
            worker.settle_defer(
                cfg, SkillStore(cfg), step, str(payload["error"]), paid_reply=payload.get("paid_reply", False)
            )
        _complete(cfg, step)
        _release_step(cfg, sid)
        return DONE
    text, flow = str(payload.get("text") or ""), step["flow"]
    if step["kind"] == "reviewer":
        try:
            _apply(cfg, step, text)
            worker.finish_review(cfg, step["plan"], text)
            marker = step["plan"]["s"].get("mod_precompact")
            if marker:
                with State(cfg.path("state.json"), cfg.host, cfg.legacy_host).edit() as data:
                    current_session = session(data, step["plan"]["sid"])
                    if current_session.get("mod_precompact") == marker:
                        current_session.pop("mod_precompact", None)
        except Exception as error:
            _failed(cfg, step, error)
            worker.memory_notice(cfg, step["plan"]["run_id"], step["plan"])
            return DONE
        worker.memory_notice(cfg, step["plan"]["run_id"], step["plan"])
        if step["queue_file"]:
            retire_queue(cfg, step["queue_file"])
        _complete(cfg, step)
        result = DONE if recovered else _pending(cfg, flow, step["plan"]["run_id"])
    else:
        try:
            result = worker.settle_answer(cfg, SkillStore(cfg), step, text)
            if "request" in result:
                result = _step(
                    cfg,
                    flow,
                    result,
                    reserve=not recovered,
                    identity=hashlib.sha256((sid + ":next").encode()).hexdigest()[:32],
                )
            else:
                _settled(cfg, flow, step, result)
                result = DONE if recovered else _pending(cfg, flow, flow.get("run_id"))
        except Exception as error:  # a change it was settling stays staged for a later pass
            _failed(cfg, step, error)
            return DONE
    if step["kind"] != "reviewer":
        _complete(cfg, step)
    if recovered:
        _release_step(cfg, sid)
    if result.get("action") == "model":
        result["allow_extra_usage"] = current.allow_extra_usage and cfg.allow_extra_usage
    return result


def _reply_has_secret(text: str) -> bool:
    pending: list = [text]
    try:
        decoded = claude.structured({"result": text})
        pending.extend((decoded, json.dumps(decoded, ensure_ascii=False)))
    except claude.ClaudeError:
        pass
    while pending:
        value = pending.pop()
        if isinstance(value, str) and secret_shaped(value):
            return True
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return False


def _safe_payload(payload: dict) -> dict:
    from . import paid

    usage = payload.get("usage")
    clean = {"usage": {k: v for k, v in usage.items() if type(v) is int and v >= 0}} if isinstance(usage, dict) else {}
    if payload.get("paid_reply") or "text" in payload or paid.spent(usage):
        clean["paid_reply"] = True
    if payload.get("cancelled"):
        clean["cancelled"] = True
    if "error" in payload:
        clean["error"] = redact(str(payload["error"]))
    else:
        text = str(payload.get("text") or "")
        if _reply_has_secret(text):
            clean["error"] = "model reply contained secret-shaped content; discarded"
        else:
            clean["text"] = text
    return clean


def log_error(cfg: C.Config | None, message: str) -> None:
    try:
        if cfg is not None:
            with open(cfg.path("errors.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.ctime()} mod {redact(message)}\n")
    except OSError:
        pass


def fail(cfg: C.Config, sid: str, payload: dict) -> dict:
    payload = _safe_payload({**payload, "error": str(payload.get("error") or "model call failed")})
    log_error(cfg, f"{sid}: {payload['error']}")
    return answer(cfg, sid, payload) if sid else DONE
