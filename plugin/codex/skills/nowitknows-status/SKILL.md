---
name: nowitknows-status
description: Shows NowItKnows memory, learning, and usage status. Use when the user requests NowItKnows status.
disable-model-invocation: true
---

Use the installed copy of this skill to find the plugin root: go up three folders from its containing folder. Run `/bin/sh "<plugin-root>/bin/nowitknows.sh" --codex` followed by the arguments below, passing each argument separately and keeping paths with spaces intact. It runs nowitknows's own Python once nowitknows's background setup has finished, and otherwise prints what it is waiting for. Keep the current working folder and selected `CODEX_HOME`.

If the command fails, report the failure plainly. Do not invent a successful result.

Run the packaged entry with `status`. Show the status in plain language, including any paused learning and the reason. Preserve file links and report usage in tokens.
