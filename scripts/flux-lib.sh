#!/bin/bash
# Shared helpers for the Vintage Story image.
#
# SOURCING THIS FILE MUST STAY SIDE-EFFECT FREE: tests/test-flux.sh sources it directly and
# calls the functions with made-up inputs. Everything that decides something takes its inputs
# as arguments, so the decision can be tested without a server, a network or Docker.

# ---------------------------------------------------------------------------------------------
# Paths. Two volumes: the install (disposable, local to the node: one folder per game version,
# about 330 MB each, and the game's logs) and the data, which is the server's --dataPath: the
# worlds (Saves/), serverconfig.json, the players' roles and lists (Playerdata/), the mods
# (Mods/), the game's own backups (Backups/) and our files in flux/.
# ---------------------------------------------------------------------------------------------
FLUX_SERVER_DIR="${FLUX_SERVER_DIR:-/mnt/vs/server}"
FLUX_DATA_DIR="${FLUX_DATA_DIR:-/mnt/vs/data}"
FLUX_DIR="${FLUX_DIR:-${FLUX_DATA_DIR}/flux}"
FLUX_LOG="${FLUX_LOG-${FLUX_DIR}/flux.log}"
FLUX_LOG_MAX_LINES="${FLUX_LOG_MAX_LINES:-2000}"
# What the hub reads: the state of the server (updating, starting, running...).
FLUX_STATE_FILE="${FLUX_STATE_FILE:-${FLUX_DIR}/state.json}"
FLUX_BACKUP_DIR="${FLUX_BACKUP_DIR:-${FLUX_DIR}/backups}"
# The game's logs, kept off the synced volume (--logPath): server-main.log is where the console's
# answers land, at once (measured, 1.22.7).
FLUX_GAME_LOG_DIR="${FLUX_GAME_LOG_DIR:-${FLUX_SERVER_DIR}/logs}"
# The console is the server's stdin: a FIFO the supervisor holds open and the server reads.
FLUX_CONSOLE_FIFO="${FLUX_CONSOLE_FIFO:-/tmp/flux-console}"
FLUX_CONSOLE_LOCK="${FLUX_CONSOLE_LOCK:-/tmp/flux-console.lock}"
FLUX_RESTART_MARKER="${FLUX_RESTART_MARKER:-/tmp/flux-restart-requested}"
FLUX_GENERATION_FILE="${FLUX_GENERATION_FILE:-/tmp/flux-generation}"
# Anego's release index: every stable version with its Linux server's URL and MD5.
FLUX_VERSIONS_URL="${FLUX_VERSIONS_URL:-https://api.vintagestory.at/stable.json}"

flux_log() {
  local line
  line="$(date -u '+%Y-%m-%dT%H:%M:%SZ') [flux] $*"
  printf '%s\n' "${line}"
  if [ -n "${FLUX_LOG}" ] && mkdir -p "$(dirname "${FLUX_LOG}")" 2>/dev/null; then
    printf '%s\n' "${line}" >>"${FLUX_LOG}" 2>/dev/null || return 0
    flux_trim_file "${FLUX_LOG}" "${FLUX_LOG_MAX_LINES}"
  fi
}

# Keeps the last $2 lines of $1. Checked cheaply first: most calls change nothing.
flux_trim_file() {
  local file="$1" max="$2" lines
  lines="$(wc -l <"${file}" 2>/dev/null || echo 0)"
  if [ "${lines}" -gt $((max + max / 10)) ]; then
    tail -n "${max}" "${file}" >"${file}.tmp" 2>/dev/null && mv "${file}.tmp" "${file}"
  fi
}

flux_truthy() {
  case "${1,,}" in
    1 | true | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------------------------------------
# Inputs.
# ---------------------------------------------------------------------------------------------

# $1 = a port as given. Prints it, or returns 1 for a value that is not a usable port.
flux_valid_port() {
  local value="$1"
  [[ "${value}" =~ ^[0-9]{1,5}$ ]] || return 1
  value=$((10#${value}))
  [ "${value}" -ge 1024 ] && [ "${value}" -le 65535 ] || return 1
  printf '%s' "${value}"
}

# $1 = VS_VERSION as given. Prints what it asks for: "latest", a minor line ("1.22", the newest
# 1.22.x) or an exact version ("1.22.7", "1.22.0-rc.8"); returns 1 for anything else.
flux_version_request() {
  local v="${1,,}"
  v="${v#v}"
  case "${v}" in
    '' | latest | stable) printf 'latest' ;;
    *)
      [[ "${v}" =~ ^[0-9]+\.[0-9]+(\.[0-9]+(-(rc|pre)\.[0-9]+)?)?$ ]] || return 1
      printf '%s' "${v}"
      ;;
  esac
}

# Sorts version numbers newest first, on stdin one per line. A release candidate sorts below its
# release (1.22.0-rc.8 < 1.22.0), which `sort -V` alone gets the wrong way round.
flux_sort_versions() {
  sed -E 's/^([0-9.]+)$/\1-zzz/' | sort -rV | sed -E 's/-zzz$//'
}

# $1 = the request (flux_version_request), stdin = the available stable versions, one per line.
# Prints the version to run, or returns 1 when none matches. "latest" and a minor line never
# pick a release candidate; an exact request is taken as it is, if it exists.
flux_pick_version() {
  local want="$1" all
  all="$(grep -E '^[0-9]+\.[0-9]+\.[0-9]+(-(rc|pre)\.[0-9]+)?$' | flux_sort_versions)"
  case "${want}" in
    latest) grep -Ex '[0-9]+\.[0-9]+\.[0-9]+' <<<"${all}" | head -1 | grep . ;;
    *.*.*) grep -Fx "${want}" <<<"${all}" | head -1 | grep . ;;
    *) grep -Ex "${want//./\\.}\.[0-9]+" <<<"${all}" | head -1 | grep . ;;
  esac
}

