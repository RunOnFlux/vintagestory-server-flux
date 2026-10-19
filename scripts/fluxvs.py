#!/usr/bin/env python3
"""flux-vs: every decision about the Vintage Story server's files, and the hub's commands.

The supervisor (flux-entrypoint.sh) calls the internal commands; the hub calls the others through
appexec, and each of those prints ONE JSON document on stdout (errors too: {"error": "..."} with a
non-zero exit).

  internal (the supervisor)
    install <request>       resolve VS_VERSION against Anego's release index, download and unpack
                            that server if it is not installed yet; prints the version
    apply                   write serverconfig.json from the environment and the panel's pending
                            files (a new world, a world to switch to, a backup to restore)
    ensure-mods             install the ModDB mods VS_MODS lists that are not installed yet
    admins                  /op the players VS_ADMINS names who are not admins yet (server running)
    backup <why>            a backup of the world, the players' data and serverconfig.json
    catalog                 the world settings of the installed game version, as the game ships them

  the hub
    status                  state, version, players online, the world's date, server figures
    online                  the players online only (one /list clients, cached 15 s)
    players                 every player the server knows: role, whitelist, ban, last seen, online
    world                   the world's settings (current values from the running server) and the
                            catalog; the world's name, seed, play style and size
    set-world '<json>'      {"key": "value"} through the game's own /worldconfig; applied on restart
    worlds                  the save files, which one runs
    new-world '<json>'      {"name", "seed", "playstyle", "size", "config": {...}} for the next start
    use-world <file>        switch to another save file on the next start
    delete-world <file>     a save file that is not the one running
    mods                    the installed mods, on and off, with what their modinfo says
    mod-install <id> [ver]  from mods.vintagestory.at, with its dependencies
    mod-remove|mod-enable|mod-disable <modid>
    mod-updates             installed mods with a newer release for this game version
    mod-update <modid|all>
    backups                 the image's backups
    backup-now              one now (through the game's /genbackup when it is running)
    restore <file>          that backup, on the next start (the current world is backed up first)
    delete-backup <file>
    versions                the stable versions, the one installed and the one asked for
    restart                 a planned restart (players are warned, the world is saved)

Written for runonflux/vintagestory-server-flux; measured against Vintage Story 1.22.7.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

SERVER_DIR = os.environ.get("FLUX_SERVER_DIR", "/mnt/vs/server")
DATA_DIR = os.environ.get("FLUX_DATA_DIR", "/mnt/vs/data")
FLUX_DIR = os.environ.get("FLUX_DIR", os.path.join(DATA_DIR, "flux"))
STATE_FILE = os.environ.get("FLUX_STATE_FILE", os.path.join(FLUX_DIR, "state.json"))
BACKUP_DIR = os.environ.get("FLUX_BACKUP_DIR", os.path.join(FLUX_DIR, "backups"))
GAME_LOG_DIR = os.environ.get("FLUX_GAME_LOG_DIR", os.path.join(SERVER_DIR, "logs"))
VERSIONS_URL = os.environ.get("FLUX_VERSIONS_URL", "https://api.vintagestory.at/stable.json")
MODDB_API = os.environ.get("FLUX_MODDB_API", "https://mods.vintagestory.at/api")
RESTART_MARKER = os.environ.get("FLUX_RESTART_MARKER", "/tmp/flux-restart-requested")
CONSOLE = os.environ.get("FLUX_CONSOLE_BIN", "flux-console")

CONFIG = os.path.join(DATA_DIR, "serverconfig.json")
SAVES = os.path.join(DATA_DIR, "Saves")
MODS = os.path.join(DATA_DIR, "Mods")
MODS_OFF = os.path.join(FLUX_DIR, "mods-disabled")
PLAYERDATA = os.path.join(DATA_DIR, "Playerdata")
GAME_BACKUPS = os.path.join(DATA_DIR, "Backups")
NEW_WORLD = os.path.join(FLUX_DIR, "new-world.json")
USE_WORLD = os.path.join(FLUX_DIR, "use-world.json")
RESTORE = os.path.join(FLUX_DIR, "restore.json")

DEFAULT_PORT = 42420
# The game's own ids for the base mods: never ModDB dependencies to install.
BASE_MODS = {"game", "survival", "creative", "essentials"}
# WhitelistMode in serverconfig.json (EnumWhitelistMode: Default, Off, On). Default means ON on a
# dedicated server (the API's own documentation), so a server nobody was told about turns
# everyone away. The image always writes Off or On.
WHITELIST_OFF, WHITELIST_ON = 1, 2

# The play styles the Create World screen offers, as VS_PLAYSTYLE takes them. The worldConfig
# each brings comes from the game itself (catalog), not from here.
PLAYSTYLE_ALIASES = {
    "standard": "surviveandbuild", "survival": "surviveandbuild", "surviveandbuild": "surviveandbuild",
    "exploration": "exploration", "wilderness": "wildernesssurvival", "wildernesssurvival": "wildernesssurvival",
    "homosapiens": "homosapiens", "creative": "creativebuilding", "creativebuilding": "creativebuilding",
}

# World sizes VS_WORLD_SIZE and the panel take, in blocks per side (the game's own worldWidth
# values; 1024000 is its default).
WORLD_SIZES = ["8192000", "4096000", "2048000", "1024000", "600000", "512000", "384000", "256000",
               "102400", "51200", "25600", "10240"]


class Fail(Exception):
    """An error the hub shows as it is."""


# ----------------------------------------------------------------------------------------------
# Small helpers.
# ----------------------------------------------------------------------------------------------

def out(data, code=0):
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    sys.exit(code)


def log(msg):
    """A line for the supervisor's log (stderr: stdout is the JSON or the value it reads)."""
    sys.stderr.write(msg + "\n")


def env(name):
    """The variable as set, or None when unset. The marketplace writes a blank declared parameter
    as NAME=, which is a value ("" = clear it), not unset."""
    return os.environ.get(name)


def truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def lenient_json(text):
    """JSON as mod authors write it: // and /* */ comments, trailing commas, a BOM. Strings are
    left alone (a URL's // is not a comment)."""
    text = text.lstrip("\ufeff")
    res, i, n, in_str, esc = [], 0, len(text), False, False
    while i < n:
        c = text[i]
        if in_str:
            res.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            res.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            res.append(c)
            i += 1
    cleaned = re.sub(r",(\s*[}\]])", r"\1", "".join(res))
    return json.loads(cleaned)


def lower_keys(d):
    return {str(k).lower(): v for k, v in (d or {}).items()} if isinstance(d, dict) else {}


def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "runonflux-vintagestory-server-flux"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_json(url, timeout=60):
    return json.loads(http_get(url, timeout).decode("utf-8"))


def version_key(v):
    """1.22.7 > 1.22.0 > 1.22.0-rc.8 > 1.22.0-pre.2: a release sorts above its candidates."""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-(rc|pre)\.(\d+))?$", str(v))
    if not m:
        return (-1,)
    a, b, c, kind, n = m.groups()
    rank = {"pre": 0, "rc": 1, None: 2}[kind]
    return (int(a), int(b), int(c), rank, int(n or 0))


def minor_of(v):
    m = re.match(r"^(\d+\.\d+)", str(v))
    return m.group(1) if m else ""


def safe_name(name, what="name"):
    """A file name the panel passed: no path, no surprises."""
    name = str(name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ ()+-]{0,150}", name) or ".." in name:
        raise Fail(f"Not a valid {what}: {name!r}")
    return name


# ----------------------------------------------------------------------------------------------
# Versions and the install.
# ----------------------------------------------------------------------------------------------

def installed_version():
    """The version the `current` link points at, or None."""
    link = os.path.join(SERVER_DIR, "current")
    try:
        return os.path.basename(os.readlink(link))
    except OSError:
        return None


def release_index():
    """Anego's stable releases: {version: {"url", "md5", "size"}} for the Linux server."""
    data = http_json(VERSIONS_URL)
    res = {}
    for ver, files in (data or {}).items():
        srv = (files or {}).get("linuxserver") or {}
        url = (srv.get("urls") or {}).get("cdn")
        if url and re.match(r"^\d+\.\d+\.\d+", ver):
            res[ver] = {"url": url, "md5": srv.get("md5"), "size": srv.get("filesize")}
    return res


