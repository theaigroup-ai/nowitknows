#!/bin/sh
# Run uv only in the background so dependency installation cannot block a foreground hook.
windows_paths=0
if [ "${OS:-}" = Windows_NT ]; then
  PYTHONUTF8=1
  PYTHONIOENCODING=utf-8
  export PYTHONUTF8 PYTHONIOENCODING
  case $(uname -s 2>/dev/null) in MINGW* | MSYS*)
    command -v cygpath >/dev/null 2>&1 && windows_paths=1 ;;
  esac
fi

native_path() {
  if [ "$windows_paths" = 1 ]; then cygpath -w "$1"; else printf '%s' "$1"; fi
}
retry_s=600  # BOOTSTRAP_RETRY_S
peek_s=1  # BOOTSTRAP_PEEK_S
mcp_minutes=15  # MCP_RETRY_MINUTES
mcp_wait_s=25  # MCP_SETUP_WAIT_S
error_chars=300  # BOOTSTRAP_ERROR_CHARS
floor='>=3.14'  # PYTHON_FLOOR

clean() { tr '\n\r\t' '   ' | tr -d '\000-\010\013\014\016-\037' | cut -c "1-$error_chars"; }

quote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

load() {  # ROOT DATA: the attempt recorded for this plugin folder; false when there is none
  record=$2/bootstrap.pid
  log=$2/bootstrap.log
  pid='' token='' started=0 kind='' need='' code=''
  [ -f "$record" ] || return 1
  { read -r pid token started kind need; read -r at; read -r label code; } < "$record"
  [ "$label" = exit ] || code=''
  case $started in '' | *[!0-9]*) started=0 ;; esac
  [ "$at" = "$1" ] && [ -n "$token" ] && return 0
  pid='' token='' code=''
  return 1
}

# Whether the recorded attempt runs: its pid lives and still carries the attempt's token, so a reused pid does not count
alive() {
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
  if [ -r "/proc/$pid/cmdline" ]; then
    args=$(tr '\0' ' ' < "/proc/$pid/cmdline")
  elif command -v ps >/dev/null 2>&1; then
    args=$(ps -ww -o args= -p "$pid" 2>/dev/null)
  else
    return 0
  fi
  case $args in *"$token"*) return 0 ;; esac
  return 1
}

error() {  # FALLBACK: the first line of the attempt's output that names an error, else its last line
  e=$(grep -i -m 1 error "$log" 2>/dev/null)
  [ -n "$e" ] || e=$(grep -v '^[[:space:]]*$' "$log" 2>/dev/null | tail -n 1)
  printf '%s' "${e:-$1}" | clean
}

report() {  # STATE: the session start notice for the attempt, as plain text
  minutes=$((retry_s / 60))
  native_root=$root
  [ "$windows_paths" != 1 ] || native_root=$(native_path "$root")
  sync="$(quote "$uv") sync --frozen --project $(quote "$native_root")"
  what="uv is installing nowitknows's Python environment"
  [ "$need" != python ] || what="uv is downloading Python 3.14 or later and the packages nowitknows needs"
  after="Its tools start when it finishes: in this session if that is within about $mcp_wait_s s of the session's start; if not, reconnect nowitknows in /mcp once it has (for about $mcp_minutes minutes after its tools fail to start, new sessions skip them too)."
  [ "${NOWITKNOWS_HOST-${LEARNLOOP_HOST-}}" != codex ] || after="Its tools start when it finishes: in this session if that is within about $mcp_wait_s s of the session's start; if not, in the next new Codex session."
  case $1 in
    started)
      [ "$need" != python ] || what="$uv has no Python 3.14 or later ($detail), so it is downloading one and the packages nowitknows needs"
      echo "nowitknows is setting up in the background: $what. $after" ;;
    restarted) echo "nowitknows's last setup stopped before it finished, so it started again in the background: $what. $after" ;;
    running) echo "nowitknows is still setting up in the background: $what. $after" ;;
    finished) echo "nowitknows has just set up its Python environment." ;;
    failed)
      e=$(error "uv exited with status $code")
      if [ "$need" = python ]; then
        echo "nowitknows needs Python 3.14 or later, and $uv has none it may use ($detail) and could not download one ($e). Install one in a terminal ($(quote "$uv") python install 3.14, with uv Python downloads allowed, or your package manager), then start a new session; nowitknows tries again $minutes minutes after its last attempt."
      else
        echo "nowitknows could not set up its Python environment ($e). Run $sync in a terminal to see the whole error; nowitknows tries again $minutes minutes after its last attempt."
      fi ;;
    twice) echo "nowitknows's background setup stopped twice before it finished ($(error 'it logged no error')), so it tries again $minutes minutes after its last attempt; run $sync in a terminal to see what stops it." ;;
  esac
}

