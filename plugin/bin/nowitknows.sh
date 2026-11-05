#!/bin/sh
# Starts nowitknows's hooks (`hook EVENT`), MCP server (`mcp`), hooks module calls (`mod ...`) and commands with the
# plugin's own Python once bin/bootstrap.sh, the only part of nowitknows that runs uv, has marked that environment ready
# for this uv.lock. Until then hooks exit 0 at once, SessionStart starting or reporting the setup.
# `--codex` first: a Codex install from the repo's catalog, whose environment lives outside
# the version folder Codex prunes on update, in the profile's nowitknows-runtime, one per uv.lock
profile_home=$HOME
windows_paths=0
if [ "${OS:-}" = Windows_NT ]; then
  PYTHONUTF8=1
  PYTHONIOENCODING=utf-8
  export PYTHONUTF8 PYTHONIOENCODING
  case $(uname -s 2>/dev/null) in MINGW* | MSYS*)
    command -v cygpath >/dev/null 2>&1 && windows_paths=1
    selected_home=${USERPROFILE:-${HOMEDRIVE:-}${HOMEPATH:-}}
    if [ -n "$selected_home" ]; then
      # Git Bash tools need shell paths for the profile Python selects on Windows.
      if command -v cygpath >/dev/null 2>&1; then
        profile_home=$(cygpath -u "$selected_home") || profile_home=$selected_home
      else
        profile_home=$(printf '%s' "$selected_home" | tr '\\' '/')
      fi
    fi
    ;;
  esac
fi

