#!/bin/bash
# Unit tests: the pure decisions in flux-lib.sh, and fluxvs.py (tests/test_fluxvs.py). No
# Docker, no network, no game. Run from anywhere.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

# shellcheck source=scripts/flux-lib.sh
FLUX_LOG="" source scripts/flux-lib.sh

pass=0
fail=0
check() { # description, expected, actual
  if [ "$2" = "$3" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'FAIL %s\n  expected: [%s]\n  actual:   [%s]\n' "$1" "$2" "$3"
  fi
}
status() { "$@" >/dev/null 2>&1; echo $?; }

# --- Inputs ------------------------------------------------------------------------------------
check "42420 is a port" "42420" "$(flux_valid_port 42420)"
check "a leading zero is not octal" "9999" "$(flux_valid_port 09999)"
check "port 80 is refused" "1" "$(status flux_valid_port 80)"
check "a word is not a port" "1" "$(status flux_valid_port abc)"

# --- Versions ----------------------------------------------------------------------------------
check "empty is latest" "latest" "$(flux_version_request '')"
check "stable is latest" "latest" "$(flux_version_request Stable)"
check "a minor line" "1.22" "$(flux_version_request 1.22)"
check "an exact version, v dropped" "1.22.7" "$(flux_version_request v1.22.7)"
check "a release candidate" "1.22.0-rc.8" "$(flux_version_request 1.22.0-rc.8)"
check "a word is not a version" "1" "$(status flux_version_request newest)"
index=$'1.21.7\n1.22.0-rc.8\n1.22.7\n1.22.0\n1.20.12\n1.20.9\n1.22.6'
check "sorted newest first, rc below its release" $'1.22.7\n1.22.6\n1.22.0\n1.22.0-rc.8\n1.21.7\n1.20.12\n1.20.9' "$(flux_sort_versions <<<"${index}")"
check "latest picks the newest release" "1.22.7" "$(flux_pick_version latest <<<"${index}")"
check "a minor line picks its newest" "1.20.12" "$(flux_pick_version 1.20 <<<"${index}")"
check "an exact version is taken" "1.22.6" "$(flux_pick_version 1.22.6 <<<"${index}")"
check "a missing version is nothing" "1" "$(status flux_pick_version 1.19 <<<"${index}")"
check "1.22.6 after 1.22.7 is a downgrade" "0" "$(status flux_is_downgrade 1.22.7 1.22.6)"
check "1.22.7 after 1.22.6 is not" "1" "$(status flux_is_downgrade 1.22.6 1.22.7)"
check "the same version is not" "1" "$(status flux_is_downgrade 1.22.7 1.22.7)"
check "1.20.12 after 1.20.9 is not" "1" "$(status flux_is_downgrade 1.20.9 1.20.12)"

# --- Restarts ----------------------------------------------------------------------------------
check "old restarts age out" "900 950" "$(flux_restarts_in_window 1000 200 100 700 900 950)"
check "a planned restart is not counted" "1" "$(status flux_restart_counts 'planned: the update to Vintage Story 1.22.8')"
check "a requested restart is not counted" "1" "$(status flux_restart_counts 'requested: the owner asked from the panel')"
check "a crash is counted" "0" "$(status flux_restart_counts 'the server ended on its own (exit 134)')"
check "keeps the newest" $'c\nd' "$(flux_files_to_prune 2 a b c d)"

# --- The console -------------------------------------------------------------------------------
check "a slash is added" "/list clients" "$(flux_console_line 'list clients')"
check "a slash is kept" "/stats" "$(flux_console_line '/stats')"
check "a line break becomes a space" "/announce a b" "$(flux_console_line $'/announce a\nb')"
check "control characters go" "/stats" "$(flux_console_line $'/st\x01ats')"
check "leading spaces go" "/time" "$(flux_console_line '   /time')"
check "an empty command is refused" "1" "$(status flux_console_line '   ')"
check "the ready line" "0" "$(status flux_server_ready_line '18.10.2026 21:47:47 [Server Event] Dedicated Server now running on Port 42420 and all ips!')"
check "another line is not" "1" "$(status flux_server_ready_line '18.10.2026 21:47:47 [Server Event] Begin game ticking...')"
check "an announcement cannot be a command" "restart in 5" "$(flux_announce_text '/restart in 5')"
FLUX_SERVER_DIR=/s
check "the install of a version" "/s/versions/1.22.7" "$(flux_install_dir 1.22.7)"

# --- The quiet filter ---------------------------------------------------------------------------
q='^/(list clients|stats|time|info seed|worldconfig [A-Za-z]+)$'
log=$'18.10.2026 21:00:01 [Server Notification] Handling Console Command /list clients\n18.10.2026 21:00:01 [Server Notification] List of online Players\nPlaying [1] Tester 1.2.3.4:5 (42ms)\n\n18.10.2026 21:00:02 [Server Notification] Handling Console Command /stats \n18.10.2026 21:00:02 [Server Notification] Version: 1.22.7\nUptime: 0 days\n18.10.2026 21:00:03 [Server Event] tyron joins.\n18.10.2026 21:00:04 [Server Notification] Handling Console Command /kick bob\n18.10.2026 21:00:04 [Server Notification] bob was kicked'
want=$'18.10.2026 21:00:03 [Server Event] tyron joins.\n18.10.2026 21:00:04 [Server Notification] Handling Console Command /kick bob\n18.10.2026 21:00:04 [Server Notification] bob was kicked'
check "polled answers leave docker logs, the rest stays" "${want}" "$(awk -v quiet="${q}" -f scripts/flux-quiet.awk <<<"${log}")"
check "with no quiet list everything passes" "${log}" "$(awk -v quiet='' -f scripts/flux-quiet.awk <<<"${log}")"

printf 'lib: %s passed, %s failed\n' "${pass}" "${fail}"

# --- flux.log: local, rotated, migrated; the start-up banner -------------------------------------
lt="$(mktemp -d)"
rule='====================================================================='
want="$(printf '%s\n %s server image by FluxCloud - https://runonflux.com\n Host this and 30+ other games in one click: https://runonflux.com/games\n%s' "${rule}" "Test Game" "${rule}")"
banner_out="$(FLUX_LOG="${lt}/logs/flux.log" flux_banner "Test Game")"
check "the banner is exactly the FluxCloud text" "${want}" "${banner_out}"
check "the banner is ASCII only" "0" "$(printf '%s' "${banner_out}" | LC_ALL=C grep -c -P '[^\x00-\x7F]')"
check "the banner goes to flux.log too" "${want}" "$(cat "${lt}/logs/flux.log")"
# shellcheck source=scripts/flux-lib.sh
check "flux.log lives in flux-logs on the install volume by default" "/x/flux-logs/flux.log" "$(unset FLUX_LOG FLUX_LOG_DIR; FLUX_SERVER_DIR=/x; source scripts/flux-lib.sh; echo "${FLUX_LOG}")"
check "an empty FLUX_LOG writes no file" "" "$(FLUX_LOG="" flux_log hello >/dev/null; ls "${lt}/none" 2>/dev/null)"
for i in $(seq 1 40); do FLUX_LOG="${lt}/logs/flux.log" FLUX_LOG_MAX_BYTES=500 flux_log "line number ${i} padding padding padding" >/dev/null; done
check "flux.log rotates to one .1" "1" "$([ -s "${lt}/logs/flux.log.1" ] && echo 1 || echo 0)"
check "and the live file stays under the cap" "1" "$([ "$(stat -c %s "${lt}/logs/flux.log")" -le 600 ] && echo 1 || echo 0)"
check "no second rotated copy" "1" "$(status test -e "${lt}/logs/flux.log.2")"
printf 'old 1\nold 2\n' >"${lt}/old.log"
printf 'new 1\n' >"${lt}/mig.log"
FLUX_LOG="${lt}/mig.log" flux_log_migrate "${lt}/old.log" "${lt}/absent.log"
check "an old flux.log is appended to the new one" "new 1 old 1 old 2" "$(tr '\n' ' ' <"${lt}/mig.log" | sed 's/ $//')"
check "and deleted from the data volume" "1" "$(status test -e "${lt}/old.log")"
check "migrating with nothing to migrate is harmless" "0" "$(FLUX_LOG="${lt}/mig.log" status flux_log_migrate "${lt}/old.log")"
rm -rf "${lt}"

# --- fluxvs.py ---------------------------------------------------------------------------------
python3 -B -m unittest -q tests/test_fluxvs.py 2>&1 | tail -n 5
py=${PIPESTATUS[0]}

[ "${fail}" = "0" ] && [ "${py}" = "0" ]
