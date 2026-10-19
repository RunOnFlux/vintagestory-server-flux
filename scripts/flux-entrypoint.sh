#!/bin/bash
# PID 1, and the supervisor of the Vintage Story server.
#
# The game runs as a CHILD, never exec, so a stop reaches us first and becomes a save, and a
# server that ends on its own is followed by the next one in place:
#
#   - every start: the server for VS_VERSION (the newest stable one by default) downloaded from
#     Anego's CDN if it is not installed yet, serverconfig.json written from the environment and
#     the panel's pending changes (fluxvs.py apply: a new world, a switch, a restore), the mods
#     VS_MODS lists installed from the ModDB, a backup, then the server with its console on a FIFO
#     the supervisor holds open
#   - once it is up: state "running" in flux/state.json, the world settings catalog of this game
#     version, and the admins VS_ADMINS names
#   - every FLUX_UPDATE_CHECK_MINUTES: a newer version for VS_VERSION means a restart (players
#     are warned first and given FLUX_UPDATE_GRACE_MINUTES): a client on a newer version cannot
#     join an older server
#   - every FLUX_BACKUP_HOURS: a backup, through the game's own /genbackup
#   - SIGTERM/SIGINT from Docker: SIGTERM to the server, which saves and quits (under 1 s
#     measured on a fresh world; Flux allows 10)
#   - the server ending on its own: a new generation in the same container
#   - FLUX_RESTART_MAX_ATTEMPTS unplanned restarts inside FLUX_RESTART_WINDOW seconds: the
#     container ends with 42 (FluxOS starts it again, with a fresh budget)
#
# Same family as runonflux/dst-server-flux and corekeeper-server-flux.

set -uo pipefail
# shellcheck source=scripts/flux-lib.sh
source /opt/flux/flux-lib.sh

FLUX_RESTART_MAX_ATTEMPTS="${FLUX_RESTART_MAX_ATTEMPTS:-5}"
FLUX_RESTART_WINDOW="${FLUX_RESTART_WINDOW:-3600}"
FLUX_RESTART_BACKOFF="${FLUX_RESTART_BACKOFF:-10}"
FLUX_STOP_GRACE="${FLUX_STOP_GRACE:-60}"
FLUX_BACKUP_HOURS="${FLUX_BACKUP_HOURS:-6}"
FLUX_UPDATE_CHECK_MINUTES="${FLUX_UPDATE_CHECK_MINUTES:-30}"
FLUX_UPDATE_GRACE_MINUTES="${FLUX_UPDATE_GRACE_MINUTES:-5}"
FLUX_READY_TIMEOUT="${FLUX_READY_TIMEOUT:-1800}"
FLUX_INSTALL_RETRY_MINUTES="${FLUX_INSTALL_RETRY_MINUTES:-5}"
PY=(python3 /opt/flux/fluxvs.py)
# The console commands the panel polls with, kept out of `docker logs` (flux-quiet.awk).
FLUX_QUIET_COMMANDS="${FLUX_QUIET_COMMANDS:-^/(list clients|stats|time|info seed|worldconfig [A-Za-z]+)$}"

s="${FLUX_SERVER_DIR}"
d="${FLUX_DATA_DIR}"

rm -f "${FLUX_RESTART_MARKER}"
mkdir -p "${s}" "${d}" "${FLUX_DIR}" "${FLUX_GAME_LOG_DIR}"

if [ -n "${VS_PORT:-}" ] && ! flux_valid_port "${VS_PORT}" >/dev/null; then
  flux_log "ERROR VS_PORT='${VS_PORT}' is not a port from 1024 to 65535; refusing to start"
  exit 42
fi
if ! request="$(flux_version_request "${VS_VERSION:-}")"; then
  flux_log "ERROR VS_VERSION='${VS_VERSION:-}' is not a version (latest, 1.22 or 1.22.7); refusing to start"
  exit 42
fi
flux_log "flux-entrypoint starting (image ${FLUX_IMAGE_VERSION:-dev}, Vintage Story ${request}, port ${VS_PORT:-42420} tcp+udp)"