def pick_version(request, versions):
    """The version to run for VS_VERSION: latest, a minor line (newest of it) or exact."""
    req = str(request or "").strip().lower().lstrip("v")
    ordered = sorted(versions, key=version_key, reverse=True)
    finals = [v for v in ordered if re.fullmatch(r"\d+\.\d+\.\d+", v)]
    if req in ("", "latest", "stable"):
        return finals[0] if finals else None
    if re.fullmatch(r"\d+\.\d+", req):
        line = [v for v in finals if minor_of(v) == req]
        return line[0] if line else None
    return req if req in versions else None


def cmd_install(args):
    request = args[0] if args else (env("VS_VERSION") or "latest")
    have = installed_version()
    try:
        index = release_index()
    except Exception as e:  # noqa: BLE001  any network failure: start what is installed
        if have and os.path.exists(os.path.join(SERVER_DIR, "versions", have, "VintagestoryServer.dll")):
            log(f"WARN the release index did not answer ({e}); starting the installed {have}")
            print(have)
            return 0
        raise Fail(f"Could not reach Anego's release index ({e}) and no server is installed yet")
    want = pick_version(request, index)
    if not want:
        if have:
            log(f"WARN VS_VERSION={request!r} matches no stable release; keeping the installed {have}")
            print(have)
            return 0
        raise Fail(f"VS_VERSION={request!r} matches no stable release of Vintage Story")
    if have and have != want and version_key(want) < version_key(have) and not truthy(env("VS_ALLOW_DOWNGRADE")):
        log(f"WARN VS_VERSION asks for {want}, older than the installed {have}: a world saved by a newer game may not load "
            f"in an older one. Keeping {have}; set VS_ALLOW_DOWNGRADE=true to go back anyway")
        print(have)
        return 0
    target = os.path.join(SERVER_DIR, "versions", want)
    if not os.path.exists(os.path.join(target, "VintagestoryServer.dll")):
        info = index[want]
        log(f"downloading Vintage Story {want} ({info.get('size') or '?'}) from {info['url']}")
        os.makedirs(os.path.join(SERVER_DIR, "versions"), exist_ok=True)
        tmpdir = tempfile.mkdtemp(prefix=".dl-", dir=os.path.join(SERVER_DIR, "versions"))
        try:
            archive = os.path.join(tmpdir, "server.tar.gz")
            md5 = hashlib.md5()
            req = urllib.request.Request(info["url"], headers={"User-Agent": "runonflux-vintagestory-server-flux"})
            with urllib.request.urlopen(req, timeout=120) as r, open(archive, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    md5.update(chunk)
                    f.write(chunk)
            if info.get("md5") and md5.hexdigest() != info["md5"]:
                raise Fail(f"The download of {want} is damaged (MD5 {md5.hexdigest()}, expected {info['md5']})")
            unpack = os.path.join(tmpdir, "x")
            os.makedirs(unpack)
            with tarfile.open(archive) as t:
                t.extractall(unpack, filter="data")
            if not os.path.exists(os.path.join(unpack, "VintagestoryServer.dll")):
                raise Fail(f"The download of {want} has no VintagestoryServer.dll")
            if os.path.exists(target):
                shutil.rmtree(target)
            os.replace(unpack, target)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        log(f"installed Vintage Story {want}")
    link = os.path.join(SERVER_DIR, "current")
    tmp_link = link + ".new"
    if os.path.lexists(tmp_link):
        os.remove(tmp_link)
    os.symlink(os.path.join("versions", want), tmp_link)
    os.replace(tmp_link, link)
    # Keep this version and the one before it (a quick way back), nothing older.
    keep = {want, have}
    for name in os.listdir(os.path.join(SERVER_DIR, "versions")):
        if name not in keep and not name.startswith(".dl-"):
            shutil.rmtree(os.path.join(SERVER_DIR, "versions", name), ignore_errors=True)
    write_json(os.path.join(FLUX_DIR, "version.json"), {"version": want, "request": request, "checked": int(time.time())})
    print(want)
    return 0


def cmd_latest(args):
    """For the update watcher: the version VS_VERSION resolves to now, or nothing."""
    request = args[0] if args else (env("VS_VERSION") or "latest")
    try:
        want = pick_version(request, release_index())
    except Exception:  # noqa: BLE001
        return 0
    if want:
        print(want)
    return 0


def cmd_versions(_args):
    request = env("VS_VERSION") or "latest"
    try:
        index = release_index()
        versions = sorted(index, key=version_key, reverse=True)
    except Exception as e:  # noqa: BLE001
        raise Fail(f"Anego's release index did not answer: {e}")
    out({"installed": installed_version(), "request": request, "resolves_to": pick_version(request, index),
         "versions": [v for v in versions if re.fullmatch(r"\d+\.\d+\.\d+", v)][:40]})


# ----------------------------------------------------------------------------------------------
# serverconfig.json.
# ----------------------------------------------------------------------------------------------

def current_save_of(cfg):
    loc = ((cfg or {}).get("WorldConfig") or {}).get("SaveFileLocation") or ""
    return os.path.basename(loc) or "default.vcdbs"


def current_save():
    return current_save_of(read_json(CONFIG, {}) or {})


def world_slug(name):
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(name or "")).strip("-").lower()
    return (s or "world")[:40]


def playstyle_fields(code, catalog):
    """PlayStyle, PlayStyleLangCode and WorldType for a play style, from the game's own list."""
    for p in (catalog or {}).get("playstyles", []):
        if p.get("code") == code:
            return {"PlayStyle": code, "PlayStyleLangCode": p.get("langCode") or code, "WorldType": p.get("worldType") or "standard"}
    if code == "creativebuilding":
        return {"PlayStyle": code, "PlayStyleLangCode": code, "WorldType": "superflat"}
    return {"PlayStyle": code, "PlayStyleLangCode": f"preset-{code}", "WorldType": "standard"}


WORLDS_META = os.path.join(FLUX_DIR, "worlds.json")


def world_meta(file):
    """What the image knows about a save it created: {name, seed, playstyle, size, created}."""
    return ((read_json(WORLDS_META, {}) or {}).get(file)) or {}


def remember_world(file, wc, size=None):
    meta = read_json(WORLDS_META, {}) or {}
    meta[file] = {"name": wc.get("WorldName"), "seed": wc.get("Seed"), "playstyle": wc.get("PlayStyle"),
                  "size": size, "created": int(time.time())}
    write_json(WORLDS_META, meta)


def point_at_save(cfg, file):
    """serverconfig.json pointed at an EXISTING save. The game reads WorldConfig's seed, play
    style and settings only to create a world (an existing save keeps its own), so the ones left
    from another world are cleared rather than shown as this one's."""
    wc = cfg.setdefault("WorldConfig", {}) or {}
    cfg["WorldConfig"] = wc
    wc["SaveFileLocation"] = os.path.join(SAVES, file)
    wc["Seed"] = None
    wc["WorldConfiguration"] = None
    wc["WorldName"] = re.sub(r"(-\d{8}-\d{6})?\.vcdbs$", "", file)


def apply_new_world(cfg, spec, catalog, now=None):
    """A new world in serverconfig.json: a save file of its own, so the old one stays on disk
    and the World tab can switch back to it."""
    now = now or time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    wc = cfg.setdefault("WorldConfig", {}) or {}
    cfg["WorldConfig"] = wc
    name = str(spec.get("name") or "").strip()[:60] or "A new world"
    playstyle = PLAYSTYLE_ALIASES.get(str(spec.get("playstyle") or "surviveandbuild").lower(), "surviveandbuild")
    wc["SaveFileLocation"] = os.path.join(SAVES, f"{world_slug(name)}-{now}.vcdbs")
    wc["WorldName"] = name
    seed = str(spec.get("seed") or "").strip()
    wc["Seed"] = seed[:40] if seed else None
    wc.update(playstyle_fields(playstyle, catalog))
    config = {str(k): str(v) for k, v in (spec.get("config") or {}).items()}
    size = str(spec.get("size") or "").strip()
    if size in WORLD_SIZES:
        config["worldWidth"] = size
        config["worldLength"] = size
        cfg["MapSizeX"] = int(size)
        cfg["MapSizeZ"] = int(size)
    wc["WorldConfiguration"] = config or None
    return wc["SaveFileLocation"]


