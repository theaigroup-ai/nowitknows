---
description: Install or update both host plugins, preview migration, and configure optional NowItKnows settings
disable-model-invocation: true
allowed-tools: Bash(sh:*)
---

Run installation and the read-only migration preview first:
`sh "${CLAUDE_PLUGIN_ROOT}/bin/nowitknows.sh" setup`
Where Codex is installed this also adds nowitknows-codex to it. If the user declined that, here or in the arguments (`--no-codex`), add `--no-codex` to that command.
Report the output, including Codex's required hook review. Migration applies only with `--apply` when the user requests it.

Then ask which optional settings to enable, with your recommendation:
1. Keep Claude Code transcripts for 3650 days instead of 30 (edits ~/.claude/settings.json).
2. Track the shared learned-skill library in git.
3. Allow nowitknows's memory, skill, and session-search tools without a permission prompt.
4. Keep project notes separate for each app (`--separate-notes`; edits ~/.claude/settings.json and nowitknows's Codex settings). By default project notes are shared between Claude Code and Codex, in Claude's memory folder for each repo; recommend keeping that default unless the user wants each app to keep its own notes.

If the user chooses any settings, run setup again with `--yes` and only their chosen flags:
`--retention-days 3650`, `--git`, `--allow-tools`, and/or `--separate-notes`.
If they choose none, configuration is complete. Do not invoke setup a second time with no option flags.
If they chose git, the GitHub backup question can only be answered in a terminal: pass on the command setup prints for it.