# $1 = installed version, $2 = the one to run. True when moving would go BACK: a world saved by
# a newer game may not load in an older one, so the supervisor refuses it without
# VS_ALLOW_DOWNGRADE.
flux_is_downgrade() {
  [ -n "$1" ] && [ -n "$2" ] && [ "$1" != "$2" ] || return 1
  [ "$(printf '%s\n%s\n' "$1" "$2" | flux_sort_versions | head -1)" = "$1" ]
}

# ---------------------------------------------------------------------------------------------
# The restart budget: a server that keeps dying is not fixed by restarting it here forever.
# ---------------------------------------------------------------------------------------------

# $1 = now (epoch seconds), $2 = window in seconds, $3.. = the times of earlier restarts.
# Prints the restarts that are still inside the window, space separated.
flux_restarts_in_window() {
  local now="$1" window="$2" t kept=()
  shift 2
  for t in "$@"; do
    [ "${t}" -gt $((now - window)) ] && kept+=("${t}")
  done
  printf '%s' "${kept[*]}"
}

# $1 = why the last generation ended. Planned and requested ones (an update, the owner's restart)
# are the plan working, not a crash.
flux_restart_counts() {
  case "$1" in
    planned* | requested*) return 1 ;;
    *) return 0 ;;
  esac
}

# $1 = number to keep, then the files NEWEST FIRST. Prints the ones to delete.
flux_files_to_prune() {
  local keep="$1" i=0 f
  shift
  for f in "$@"; do
    i=$((i + 1))
    [ "${i}" -gt "${keep}" ] && printf '%s\n' "${f}"
  done
  return 0
}

# $1 = directory, $2 = glob, $3 = number to keep.
flux_prune() {
  local f files=()
  # shellcheck disable=SC2012,SC2086  # $2 is a glob on purpose; names are ours, no spaces
  mapfile -t files < <(ls -t "$1"/$2 2>/dev/null)
  while IFS= read -r f; do
    [ -n "${f}" ] && rm -rf "${f}"
  done < <(flux_files_to_prune "$3" "${files[@]}")
}

# ---------------------------------------------------------------------------------------------
# The running server.
# ---------------------------------------------------------------------------------------------

# $1 = a version. Where it is installed.
flux_install_dir() {
  printf '%s' "${FLUX_SERVER_DIR}/versions/$1"
}

# The pid of the game server, or nothing. Anchored on the arguments the supervisor starts it
# with: a bare "VintagestoryServer" also matches any shell whose command line mentions it.
flux_server_pid() {
  pgrep -f "^dotnet VintagestoryServer\.dll --dataPath " 2>/dev/null | head -1
}

# $1 = a console command as given. One line with no control characters, starting with "/": the
# game takes one command per line, and a line without the slash is said in chat as "Admin".
flux_console_line() {
  local cmd="${1//[$'\r\n']/ }"
  cmd="$(printf '%s' "${cmd}" | LC_ALL=C tr -d '\000-\037\177')"
  cmd="${cmd#"${cmd%%[![:space:]]*}"}"
  [ -n "${cmd}" ] || return 1
  [ "${cmd:0:1}" = "/" ] || cmd="/${cmd}"
  printf '%s' "${cmd}"
}

# $1 = one command. Written into the server's stdin; returns 1 when the server is not running,
# without blocking.
flux_console() {
  local cmd
  [ -p "${FLUX_CONSOLE_FIFO}" ] || return 1
  [ -n "$(flux_server_pid)" ] || return 1
  cmd="$(flux_console_line "$1")" || return 1
  # shellcheck disable=SC2016  # expanded by the inner bash, from its own arguments
  timeout 2 bash -c 'printf "%s\n" "$1" >"$2"' _ "${cmd}" "${FLUX_CONSOLE_FIFO}"
}

# $1 = a line of the server's log. True when the server is up and taking players: "Dedicated
# Server now running on Port 42420 and all ips!" (1.22.7).
flux_server_ready_line() {
  [[ "$1" == *"Dedicated Server now running on Port"* ]]
}

# $1 = a message for the players. One line, no slash at the start (it would be a command).
flux_announce_text() {
  local m="${1//[$'\r\n']/ }"
  m="${m#/}"
  printf '%s' "${m:0:200}"
}
