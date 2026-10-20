# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **Docker image repo**, not an application: the Vintage Story dedicated server for Flux,
`runonflux/vintagestory-server-flux`. ubuntu:24.04, Microsoft's .NET 10 runtime tarball (pinned
with its SHA-512), and a supervisor in bash. The game itself is NOT in the image: it is
downloaded from Anego's CDN on the first start. Same family as `~/work/dst-server-flux` and
`~/work/corekeeper-server-flux`; read them for the patterns this one follows.

The rule that shapes everything: **the container supervises its own server.** PID 1 is
`flux-entrypoint.sh`, never the game. The game runs as a child so a stop becomes a save, and a
server that ends on its own is followed by the next one in place.

## Commands

```bash
./tests/test-flux.sh
docker build -t vintagestory-server-flux:local .
./tests/test-image.sh                    # the scripts INSIDE the built image; CI gates the publish on it
docker run --rm -v "$PWD:/mnt" -w /mnt koalaman/shellcheck:stable -x scripts/*.sh tests/*.sh tests/image/*.sh .github/ci/*.sh
```

A change to what the image does bumps `VERSION` and gets a line under "Versions" in the README.

## The files

- `scripts/flux-entrypoint.sh`: PID 1. Per generation: `fluxvs.py install` (the version), `apply`
  (serverconfig.json, pending new world / switch / restore), `ensure-mods`, a backup, the server on
  its FIFO, then the watchers (ready, updates, backups).
- `scripts/fluxvs.py`: every decision about the server's files and the hub's commands
  (`flux-vs ...`). The one place those rules live.
- `scripts/flux-console.sh`: `flux-console <command>` on the PATH.
- `scripts/flux-lib.sh`: shared helpers. **Sourcing it must stay side-effect free**: the tests
  source it directly.
- `tests/image/stand-in-server.pl`: the image test's stand-in for `dotnet VintagestoryServer.dll`.

## Things that will bite you

- **WhitelistMode `Default` (0) is ON on a dedicated server** (EnumWhitelistMode docs: Off = 1,
  On = 2). Since 1.20 a fresh server turns everyone away and is not on the public list. `apply`
  turns a Default into Off unless `VS_WHITELIST` says otherwise.
- **The game's own `/moddb install` is broken on 1.22.7**: it parses the ModDB's game version
  tag ids (e.g. -281569466515455) as Int32 and throws. `fluxvs.py` installs from
  `mods.vintagestory.at/api/mod/<id>` itself and resolves `modinfo.json` dependencies.
- **WorldConfig in serverconfig.json only matters when a world is CREATED.** An existing save
  keeps its own settings; they are read and changed with `/worldconfig <key> [value]`, which
  writes them into the save and needs a restart. `worldWidth`/`worldLength` answer "Changing world
  size is not supported". A new world gets a save file of its own, so the old one stays.
- **Console answers land in `server-main.log`**, under `--logPath` (local, `/mnt/vs/server/logs`).
  The log is rewritten at each start, so the supervisor deletes it before starting (the ready
  watcher would otherwise read the last generation's "now running").
- **Two commands written back to back can arrive as one garbled line** ("too many arguments",
  measured): one command at a time, each after the previous one's "Handling Console Command".
- **Find the server by `^dotnet VintagestoryServer\.dll --dataPath `.** The stand-in server in
  the image test renames its command line to that.
- **SIGTERM saves at once** ("Server termination event SIGTERM received", 0.9 s on a host, 1.6 s
  `docker stop`). The console `/stop` once took 30 s to save on a busy host: never use it to stop.
- **Players are names, resolved by the game on Anego's auth server** (`/whitelist add`, `/ban`,
  `/op` take a player name; an unknown name: "No player with name or uid ... exists"). Their
  files (`Playerdata/*.json`) are written at once.
- **`/list clients` with players is not verified**: the line format comes from the game's string
  table (`{4} [{0}] {1} {2} ({3}ms) ({5}s inactive)`); the parser looks for `[id] name address (Nms)`.
- **Empty env = set**: the marketplace writes blank declared parameters as `NAME=`; `VS_PASSWORD=`
  clears the password.
- **Flux stops a container with Docker's default 10 seconds.**
- **`flux.log` is local** (`$FLUX_SERVER_DIR/flux-logs/flux.log`, 5 MB then one `.1`), never on the synced data volume: a constantly written file there is constant Syncthing traffic. `flux_log_migrate` moves an old one off the data volume at start. `flux_banner` prints the FluxCloud banner at container start and every generation; its text is tested.
- **The scheduled CI run publishes only with something to apply** (`.github/ci/needs-rebuild.sh`): urgent security updates, or commits on main since the published `:latest` (label `org.opencontainers.image.revision`) that touch the image inputs.