native_path() {
  if [ "$windows_paths" = 1 ]; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

host=claude
[ "${1:-}" != --codex ] || { host=codex; shift; }
[ "${1:-}" != --cursor ] || { host=cursor; shift; }
# the physical path, so every launch names the plugin folder in bootstrap.pid alike, however the host reached it
root=$(cd -P "${0%/*}/.." && pwd -P) || exit 0
if [ "$host" = codex ]; then
  selected=${NOWITKNOWS_CODEX_HOME-${LEARNLOOP_CODEX_HOME-}}
  data=${selected:-${CODEX_HOME:-$profile_home/.codex}}
  case $data in \~/*) data=$profile_home/${data#\~/} ;; esac
  native_profile=$data
  key=$(cksum < "$root/uv.lock" | tr -c '0-9\n' -)
  [ -n "$key" ] || exit 0
  runtime=$data/nowitknows-runtime/$key
  [ ! -e "$data/learnloop-runtime" ] || runtime=$data/learnloop-runtime/$key
  data=$runtime
  venv=$data/.venv
  entry=nowitknows_codex.py
  NOWITKNOWS_HOST=codex
  export NOWITKNOWS_HOST
elif [ "$host" = cursor ]; then
  selected=${NOWITKNOWS_CURSOR_HOME-${CURSOR_CONFIG_DIR-}}
  data=${selected:-$profile_home/.cursor}
  case $data in \~/*) data=$profile_home/${data#\~/} ;; esac
  native_profile=$data
  key=$(cksum < "$root/uv.lock" | tr -c '0-9\n' -)
  [ -n "$key" ] || exit 0
  runtime=$data/nowitknows-runtime/$key
  data=$runtime
  venv=$data/.venv
  entry=nowitknows_cursor.py
  NOWITKNOWS_HOST=cursor
  export NOWITKNOWS_HOST
else
  default=$profile_home/.nowitknows
  [ ! -e "$profile_home/.learnloop" ] || default=$profile_home/.learnloop
  selected=${NOWITKNOWS_DATA_DIR-${LEARNLOOP_DATA_DIR-}}
  data=${CLAUDE_PLUGIN_OPTION_DATA_DIR:-${selected:-$default}}
  case $data in \~/*) data=$profile_home/${data#\~/} ;; esac
  venv=$root/.venv
  entry=nowitknows.py
fi
# bin/bootstrap.sh builds this environment; set here always, so a value in the user's shell never moves it
NOWITKNOWS_VENV=$venv
export NOWITKNOWS_VENV
setup=$root/bin/bootstrap.sh
floor='>=3.14'
uv_minimum=0.9.5
mcp_wait_s=25
mcp_start_s=5
connect_s=30

ready() {  # bin/bootstrap.sh's mark of a finished sync for this uv.lock, beside pyvenv.cfg and the Python it names
  python=$venv/bin/python
  [ -x "$python" ] || python=$venv/Scripts/python.exe
  ready_mark=$venv/nowitknows-ready
  [ ! -e "$venv/learnloop-ready" ] || ready_mark=$venv/learnloop-ready
  [ -f "$venv/pyvenv.cfg" ] && [ -x "$python" ] && [ -f "$ready_mark" ] || return 1
  read -r mark < "$ready_mark" && [ "$mark" = "$(cksum < "$root/uv.lock")" ] || return 1
  python_entry=$root/bin/$entry
  if [ "$windows_paths" = 1 ]; then
    # Shell file operations and executable lookup retain shell paths; native Python receives Windows paths.
    python_entry=$(native_path "$python_entry")
    NOWITKNOWS_VENV=$(native_path "$venv")
    native_data=${NOWITKNOWS_DATA_DIR-${LEARNLOOP_DATA_DIR-}}
    [ "$host" != claude ] || native_data=$data
    if [ -n "$native_data" ]; then
      case $native_data in \~/*) native_data=$profile_home/${native_data#\~/} ;; esac
      NOWITKNOWS_DATA_DIR=$(native_path "$native_data")
      export NOWITKNOWS_DATA_DIR
    fi
    if [ -n "${CLAUDE_PLUGIN_OPTION_DATA_DIR:-}" ]; then
      native_data=$CLAUDE_PLUGIN_OPTION_DATA_DIR
      case $native_data in \~/*) native_data=$profile_home/${native_data#\~/} ;; esac
      CLAUDE_PLUGIN_OPTION_DATA_DIR=$(native_path "$native_data")
      export CLAUDE_PLUGIN_OPTION_DATA_DIR
    fi
    if [ "$host" = codex ]; then
      NOWITKNOWS_CODEX_HOME=$(native_path "$native_profile")
      export NOWITKNOWS_CODEX_HOME
    elif [ "$host" = cursor ]; then
      NOWITKNOWS_CURSOR_HOME=$(native_path "$native_profile")
      export NOWITKNOWS_CURSOR_HOME
    fi
  fi
  return 0
}

# A Codex session that started before the environment was ready: its first prompt that runs Python gets what the
# start would have given it (nowitknows/codex_host.py's _missed_start takes the note)
missed() {
  [ "$host" = codex ] || return 0
  sid=$(printf '%s' "$input" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([A-Za-z0-9_-]*\)".*/\1/p' | head -n 1)
  [ -n "$sid" ] && mkdir -p "$data" 2>/dev/null && : > "$data/start-missed.$sid"
}

# A Codex or Cursor subagent's event: left untouched
subagent() {
  printf '%s' "$1" | grep -q -E '"(agent_id|subagent_id|parent_conversation_id)"[[:space:]]*:[[:space:]]*"[^"]|"source"[[:space:]]*:[[:space:]]*"subagent"|"is_background_agent"[[:space:]]*:[[:space:]]*true'
}

status_command=/nowitknows:status
reconnect="reconnect nowitknows in /mcp"
if [ "$host" = codex ]; then
  status_command=\$nowitknows-codex:nowitknows-status
  reconnect="a new Codex session starts them"
elif [ "$host" = cursor ]; then
  status_command=/nowitknows-status
  reconnect="a new Cursor session starts them"
fi

quote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

json() {  # TEXT: as a JSON string's contents, on one line
  printf '%s' "$1" | tr '\n\r\t' '   ' | tr -d '\000-\010\013\014\016-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'
}

# Automated sessions have no reader, so leave their notices for an attended session.
attended() {
  [ "$host" = claude ] || return 1  # Codex says who runs a session only in its rollout: nowitknows/codex_host.py tells
  [ "${CLAUDE_CODE_ENVIRONMENT_KIND:-}" != bridge ] || return 0
  if [ -n "${CLAUDE_CODE_SESSION_ATTENDED+set}" ]; then
    [ "$CLAUDE_CODE_SESSION_ATTENDED" = 1 ]
    return
  fi
  case ${CLAUDE_CODE_ENTRYPOINT:-} in
    sdk-cli | sdk-ts | sdk-py | mcp | bench | claude-code-github-action | claude-security | remote_trigger | \
      remote_cowork_trigger | remote_baku) return 1 ;;
  esac
}