def apply_config(cfg, environ, catalog=None, save_exists=True, new_world=False):
    """serverconfig.json from the environment. A variable that is SET (even empty) is written;
    one that is not set leaves the file's own value. The world the deploy form describes
    (VS_WORLD_NAME, VS_SEED, VS_PLAYSTYLE, VS_WORLD_SIZE) is only written for a save that does not
    exist yet, and never over a new world the panel asked for (new_world). Returns the lines to log."""
    notes = []
    get = environ.get

    port = get("VS_PORT")
    cfg["Port"] = int(port) if port and port.isdigit() else int(cfg.get("Port") or DEFAULT_PORT)
    cfg["Ip"] = None
    cfg["Upnp"] = False
    # The game's mods folder in the install, and the owner's on the data volume.
    cfg["ModPaths"] = ["Mods", MODS]

    if get("VS_NAME") is not None and get("VS_NAME").strip():
        cfg["ServerName"] = get("VS_NAME").strip()[:100]
    if get("VS_PASSWORD") is not None:
        cfg["Password"] = get("VS_PASSWORD") or None
    if get("VS_MAX_CLIENTS") is not None and get("VS_MAX_CLIENTS").strip():
        try:
            cfg["MaxClients"] = max(1, min(256, int(get("VS_MAX_CLIENTS"))))
        except ValueError:
            notes.append(f"WARN VS_MAX_CLIENTS={get('VS_MAX_CLIENTS')!r} is not a number; keeping {cfg.get('MaxClients')}")
    if get("VS_ADVERTISE") is not None:
        cfg["AdvertiseServer"] = truthy(get("VS_ADVERTISE"))
    wl = get("VS_WHITELIST")
    if wl is not None and wl.strip():
        cfg["WhitelistMode"] = WHITELIST_ON if truthy(wl) else WHITELIST_OFF
    elif cfg.get("WhitelistMode") in (0, None):
        # "Default" is ON on a dedicated server: nobody could join a server whose owner never
        # chose. Off, as the deploy form's own default.
        cfg["WhitelistMode"] = WHITELIST_OFF
        notes.append("WhitelistMode was Default (= on for a dedicated server): set to Off; VS_WHITELIST=true turns it on")

    wc = cfg.get("WorldConfig") or {}
    cfg["WorldConfig"] = wc
    if not save_exists and not new_world:
        # The first world: what the deploy form asked for.
        spec = {"name": get("VS_WORLD_NAME") or wc.get("WorldName") or "A new world",
                "seed": get("VS_SEED") or "",
                "playstyle": get("VS_PLAYSTYLE") or wc.get("PlayStyle") or "surviveandbuild",
                "size": get("VS_WORLD_SIZE") or ""}
        loc = wc.get("SaveFileLocation") or ""
        # Keep a location the panel already chose (a switch to an older save); only a missing or
        # foreign one becomes our default name.
        if not loc.startswith(SAVES + "/"):
            loc = os.path.join(SAVES, "default.vcdbs")
        apply_new_world(cfg, spec, catalog)
        cfg["WorldConfig"]["SaveFileLocation"] = loc
        notes.append(f"a new world on this start: {cfg['WorldConfig']['WorldName']!r}, play style "
                     f"{cfg['WorldConfig']['PlayStyle']}, seed {cfg['WorldConfig']['Seed'] or 'random'}")
    elif not str(wc.get("SaveFileLocation") or "").startswith(SAVES + "/"):
        # A config written elsewhere (a different data path): keep the save's name, in our folder.
        name = os.path.basename(str(wc.get("SaveFileLocation") or "")) or "default.vcdbs"
        wc["SaveFileLocation"] = os.path.join(SAVES, name)
    return notes


def ensure_config(cfg_path=CONFIG):
    """serverconfig.json exists: the game's own defaults (--genconfig) the first time."""
    if os.path.exists(cfg_path) and read_json(cfg_path) is not None:
        return
    install = os.path.join(SERVER_DIR, "current")
    if os.path.exists(cfg_path):
        bad = cfg_path + f".broken-{int(time.time())}"
        os.replace(cfg_path, bad)
        log(f"WARN serverconfig.json was not valid JSON: kept as {os.path.basename(bad)}, a new one made")
    subprocess.run(["dotnet", "VintagestoryServer.dll", "--genconfig", "--dataPath", DATA_DIR],
                   cwd=install, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=False)
    if read_json(cfg_path) is None:
        raise Fail("The game could not write its default serverconfig.json")


def cmd_apply(_args):
    os.makedirs(FLUX_DIR, exist_ok=True)
    os.makedirs(SAVES, exist_ok=True)
    os.makedirs(MODS, exist_ok=True)
    apply_restore()
    ensure_config()
    cfg = read_json(CONFIG, {})
    catalog = load_catalog()
    if os.path.exists(USE_WORLD):
        want = (read_json(USE_WORLD, {}) or {}).get("file")
        os.remove(USE_WORLD)
        if want and os.path.exists(os.path.join(SAVES, want)):
            point_at_save(cfg, want)
            log(f"switched to the world {want}")
        else:
            log(f"WARN the world to switch to ({want}) is not in Saves/; keeping {current_save()}")
    new_world = os.path.exists(NEW_WORLD)
    if new_world:
        spec = read_json(NEW_WORLD, {}) or {}
        os.remove(NEW_WORLD)
        loc = apply_new_world(cfg, spec, catalog)
        remember_world(os.path.basename(loc), cfg["WorldConfig"], cfg.get("MapSizeX"))
        log(f"a new world on this start: {os.path.basename(loc)} ({cfg['WorldConfig']['PlayStyle']}, seed "
            f"{cfg['WorldConfig']['Seed'] or 'random'}); the previous one stays in Saves/")
    save = (cfg.get("WorldConfig") or {}).get("SaveFileLocation") or ""
    save_exists = bool(save) and os.path.exists(os.path.join(SAVES, os.path.basename(save)))
    for line in apply_config(cfg, os.environ, catalog, save_exists=save_exists, new_world=new_world):
        log(line)
    if not save_exists and not new_world:
        remember_world(current_save_of(cfg), cfg["WorldConfig"], cfg.get("MapSizeX"))
    write_json(CONFIG, cfg)
    return 0


# ----------------------------------------------------------------------------------------------
# The world settings catalog: the game's own, from the base mods' embedded world config.
# ----------------------------------------------------------------------------------------------

def _embedded_json_objects(blob):
    """The JSON object around every "playstyles" key in a base mod's DLL (the worldconfig
    resource the Create World screen reads)."""
    found = []
    start = 0
    while True:
        k = blob.find(b'"playstyles"', start)
        if k < 0:
            return found
        s = blob.rfind(b"{", 0, k)
        depth, in_str, esc, end = 0, False, False, -1
        for n in range(s, min(len(blob), s + 400000)):
            c = blob[n]
            if in_str:
                if esc:
                    esc = False
                elif c == 0x5C:
                    esc = True
                elif c == 0x22:
                    in_str = False
            elif c == 0x22:
                in_str = True
            elif c == 0x7B:
                depth += 1
            elif c == 0x7D:
                depth -= 1
                if depth == 0:
                    end = n
                    break
        if end > 0:
            try:
                found.append(lenient_json(blob[s:end + 1].decode("utf-8")))
            except ValueError:
                pass
        start = k + 1


