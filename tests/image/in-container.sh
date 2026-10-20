#!/bin/bash
# wait_for evals its first argument later: the single quotes are on purpose throughout.
# shellcheck disable=SC2016,SC2034  # gen is read inside those quoted conditions
# Runs INSIDE the image (tests/test-image.sh). The repository is mounted read-only at /repo.
set -uo pipefail
export LC_ALL=C

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
# Waits up to $2 seconds for the command in $1 to succeed.
wait_for() {
  local _
  for _ in $(seq 1 $(($2 * 5))); do
    eval "$1" && return 0
    sleep 0.2
  done
  return 1
}
json() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {"d": d}))' "$1"; }

# --- What the scripts need is in the image -----------------------------------------------------
for tool in dotnet python3 perl pgrep pkill mkfifo timeout tail sed flock tar curl; do
  check "${tool} is installed" "0" "$(status command -v "${tool}")"
done
check "the .NET 10 runtime is there" "0" "$(status bash -c 'dotnet --list-runtimes | grep -q "Microsoft.NETCore.App 10\."')"
check "libicu is there (.NET globalisation)" "0" "$(status bash -c 'ldconfig -p | grep -q libicuuc')"
check "flux-console is on the PATH" "0" "$(status command -v flux-console)"
check "flux-vs is on the PATH" "0" "$(status command -v flux-vs)"
check "flux-vs starts (usage)" "64" "$(status flux-vs)"
for script in flux-entrypoint.sh flux-console.sh flux-lib.sh; do
  check "${script} parses" "0" "$(status bash -n "/opt/flux/${script}")"
done

# --- The unit tests, with the image's Python ----------------------------------------------------
(cd /repo && python3 -m unittest -q tests/test_fluxvs.py) >/tmp/unit.out 2>&1
check "the unit tests pass with the image's Python" "0" "$?"
grep -q FAIL /tmp/unit.out && cat /tmp/unit.out

# --- The supervisor, around a stand-in release and a stand-in server ----------------------------
# The release index and the "server download" are local files (urllib reads file://), and
# `dotnet` is the stand-in (tests/image/stand-in-server.pl): the real runtime is checked above.
mkdir -p /tmp/release/pkg/Mods /tmp/release/pkg/assets/game/lang
: >/tmp/release/pkg/VintagestoryServer.dll
python3 - <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location("t", "/repo/tests/test_fluxvs.py")
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)
open("/tmp/release/pkg/Mods/VSSurvivalMod.dll", "wb").write(t.WORLDCONFIG)
import json
json.dump(t.LANG, open("/tmp/release/pkg/assets/game/lang/en.json", "w"))
PY
tar -C /tmp/release/pkg -czf /tmp/release/vs_server.tar.gz .
md5="$(md5sum /tmp/release/vs_server.tar.gz | cut -d' ' -f1)"
cat >/tmp/release/stable.json <<JSON
{"1.22.7": {"linuxserver": {"filesize": "1 KB", "md5": "${md5}", "urls": {"cdn": "file:///tmp/release/vs_server.tar.gz"}}},
 "1.22.6": {"linuxserver": {"filesize": "1 KB", "md5": "${md5}", "urls": {"cdn": "file:///tmp/release/vs_server.tar.gz"}}}}
JSON
rm -f /usr/local/bin/dotnet
printf '#!/bin/bash\nexec perl /repo/tests/image/stand-in-server.pl "$@"\n' >/usr/local/bin/dotnet
chmod +x /usr/local/bin/dotnet

export FLUX_VERSIONS_URL=file:///tmp/release/stable.json FLUX_RESTART_BACKOFF=1 FLUX_UPDATE_CHECK_MINUTES=0
export VS_NAME="Image test" VS_PASSWORD=secret VS_MAX_CLIENTS=8 VS_SEED=777 VS_PLAYSTYLE=exploration VS_ADMINS=Tester
d=/mnt/vs/data
/opt/flux/flux-entrypoint.sh >/tmp/sup.log 2>&1 &
sup=$!

check "the server comes up" "0" "$(status wait_for 'grep -q "\"state\": \"running\"" ${d}/flux/state.json 2>/dev/null' 30)"
check "the version is installed and linked" "1.22.7" "$(readlink /mnt/vs/server/current | sed 's#.*/##')"
check "the name is written" "Image test" "$(json 'd["ServerName"]' <"${d}/serverconfig.json")"
check "the password is written" "secret" "$(json 'd["Password"]' <"${d}/serverconfig.json")"
check "max players are written" "8" "$(json 'd["MaxClients"]' <"${d}/serverconfig.json")"
check "the whitelist is off, not Default" "1" "$(json 'd["WhitelistMode"]' <"${d}/serverconfig.json")"
check "the first world has the seed" "777" "$(json 'd["WorldConfig"]["Seed"]' <"${d}/serverconfig.json")"
check "and the play style" "exploration" "$(json 'd["WorldConfig"]["PlayStyle"]' <"${d}/serverconfig.json")"
check "the catalog is cached" "0" "$(status wait_for 'test -s ${d}/flux/world-catalog.json' 10)"
check "the admin is made" "0" "$(status wait_for 'grep -q "VS_ADMINS: Tester is now an admin" /tmp/sup.log' 10)"