begin() {  # KIND STATE: a new attempt, then a short look at how it went
  kind=$1
  how=$2
  need=$want
  started=$now
  token=nowitknows-setup-$now-$$
  mine=$token
  : > "$log"
  set -- /bin/sh "$root/bin/bootstrap.sh" __run "$token" "$started" "$kind" "$need" "$uv" "$root" "$data"
  # Claude Code ends a hook that runs past its timeout by signalling its process group and every descendant, so the
  # attempt starts in a session of its own, from a subshell that exits at once and leaves it no hook's descendant.
  # macOS has no setsid command and perl is part of its base system; nohup, the last resort, only ignores hangups.
  (
    if command -v setsid >/dev/null 2>&1; then
      setsid "$@" &
    elif [ -x /usr/bin/perl ] && /usr/bin/perl -MPOSIX -e 1 2>/dev/null; then
      /usr/bin/perl -MPOSIX -e 'POSIX::setsid(); exec @ARGV or exit 127' "$@" &
    else
      nohup "$@" &
    fi
  ) < /dev/null >> "$log" 2>&1
  sleep "$peek_s"
  if load "$root" "$data" && [ "$token" = "$mine" ] && [ -n "$code" ]; then
    if [ "$code" = 0 ]; then report finished; else report failed; fi
  else
    need=$want
    report "$how"
  fi
}

ensure() {  # UV ROOT DATA NEED [DETAIL]
  uv=$1 root=$2 data=$3 want=$4 detail=''
  [ -z "${5:-}" ] || detail=$(printf '%s' "$5" | head -n 1 | clean)
  mkdir -p "$data" 2>/dev/null
  now=$(date +%s)
  if load "$root" "$data"; then
    age=$((now - started))
    if [ -z "$code" ]; then
      if alive; then
        report running
      elif [ "$kind" = retry ] && [ "$age" -lt "$retry_s" ]; then
        report twice
      elif [ "$kind" = retry ]; then
        begin first restarted
      else
        begin retry restarted
      fi
      return
    fi
    # a failure waits out its window unless what is missing has changed since, such as a Python installed meanwhile
    if [ "$code" != 0 ] && [ "$age" -lt "$retry_s" ] && [ "$want" = "$need" ]; then
      report failed
      return
    fi
  fi
  begin first started
}

note() {  # [CODE]: the attempt's record, with this process's pid, and its exit status once it has one
  printf '%s %s %s %s %s\n%s\n' "$$" "$token" "$started" "$kind" "$need" "$root" > "$record.$$" || return
  [ -z "${1:-}" ] || printf 'exit %s\n' "$1" >> "$record.$$"
  mv -f "$record.$$" "$record"
}