def build_catalog(install_dir):
    """{"version", "playstyles": [...], "attributes": [...]} with the English names."""
    lang = {}
    try:
        with open(os.path.join(install_dir, "assets", "game", "lang", "en.json"), encoding="utf-8-sig") as f:
            lang = lenient_json(f.read())
    except (OSError, ValueError):
        pass
    playstyles, attributes = [], []
    for dll in ("VSSurvivalMod.dll", "VSCreativeMod.dll"):
        try:
            with open(os.path.join(install_dir, "Mods", dll), "rb") as f:
                blob = f.read()
        except OSError:
            continue
        for obj in _embedded_json_objects(blob):
            for p in obj.get("playstyles") or []:
                code = p.get("code")
                if not code or any(x["code"] == code for x in playstyles):
                    continue
                lc = p.get("langCode") or code
                playstyles.append({
                    "code": code,
                    "name": lang.get(f"playstyle-{lc}") or code,
                    "description": lang.get(f"playstyle-desc-{lc}") or "",
                    "worldType": p.get("worldType") or "standard",
                    "langCode": lc,
                    "order": p.get("listOrder", 0),
                    "worldConfig": {str(k): str(v) for k, v in (p.get("worldConfig") or {}).items()},
                })
            for a in obj.get("worldConfigAttributes") or []:
                code = a.get("code")
                if not code or any(x["code"] == code for x in attributes):
                    continue
                values = [str(v) for v in a.get("values") or []]
                names = [str(v) for v in a.get("names") or []] or values
                kind = "bool" if a.get("dataType") == "bool" else ("select" if values else str(a.get("dataType") or "string"))
                if kind == "bool":
                    values, names = ["true", "false"], ["On", "Off"]
                attributes.append({
                    "code": code,
                    "category": a.get("category") or "",
                    "categoryName": lang.get(f"worldconfig-category-{a.get('category')}") or a.get("category") or "",
                    "name": lang.get(f"worldattribute-{code}") or code,
                    "description": lang.get(f"worldattribute-{code}-desc") or "",
                    "type": kind,
                    "values": [{"value": v, "label": lang.get(f"worldconfig-{code}-{names[i] if i < len(names) else v}")
                                or (names[i] if i < len(names) else v)} for i, v in enumerate(values)],
                    "default": str(a.get("default")) if a.get("default") is not None else None,
                    "creationOnly": bool(a.get("onlyDuringWorldCreate")),
                })
    playstyles.sort(key=lambda p: p.get("order", 0))
    return {"playstyles": playstyles, "attributes": attributes}


def load_catalog():
    """The catalog of the installed version, cached in flux/ per version."""
    ver = installed_version()
    if not ver:
        return None
    path = os.path.join(FLUX_DIR, "world-catalog.json")
    have = read_json(path)
    if have and have.get("version") == ver and have.get("attributes"):
        return have
    cat = build_catalog(os.path.join(SERVER_DIR, "versions", ver))
    cat["version"] = ver
    if cat["attributes"]:
        write_json(path, cat)
    return cat


def cmd_catalog(_args):
    cat = load_catalog()
    if not cat or not cat.get("attributes"):
        raise Fail("The world settings could not be read from the installed game")
    log(f"world settings catalog of {cat['version']}: {len(cat['playstyles'])} play styles, {len(cat['attributes'])} settings")
    return 0


# ----------------------------------------------------------------------------------------------
# The console, through flux-console (one command at a time, the answer as text).
# ----------------------------------------------------------------------------------------------

CONSOLE_FIFO = os.environ.get("FLUX_CONSOLE_FIFO", "/tmp/flux-console")
CONSOLE_LOCK = os.environ.get("FLUX_CONSOLE_LOCK", "/tmp/flux-console.lock")
LOG_PREFIX = re.compile(r"^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2} \[[A-Za-z ]+\] ?")


def console_many(commands, done=None, timeout=4.0, quiet=0.3):
    """Several console commands, one after the other, under flux-console's lock: each is written
    once the previous one has been answered, and its answer is the game log's lines after its
    "Handling Console Command", until `done(text)` says so or the log is quiet for `quiet` s.
    `done` answers True (complete), False (keep waiting) or None (complete once quiet).
    Returns {command: answer} (None for a command the server did not take), or None when the
    server is not running."""
    import fcntl
    if not os.path.exists(CONSOLE_FIFO) or not running():
        return None
    log_path = os.path.join(GAME_LOG_DIR, "server-main.log")
    answers = {}
    with open(CONSOLE_LOCK, "w") as lock:
        deadline = time.time() + 15
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() > deadline:
                    raise Fail("The console is busy with another command; try again")
                time.sleep(0.1)
        fd = os.open(CONSOLE_FIFO, os.O_WRONLY | os.O_NONBLOCK)
        try:
            for cmd in commands:
                start = os.path.getsize(log_path) if os.path.exists(log_path) else 0
                os.write(fd, (cmd.replace("\n", " ") + "\n").encode("utf-8"))
                t0, last_size, last_change, text = time.time(), -1, time.time(), None
                while time.time() - t0 < timeout:
                    time.sleep(0.05)
                    size = os.path.getsize(log_path) if os.path.exists(log_path) else 0
                    if size < start:
                        start = 0
                    if size != last_size:
                        last_size, last_change = size, time.time()
                    with open(log_path, "rb") as f:
                        f.seek(start)
                        chunk = f.read().decode("utf-8", "replace")
                    i = chunk.find("Handling Console Command")
                    if i < 0:
                        continue
                    nl = chunk.find("\n", i)
                    body = chunk[nl + 1:] if nl >= 0 else ""
                    lines = [LOG_PREFIX.sub("", ln) for ln in body.splitlines()]
                    text = "\n".join(lines)
                    verdict = done(cmd, text) if done else None
                    if verdict:
                        break
                    if verdict is None and body.strip() and time.time() - last_change >= quiet:
                        break
                answers[cmd] = text
        finally:
            os.close(fd)
    return answers


def console(command, wait=None):
    """One command; its answer as text, or None when the server is not running."""
    res = console_many([command], timeout=float(wait or 3))
    return None if res is None else (res.get(command) or "")


def running():
    try:
        return subprocess.run(["pgrep", "-f", "^dotnet VintagestoryServer\\.dll --dataPath "], capture_output=True, check=False).returncode == 0
    except OSError:
        return False


LIST_LINE = re.compile(r"\[(\d+)\]\s+(\S+)\s+(\S+)\s+\((\d+)ms\)(?:\s*\((\d+)s inactive\))?")


def parse_clients(text):
    """/list clients: one line per player, '{4} [{0}] {1} {2} ({3}ms) ({5}s inactive)' in the
    game's string table (1.22.7): [client id] name, address, ping."""
    res = []
    for line in str(text or "").splitlines():
        m = LIST_LINE.search(line)
        if m:
            cid, name, addr, ping, idle = m.groups()
            res.append({"id": int(cid), "name": name, "address": addr, "ping": int(ping),
                        "idle": int(idle) if idle else None})
    return res


def parse_stats(text):
    t = str(text or "")
    res = {}
    m = re.search(r"Version:\s*(\S+)", t)
    if m:
        res["version"] = m.group(1)
    m = re.search(r"Uptime:\s*(\d+) days, (\d+) hours, (\d+) minutes, (\d+) seconds", t)
    if m:
        d, h, mi, s = map(int, m.groups())
        res["uptime"] = ((d * 24 + h) * 60 + mi) * 60 + s
    m = re.search(r"Players online:\s*(\d+)\s*/\s*(\d+)", t)
    if m:
        res["online"], res["max_players"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"Memory usage Managed/Total:\s*([\d.]+)Mb\s*/\s*([\d.]+)\s*Mb", t)
    if m:
        res["memory_mb"] = float(m.group(2))
    m = re.search(r"Last 2s Ticks/s:\s*([\d.]+)", t)
    if m:
        res["tps"] = float(m.group(1))
    m = re.search(r"Loaded chunks:\s*(\d+)", t)
    if m:
        res["chunks"] = int(m.group(1))
    return res


def parse_time(text):
    """'Server time: 1. May, Year 0, 08:00' and 'Game Speed: 48 IRL minutes'."""
    t = str(text or "")
    res = {}
    m = re.search(r"Server time:\s*(\d+)\.\s*(\w+),\s*Year\s*(\d+),\s*(\d{1,2}:\d{2})", t)
    if m:
        res = {"day": int(m.group(1)), "month": m.group(2), "year": int(m.group(3)), "time": m.group(4),
               "text": f"{m.group(1)}. {m.group(2)}, Year {m.group(3)}, {m.group(4)}"}
    return res