# This session's owed setup kind (nowitknows/hooks.py reads the same file); none without a usable session id
owed_file() {
  case ${CLAUDE_CODE_SESSION_ID:-} in '' | *[!A-Za-z0-9_-]*) return 1 ;; esac
  owed=$data/setup-owed.$CLAUDE_CODE_SESSION_ID
}

owe() {  # TEXT: a setup notice shown in an attended session is owed to its model, told at its next prompt
  attended && owed_file || return 0
  k=$(/bin/sh "$setup" kind "$1")
  [ -n "$k" ] || return 0
  mkdir -p "$data" 2>/dev/null && printf '%s\n' "$k" > "$owed"
}

tell() {  # this session's owed setup notice, told once at an attended prompt that Python cannot answer
  attended && owed_file || return 0
  mv -f "$owed" "$owed.$$" 2>/dev/null || return 0  # taken whole, so two prompts never both tell it
  { read -r k < "$owed.$$"; } 2>/dev/null
  rm -f "$owed.$$"
  said=$(/bin/sh "$setup" told "$k") || return 0
  printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"%s\\n- %s"}}\n' \
    "$(json "nowitknows notices; in this reply, tell the user in one short sentence:")" "$(json "$said")"
}

notice() {  # TEXT: SessionStart's systemMessage, JSON-escaped
  [ -z "$1" ] || printf '{"systemMessage":"%s"}\n' "$(printf '%s' "$1" | tr '\n\r\t' '   ' |
    tr -d '\000-\010\013\014\016-\037' | sed 's/\\/\\\\/g; s/"/\\"/g')"
}

find_uv() {  # sets uv to the first that starts from the plugin folder, where a pyenv shim may find a Python with uv
  for uv in uv "$profile_home/.local/bin/uv" "$profile_home/.cargo/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    if (cd "$root" && "$uv" --version) > /dev/null 2>&1; then
      [ "$uv" != uv ] || uv=$(command -v uv)
      return 0
    fi
  done
  return 1
}

any_uv() {  # whether any uv the setup could use is there, by its files only: the MCP start runs no uv
  command -v uv > /dev/null 2>&1 && return 0
  for each in "$profile_home/.local/bin/uv" "$profile_home/.cargo/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    [ -x "$each" ] && return 0
  done
  return 1
}

old() {  # UV: whether it predates the oldest uv checked to install Python 3.14
  (cd "$root" && "$1" --version) 2>/dev/null | awk -v m="$uv_minimum" 'NR == 1 { split($2, a, "."); split(m, b, "."); for (i = 1; i <= 3; i++) if (a[i] + 0 != b[i] + 0) { o = a[i] + 0 < b[i] + 0; break } } END { exit !o }'
}

setting_up() {  # SessionStart's notice before the environment is ready, starting the setup where uv can
  if ! find_uv; then
    echo "nowitknows is installed but needs uv, and none was found. Install it in a terminal with: curl -LsSf https://astral.sh/uv/install.sh | sh (Windows: https://docs.astral.sh/uv/getting-started/installation/), then start a new session."
  elif reason=$( (cd "$root" && "$uv" python find "$floor") 2>&1 > /dev/null); then
    /bin/sh "$setup" ensure "$uv" "$root" "$data" env
  elif old "$uv"; then  # a uv this old lists no Python 3.14 to install; with one already there it still syncs
    echo "nowitknows needs Python 3.14 or later, which $uv cannot install: it is $( (cd "$root" && "$uv" --version) 2>/dev/null | head -n 1), and installing Python 3.14 needs uv $uv_minimum or later. Update it in a terminal with: $(quote "$uv") self update (or with the package manager that installed it), then start a new session."
  else
    /bin/sh "$setup" ensure "$uv" "$root" "$data" python "$reason"
  fi
}