run() {  # TOKEN STARTED KIND NEED UV ROOT DATA: the attempt itself, in its own session
  token=$1 started=$2 kind=$3 need=$4 uv=$5 root=$6 data=$7
  record=$data/bootstrap.pid
  note
  venv=${NOWITKNOWS_VENV-${LEARNLOOP_VENV-$root/.venv}}  # bin/nowitknows.sh always sets it: the plugin's .venv, or Codex's runtime one
  venv=${venv:-$root/.venv}
  # the environment is nowitknows's own, whatever UV_PROJECT_ENVIRONMENT the user's shell sets
  native_root=$root
  UV_PROJECT_ENVIRONMENT=$venv
  export UV_PROJECT_ENVIRONMENT
  if [ "$windows_paths" = 1 ]; then
    native_root=$(native_path "$root")
    UV_PROJECT_ENVIRONMENT=$(native_path "$venv")
    NOWITKNOWS_VENV=$UV_PROJECT_ENVIRONMENT
    export NOWITKNOWS_VENV
  fi
  ready_mark=$venv/nowitknows-ready
  [ ! -e "$venv/learnloop-ready" ] || ready_mark=$venv/learnloop-ready
  # Leave an invalid legacy mark in place across a failed sync so a retry keeps the same filename.
  [ "$ready_mark" != "$venv/learnloop-ready" ] || : > "$ready_mark"
  rm -f "$venv/nowitknows-ready"
  # uv refuses a .venv that holds no Python, and takes one that holds a Python but no pyvenv.cfg for that Python's own
  # environment, installing into it; such a .venv is a build artifact an interrupted install left half made
  if [ -d "$venv" ] && [ ! -f "$venv/pyvenv.cfg" ]; then
    rm -rf "$venv.incomplete"
    mv "$venv" "$venv.incomplete" && printf '%s\n' "nowitknows: moved $venv, which has no pyvenv.cfg, to $venv.incomplete"
  fi
  mkdir -p "${venv%/*}" && cd "$root" && "$uv" sync --quiet --frozen --python "$floor" --project "$native_root"
  code=$?
  if [ "$code" = 0 ]; then
    # nowitknows/hooks.py says once that it finished; written before the ready mark, so a server that finds the
    # environment ready finds this too, and removes it as its session's tools start
    printf '%s\n' "$token" > "$data/bootstrap.finished"
    # the mark the launcher reads: this uv.lock's checksum, written only once the sync succeeded
    cksum < "$root/uv.lock" > "$ready_mark.$$" && mv -f "$ready_mark.$$" "$ready_mark"
  fi
  note "$code"
}

status() {  # ROOT DATA
  root=$1
  load "$1" "$2" && [ -z "$code" ] && alive || return 1
  report running
}

why() {  # ROOT DATA: what keeps the environment from being ready
  if ! load "$1" "$2"; then
    echo "its Python environment is not set up"
  elif [ -z "$code" ] && alive; then
    echo "its background setup is still running"
  elif [ -z "$code" ]; then
    echo "its background setup stopped before it finished"
  elif [ "$code" != 0 ]; then
    echo "its background setup failed ($(error "uv exited with status $code"))"
  else
    echo "its Python environment is not set up for this version of nowitknows"
  fi
}

kind() {  # TEXT: the kind of a setup notice, from how it opens, as notify.setup_kind reads it
  case $1 in
    "nowitknows is installed but needs uv"*) echo uv-missing ;;
    "nowitknows needs Python 3.14 or later, which"*) echo uv-old ;;
    "nowitknows could not start"*) echo start-failed ;;
    "nowitknows is setting up"* | "nowitknows is still setting up"* | "nowitknows's last setup stopped"*) echo setup-running ;;
    "nowitknows has just set up"*) echo setup-done ;;
    "nowitknows needs Python"* | "nowitknows could not set up"* | "nowitknows's background setup stopped twice"*)
      echo setup-failed ;;
  esac
}

told() {  # KIND: what the model is told for it, notify.FIXED's words
  case $1 in
    uv-missing) echo "nowitknows needs uv, and none was found; its setup notice gives the command that installs it" ;;
    uv-old) echo "nowitknows needs a newer uv to install Python 3.14; its setup notice says how to update it" ;;
    start-failed) echo "nowitknows could not start; /nowitknows:status shows the error" ;;
    setup-running) echo "nowitknows is installing what its tools need in the background; they start once it finishes" ;;
    setup-done) echo "nowitknows has finished setting up; reconnecting nowitknows in /mcp starts its tools" ;;
    setup-failed) echo "nowitknows could not install what its tools need; its setup notice says how to finish" ;;
    *) return 1 ;;
  esac
}

case ${1:-} in
  kind) shift; kind "$@" ;;
  told) shift; told "$@" || exit 1 ;;
  ensure) shift; ensure "$@" ;;
  status) shift; status "$@" || exit 1 ;;
  why) shift; why "$@" ;;
  __run) shift; run "$@" ;;
esac
exit 0
