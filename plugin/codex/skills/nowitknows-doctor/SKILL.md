---
name: nowitknows-doctor
description: Checks the NowItKnows Codex installation. Use when NowItKnows is missing, paused, or behaving unexpectedly.
disable-model-invocation: true
---

Use the installed copy of this skill to find the plugin root: go up three folders from its containing folder. Run `/bin/sh "<plugin-root>/bin/nowitknows.sh" --codex` followed by the arguments below, passing each argument separately and keeping paths with spaces intact. It runs nowitknows's own Python once nowitknows's background setup has finished, and otherwise prints what it is waiting for. Keep the current working folder and selected `CODEX_HOME`.

If the command fails, report the failure plainly. Do not invent a successful result.

Run the packaged entry with `doctor`. Explain `account_check` and `review_transport` separately. The transport check validates an isolated reviewer thread without submitting a model turn or spending model tokens. It does not prove a model response or successful learning. `learning_status` remains the last actual learning result; a successful diagnostic does not clear an earlier failure. Explain the next action for a failed check. If hook review is pending, tell the user to open `/hooks` in their selected Codex profile and review nowitknows-codex's exact eight definitions. Do not change hook trust, credentials, or another plugin's settings.
