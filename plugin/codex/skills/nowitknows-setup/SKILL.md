---
name: nowitknows-setup
description: Sets up NowItKnows for Codex. Use when the user requests NowItKnows setup.
disable-model-invocation: true
---

Use the installed copy of this skill to find the plugin root: go up three folders from its containing folder. Run `/bin/sh "<plugin-root>/bin/nowitknows.sh" --codex` followed by the arguments below, passing each argument separately and keeping paths with spaces intact. It runs nowitknows's own Python once nowitknows's background setup has finished, and otherwise prints what it is waiting for. Keep the current working folder and selected `CODEX_HOME`.

If the command fails, report the failure plainly. Do not invent a successful result.

First run the packaged entry with `doctor`, then with `setup`. Explain the setup guidance and any paused learning in plain language. Run only options supported by the packaged entry's help. Codex manages its own chat retention and tool permissions; do not edit Claude settings or promise Claude-specific options. Do not change hook trust. When trust is pending, guide the user to `/hooks` in their selected Codex profile to review nowitknows-codex's exact eight definitions.

When the user asks to move older nowitknows data or project notes into the shared folders, run the entry with `migrate` (a read-only plan) and show it. Run `migrate --apply` only after the user confirms. What this version writes does not hold it up; if it refuses, show its report, which names what does, such as a chat still on an older nowitknows.

When the user requests a setting change, run the entry with `configure` followed by each chosen `key=value` as a separate argument. For example, `configure review=off` pauses learning, and `configure allow_extra_usage=off` requests learning within included usage only. Use the current setup output and help for available settings. Report the resulting saved settings, including any pause reason. Do not change settings the user did not request.