def status_answered(cmd, text):
    """Whether a status command's answer is complete: /stats ends with the UDP line, /time with
    the game speed; /list clients has no last line, so it waits for the quiet (None)."""
    if cmd == "/stats":
        return "Network UDP" in text
    if cmd == "/time":
        return "Game Speed" in text
    if cmd == "/info seed":
        return bool(text.strip())
    return None


def cmd_status(_args):
    state = read_json(STATE_FILE, {}) or {}
    cfg = read_json(CONFIG, {}) or {}
    wc = cfg.get("WorldConfig") or {}
    res = {
        "state": state.get("state") or ("running" if running() else "stopped"),
        "message": state.get("message"),
        # The supervisor's generation (counted up at every start) and when it began: a restart is
        # over when these move and the state is running again.
        "generation": state.get("generation"),
        "since": state.get("since"),
        "version": installed_version(),
        "image": os.environ.get("FLUX_IMAGE_VERSION", "dev"),
        "name": cfg.get("ServerName"),
        "max_players": cfg.get("MaxClients"),
        "whitelist": cfg.get("WhitelistMode") == WHITELIST_ON,
        "advertised": bool(cfg.get("AdvertiseServer")),
        "password": bool(cfg.get("Password")),
        "world": {"name": wc.get("WorldName"), "seed": wc.get("Seed"), "playstyle": world_meta(current_save()).get("playstyle"),
                  "file": current_save()},
        "players": [],
        "answered": False,
    }
    if res["state"] == "running" and running():
        answers = console_many(["/stats", "/list clients", "/time", "/info seed"], done=status_answered)
        stats = (answers or {}).get("/stats")
        if stats:
            st = parse_stats(stats)
            res["answered"] = True
            res.update({k: v for k, v in st.items() if k in ("uptime", "memory_mb", "tps", "chunks")})
            if st.get("max_players"):
                res["max_players"] = st["max_players"]
            res["players"] = parse_clients(answers.get("/list clients"))
            res["world"]["date"] = parse_time(answers.get("/time")) or None
            seed = (answers.get("/info seed") or "").strip().splitlines()
            if seed and re.fullmatch(r"-?\d+", seed[0].strip()):
                res["world"]["seed"] = seed[0].strip()
    out(res)


ONLINE_CACHE = os.environ.get("FLUX_ONLINE_CACHE", "/tmp/flux-online.json")


def cmd_online(_args):
    """The players online, for the dashboard's 30-second player count: one /list clients, shared
    for 15 s between every caller (the panel and the dashboard both poll)."""
    if (read_json(STATE_FILE, {}) or {}).get("state") != "running" or not running():
        out({"answered": False, "players": []})
    hit = read_json(ONLINE_CACHE)
    if hit and time.time() - hit.get("at", 0) < 15:
        out(hit["data"])
    reply = console("/list clients")
    data = {"answered": reply is not None and "List of online Players" in reply, "players": parse_clients(reply)}
    if data["answered"]:
        write_json(ONLINE_CACHE, {"at": time.time(), "data": data})
    out(data)


# ----------------------------------------------------------------------------------------------
# Players: the game's own files, which it writes as soon as a command changes them (measured).
# ----------------------------------------------------------------------------------------------

def cmd_players(_args):
    data = read_json(os.path.join(PLAYERDATA, "playerdata.json"), []) or []
    white = read_json(os.path.join(PLAYERDATA, "playerswhitelisted.json"), []) or []
    banned = read_json(os.path.join(PLAYERDATA, "playersbanned.json"), []) or []
    cfg = read_json(CONFIG, {}) or {}
    roles = [{"code": r.get("Code"), "name": r.get("Name"), "level": r.get("PrivilegeLevel"), "description": r.get("Description")}
             for r in cfg.get("Roles") or [] if r.get("Code")]
    online = parse_clients(console("/list clients")) if running() else []
    players = {}

    def entry(uid, name):
        key = uid or f"name:{(name or '').lower()}"
        p = players.setdefault(key, {"uid": uid, "name": name, "role": None, "whitelisted": None, "banned": None,
                                     "firstJoin": None, "lastJoin": None, "online": False})
        if name and not p["name"]:
            p["name"] = name
        return p

    for d in data:
        p = entry(d.get("PlayerUID"), d.get("LastKnownPlayername"))
        p["role"] = d.get("RoleCode")
        p["firstJoin"] = d.get("FirstJoinDate")
        p["lastJoin"] = d.get("LastJoinDate")
    for w in white:
        p = entry(w.get("PlayerUID"), w.get("PlayerName"))
        p["whitelisted"] = {"until": w.get("UntilDate"), "reason": w.get("Reason"), "by": w.get("IssuedByPlayerName")}
    for b in banned:
        p = entry(b.get("PlayerUID"), b.get("PlayerName"))
        p["banned"] = {"until": b.get("UntilDate"), "reason": b.get("Reason"), "by": b.get("IssuedByPlayerName")}
    names = {}
    for p in players.values():
        if p["name"]:
            names[p["name"].lower()] = p
    for o in online:
        p = names.get(o["name"].lower()) or entry(None, o["name"])
        p["online"] = True
        p["ping"] = o["ping"]
    out({"players": sorted(players.values(), key=lambda p: (not p["online"], (p["name"] or "").lower())),
         "roles": roles, "defaultRole": cfg.get("DefaultRoleCode"),
         "whitelist": cfg.get("WhitelistMode") == WHITELIST_ON, "running": running()})


def cmd_admins(_args):
    """VS_ADMINS: names made admins once the server is up (the game looks each name up on its auth
    server). A name already admin in playerdata.json is skipped."""
    names = [n for n in re.split(r"[\s,;]+", env("VS_ADMINS") or "") if n]
    if not names:
        return 0
    data = read_json(os.path.join(PLAYERDATA, "playerdata.json"), []) or []
    admins = {str(d.get("LastKnownPlayername") or "").lower() for d in data if d.get("RoleCode") == "admin"}
    for n in names:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{2,32}", n):
            log(f"WARN VS_ADMINS: {n!r} is not a Vintage Story player name; skipped")
            continue
        if n.lower() in admins:
            continue
        reply = console(f"/op {n}", wait=5) or ""
        if "assigned" in reply:
            log(f"VS_ADMINS: {n} is now an admin")
        else:
            log(f"WARN VS_ADMINS: /op {n} answered: {reply.strip()[:200] or 'nothing'}")
    return 0


# ----------------------------------------------------------------------------------------------
# The world.
# ----------------------------------------------------------------------------------------------

def world_values(catalog):
    """The running world's settings, asked one by one through /worldconfig (the save holds them;
    the game answers 'X currently has value: Y'), in one pass under the console lock."""
    codes = [a["code"] for a in catalog.get("attributes", [])]
    cmds = [f"/worldconfig {c}" for c in codes]
    answers = console_many(cmds, done=lambda cmd, text: bool(text.strip()), timeout=2) or {}
    kinds = {a["code"]: a.get("type") for a in catalog.get("attributes", [])}
    values = {}
    for c in codes:
        m = re.search(re.escape(c) + r" currently has value:\s*(.*)", answers.get(f"/worldconfig {c}") or "")
        if m:
            v = m.group(1).strip()
            # The game prints a bool as .NET does ("True"); the catalog's values are "true"/"false".
            values[c] = v.lower() if kinds.get(c) == "bool" else v
    return values


def cmd_world(_args):
    catalog = load_catalog() or {"playstyles": [], "attributes": []}
    cfg = read_json(CONFIG, {}) or {}
    wc = cfg.get("WorldConfig") or {}
    live = running() and (read_json(STATE_FILE, {}) or {}).get("state") == "running"
    values = world_values(catalog) if live else {}
    seed = wc.get("Seed")
    if live:
        reply = (console("/info seed") or "").strip()
        if re.fullmatch(r"-?\d+", reply):
            seed = reply
    out({"world": {"name": wc.get("WorldName"), "seed": seed, "playstyle": world_meta(current_save()).get("playstyle"),
                   "file": current_save(), "size": cfg.get("MapSizeX")},
         "running": live, "values": values, "catalog": catalog, "sizes": WORLD_SIZES,
         "pendingNewWorld": read_json(NEW_WORLD), "pendingSwitch": read_json(USE_WORLD)})


