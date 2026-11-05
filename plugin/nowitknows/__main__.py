from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import config as C


def _hook(event: str) -> int:
    """Never fail the session: any error becomes an empty hook result plus a log line."""
    cfg, raw, out = None, "", {}
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        cfg = C.get()
        from .hooks import handle

        out = handle(event, json.loads(raw) if raw.strip() else {}, cfg)
        if C.env("NOWITKNOWS_DEBUG", cfg.env) == "1":
            with open(cfg.path("debug.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "event": event, "input": raw, "output": out}) + "\n")
    except Exception:
        import traceback

        report = f"{time.ctime()} hook {event}\n{traceback.format_exc()}\n"
        try:
            with open(cfg.path("errors.log"), "a", encoding="utf-8") as f:  # type: ignore[union-attr]
                f.write(report)
        except (AttributeError, OSError):
            sys.stderr.write(report)
        out = {}
    sys.stdout.write(json.dumps(out))
    return 0


def _worker(ns: argparse.Namespace) -> int:
    from .worker import main

    cfg = C.get()
    try:
        result = main(
            cfg,
            {
                "reason": ns.reason,
                "session": ns.session,
                "transcript": ns.transcript,
                "cwd": ns.cwd,
                "curate": bool(int(ns.curate)),
                "triggers": bool(int(ns.triggers)),
                "learn": bool(int(ns.learn)),
            },
        )
        print(json.dumps(result, default=str))
    except Exception:
        import traceback

        with open(cfg.path("errors.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.ctime()} worker\n{traceback.format_exc()}\n")
        return 1
    return 0


def _mod(ns: argparse.Namespace) -> int:
    from . import modreview

    cfg, payload = None, None
    try:
        cfg = C.for_command()
        if ns.operation == "start":
            result = modreview.start(cfg, ns.session, ns.cwd)
        elif ns.operation == "plan":
            result = modreview.plan(cfg, ns.session, ns.transcript, ns.cwd)
        else:
            payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(f"mod {ns.operation} must be an object")
            result = getattr(modreview, ns.operation)(cfg, ns.step, payload)
    except TimeoutError:
        result = {"action": "error"} if ns.operation == "answer" and isinstance(payload, dict) else modreview.DONE
    except Exception:
        import traceback

        modreview.log_error(cfg, f"{ns.operation}\n{traceback.format_exc()}")
        result = {"action": "error"} if ns.operation == "answer" and isinstance(payload, dict) else modreview.DONE
    print(json.dumps(result))
    return 0


def _setup(ns: argparse.Namespace) -> int:
    from .setup import install, run

    cfg = C.for_command(create=not ns.dry_run)
    if not (ns.retention_days or ns.git or ns.allow_tools or ns.separate_notes or ns.backup_repo):
        return install(cfg, apply=ns.apply, dry_run=ns.dry_run, codex=not ns.no_codex, cursor=ns.cursor)

    return run(
        cfg,
        retention_days=ns.retention_days,
        git=ns.git,
        allow_tools=ns.allow_tools,
        assume_yes=ns.yes,
        dry_run=ns.dry_run,
        separate_notes=ns.separate_notes,
        backup_repo=ns.backup_repo,
    )


def _migrate(ns: argparse.Namespace) -> int:
    from .migrate import run

    return run(C.for_command(create=False), apply=ns.apply)  # notes go where sessions keep them


def _status(_: argparse.Namespace) -> int:
    from .setup import status

    print(status(C.for_command(), Path.cwd()))
    return 0


def _changes(ns: argparse.Namespace) -> int:
    from .setup import changes

    print(changes(C.for_command(), ns.n))
    return 0


def _keep(ns: argparse.Namespace) -> int:
    from .notify import PREFIX, link
    from .skills import SkillStore

    name = ns.name.strip()
    res = (
        SkillStore(C.for_command()).keep(name)
        if name
        else {"ok": False, "error": "usage: /nowitknows:keep <skill-name>"}
    )
    if not res["ok"]:
        print(f"nowitknows: {res['error']}")
        return 1
    verb = "restored" if res["action"] == "restore" else "kept"
    print(f"{PREFIX} {verb} skill {name}; it is yours now, and nowitknows never archives it {link(res['path'])}")
    return 0


def _report(ns: argparse.Namespace) -> int:
    from .report import render

    if ns.json:
        from .telemetry import summary

        print(json.dumps(summary(C.for_command()), indent=2, sort_keys=True))
    else:
        print(render(C.for_command(), ns.weeks))
    return 0


def _metrics(ns: argparse.Namespace) -> int:
    from .session_metrics import backfill_now

    print(json.dumps(backfill_now(C.for_command(create=False)), sort_keys=True))
    return 0


def _ui(ns: argparse.Namespace) -> int:
    from .ui import run

    return run(C.for_command(create=False), open_browser=ns.open)


def _index(_: argparse.Namespace) -> int:
    from .migrate import index_all

    cfg = C.get()
    print(index_all(cfg))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="nowitknows")
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hook")
    h.add_argument("event")
    w = sub.add_parser("worker")
    for a in ("reason", "session", "transcript", "cwd"):
        w.add_argument(f"--{a}", default="")
    w.add_argument("--curate", default="0")
    w.add_argument("--triggers", default="1")
    w.add_argument("--learn", default="1")
    mod = sub.add_parser("mod").add_subparsers(dest="operation", required=True)
    for operation in ("start", "plan", "answer", "fail"):
        command = mod.add_parser(operation)
        if operation in ("start", "plan"):
            command.add_argument("--session", required=True)
            command.add_argument("--cwd", required=True)
            if operation == "plan":
                command.add_argument("--transcript", required=True)
        else:
            command.add_argument("--step", default="")
    sub.add_parser("mcp")
    s = sub.add_parser("setup")
    s.add_argument("--retention-days", type=int, default=0)
    s.add_argument("--git", action="store_true")
    s.add_argument("--backup-repo", metavar="OWNER/NAME", help="name a private GitHub skill backup")
    s.add_argument("--allow-tools", action="store_true")
    s.add_argument("--separate-notes", action="store_true")
    s.add_argument("--no-codex", action="store_true", help="do not add nowitknows-codex where Codex is installed")
    s.add_argument("--cursor", action="store_true", default=None, help="also install nowitknows-cursor into ~/.cursor")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--apply", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    sub.add_parser("migrate").add_argument("--apply", action="store_true")
    sub.add_parser("status")
    c = sub.add_parser("changes")
    c.add_argument("--n", type=int, default=C.CHANGES_SHOWN)
    sub.add_parser("index")
    r = sub.add_parser("report")
    r.add_argument("--weeks", type=int, default=C.REPORT_WEEKS)
    r.add_argument("--json", action="store_true")
    sub.add_parser("metrics", help="collect local session counts").add_argument(
        "--backfill", action="store_true", required=True
    )
    sub.add_parser("ui", help="serve a read-only local snapshot").add_argument("--open", action="store_true")
    k = sub.add_parser("keep")
    k.add_argument("name")
    ns = p.parse_args(argv)
    if ns.cmd == "hook":
        return _hook(ns.event)
    if ns.cmd == "mcp":
        try:
            from .mcp_server import main as serve
        except ImportError as e:  # the background setup has not finished installing the packages
            sys.stderr.write(
                f"nowitknows's tools cannot start yet ({e}). Each session start says how its background setup is "
                "going; once that has finished, reconnect nowitknows in /mcp.\n"
            )
            return 1
        serve()
        return 0
    cmds = {
        "mod": _mod,
        "worker": _worker,
        "setup": _setup,
        "migrate": _migrate,
        "status": _status,
        "changes": _changes,
        "index": _index,
        "keep": _keep,
        "report": _report,
        "metrics": _metrics,
        "ui": _ui,
    }
    # Claude Code reads command output as UTF-8; a Windows pipe would get the ANSI code page ("·" garbled or fatal).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return cmds[ns.cmd](ns)


if __name__ == "__main__":
    raise SystemExit(main())
