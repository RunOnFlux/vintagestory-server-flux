# vintagestory-server-flux

The Vintage Story dedicated server for [Flux](https://runonflux.io), as
`runonflux/vintagestory-server-flux`. Vintage Story is not on Steam: the server is a direct
download from Anego Studios' CDN (no login, about 51 MB), fetched on the first start for the
version the owner asks for, and run on .NET 10 under a supervisor that:

- downloads and follows the version in `VS_VERSION`: the newest stable one (default), the newest
  patch of a line (`1.22`) or one exact version (`1.22.7`), checked against Anego's MD5. A newer
  release means a restart (players are warned first): a client on a newer version cannot join
- writes `serverconfig.json` from the environment: name, password, player limit, whitelist,
  public server list, and the first world's name, seed, play style and size (`VS_*` below)
- saves on stop (SIGTERM: 1.6-1.7 s for `docker stop` measured, also during world generation;
  Flux allows 10)
- restarts the server in place when it dies, with a restart budget
- makes a backup of the world, the players' data and `serverconfig.json` on start (when the
  world changed) and every 6 hours, through the game's own `/genbackup` while it runs; restores
  one on the next start, keeping the world it replaces
- installs mods from the official ModDB (mods.vintagestory.at) with their dependencies (the game's
  own `/moddb install` fails on 1.22.7), turns them on and off, and finds updates
- makes the admins `VS_ADMINS` names
- gives the hub a console and JSON: status (players, calendar, figures), players (roles,
  whitelist, bans), the world's settings with the game's own catalog, new worlds and saves, mods,
  backups and versions

## Volumes

| Path | Flux mount | What |
|---|---|---|
| `/mnt/vs/data` | `g:` (synced) | the server's `--dataPath`: `Saves/` (the worlds), `serverconfig.json`, `Playerdata/` (roles, whitelist, bans), `Mods/`, and `flux/` (state, backups, the world catalog, disabled mods) |
| `/mnt/vs/server` | `ml:server` (local) | the game, one folder per version under `versions/` (about 330 MB each; the running one and the one before are kept), `current` pointing at the running one, and the game's logs (`logs/`). Downloaded again on a new node Also `flux-logs/flux.log` (our supervisor log, 5 MB then one `.1`): local, never synced. |

Spec: `g:/mnt/vs/data|ml:server:/mnt/vs/server`.

## Ports

One port, TCP and UDP: `VS_PORT` (default 42420). The server announces its port to the public
server list, so the public port and the container port should be the same number.

## Environment

A variable that is **set** (even to `""`) is written on every start; one that is not set leaves
the file's own value.

| Variable | serverconfig.json | |
|---|---|---|
| `VS_NAME` | `ServerName` | the name in the server list and the multiplayer screen |
| `VS_PASSWORD` | `Password` | empty = no password |
| `VS_MAX_CLIENTS` | `MaxClients` | 1-256 |
| `VS_WHITELIST` | `WhitelistMode` | `true` = only whitelisted players join (On), `false` = Off. **Unset, a server still on the game's `Default` is set to Off**: Default means ON on a dedicated server (the game's API docs), so a server whose owner never chose would turn everyone away |
| `VS_ADVERTISE` | `AdvertiseServer` | `true` lists the server on the public server list (off by default since 1.20) |
| `VS_PORT` | `Port` | |
| `VS_ADMINS` | `Playerdata/playerdata.json` | player names, comma separated: `/op` once the server is up (the game looks each up on its auth server) |
| `VS_MODS` | `Mods/` | ModDB ids or links, comma separated (`carryon`, `primitivesurvival@3.9.1`): installed on start when missing, with their dependencies |
| `VS_VERSION` | the install | `latest` (default), `1.22`, `1.22.7`. Going BACK to an older version is refused (a world saved by a newer game may not load in an older one) unless `VS_ALLOW_DOWNGRADE=true` |
| `VS_WORLD_NAME`, `VS_SEED`, `VS_PLAYSTYLE`, `VS_WORLD_SIZE` | `WorldConfig` | **the first world only** (a save that does not exist yet). Play styles: `surviveandbuild` (standard), `exploration`, `wildernesssurvival`, `homosapiens`, `creativebuilding`. Sizes: the game's `worldWidth` values, `1024000` by default |