server_pid=""
helpers=()
terminating=0
stopping=0
generation=0
restart_history=()

# flux/state.json, what the hub shows while the server is not simply running: $1 = state, $2 = a
# sentence for the owner. Written beside, then renamed.
write_state() {
  python3 - "$1" "${2:-}" "$(readlink "${s}/current" 2>/dev/null | sed 's#.*/##')" "${generation}" "${FLUX_STATE_FILE}" <<'PY'
import json, os, sys, time
state, message, version, generation, path = sys.argv[1:]
data = {"state": state, "message": message or None, "version": version or None, "generation": int(generation or 0),
        "since": int(time.time()), "image": os.environ.get("FLUX_IMAGE_VERSION", "dev")}
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path + ".tmp", "w") as f:
    json.dump(data, f)
os.replace(path + ".tmp", path)
PY
}

# shellcheck disable=SC2329  # invoked by the trap below
term_handler() {
  terminating=1
  flux_log "stop requested: saving the world and shutting down, no restart will follow"
  stop_server
}
trap term_handler TERM INT

# SIGTERM: the game's own handler saves everything and exits ("Server termination event SIGTERM
# received", measured at 0.9 s on a fresh world).
stop_server() {
  stopping=1
  [ -n "${server_pid}" ] && kill -TERM "${server_pid}" 2>/dev/null
}

players_online() {
  "${PY[@]}" status 2>/dev/null | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("players") or []))' 2>/dev/null || echo 0
}

announce() {
  flux_console "/announce $(flux_announce_text "$1")" >/dev/null 2>&1
}

install_server() {
  local v
  write_state updating "Downloading Vintage Story ${request} from Anego Studios' servers (about 51 MB)."
  # The version on stdout; the download's progress lines on stderr, logged (and kept OUT of the
  # capture: flux_log prints to stdout).
  if v="$("${PY[@]}" install "${request}" 2> >(while IFS= read -r line; do flux_log "${line}" >&2; done))" && [ -n "${v}" ]; then
    installed="$(tail -n 1 <<<"${v}")"
    return 0
  fi
  return 1
}

# shellcheck disable=SC2329  # run in the background below
# The game's log, watched for the line that says players can join; then the hub's lists.
ready_watch() {
  local started line
  started="$(date +%s)"
  while IFS= read -r line; do
    if flux_server_ready_line "${line}"; then
      flux_log "the server is up: Vintage Story ${installed}, ready in $(($(date +%s) - started))s"
      write_state running ""
      "${PY[@]}" catalog 2>&1 | while IFS= read -r l; do flux_log "${l}"; done
      "${PY[@]}" admins 2>&1 | while IFS= read -r l; do flux_log "${l}"; done
      exit 0
    fi
  done < <(timeout "${FLUX_READY_TIMEOUT}" tail -n +1 -F "${FLUX_GAME_LOG_DIR}/server-main.log" 2>/dev/null)
  flux_log "WARN the server did not say it was ready within ${FLUX_READY_TIMEOUT}s"
}

# shellcheck disable=SC2329  # run in the background below
backupper() {
  [ "${FLUX_BACKUP_HOURS}" -gt 0 ] 2>/dev/null || exit 0
  while true; do
    sleep $((FLUX_BACKUP_HOURS * 3600))
    "${PY[@]}" backup "every-${FLUX_BACKUP_HOURS}h" 2>&1 | while IFS= read -r l; do flux_log "${l}"; done
  done
}