def check_world_values(values, catalog, creating=False):
    """{"code": "value"} checked against the catalog. Returns the clean dict or raises."""
    attrs = {a["code"]: a for a in catalog.get("attributes", [])}
    clean = {}
    for code, value in (values or {}).items():
        a = attrs.get(code)
        if not a:
            raise Fail(f"Unknown world setting: {code}")
        v = str(value).strip()
        if a["type"] in ("select", "bool") and v not in [x["value"] for x in a["values"]]:
            raise Fail(f"{a['name']}: {v!r} is not one of its values")
        if a["type"] not in ("select", "bool") and not re.fullmatch(r"[A-Za-z0-9._ -]{0,40}", v):
            raise Fail(f"{a['name']}: {v!r} is not a valid value")
        if a.get("creationOnly") and not creating:
            raise Fail(f"{a['name']} only applies when a world is created")
        clean[code] = v
    return clean


def cmd_set_world(args):
    if not args:
        raise Fail("usage: set-world '{\"key\": \"value\"}'")
    try:
        values = json.loads(args[0])
    except ValueError as e:
        raise Fail(f"Not JSON: {e}")
    if not isinstance(values, dict) or not values:
        raise Fail("Nothing to change")
    catalog = load_catalog() or {}
    clean = check_world_values(values, catalog)
    if not running():
        raise Fail("The server is not running: start it first, the world's settings are kept in the world itself")
    done, failed = [], []
    for code, v in clean.items():
        reply = console(f"/worldconfig {code} {v}", wait=2) or ""
        (done if re.search(r"\bset\b", reply, re.I) else failed).append({"code": code, "value": v, "reply": reply.strip()[:200]})
    out({"ok": not failed, "set": done, "failed": failed, "restartNeeded": bool(done)}, 0 if not failed else 1)


def list_saves():
    cur = current_save()
    res = []
    try:
        names = os.listdir(SAVES)
    except OSError:
        names = []
    for n in sorted(names):
        if n.endswith(".vcdbs"):
            st = os.stat(os.path.join(SAVES, n))
            m = world_meta(n)
            res.append({"file": n, "size": st.st_size, "modified": int(st.st_mtime), "current": n == cur,
                        "name": m.get("name"), "seed": m.get("seed"), "playstyle": m.get("playstyle")})
    return res


def cmd_worlds(_args):
    out({"worlds": list_saves(), "current": current_save(), "pendingNewWorld": read_json(NEW_WORLD), "pendingSwitch": read_json(USE_WORLD)})


def cmd_new_world(args):
    if not args:
        raise Fail("usage: new-world '{\"name\":..., \"seed\":..., \"playstyle\":..., \"size\":..., \"config\": {...}}'")
    try:
        spec = json.loads(args[0])
    except ValueError as e:
        raise Fail(f"Not JSON: {e}")
    if not isinstance(spec, dict):
        raise Fail("Not a world description")
    catalog = load_catalog() or {}
    ps = str(spec.get("playstyle") or "surviveandbuild").lower()
    if ps not in PLAYSTYLE_ALIASES:
        raise Fail(f"Unknown play style: {ps}")
    size = str(spec.get("size") or "")
    if size and size not in WORLD_SIZES:
        raise Fail(f"Not a world size the game offers: {size}")
    seed = str(spec.get("seed") or "").strip()
    if seed and not re.fullmatch(r"-?[A-Za-z0-9]{1,40}", seed):
        raise Fail("A seed is a number or a word, letters and digits only")
    clean = {
        "name": str(spec.get("name") or "A new world").strip()[:60],
        "seed": seed,
        "playstyle": PLAYSTYLE_ALIASES[ps],
        "size": size,
        "config": check_world_values(spec.get("config") or {}, catalog, creating=True),
    }
    write_json(NEW_WORLD, clean)
    if os.path.exists(USE_WORLD):
        os.remove(USE_WORLD)
    out({"ok": True, "pending": clean, "restartNeeded": True})


def cmd_use_world(args):
    name = safe_name(args[0] if args else "", "world file")
    if not name.endswith(".vcdbs") or not os.path.exists(os.path.join(SAVES, name)):
        raise Fail(f"No such world in Saves/: {name}")
    write_json(USE_WORLD, {"file": name})
    if os.path.exists(NEW_WORLD):
        os.remove(NEW_WORLD)
    out({"ok": True, "pending": name, "restartNeeded": True})


def cmd_delete_world(args):
    name = safe_name(args[0] if args else "", "world file")
    if not name.endswith(".vcdbs") or not os.path.exists(os.path.join(SAVES, name)):
        raise Fail(f"No such world in Saves/: {name}")
    if name == current_save():
        raise Fail("That is the world the server runs: switch to another one first")
    for suffix in ("", "-wal", "-shm"):
        p = os.path.join(SAVES, name + suffix)
        if os.path.exists(p):
            os.remove(p)
    meta = read_json(WORLDS_META, {}) or {}
    if meta.pop(name, None) is not None:
        write_json(WORLDS_META, meta)
    out({"ok": True, "worlds": list_saves()})


# ----------------------------------------------------------------------------------------------
# Mods: the official ModDB (mods.vintagestory.at). The game's own /moddb install fails on 1.22.7
# (it parses the ModDB's game version ids as 32-bit integers; they are not), so the image
# installs the zip itself. Clients download a server's mods from the ModDB when they join.
# ----------------------------------------------------------------------------------------------

def read_modinfo(path):
    """modinfo.json of a mod zip or folder, keys lower-cased; {} when there is none."""
    try:
        if os.path.isdir(path):
            with open(os.path.join(path, "modinfo.json"), encoding="utf-8-sig") as f:
                return lower_keys(lenient_json(f.read()))
        with zipfile.ZipFile(path) as z:
            names = {n.lower(): n for n in z.namelist()}
            n = names.get("modinfo.json")
            if not n:
                return {}
            return lower_keys(lenient_json(z.read(n).decode("utf-8-sig", "replace")))
    except (OSError, ValueError, zipfile.BadZipFile, KeyError):
        return {}


def mod_entry(path, enabled):
    info = read_modinfo(path)
    base = os.path.basename(path)
    guess = re.match(r"^([A-Za-z0-9_]+?)[-_ ]?v?\d", base)
    deps = info.get("dependencies") if isinstance(info.get("dependencies"), dict) else {}
    st = os.stat(path)
    return {
        "file": base,
        "modid": str(info.get("modid") or (guess.group(1).lower() if guess else os.path.splitext(base)[0].lower())),
        "name": info.get("name") or os.path.splitext(base)[0],
        "version": str(info.get("version") or ""),
        "side": str(info.get("side") or "").lower() or None,
        "type": str(info.get("type") or "").lower() or None,
        "description": str(info.get("description") or "")[:300],
        "authors": info.get("authors") if isinstance(info.get("authors"), list) else None,
        "dependencies": {str(k): str(v) for k, v in deps.items() if str(k).lower() not in BASE_MODS},
        "enabled": enabled,
        "size": st.st_size,
        "modified": int(st.st_mtime),
        "hasModinfo": bool(info),
    }


def installed_mods():
    res = []
    for folder, enabled in ((MODS, True), (MODS_OFF, False)):
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for n in names:
            p = os.path.join(folder, n)
            if n.startswith(".") or not (n.lower().endswith((".zip", ".cs", ".dll")) or os.path.isdir(p)):
                continue
            res.append(mod_entry(p, enabled))
    return res


def cmd_mods(_args):
    out({"mods": installed_mods(), "gameVersion": installed_version(), "folder": "Mods"})


