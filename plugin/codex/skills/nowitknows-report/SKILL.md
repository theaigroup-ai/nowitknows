---
name: nowitknows-report
description: Shows NowItKnows learning outcomes and token usage. Use when the user requests a NowItKnows report.
disable-model-invocation: true
---

Use the installed copy of this skill to find the plugin root: go up three folders from its containing folder. Run `/bin/sh "<plugin-root>/bin/nowitknows.sh" --codex` followed by the arguments below, passing each argument separately and keeping paths with spaces intact. It runs nowitknows's own Python once nowitknows's background setup has finished, and otherwise prints what it is waiting for. Keep the current working folder and selected `CODEX_HOME`.

If the command fails, report the failure plainly. Do not invent a successful result.

Run the packaged entry with `report`. Show the report, preserving its counts, token totals, and file links.

When the user requests the optional local browser viewer, run the packaged entry with `ui` instead, as a foreground terminal command. It prints a single-use local URL and runs until Ctrl-C. Add `--open` only when the user asks to open the browser. The viewer reads the selected data folder; native notes outside it are omitted and retained backups are labeled historical. Never start it from a hook.