# shellcheck disable=SC2329  # run in the background below
# A newer release for VS_VERSION: clients on the newer version cannot join this one, and the
# launcher moves players to new versions quickly.
update_watch() {
  local latest n left
  local every="${FLUX_UPDATE_CHECK_SECONDS:-$((FLUX_UPDATE_CHECK_MINUTES * 60))}"
  [ "${every}" -gt 0 ] 2>/dev/null || exit 0
  while true; do
    sleep "${every}"
    latest="$("${PY[@]}" latest "${request}" 2>/dev/null)"
    [ -n "${latest}" ] && [ "${latest}" != "${installed}" ] || continue
    if flux_is_downgrade "${installed}" "${latest}" && ! flux_truthy "${VS_ALLOW_DOWNGRADE:-}"; then
      continue
    fi
    n="$(players_online)"
    if [ "${n}" -gt 0 ] 2>/dev/null; then
      flux_log "Vintage Story ${latest} is out (running ${installed}): ${n} player(s) online, restarting in ${FLUX_UPDATE_GRACE_MINUTES} min"
      left="${FLUX_UPDATE_GRACE_MINUTES}"
      while [ "${left}" -gt 0 ]; do
        announce "The server restarts in ${left} minute(s) to update to Vintage Story ${latest}. The world is saved."
        if [ "${left}" -gt 1 ]; then sleep 60; left=$((left - 1)); else sleep 50; left=0; fi
        [ "$(players_online)" = "0" ] && break
      done
      announce "Restarting now for the update to ${latest}. Update your game in the launcher to rejoin."
      sleep 5
    else
      flux_log "Vintage Story ${latest} is out (running ${installed}): nobody online, restarting now"
    fi
    printf 'planned: the update to Vintage Story %s\n' "${latest}" >"${FLUX_RESTART_MARKER}"
    exit 0
  done
}

start_server() {
  # The previous generation's log says "now running" too: the watcher must only see this one's.
  rm -f "${FLUX_CONSOLE_FIFO}" "${FLUX_GAME_LOG_DIR}/server-main.log"
  mkfifo -m 600 "${FLUX_CONSOLE_FIFO}"
  (
    cd "${s}/current" || exit 1
    # The FIFO held open read-write, so the server never sees end-of-file when a writer
    # (flux-console, flux-vs) closes its end.
    exec 3<>"${FLUX_CONSOLE_FIFO}"
    # stdout through the quiet filter: the panel's polling (status, the player count, the world's
    # settings) would otherwise fill `docker logs`, the panel's Console tab, with the same answers
    # every 30 s. The game's own log keeps everything; flux-console reads that one.
    exec dotnet VintagestoryServer.dll --dataPath "${d}" --logPath "${FLUX_GAME_LOG_DIR}" \
      --archiveLogFileCount 3 <&3 > >(exec awk -v quiet="${FLUX_QUIET_COMMANDS}" -f /opt/flux/flux-quiet.awk)
  ) &
  server_pid=$!
}

start_generation() {
  local rc out
  generation=$((generation + 1))
  stopping=0
  rm -f "${FLUX_RESTART_MARKER}"

  install_server || return 3
  [ "${terminating}" = "1" ] && return 2
  out="$("${PY[@]}" apply 2>&1)"
  rc=$?
  while IFS= read -r line; do [ -n "${line}" ] && flux_log "${line}"; done <<<"${out}"
  if [ "${rc}" != "0" ]; then
    flux_log "ERROR serverconfig.json could not be written (exit ${rc})"
    return 1
  fi
  "${PY[@]}" ensure-mods 2>&1 | while IFS= read -r line; do flux_log "${line}"; done
  "${PY[@]}" backup start 2>&1 | while IFS= read -r line; do flux_log "${line}"; done
  [ "${terminating}" = "1" ] && return 2

  write_state starting "The world is loading."
  printf '%s\n' "${generation}" >"${FLUX_GENERATION_FILE}"
  flux_log "starting the server (generation ${generation}, Vintage Story ${installed})"
  start_server
  helpers=()
  ready_watch & helpers+=($!)
  update_watch & helpers+=($!)
  backupper & helpers+=($!)
  return 0
}

sweep_generation() {
  local p
  for p in "${helpers[@]}"; do kill "${p}" 2>/dev/null; done
  helpers=()
  pkill -f "tail -n \+1 -F ${FLUX_GAME_LOG_DIR}/server-main.log" 2>/dev/null
  p="$(flux_server_pid)"
  [ -n "${p}" ] && kill -KILL "${p}" 2>/dev/null
  server_pid=""
}

