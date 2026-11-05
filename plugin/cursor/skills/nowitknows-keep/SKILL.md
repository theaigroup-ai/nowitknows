---
name: nowitknows-keep
description: Keeps or restores a learned skill. Use when the user asks NowItKnows to keep a skill or undo an archive.
disable-model-invocation: true
---

Use the installed copy of this skill to find the plugin root: go up three folders from its containing folder. Run `/bin/sh "<plugin-root>/bin/nowitknows.sh" --cursor` followed by the arguments below, passing each argument separately and keeping paths with spaces intact. It runs nowitknows's own Python once nowitknows's background setup has finished, and otherwise prints what it is waiting for. Keep the current working folder.

If the command fails, report the failure plainly. Do not invent a successful result.

Use the skill name from the user's request. If no single name is clear, ask for it before running the command. Run the packaged entry with `keep` and the exact name as a separate argument. Show its result and preserve the file link. Never turn a skill name into shell code.