case ${1:-} in
  hook)
    # every hook exits 0, since a hook's exit 2 blocks the prompt, the tool call or the stop
    if ! ready; then
      [ "$2" != UserPromptSubmit ] || { tell; exit 0; }
      [ "$2" = SessionStart ] || [ "$2" = sessionStart ] || [ "$2" = beforeSubmitPrompt ] || exit 0
      if [ "$host" = codex ] || [ "$host" = cursor ]; then
        input=$(cat)
        ! subagent "$input" || exit 0
      fi
      line=$(/bin/sh "$setup" status "$root" "$data" < /dev/null) || line=$(setting_up < /dev/null)
      # else the setup finished while it looked: the session gets its context
      if ! ready; then
        if [ "$host" = cursor ]; then
          if [ -n "$line" ]; then
            printf '{"additional_context":"%s"}\n' "$(json "$line")"
          else
            printf '{}\n'
          fi
        else
          notice "$line"
          owe "$line"
          missed
        fi
        exit 0
      fi
    fi
    if [ -n "${input+set}" ]; then  # the setup finished while it looked; the hook input is read already
      printf '%s' "$input" | "$python" "$python_entry" hook "$2"
    else
      "$python" "$python_entry" hook "$2"
    fi
    status=$?
    [ "$status" = 0 ] || [ "$2" != UserPromptSubmit ] || tell  # a start that failed may fail at the prompt too
    if [ "$status" != 0 ] && [ "$2" = SessionStart ]; then
      line="nowitknows could not start: $python exited with status $status. Run $status_command to see the error."
      notice "$line"
      owe "$line"
    fi
    exit 0 ;;
  mcp)
    # A setup that runs, or that this session's start is about to begin, often finishes within Claude Code's connect
    # timeout. Claude Code 2.1.308 starts a server that exits meanwhile once more, within that timeout, so both starts
    # share one wait, timed from the first in this Claude Code process (the parent): the second still ends in time
    # for the failure to carry the reason below, not a timeout.
    wait_file=$data/mcp-wait.$PPID
    if ! ready; then
      now=$(date +%s)
      { read -r first < "$wait_file"; } 2>/dev/null || first=''
      case $first in '' | *[!0-9]*) first=0 ;; esac
      if [ "$first" -le "$now" ] && [ $((now - first)) -lt "$connect_s" ]; then
        again=1
      else
        first=$now
        mkdir -p "$data" 2>/dev/null
        for old in "$data"/mcp-wait.*; do  # left by Claude Code processes that ended while their server waited
          { read -r t < "$old"; } 2>/dev/null || continue
          case $t in '' | *[!0-9]*) continue ;; esac
          [ $((now - t)) -lt 3600 ] || rm -f "$old"
        done
        printf '%s\n' "$now" > "$wait_file"
      fi
      until ready; do
        waited=$(($(date +%s) - first))
        if /bin/sh "$setup" status "$root" "$data" > /dev/null; then
          [ "$waited" -lt "$mcp_wait_s" ] || break
        elif [ "$waited" -ge "$mcp_start_s" ]; then
          break
        fi
        sleep 1
      done
    fi
    if ready; then
      # this session's tools start, so no hook says the setup finished (nowitknows/hooks.py waits for this); Codex's
      # hooks say it in any case, with words that hold whether or not this session's tools run
      [ "$host" = codex ] || rm -f "$data/bootstrap.finished"
      rm -f "$wait_file"
      exec "$python" "$python_entry" mcp
    fi
    [ -z "${again:-}" ] || rm -f "$wait_file"  # Claude Code starts it no third time
    if any_uv || /bin/sh "$setup" status "$root" "$data" > /dev/null; then
      echo "nowitknows's tools cannot start yet: $(/bin/sh "$setup" why "$root" "$data"). Each session start says how its background setup is going; once it has finished, $reconnect." >&2
    else  # no setup can run without uv
      echo "nowitknows's tools cannot start yet: nowitknows needs uv, and none was found. The session start's notice gives the command that installs it; then start a new session." >&2
    fi
    exit 1 ;;
  *)
    ready && exec "$python" "$python_entry" "$@"
    echo "nowitknows cannot run yet: $(/bin/sh "$setup" why "$root" "$data"). Each session start says how its background setup is going."
    exit 1 ;;
esac