Supervisor knobs: `FLUX_BACKUP_HOURS` (6), `FLUX_BACKUP_KEEP` (8; a `before-restore` backup is
never rotated away), `FLUX_UPDATE_CHECK_MINUTES` (30, 0 = off), `FLUX_UPDATE_GRACE_MINUTES` (5),
`FLUX_RESTART_MAX_ATTEMPTS` (5 in `FLUX_RESTART_WINDOW` 3600 s, then exit 42),
`FLUX_INSTALL_RETRY_MINUTES` (5).

Always set, whatever the file says: `Ip` (all), `Upnp` off, `ModPaths` (the game's and
`/mnt/vs/data/Mods`), the save inside `/mnt/vs/data/Saves`.

## For the hub

Commands, run through appexec. Each prints one JSON document (`{"error": "..."}` and exit 1 on a
refusal).

| Command | |
|---|---|
| `flux-console <command>` | one command into the server's console (a slash is added when missing); prints the answer, read from the game's log. One at a time, under a lock |
| `flux-vs status` | `state` (`updating`, `starting`, `running`, `restarting`, `crashed`, `stopped`, `install_failed`) and `message`, `version`, `players` (`name`, `id`, `address`, `ping`), `max_players`, `name`, `whitelist`, `advertised`, `password` (a boolean), `world` (`name`, `seed`, `playstyle`, `file`, `date`), `uptime`, `memory_mb`, `tps`, `chunks` |
| `flux-vs players` | every player in the game's files: `role`, `whitelisted`, `banned` (until, reason, by), first/last join, online and ping; the roles of `serverconfig.json` and the default role |
| `flux-vs world` | the running world's settings (asked through `/worldconfig`), the catalog (every play style and setting with the game's English names, read from the base mods of the installed version), the world's name, seed, size |
| `flux-vs set-world '<json>'` | `{"temporalStorms": "rare"}` through `/worldconfig`; applied on restart. Creation-only settings are refused |
| `flux-vs worlds` / `new-world '<json>'` / `use-world <file>` / `delete-world <file>` | the save files; a new world (`name`, `seed`, `playstyle`, `size`, `config`) or a switch on the next start, the old save kept |
| `flux-vs mods` / `mod-install <id> [version]` / `mod-remove` / `mod-enable` / `mod-disable <modid>` / `mod-updates` / `mod-update <modid\|all>` | the ModDB mods |
| `flux-vs backups` / `backup-now` / `restore <file>` / `delete-backup <file>` | the backups in `flux/backups` |
| `flux-vs versions` | the stable versions, the installed one, `VS_VERSION` and what it resolves to |
| `flux-vs restart` | a restart marker the supervisor acts on within a second; prints the generation to wait past |

## Measured (Vintage Story 1.22.7, .NET 10.0.13, 2026-10-18)

- first start 33 s including the 51 MB download; later starts 20-28 s to "Dedicated Server now running"
- about 1.0 GB RAM idle on a new world (`docker stats` 0.8-1.1 GiB, the game's `/stats` 1.0-1.2 GB), ~1-2 cores while generating terrain, ~4-6 % of a core idle
- `docker stop` 1.6-1.7 s (SIGTERM saves), also in the middle of world generation
- install 323 MB; a new world 11 MB, 60 MB after 1,089 chunk columns were generated
- Anego's own guidance: 1 GB + 300 MB per player, 4 threads

## Versions

- 1.0.0 (2026-10-18): first release.
- 1.0.1 (2026-10-20): `flux.log` moved off the synced data volume to `/mnt/vs/server/flux-logs/flux.log` (5 MB, one `.1`; an old one is moved there once); start-up banner; the weekly rebuild only when it has something to apply.

## Development

```bash
./tests/test-flux.sh
docker build -t vintagestory-server-flux:local .
./tests/test-image.sh
docker run --rm -v "$PWD:/mnt" -w /mnt koalaman/shellcheck:stable -x scripts/*.sh tests/*.sh tests/image/*.sh .github/ci/*.sh
```
