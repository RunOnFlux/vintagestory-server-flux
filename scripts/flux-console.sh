#!/bin/bash
# flux-console <command...>: types one command into the server's console and prints what the
# server answered.
#
#   flux-console /list clients
#   flux-console 'announce The server restarts in 5 minutes'
#   flux-console /stats
#
# A command without the leading slash gets one (a line without it would be said in chat). The
# answer is read back from the game's own log (server-main.log, under --logPath), where it lands
# at once (measured, 1.22.7): the lines after the server's "Handling Console Command" for it,
# until the log has been quiet for a moment, up to FLUX_CONSOLE_WAIT seconds (default 3).
#
# ONE AT A TIME, under a lock shared with flux-vs: two commands written back to back can reach
# the game as one garbled line ("too many arguments", measured), and two answers would mix.
# Exit 1 when the server is not running, 64 without a command, 75 when the console stayed busy.
set -uo pipefail
# shellcheck source=scripts/flux-lib.sh
FLUX_LOG="" source /opt/flux/flux-lib.sh

[ $# -gt 0 ] || { echo "usage: flux-console <command...>" >&2; exit 64; }
cmd="$(flux_console_line "$*")" || { echo "usage: flux-console <command...>" >&2; exit 64; }

exec 9>"${FLUX_CONSOLE_LOCK}"
if ! flock -w "${FLUX_CONSOLE_LOCK_WAIT:-15}" 9; then
  echo "the console is busy with another command; try again" >&2
  exit 75
fi

log="${FLUX_GAME_LOG_DIR}/server-main.log"
before=0
[ -f "${log}" ] && before="$(wc -c <"${log}")"

if ! flux_console "${cmd}"; then
  echo "no server is running" >&2
  exit 1
fi

# The game logs "Handling Console Command <cmd>" first, then the answer, possibly over several
# lines and a little later (a lookup on the auth server takes about a second).
limit=$(( ${FLUX_CONSOLE_WAIT:-3} * 10 ))
waited=0 last=-1 quiet=0 acked=0
while [ "${waited}" -lt "${limit}" ]; do
  sleep 0.1
  waited=$((waited + 1))
  [ -f "${log}" ] || continue
  size="$(wc -c <"${log}")"
  # A server that restarted rewrote the log: start over from its beginning.
  [ "${size}" -lt "${before}" ] && before=0
  if [ "${acked}" = "0" ] && tail -c +$((before + 1)) "${log}" | grep -q 'Handling Console Command'; then
    acked=1
  fi
  if [ "${size}" = "${last}" ]; then
    quiet=$((quiet + 1))
    # Answered, and nothing more for 0.5 s.
    [ "${acked}" = "1" ] && [ "${quiet}" -ge 5 ] && [ "${size}" -gt "$(( before + 0 ))" ] && \
      [ "$(tail -c +$((before + 1)) "${log}" | grep -vc 'Handling Console Command')" -gt 0 ] && break
  else
    quiet=0
    last="${size}"
  fi
done

[ -f "${log}" ] && tail -c +$((before + 1)) "${log}" \
  | grep -v 'Handling Console Command' \
  | sed -E 's/^[0-9]{2}\.[0-9]{2}\.[0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} \[[A-Za-z ]+\] ?//'
exit 0