def moddb_mod(ident):
    ident = str(ident or "").strip()
    m = re.search(r"mods\.vintagestory\.at/(?:show/mod/)?([A-Za-z0-9_-]+)", ident)
    if m:
        ident = m.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", ident):
        raise Fail(f"Not a ModDB mod id: {ident!r}")
    try:
        data = http_json(f"{MODDB_API}/mod/{urllib.parse.quote(ident)}", timeout=30)
    except Exception as e:  # noqa: BLE001
        raise Fail(f"The ModDB did not answer for {ident}: {e}")
    mod = (data or {}).get("mod")
    if not mod:
        raise Fail(f"No mod {ident!r} on mods.vintagestory.at")
    return mod


def pick_release(mod, game_version, want=None):
    """The release to install: the version asked for, else the newest one tagged for this exact
    game version, else the newest for the same minor (1.22.x). None when there is none."""
    rels = [r for r in mod.get("releases") or [] if r.get("mainfile")]
    if want:
        for r in rels:
            if str(r.get("modversion")) == str(want):
                return r
        return None
    exact = [r for r in rels if game_version in (r.get("tags") or [])]
    if exact:
        return exact[0]
    minor = minor_of(game_version)
    same = [r for r in rels if any(minor_of(t) == minor for t in r.get("tags") or [])]
    return same[0] if same else None


def install_release(rel, modidstr):
    """Downloads the release's zip into Mods/, replacing any other file of the same mod."""
    url = rel["mainfile"]
    fname = safe_name(rel.get("filename") or os.path.basename(urllib.parse.urlparse(url).path), "mod file")
    os.makedirs(MODS, exist_ok=True)
    tmp = os.path.join(FLUX_DIR, "tmp")
    os.makedirs(tmp, exist_ok=True)
    part = os.path.join(tmp, fname + ".part")
    with open(part, "wb") as f:
        f.write(http_get(url, timeout=120))
    if fname.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(part) as z:
                bad = z.testzip()
            if bad:
                raise Fail(f"The download of {fname} is damaged ({bad})")
        except zipfile.BadZipFile:
            os.remove(part)
            raise Fail(f"The download of {fname} is not a zip")
    info = read_modinfo(part)
    modid = str(info.get("modid") or modidstr or "").lower()
    for m in installed_mods():
        if m["modid"].lower() == modid and m["file"] != fname:
            folder = MODS if m["enabled"] else MODS_OFF
            p = os.path.join(folder, m["file"])
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    if os.path.exists(os.path.join(MODS_OFF, fname)):
        os.remove(os.path.join(MODS_OFF, fname))
    os.replace(part, os.path.join(MODS, fname))
    return mod_entry(os.path.join(MODS, fname), True)


def install_mod(ident, want=None, seen=None, game_version=None):
    """One mod and, recursively, its dependencies that are not installed. Returns what changed."""
    seen = seen if seen is not None else set()
    game_version = game_version or installed_version() or ""
    mod = moddb_mod(ident)
    rel = pick_release(mod, game_version, want)
    if not rel:
        tags = sorted({t for r in mod.get("releases") or [] for t in r.get("tags") or []}, key=version_key, reverse=True)[:3]
        raise Fail(f"{mod.get('name') or ident} has no release for Vintage Story {minor_of(game_version) or game_version}"
                   f"{' (newest: ' + ', '.join(tags) + ')' if tags else ''}")
    modidstr = str(rel.get("modidstr") or ident).lower()
    seen.add(modidstr)
    entry = install_release(rel, modidstr)
    changed = [{"modid": entry["modid"], "name": entry["name"], "version": entry["version"], "file": entry["file"],
                "side": entry["side"], "dependency": bool(seen - {modidstr}) and len(seen) > 1}]
    have = {m["modid"].lower() for m in installed_mods()}
    for dep in entry["dependencies"]:
        d = dep.lower()
        if d in seen or d in have or d in BASE_MODS:
            continue
        try:
            sub = install_mod(d, None, seen, game_version)
            for s in sub:
                s["dependency"] = True
            changed += sub
        except Fail as e:
            changed.append({"modid": d, "error": str(e), "dependency": True})
    return changed


def cmd_mod_install(args):
    if not args:
        raise Fail("usage: mod-install <modid|link> [version]")
    changed = install_mod(args[0], args[1] if len(args) > 1 else None)
    out({"ok": all("error" not in c for c in changed), "installed": changed, "restartNeeded": True})


def find_installed(modid):
    for m in installed_mods():
        if m["modid"].lower() == str(modid).lower() or m["file"] == modid:
            return m
    raise Fail(f"No installed mod {modid!r}")


def cmd_mod_remove(args):
    m = find_installed(args[0] if args else "")
    p = os.path.join(MODS if m["enabled"] else MODS_OFF, m["file"])
    shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    out({"ok": True, "removed": m["modid"], "restartNeeded": True})


def cmd_mod_toggle(args, enable):
    m = find_installed(args[0] if args else "")
    if m["enabled"] == enable:
        out({"ok": True, "unchanged": m["modid"], "restartNeeded": False})
    src = os.path.join(MODS_OFF if enable else MODS, m["file"])
    dst_dir = MODS if enable else MODS_OFF
    os.makedirs(dst_dir, exist_ok=True)
    os.replace(src, os.path.join(dst_dir, m["file"]))
    out({"ok": True, "modid": m["modid"], "enabled": enable, "restartNeeded": True})


def mod_updates():
    game = installed_version() or ""
    res = []
    for m in installed_mods():
        if not m["hasModinfo"]:
            continue
        try:
            mod = moddb_mod(m["modid"])
        except Fail:
            continue
        rel = pick_release(mod, game)
        if rel and rel.get("modversion") and version_key_loose(rel["modversion"]) > version_key_loose(m["version"]):
            res.append({"modid": m["modid"], "name": m["name"], "installed": m["version"], "latest": rel["modversion"],
                        "file": rel.get("filename"), "enabled": m["enabled"]})
    return res


def version_key_loose(v):
    """Mod versions are free text ('2.0.0-rc.1', '1.14.3'): numbers compared as numbers, a
    pre-release below its release."""
    s = str(v or "").lower().lstrip("v")
    main, _, pre = s.partition("-")
    nums = tuple(int(x) for x in re.findall(r"\d+", main)[:4])
    return nums + ((0,) if pre else (1,), )


def cmd_mod_updates(_args):
    out({"updates": mod_updates(), "gameVersion": installed_version()})


def cmd_mod_update(args):
    target = (args[0] if args else "all").lower()
    ups = mod_updates()
    if target != "all":
        ups = [u for u in ups if u["modid"].lower() == target]
    done = []
    for u in ups:
        changed = install_mod(u["modid"])
        if not u["enabled"]:
            # An update of a mod that was off stays off.
            m = find_installed(u["modid"])
            os.makedirs(MODS_OFF, exist_ok=True)
            os.replace(os.path.join(MODS, m["file"]), os.path.join(MODS_OFF, m["file"]))
        done += changed
    out({"ok": all("error" not in c for c in done), "updated": done, "restartNeeded": bool(done)})


def cmd_ensure_mods(_args):
    wanted = [m for m in re.split(r"[\s,;]+", env("VS_MODS") or "") if m]
    if not wanted:
        return 0
    have = {m["modid"].lower() for m in installed_mods()}
    for ident in wanted:
        name, _, ver = ident.partition("@")
        if name.lower() in have:
            continue
        try:
            for c in install_mod(name, ver or None):
                if "error" in c:
                    log(f"WARN VS_MODS: {c['modid']}: {c['error']}")
                else:
                    log(f"VS_MODS: installed {c['name']} {c['version']}{' (a dependency)' if c.get('dependency') else ''}")
        except Fail as e:
            log(f"WARN VS_MODS: {ident}: {e}")
    return 0


# ----------------------------------------------------------------------------------------------
# Backups: the world, the players' data and serverconfig.json, as one .tar.gz in flux/backups.
# A running world is copied by the game itself (/genbackup: a consistent copy of the SQLite save);
# a stopped one is copied as it is.
# ----------------------------------------------------------------------------------------------

