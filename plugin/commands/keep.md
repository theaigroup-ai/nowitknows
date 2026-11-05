---
description: Keep a NowItKnows skill for good, so NowItKnows never archives it, and bring it back if it was
argument-hint: <skill-name>
disable-model-invocation: true
allowed-tools: Bash(sh:*)
---
!`sh "${CLAUDE_PLUGIN_ROOT}/bin/nowitknows.sh" keep "$ARGUMENTS"`

Reproduce the line above exactly, keeping its link. Add nothing else.