# Until the server ends. A stop or restart that is asked for (a signal, the restart marker) sends
# SIGTERM; one not honoured within FLUX_STOP_GRACE is killed.
gen_reason=""
wait_for_generation() {
  local asked_at=0 now
  gen_reason=""
  while true; do
    if [ -z "${server_pid}" ] || ! kill -0 "${server_pid}" 2>/dev/null; then
      if [ "${stopping}" = "0" ]; then
        wait "${server_pid}" 2>/dev/null
        gen_reason="the server ended on its own (exit $?)"
      fi
      break
    fi
    if [ "${stopping}" = "0" ] && { [ "${terminating}" = "1" ] || [ -f "${FLUX_RESTART_MARKER}" ]; }; then
      stop_server
    fi
    if [ "${stopping}" = "1" ]; then
      now="$(date -u +%s)"
      [ "${asked_at}" = "0" ] && asked_at="${now}"
      if [ $((now - asked_at)) -ge "${FLUX_STOP_GRACE}" ]; then
        flux_log "WARN the server was asked to stop ${FLUX_STOP_GRACE}s ago and has not; ending it"
        kill -KILL "${server_pid}" 2>/dev/null
      fi
    fi
    sleep 0.5 &
    wait $!
  done
}

idle_until() {
  local until_ts="$1"
  while [ "$(date +%s)" -lt "${until_ts}" ]; do
    [ "${terminating}" = "1" ] && return 0
    sleep 2 &
    wait $!
  done
}

installed=""
while true; do
  if [ "${terminating}" = "1" ]; then
    flux_log "stopped on request"
    exit 0
  fi
  start_generation
  started=$?
  if [ "${started}" = "2" ]; then
    flux_log "stopped on request before the server started"
    exit 0
  elif [ "${started}" = "3" ]; then
    write_state install_failed "The Vintage Story server could not be downloaded (VS_VERSION=${request}). Trying again in ${FLUX_INSTALL_RETRY_MINUTES} minutes."
    flux_log "ERROR the server could not be installed; trying again in ${FLUX_INSTALL_RETRY_MINUTES} min"
    idle_until $(($(date +%s) + FLUX_INSTALL_RETRY_MINUTES * 60))
    continue
  elif [ "${started}" != "0" ]; then
    flux_log "ERROR the server could not be prepared; ending the container (42)"
    exit 42
  fi
  wait_for_generation

  reason="${gen_reason}"
  if [ -f "${FLUX_RESTART_MARKER}" ]; then
    reason="$(cat "${FLUX_RESTART_MARKER}" 2>/dev/null)"
  fi
  sweep_generation
  rm -f "${FLUX_RESTART_MARKER}"

  if [ "${terminating}" = "1" ]; then
    write_state stopped ""
    flux_log "stopped on request"
    exit 0
  fi
  [ -z "${reason}" ] && reason="the server ended on its own"

  if flux_restart_counts "${reason}"; then
    now="$(date -u +%s)"
    read -r -a restart_history <<<"$(flux_restarts_in_window "${now}" "${FLUX_RESTART_WINDOW}" "${restart_history[@]}")"
    restart_history+=("${now}")
    if [ "${#restart_history[@]}" -gt "${FLUX_RESTART_MAX_ATTEMPTS}" ]; then
      write_state crashed "The server keeps stopping (${reason}). Check the console log."
      flux_log "${reason}: ${#restart_history[@]} restarts within ${FLUX_RESTART_WINDOW}s, so restarting it here is not working. Ending the container so the platform can rebuild or move it (42)."
      exit 42
    fi
  fi

  write_state restarting "${reason}"
  flux_log "${reason}: restarting the server in place in ${FLUX_RESTART_BACKOFF}s"
  sleep "${FLUX_RESTART_BACKOFF}" &
  wait $!
done