def list_backups():
    res = []
    try:
        names = os.listdir(BACKUP_DIR)
    except OSError:
        names = []
    for n in names:
        if n.endswith(".tar.gz"):
            st = os.stat(os.path.join(BACKUP_DIR, n))
            m = re.match(r"^(\d{8}-\d{6})-(.+)\.tar\.gz$", n)
            res.append({"file": n, "size": st.st_size, "created": int(st.st_mtime), "label": m.group(2) if m else None})
    return sorted(res, key=lambda b: b["created"], reverse=True)


def make_backup(why, live):
    save = current_save()
    src = os.path.join(SAVES, save)
    if not os.path.exists(src):
        raise Fail("There is no world to back up yet")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    label = re.sub(r"[^a-z0-9]+", "-", str(why).lower()).strip("-")[:30] or "manual"
    copy = None
    if live:
        name = f"flux-{stamp}.vcdbs"
        os.makedirs(GAME_BACKUPS, exist_ok=True)
        console(f"/genbackup {name}", wait=3)
        copy = os.path.join(GAME_BACKUPS, name)
        # The game writes the copy in the background: wait until it stops growing.
        last, steady = -1, 0
        for _ in range(600):
            time.sleep(0.5)
            size = os.path.getsize(copy) if os.path.exists(copy) else -1
            steady = steady + 1 if size == last and size > 0 else 0
            last = size
            if steady >= 4:
                break
        if not os.path.exists(copy):
            raise Fail("The game did not write its backup (/genbackup)")
    out_path = os.path.join(BACKUP_DIR, f"{stamp}-{label}.tar.gz")
    part = out_path + ".part"
    try:
        with tarfile.open(part, "w:gz") as t:
            t.add(copy or src, arcname=f"Saves/{save}")
            for extra in ("Playerdata", "serverconfig.json"):
                p = os.path.join(DATA_DIR, extra)
                if os.path.exists(p):
                    t.add(p, arcname=extra)
        os.replace(part, out_path)
    finally:
        if os.path.exists(part):
            os.remove(part)
        if copy and os.path.exists(copy):
            os.remove(copy)
    keep = int(os.environ.get("FLUX_BACKUP_KEEP", "8") or 8)
    for old in list_backups()[keep:]:
        # A backup made before a restore is never rotated away by the next ones.
        if old["label"] != "before-restore":
            os.remove(os.path.join(BACKUP_DIR, old["file"]))
    return os.path.basename(out_path)


def backup_is_current(save_path, backups):
    """True when the newest backup is newer than the world: nothing was saved since, so another
    copy would only rotate a different, older one away (a crash loop would empty the list)."""
    if not backups or not os.path.exists(save_path):
        return False
    newest = backups[0]["created"]
    return all(os.path.getmtime(p) <= newest for p in (save_path, save_path + "-wal") if os.path.exists(p))


def cmd_backup(args):
    why = args[0] if args else "start"
    live = running() and (read_json(STATE_FILE, {}) or {}).get("state") == "running"
    if backup_is_current(os.path.join(SAVES, current_save()), list_backups()):
        return 0
    try:
        name = make_backup(why, live)
        log(f"backup ({why}): flux/backups/{name}")
    except Fail as e:
        log(f"backup ({why}) skipped: {e}")
    return 0


def cmd_backups(_args):
    out({"backups": list_backups(), "pendingRestore": read_json(RESTORE), "world": current_save()})


def cmd_backup_now(args):
    live = running() and (read_json(STATE_FILE, {}) or {}).get("state") == "running"
    name = make_backup(args[0] if args else "manual", live)
    out({"ok": True, "file": name, "backups": list_backups()})


def cmd_restore(args):
    name = safe_name(args[0] if args else "", "backup")
    if not os.path.exists(os.path.join(BACKUP_DIR, name)):
        raise Fail(f"No such backup: {name}")
    write_json(RESTORE, {"file": name, "asked": int(time.time())})
    out({"ok": True, "pending": name, "restartNeeded": True})


def cmd_delete_backup(args):
    name = safe_name(args[0] if args else "", "backup")
    p = os.path.join(BACKUP_DIR, name)
    if not os.path.exists(p):
        raise Fail(f"No such backup: {name}")
    os.remove(p)
    out({"ok": True, "backups": list_backups()})


def apply_restore():
    """A restore the panel asked for, before the server starts: the current world is backed up
    first ("before-restore"), then the backup's world and players' data are put in place and the
    world it holds becomes the one that runs."""
    if not os.path.exists(RESTORE):
        return
    want = (read_json(RESTORE, {}) or {}).get("file")
    os.remove(RESTORE)
    path = os.path.join(BACKUP_DIR, str(want or ""))
    if not want or not os.path.exists(path):
        log(f"WARN the backup to restore ({want}) is gone; nothing restored")
        return
    try:
        make_backup("before-restore", live=False)
    except Fail:
        pass
    with tarfile.open(path) as t:
        members = [m for m in t.getmembers() if m.name.startswith(("Saves/", "Playerdata")) and ".." not in m.name]
        saves = [m.name[len("Saves/"):] for m in members if m.name.startswith("Saves/") and m.name.endswith(".vcdbs")]
        if not saves:
            log(f"WARN {want} holds no world; nothing restored")
            return
        if any(m.name.startswith("Playerdata") for m in members) and os.path.isdir(PLAYERDATA):
            shutil.rmtree(PLAYERDATA)
        for suffix in ("-wal", "-shm"):
            p = os.path.join(SAVES, saves[0] + suffix)
            if os.path.exists(p):
                os.remove(p)
        t.extractall(DATA_DIR, members=members, filter="data")
    cfg = read_json(CONFIG, {}) or {}
    if cfg:
        point_at_save(cfg, saves[0])
        write_json(CONFIG, cfg)
    log(f"restored {want}: the world {saves[0]} and the players' data")


# ----------------------------------------------------------------------------------------------
# A restart the panel asks for.
# ----------------------------------------------------------------------------------------------

def cmd_restart(args):
    why = " ".join(args) or "the owner asked from the panel"
    with open(RESTART_MARKER, "w", encoding="utf-8") as f:
        f.write(f"requested: {why}\n")
    gen = ""
    try:
        with open(os.environ.get("FLUX_GENERATION_FILE", "/tmp/flux-generation"), encoding="utf-8") as f:
            gen = f.read().strip()
    except OSError:
        pass
    out({"ok": True, "generation": gen})


COMMANDS = {
    "install": cmd_install, "latest": cmd_latest, "apply": cmd_apply, "catalog": cmd_catalog,
    "ensure-mods": cmd_ensure_mods, "admins": cmd_admins, "backup": cmd_backup,
    "status": cmd_status, "online": cmd_online, "players": cmd_players, "world": cmd_world, "set-world": cmd_set_world,
    "worlds": cmd_worlds, "new-world": cmd_new_world, "use-world": cmd_use_world, "delete-world": cmd_delete_world,
    "mods": cmd_mods, "mod-install": cmd_mod_install, "mod-remove": cmd_mod_remove,
    "mod-enable": lambda a: cmd_mod_toggle(a, True), "mod-disable": lambda a: cmd_mod_toggle(a, False),
    "mod-updates": cmd_mod_updates, "mod-update": cmd_mod_update,
    "backups": cmd_backups, "backup-now": cmd_backup_now, "restore": cmd_restore, "delete-backup": cmd_delete_backup,
    "versions": cmd_versions, "restart": cmd_restart,
}

# The supervisor's commands: plain text out, errors on stderr.
INTERNAL = {"install", "latest", "apply", "catalog", "ensure-mods", "admins", "backup"}


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        sys.stderr.write("usage: flux-vs " + "|".join(sorted(COMMANDS)) + "\n")
        return 64
    name = argv[0]
    try:
        return COMMANDS[name](argv[1:]) or 0
    except Fail as e:
        if name in INTERNAL:
            log(f"ERROR {e}")
            return 1
        out({"error": str(e)}, 1)
    except Exception as e:  # noqa: BLE001  the hub shows the reason, not a traceback
        if name in INTERNAL:
            log(f"ERROR {name}: {type(e).__name__}: {e}")
            return 1
        out({"error": f"{type(e).__name__}: {e}"}, 1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