st="$(flux-vs status)"
check "status answers" "True" "$(json 'd["answered"]' <<<"${st}")"
check "status lists the player" "Tester" "$(json 'd["players"][0]["name"]' <<<"${st}")"
check "status has the calendar" "June" "$(json 'd["world"]["date"]["month"]' <<<"${st}")"
check "status has the live seed" "424242" "$(json 'd["world"]["seed"]' <<<"${st}")"
check "the console answers" "424242" "$(flux-console /info seed | tr -d '\r\n')"
check "online lists the player" "Tester" "$(flux-vs online | json 'd["players"][0]["name"]')"
check "polled answers stay out of docker logs" "1" "$(status grep -q 'List of online Players' /tmp/sup.log)"
check "but reach the game's log" "0" "$(status grep -q 'List of online Players' /mnt/vs/server/logs/server-main.log)"
w="$(flux-vs world)"
check "world reads the live values" "sometimes" "$(json 'd["values"]["temporalStorms"]' <<<"${w}")"
check "world has the catalog" "2" "$(json 'len(d["catalog"]["playstyles"])' <<<"${w}")"
check "set-world sets" "True" "$(flux-vs set-world '{"temporalStorms":"rare"}' | json 'd["ok"]')"
check "set-world refuses a creation-only setting" "1" "$(status flux-vs set-world '{"worldWidth":"256000"}')"
b="$(flux-vs backup-now)"
check "a backup through /genbackup" "True" "$(json 'd["ok"]' <<<"${b}")"
check "it holds the world" "0" "$(status bash -c "tar -tzf ${d}/flux/backups/$(json 'd["file"]' <<<"${b}") | grep -q Saves/default.vcdbs")"
check "the game's copy is cleaned up" "" "$(ls "${d}/Backups" 2>/dev/null)"

# A panel restart with a new world waiting.
check "a new world is queued" "True" "$(flux-vs new-world '{"name":"Frontier","seed":"5","playstyle":"wilderness","size":"256000"}' | json 'd["ok"]')"
gen="$(cat /tmp/flux-generation)"
flux-vs restart >/dev/null
check "the restart comes back as a new generation" "0" "$(status wait_for '[ "$(cat /tmp/flux-generation)" != "${gen}" ] && grep -q "\"state\": \"running\"" ${d}/flux/state.json' 30)"
check "on the new world" "0" "$(status bash -c "flux-vs worlds | grep -q 'frontier-.*current\": true'")"
check "the old world stays" "0" "$(status test -f "${d}/Saves/default.vcdbs")"
check "the new world's size" "256000" "$(json 'd["MapSizeX"]' <"${d}/serverconfig.json")"

# A crash: the server comes back in place.
gen="$(cat /tmp/flux-generation)"
pkill -KILL -f '^dotnet VintagestoryServer\.dll --dataPath '
check "a crash is restarted in place" "0" "$(status wait_for '[ "$(cat /tmp/flux-generation)" != "${gen}" ] && grep -q "\"state\": \"running\"" ${d}/flux/state.json' 30)"

# A restore: back to the default world.
bk="$(flux-vs backups | json '[x["file"] for x in d["backups"] if x["label"] == "manual"][0]')"
flux-vs restore "${bk}" >/dev/null
gen="$(cat /tmp/flux-generation)"
flux-vs restart >/dev/null
check "a restore runs on the next start" "0" "$(status wait_for '[ "$(cat /tmp/flux-generation)" != "${gen}" ] && grep -q "restored ${bk}" /tmp/sup.log' 30)"
check "the restored world runs" "default.vcdbs" "$(flux-vs worlds | json 'd["current"]')"
check "the world before it was kept" "0" "$(status bash -c "flux-vs backups | grep -q before-restore")"

# Docker's stop: a save, then the supervisor ends.
wait_for 'grep -q "\"state\": \"running\"" ${d}/flux/state.json' 30
start="$(date +%s)"
kill -TERM "${sup}"
wait "${sup}"
check "SIGTERM ends the supervisor with 0" "0" "$?"
check "within Docker's 10 seconds" "0" "$(status test $(($(date +%s) - start)) -lt 10)"
check "the world was saved" "0" "$(status grep -q 'World saved!' /tmp/sup.log)"
check "the state says stopped" "stopped" "$(json 'd["state"]' <"${d}/flux/state.json")"

# A bad port refuses to start.
VS_PORT=80 /opt/flux/flux-entrypoint.sh >/dev/null 2>&1
check "a bad port ends with 42" "42" "$?"
VS_VERSION=newest /opt/flux/flux-entrypoint.sh >/dev/null 2>&1
check "a bad version ends with 42" "42" "$?"

# --- flux.log is local; the banner ----------------------------------------------------------------
fl_srv=/mnt/vs/server
fl_data=/mnt/vs/data
check "flux.log is on the install volume" "0" "$(status test -s "${fl_srv}/flux-logs/flux.log")"
check "and not on the synced data volume" "1" "$(status test -e "${fl_data}/flux/flux.log")"
check "the banner is in the container output" "0" "$(status grep -q 'Vintage Story server image by FluxCloud - https://runonflux.com' /tmp/sup.log)"
check "and in flux.log" "0" "$(status grep -q 'Host this and 30+ other games in one click: https://runonflux.com/games' "${fl_srv}/flux-logs/flux.log")"
check "at the container start and again for each generation" "1" "$([ "$(grep -c 'server image by FluxCloud' /tmp/sup.log)" -ge 2 ] && echo 1 || echo 0)"

printf 'image: %s passed, %s failed\n' "${pass}" "${fail}"
[ "${fail}" = "0" ] || { echo '--- supervisor log'; tail -n 60 /tmp/sup.log; }
[ "${fail}" = "0" ]
