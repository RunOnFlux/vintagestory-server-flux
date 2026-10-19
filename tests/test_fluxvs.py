"""Unit tests for scripts/fluxvs.py: the decisions, with made-up inputs. No server, no network.

    python3 -m unittest -q tests/test_fluxvs.py
"""

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "fluxvs.py")


def load(tmp):
    """fluxvs with its paths inside tmp."""
    os.environ["FLUX_SERVER_DIR"] = os.path.join(tmp, "server")
    os.environ["FLUX_DATA_DIR"] = os.path.join(tmp, "data")
    os.environ.pop("FLUX_DIR", None)
    spec = importlib.util.spec_from_file_location("fluxvs_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The shape of the worldconfig resource the base mods embed (VSSurvivalMod.dll, 1.22.7), with
# the comments and CRLFs the real one has, inside unrelated bytes.
WORLDCONFIG = b"""\x00\x01garbage{ "unrelated": 1 }\x00
{\r\n    "playstyles": [\r\n        {\r\n            "code": "surviveandbuild",\r\n            "langCode": "preset-surviveandbuild",\r\n
            "listOrder": 5,\r\n            "worldType": "standard",\r\n            "worldConfig": { "temporalStorms": "sometimes" }\r\n        },\r\n
        {\r\n            "code": "exploration", // the relaxed one\r\n            "langCode": "preset-exploration",\r\n            "listOrder": 6,\r\n
            "worldType": "standard",\r\n            "worldConfig": { "creatureHostility": "passive", "temporalStorms": "off" }\r\n        }\r\n    ],\r\n
    "worldConfigAttributes": [\r\n
        { "category": "temporalstability", "code": "temporalStorms", "dataType": "dropdown", "values": ["off", "rare"], "names": ["Off", "Rare"], "default": "rare" },\r\n
        { "category": "worldgen", "code": "worldWidth", "dataType": "dropdown", "values": ["1024000", "256000"], "names": ["1 mil blocks", "256k blocks"], "default": "1024000", "onlyDuringWorldCreate": true },\r\n
        { "category": "survivalchallenges", "code": "allowFireSpread", "dataType": "bool", "default": "true" },\r\n
    ]\r\n}\x00\x00more garbage"""

LANG = {
    "playstyle-preset-surviveandbuild": "Standard",
    "playstyle-desc-preset-surviveandbuild": "The default survival experience.",
    "playstyle-preset-exploration": "Exploration",
    "worldattribute-temporalStorms": "Temporal storms",
    "worldattribute-temporalStorms-desc": "How often temporal storms should occur",
    "worldconfig-temporalStorms-Rare": "Rarely",
    "worldconfig-category-temporalstability": "Temporal stability",
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.vs = load(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class Versions(Base):
    INDEX = ["1.22.7", "1.22.6", "1.22.0", "1.22.0-rc.8", "1.21.7", "1.21.6", "1.20.12"]

    def test_latest_is_the_newest_release(self):
        self.assertEqual(self.vs.pick_version("latest", self.INDEX), "1.22.7")
        self.assertEqual(self.vs.pick_version("", self.INDEX), "1.22.7")

    def test_a_minor_line_takes_its_newest_patch(self):
        self.assertEqual(self.vs.pick_version("1.21", self.INDEX), "1.21.7")

    def test_an_exact_version(self):
        self.assertEqual(self.vs.pick_version("1.22.6", self.INDEX), "1.22.6")
        self.assertEqual(self.vs.pick_version("v1.20.12", self.INDEX), "1.20.12")

    def test_unknown_versions_resolve_to_nothing(self):
        self.assertIsNone(self.vs.pick_version("1.99", self.INDEX))
        self.assertIsNone(self.vs.pick_version("1.22.9", self.INDEX))

    def test_a_release_sorts_above_its_candidates(self):
        self.assertGreater(self.vs.version_key("1.22.0"), self.vs.version_key("1.22.0-rc.8"))
        self.assertGreater(self.vs.version_key("1.20.12"), self.vs.version_key("1.20.9"))

    def test_mod_versions_compare_as_numbers(self):
        k = self.vs.version_key_loose
        self.assertGreater(k("2.0.0"), k("2.0.0-rc.1"))
        self.assertGreater(k("1.14.10"), k("1.14.9"))
        self.assertGreater(k("v2.0.0-rc.1"), k("1.14.3"))


class LenientJson(Base):
    def test_comments_and_trailing_commas(self):
        text = '﻿{ "a": 1, // one\n "url": "http://x//y", /* block */ "b": [1, 2,], }'
        self.assertEqual(self.vs.lenient_json(text), {"a": 1, "url": "http://x//y", "b": [1, 2]})


class Catalog(Base):
    def install_fixture(self):
        d = os.path.join(self.tmp, "install")
        os.makedirs(os.path.join(d, "Mods"))
        os.makedirs(os.path.join(d, "assets", "game", "lang"))
        with open(os.path.join(d, "Mods", "VSSurvivalMod.dll"), "wb") as f:
            f.write(WORLDCONFIG)
        with open(os.path.join(d, "assets", "game", "lang", "en.json"), "w") as f:
            json.dump(LANG, f)
        return d

    def test_read_from_the_base_mod(self):
        cat = self.vs.build_catalog(self.install_fixture())
        self.assertEqual([p["code"] for p in cat["playstyles"]], ["surviveandbuild", "exploration"])
        self.assertEqual(cat["playstyles"][0]["name"], "Standard")
        self.assertEqual(cat["playstyles"][1]["worldConfig"], {"creatureHostility": "passive", "temporalStorms": "off"})
        storms = cat["attributes"][0]
        self.assertEqual(storms["name"], "Temporal storms")
        self.assertEqual(storms["categoryName"], "Temporal stability")
        self.assertEqual(storms["values"], [{"value": "off", "label": "Off"}, {"value": "rare", "label": "Rarely"}])
        self.assertFalse(storms["creationOnly"])
        self.assertTrue(cat["attributes"][1]["creationOnly"])
        fire = cat["attributes"][2]
        self.assertEqual(fire["type"], "bool")
        self.assertEqual([v["value"] for v in fire["values"]], ["true", "false"])

    def test_values_are_checked_against_it(self):
        cat = self.vs.build_catalog(self.install_fixture())
        self.assertEqual(self.vs.check_world_values({"temporalStorms": "off"}, cat), {"temporalStorms": "off"})
        with self.assertRaises(self.vs.Fail):
            self.vs.check_world_values({"temporalStorms": "sometimes"}, cat)
        with self.assertRaises(self.vs.Fail):
            self.vs.check_world_values({"nope": "1"}, cat)
        with self.assertRaises(self.vs.Fail):
            self.vs.check_world_values({"worldWidth": "256000"}, cat)
        self.assertEqual(self.vs.check_world_values({"worldWidth": "256000"}, cat, creating=True), {"worldWidth": "256000"})


class Config(Base):
    def base_cfg(self):
        return {"Port": 42420, "ServerName": "Vintage Story Server", "Password": None, "MaxClients": 16,
                "AdvertiseServer": False, "WhitelistMode": 0, "ModPaths": ["Mods"],
                "WorldConfig": {"SaveFileLocation": "/elsewhere/Saves/default.vcdbs", "Seed": None, "WorldName": "A new world",
                                "PlayStyle": "surviveandbuild", "WorldConfiguration": None}}

    def test_env_is_written(self):
        cfg = self.base_cfg()
        env = {"VS_PORT": "42431", "VS_NAME": " My server ", "VS_PASSWORD": "pw", "VS_MAX_CLIENTS": "8",
               "VS_ADVERTISE": "true", "VS_WHITELIST": "on"}
        self.vs.apply_config(cfg, env, save_exists=True)
        self.assertEqual(cfg["Port"], 42431)
        self.assertEqual(cfg["ServerName"], "My server")
        self.assertEqual(cfg["Password"], "pw")
        self.assertEqual(cfg["MaxClients"], 8)
        self.assertTrue(cfg["AdvertiseServer"])
        self.assertEqual(cfg["WhitelistMode"], self.vs.WHITELIST_ON)
        self.assertEqual(cfg["ModPaths"], ["Mods", self.vs.MODS])

    def test_an_empty_password_clears_it_and_unset_keeps_it(self):
        cfg = self.base_cfg()
        cfg["Password"] = "old"
        self.vs.apply_config(cfg, {}, save_exists=True)
        self.assertEqual(cfg["Password"], "old")
        self.vs.apply_config(cfg, {"VS_PASSWORD": ""}, save_exists=True)
        self.assertIsNone(cfg["Password"])

    def test_the_default_whitelist_becomes_off(self):
        cfg = self.base_cfg()
        notes = self.vs.apply_config(cfg, {}, save_exists=True)
        self.assertEqual(cfg["WhitelistMode"], self.vs.WHITELIST_OFF)
        self.assertTrue(any("WhitelistMode" in n for n in notes))
        cfg["WhitelistMode"] = self.vs.WHITELIST_ON
        self.vs.apply_config(cfg, {}, save_exists=True)
        self.assertEqual(cfg["WhitelistMode"], self.vs.WHITELIST_ON)

    def test_a_bad_player_count_keeps_the_old_one(self):
        cfg = self.base_cfg()
        notes = self.vs.apply_config(cfg, {"VS_MAX_CLIENTS": "lots"}, save_exists=True)
        self.assertEqual(cfg["MaxClients"], 16)
        self.assertTrue(any("VS_MAX_CLIENTS" in n for n in notes))

    def test_the_first_world_comes_from_the_deploy_form(self):
        cfg = self.base_cfg()
        env = {"VS_SEED": "777", "VS_PLAYSTYLE": "wilderness", "VS_WORLD_SIZE": "256000", "VS_WORLD_NAME": "Frontier"}
        self.vs.apply_config(cfg, env, catalog=None, save_exists=False)
        wc = cfg["WorldConfig"]
        self.assertEqual(wc["Seed"], "777")
        self.assertEqual(wc["PlayStyle"], "wildernesssurvival")
        self.assertEqual(wc["WorldName"], "Frontier")
        self.assertEqual(wc["WorldConfiguration"], {"worldWidth": "256000", "worldLength": "256000"})
        self.assertEqual(cfg["MapSizeX"], 256000)
        self.assertEqual(wc["SaveFileLocation"], os.path.join(self.vs.SAVES, "default.vcdbs"))

    def test_an_existing_world_ignores_the_world_env(self):
        cfg = self.base_cfg()
        self.vs.apply_config(cfg, {"VS_SEED": "777"}, save_exists=True)
        self.assertIsNone(cfg["WorldConfig"]["Seed"])
        self.assertEqual(cfg["WorldConfig"]["SaveFileLocation"], os.path.join(self.vs.SAVES, "default.vcdbs"))

    def test_a_new_world_from_the_panel_is_not_overwritten_by_the_env(self):
        cfg = self.base_cfg()
        self.vs.apply_new_world(cfg, {"name": "Frontier", "seed": "1", "playstyle": "exploration", "config": {"playerlives": "3"}}, None, now="20260101-000000")
        self.vs.apply_config(cfg, {"VS_SEED": "999", "VS_PLAYSTYLE": "creative"}, save_exists=False, new_world=True)
        wc = cfg["WorldConfig"]
        self.assertEqual((wc["Seed"], wc["PlayStyle"]), ("1", "exploration"))
        self.assertEqual(wc["WorldConfiguration"], {"playerlives": "3"})
        self.assertTrue(wc["SaveFileLocation"].endswith("/frontier-20260101-000000.vcdbs"))

    def test_creative_is_a_superflat_world(self):
        cfg = self.base_cfg()
        self.vs.apply_new_world(cfg, {"playstyle": "creative"}, None)
        self.assertEqual((cfg["WorldConfig"]["PlayStyle"], cfg["WorldConfig"]["WorldType"]), ("creativebuilding", "superflat"))

    def test_switching_to_a_save_forgets_the_last_world_s_seed(self):
        cfg = self.base_cfg()
        cfg["WorldConfig"]["Seed"] = "777"
        self.vs.point_at_save(cfg, "frontier-20260101-000000.vcdbs")
        self.assertIsNone(cfg["WorldConfig"]["Seed"])
        self.assertEqual(cfg["WorldConfig"]["WorldName"], "frontier")


class Console(Base):
    def test_the_player_list(self):
        text = ("List of online Players\n"
                "Playing [3] Tyron 203.0.113.9:51234 (45ms)\n"
                "Playing [7] Saraty 198.51.100.2:40000 (120ms) (95s inactive)\n")
        self.assertEqual(self.vs.parse_clients(text), [
            {"id": 3, "name": "Tyron", "address": "203.0.113.9:51234", "ping": 45, "idle": None},
            {"id": 7, "name": "Saraty", "address": "198.51.100.2:40000", "ping": 120, "idle": 95}])
        self.assertEqual(self.vs.parse_clients("List of online Players\n"), [])

    def test_stats_as_1_22_7_prints_them(self):
        text = ("Version: 1.22.7\nUptime: 0 days, 1 hours, 2 minutes, 3 seconds\nPlayers last online: 57 seconds ago\n"
                "Players online: 2 / 16\n\nMemory usage Managed/Total: 677.1Mb / 1007 Mb\nLast 2s Average Tick Time: 0.15 ms\n"
                "Last 2s Ticks/s: 30\nLoaded chunks: 16\n")
        st = self.vs.parse_stats(text)
        self.assertEqual(st["uptime"], 3723)
        self.assertEqual((st["online"], st["max_players"]), (2, 16))
        self.assertEqual(st["memory_mb"], 1007.0)
        self.assertEqual(st["tps"], 30.0)
        self.assertEqual(st["chunks"], 16)

    def test_the_calendar(self):
        t = self.vs.parse_time("Server time: 1. May, Year 0, 08:00\nGame Speed: 48 IRL minutes")
        self.assertEqual((t["day"], t["month"], t["year"], t["time"]), (1, "May", 0, "08:00"))

    def test_status_answers_complete_on_their_last_line(self):
        self.assertTrue(self.vs.status_answered("/stats", "x\nNetwork UDP: 0 Packets/s"))
        self.assertFalse(self.vs.status_answered("/stats", "Version: 1.22.7"))
        self.assertIsNone(self.vs.status_answered("/list clients", "List of online Players"))


class Mods(Base):
    def make_zip(self, name, modinfo):
        os.makedirs(self.vs.MODS, exist_ok=True)
        p = os.path.join(self.vs.MODS, name)
        with zipfile.ZipFile(p, "w") as z:
            if modinfo is not None:
                z.writestr("modinfo.json", modinfo)
            z.writestr("assets/x.json", "{}")
        return p

    def test_modinfo_is_read_leniently(self):
        p = self.make_zip("CarryOn-1.22.0_v2.0.0-rc.1.zip",
                          '{ "type": "code", "ModID": "carryon", "Name": "Carry On", "Version": "2.0.0-rc.1", // c\n'
                          ' "dependencies": { "game": "1.22.0", "carryonlib": "1.0.0-rc.1", }, }')
        m = self.vs.mod_entry(p, True)
        self.assertEqual((m["modid"], m["name"], m["version"]), ("carryon", "Carry On", "2.0.0-rc.1"))
        self.assertEqual(m["dependencies"], {"carryonlib": "1.0.0-rc.1"})

    def test_a_zip_without_modinfo_is_named_by_its_file(self):
        m = self.vs.mod_entry(self.make_zip("primitivesurvival_v3.9.1.zip", None), True)
        self.assertEqual(m["modid"], "primitivesurvival")
        self.assertFalse(m["hasModinfo"])

    def test_the_release_for_this_game_version(self):
        mod = {"releases": [
            {"modversion": "3.0.0", "mainfile": "u3", "tags": ["1.23.0"]},
            {"modversion": "2.1.0", "mainfile": "u21", "tags": ["1.22.0", "1.22.1"]},
            {"modversion": "2.0.0", "mainfile": "u20", "tags": ["1.22.7"]},
            {"modversion": "1.0.0", "mainfile": "u1", "tags": ["1.21.7"]},
        ]}
        self.assertEqual(self.vs.pick_release(mod, "1.22.7")["modversion"], "2.0.0")
        self.assertEqual(self.vs.pick_release(mod, "1.22.5")["modversion"], "2.1.0")
        self.assertIsNone(self.vs.pick_release(mod, "1.20.12"))
        self.assertEqual(self.vs.pick_release(mod, "1.22.7", want="1.0.0")["modversion"], "1.0.0")


class Backups(Base):
    def test_an_unchanged_world_is_not_backed_up_again(self):
        os.makedirs(self.vs.SAVES)
        save = os.path.join(self.vs.SAVES, "default.vcdbs")
        with open(save, "w") as f:
            f.write("x")
        old = time.time() - 100
        os.utime(save, (old, old))
        self.assertTrue(self.vs.backup_is_current(save, [{"created": int(time.time())}]))
        self.assertFalse(self.vs.backup_is_current(save, [{"created": int(old) - 50}]))
        self.assertFalse(self.vs.backup_is_current(save, []))

    def test_names_from_the_panel_are_plain_file_names(self):
        self.assertEqual(self.vs.safe_name("20261018-210204-manual.tar.gz"), "20261018-210204-manual.tar.gz")
        for bad in ("../x", "/etc/passwd", "a/b", "", ".hidden"):
            with self.assertRaises(self.vs.Fail):
                self.vs.safe_name(bad)


class Cli(Base):
    def test_a_hub_command_failing_prints_json(self):
        old = sys.stdout
        sys.stdout = io.StringIO()
        try:
            with self.assertRaises(SystemExit) as e:
                self.vs.main(["restore", "../../etc/passwd"])
            printed = sys.stdout.getvalue()
        finally:
            sys.stdout = old
        self.assertEqual(e.exception.code, 1)
        self.assertIn("error", json.loads(printed))

    def test_usage(self):
        old = sys.stderr
        sys.stderr = io.StringIO()
        try:
            self.assertEqual(self.vs.main([]), 64)
        finally:
            sys.stderr = old


if __name__ == "__main__":
    unittest.main()
