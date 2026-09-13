from __future__ import annotations

import http.server
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from models_ini import parse

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "models.ini"
ORIGINAL = FIXTURE.read_text(encoding="utf-8")


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="modelsini-api-")
        self.prev = {k: os.environ.get(k)
                     for k in ("MODELS_INI_FILE", "MODELS_INI_DIR")}
        os.environ["MODELS_INI_FILE"] = os.path.join(self.tmp, "models.ini")
        os.environ["MODELS_INI_DIR"] = self.tmp
        (Path(self.tmp) / "models.ini").write_text(ORIGINAL, encoding="utf-8")
        self.fake_status = "0"

        import app as app_mod
        self.app_mod = app_mod
        self.client = app_mod.app.test_client()

    def tearDown(self):
        if getattr(self, "fake_git_env", None):
            if self.fake_git_env["PATH"] is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = self.fake_git_env["PATH"]
            os.environ.pop("GIT_FAKE_LOG", None)
            os.environ.pop("GIT_FAKE_STATUS", None)
        for k, v in self.prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── helpers ────────────────────────────────────────────
    def read_file(self) -> str:
        return (Path(self.tmp) / "models.ini").read_text(encoding="utf-8")

    def assert_roundtrip(self, text: str = None):
        text = self.read_file() if text is None else text
        self.assertEqual(parse(text).render(), text)

    def get(self):
        r = self.client.get("/api/models")
        return r.status_code, r.get_json()

    def post(self, url: str, body: dict):
        r = self.client.post(url, json=body)
        return r.status_code, r.get_json()

    # ── GET ─────────────────────────────────────────────────
    def test_get_models(self):
        code, d = self.get()
        self.assertEqual(code, 200)
        self.assertTrue(d["writable"])
        self.assertEqual(len(d["models"]), 39)      # 21 active + 18 archived
        self.assertEqual(d["models"][1]["name"], "General-Bot-small")
        self.assertFalse(d["models"][1]["archived"])
        # aliases: Qwen3.8-27B model shared by five active sections
        # (Coding-Bot is archived)
        q = [m for m in d["aliases"] if m.endswith("Qwen3.8-27B-Q6_K.gguf")]
        self.assertEqual(len(d["aliases"][q[0]]), 5)
        # git: tmp dir is not a repository -> graceful error object
        self.assertFalse(d["git"]["ok"])

    def test_backup_download(self):
        import re
        r = self.client.get("/api/models/backup")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "text/plain")
        disp = r.headers.get("Content-Disposition", "")
        self.assertTrue(disp.startswith("attachment;"))
        m = re.search(r'filename="models-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})\.ini"',
                      disp)
        self.assertIsNotNone(m, disp)
        # byte-for-byte copy of the live file
        self.assertEqual(r.data, ORIGINAL.encode("utf-8"))

    def test_backup_follows_file_changes(self):
        marker = ORIGINAL + "\n; backup-test\n"
        Path(self.tmp, "models.ini").write_text(marker, encoding="utf-8")
        r = self.client.get("/api/models/backup")
        self.assertEqual(r.data, marker.encode("utf-8"))

    def test_backup_missing_file(self):
        Path(self.tmp, "models.ini").unlink()
        r = self.client.get("/api/models/backup")
        self.assertEqual(r.status_code, 404)

    def test_gate_blocks_on_binary_junk(self):
        (Path(self.tmp) / "models.ini").write_bytes(b"\x00\x01\xff\xfe\xfd")
        code, d = self.get()
        self.assertEqual(code, 200)
        self.assertFalse(d["writable"])
        self.assertIn("cannot read", d["write_reason"])

    def test_gate_blocks_on_junk_and_recovers(self):
        junk = Path(self.tmp) / "models.ini"
        junk.write_text("hello\n", encoding="utf-8")
        code, d = self.get()
        self.assertEqual(code, 200)
        self.assertFalse(d["writable"])
        self.assertIn("no sections", d["write_reason"])

        code, d = self.post("/api/models/section/add",
                            {"name": "X", "model": "/x.gguf"})
        self.assertEqual(code, 503)
        self.assertFalse(d["ok"])
        self.assertIn("read-only", d["error"])
        self.assertEqual(self.read_file(), "hello\n")     # untouched

        junk.write_text(ORIGINAL, encoding="utf-8")
        code, d = self.get()
        self.assertTrue(d["writable"])

    # ── mutations ───────────────────────────────────────────
    def _section(self, d: dict, name: str):
        match = [s for s in d["models"] if s["name"] == name]
        self.assertEqual(len(match), 1, f"expected one [{name}]")
        return match[0]

    def test_add(self):
        key_before = json.dumps(
            [s["name"] for s in self.get()[1]["models"]])
        code, d = self.post("/api/models/section/add", {
            "name": "ZTest",
            "model": "/mnt/ssd/z.gguf",
            "params": {"temp": "0.5"},
        })
        self.assertEqual(code, 200)
        self.assertTrue(d["ok"])
        self.assert_roundtrip()

        s = self._section(self.get()[1], "ZTest")
        self.assertFalse(s["archived"])
        self.assertEqual(s["model"], "/mnt/ssd/z.gguf")
        self.assertEqual(dict((kv["key"], kv["value"]) for kv in s["keys"]),
                         {"model": "/mnt/ssd/z.gguf", "temp": "0.5"})

    def test_add_invalid(self):
        code, d = self.post("/api/models/section/add",
                            {"name": "bad name", "model": "/x.gguf"})
        self.assertEqual(code, 400)
        code, d = self.post("/api/models/section/add",
                            {"name": "ok", "model": ""})
        self.assertEqual(code, 400)
        code, d = self.post("/api/models/section/add",
                            {"name": "Qwen3.5-4B", "model": "/x.gguf"})
        self.assertEqual(code, 400)                    # duplicate name

    def test_edit_set_remove_rename(self):
        code, d = self.post("/api/models/section/edit", {
            "name": "Qwen3.5-4B",
            "new_name": "Qwen3.5-4B-x",
            "set": {"temp": "0.9", "foo": "bar"},
            "remove": ["parallel"],
        })
        self.assertEqual(code, 200)
        self.assert_roundtrip()
        s = self._section(self.get()[1], "Qwen3.5-4B-x")
        keys = dict((kv["key"], kv["value"]) for kv in s["keys"])
        self.assertEqual(keys["temp"], "0.9")
        self.assertEqual(keys["foo"], "bar")
        self.assertNotIn("parallel", keys)

    def test_edit_unknown_section(self):
        code, d = self.post("/api/models/section/edit",
                            {"name": "no-such", "set": {"a": "b"}})
        self.assertEqual(code, 400)

    def test_edit_rename_collision_with_archived(self):
        before = self.read_file()
        code, d = self.post("/api/models/section/edit", {
            "name": "Qwen3.5-4B",
            "new_name": "gemma-4-31B-Instruct",
        })
        self.assertEqual(code, 400)
        self.assertEqual(self.read_file(), before)

    def test_edit_archived_only_section(self):
        code, d = self.post("/api/models/section/edit", {
            "name": "Coding-Bot", "archived": True,
            "set": {"zz_test": "1"},
        })
        self.assertEqual(code, 200)
        self.assert_roundtrip()
        s = self._section(self.get()[1], "Coding-Bot")
        self.assertTrue(s["archived"])
        self.assertEqual(dict((kv["key"], kv["value"])
                              for kv in s["keys"])["zz_test"], "1")

    def test_edit_archived_twin_keeps_active_untouched(self):
        def active_keys():
            twins = [s for s in self.get()[1]["models"]
                     if s["name"] == "gemma-4-26B" and not s["archived"]]
            return dict((kv["key"], kv["value"]) for kv in twins[0]["keys"])

        before = active_keys()
        code, d = self.post("/api/models/section/edit", {
            "name": "gemma-4-26B", "archived": True,
            "set": {"zz_test": "1"},
        })
        self.assertEqual(code, 200)
        self.assert_roundtrip()
        self.assertEqual(active_keys(), before)
        twins = [s for s in self.get()[1]["models"]
                 if s["name"] == "gemma-4-26B" and s["archived"]]
        self.assertEqual(dict((kv["key"], kv["value"])
                              for kv in twins[0]["keys"])["zz_test"], "1")

    def test_edit_archived_existing_key_stays_commented(self):
        code, d = self.post("/api/models/section/edit", {
            "name": "gemma-4-26B", "archived": True,
            "set": {"image-min-tokens": "301"},
        })
        self.assertEqual(code, 200)
        lines = self.read_file().splitlines()
        self.assertIn("# image-min-tokens = 301", lines)
        self.assertNotIn("image-min-tokens = 301", lines)
        self.assert_roundtrip()

    def test_edit_archived_remove_key(self):
        code, d = self.post("/api/models/section/edit", {
            "name": "gemma-4-26B", "archived": True,
            "remove": ["image-min-tokens"],
        })
        self.assertEqual(code, 200)
        s = [x for x in self.get()[1]["models"]
             if x["name"] == "gemma-4-26B" and x["archived"]][0]
        self.assertNotIn(
            "image-min-tokens", [kv["key"] for kv in s["keys"]])
        self.assert_roundtrip()

    def test_edit_reorder_keys(self):
        code, d = self.post("/api/models/section/edit", {
            "name": "General-Bot-small",
            "set": {},
            "key_order": ["top-p", "parallel", "reasoning", "temp",
                          "top-k", "ctx-size", "mmproj"],
        })
        self.assertEqual(code, 200)
        self.assertTrue(d["ok"])
        self.assert_roundtrip()
        s = self._section(self.get()[1], "General-Bot-small")
        self.assertEqual([kv["key"] for kv in s["keys"]],
                         ["model", "top-p", "parallel", "reasoning",
                          "temp", "top-k", "ctx-size", "mmproj"])

    def test_edit_reorder_with_value_change(self):
        code, d = self.post("/api/models/section/edit", {
            "name": "General-Bot-small",
            "set": {"temp": "0.7"},
            "key_order": ["temp", "top-p", "parallel", "reasoning",
                          "top-k", "ctx-size", "mmproj"],
        })
        self.assertEqual(code, 200)
        self.assert_roundtrip()
        s = self._section(self.get()[1], "General-Bot-small")
        keys = {kv["key"]: kv["value"] for kv in s["keys"]}
        self.assertEqual(keys["temp"], "0.7")
        self.assertEqual([kv["key"] for kv in s["keys"]],
                         ["model", "temp", "top-p", "parallel", "reasoning",
                          "top-k", "ctx-size", "mmproj"])

    def test_edit_reorder_noop_keeps_file(self):
        before = self.read_file()
        code, d = self.post("/api/models/section/edit", {
            "name": "General-Bot-small",
            "key_order": ["parallel", "reasoning", "temp", "top-p",
                          "top-k", "ctx-size", "mmproj"],
        })
        self.assertEqual(code, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(self.read_file(), before)

    def test_edit_reorder_bad_type_rejected(self):
        before = self.read_file()
        code, d = self.post("/api/models/section/edit",
                            {"name": "General-Bot-small", "key_order": "x"})
        self.assertEqual(code, 400)
        self.assertEqual(self.read_file(), before)

    def test_edit_rename_invalid_new_name(self):
        before = self.read_file()
        code, d = self.post("/api/models/section/edit", {
            "name": "Qwen3.5-4B", "new_name": "bad name"})
        self.assertEqual(code, 400)
        self.assertEqual(self.read_file(), before)

    def test_edit_global_section_protected(self):
        before = self.read_file()
        code, d = self.post("/api/models/section/edit",
                            {"name": "*", "new_name": "defaults"})
        self.assertEqual(code, 400)
        self.assertIn("global", d["error"])
        code, d = self.post("/api/models/section/delete", {"name": "*"})
        self.assertEqual(code, 400)
        self.assertIn("global", d["error"])
        self.assertEqual(self.read_file(), before)
        self.assertIn("[*]", before)

    def test_bad_json_types_rejected(self):
        code, d = self.post("/api/models/section/add",
                            {"name": "X", "model": "/x", "params": [1, 2]})
        self.assertEqual(code, 400)
        code, d = self.post("/api/models/section/edit",
                            {"name": "Qwen3.5-4B", "set": [1, 2]})
        self.assertEqual(code, 400)
        code, d = self.post("/api/models/section/edit",
                            {"name": "Qwen3.5-4B", "remove": "x"})
        self.assertEqual(code, 400)
        self.assertEqual(self.read_file(), ORIGINAL)

    def test_edit_wrong_state_rejected(self):
        before = self.read_file()
        # [Coding-Bot] exists only archived; default state is active
        code, d = self.post("/api/models/section/edit",
                            {"name": "Coding-Bot", "set": {"a": "b"}})
        self.assertEqual(code, 400)
        self.assertEqual(self.read_file(), before)

    def test_archive_then_restore(self):
        code, d = self.post("/api/models/section/archive",
                            {"name": "Qwen3.8-27B-low"})
        self.assertEqual(code, 200)
        self.assert_roundtrip()
        self.assertIn("# [Qwen3.8-27B-low]", self.read_file())
        self.assertEqual(self._section(self.get()[1], "Qwen3.8-27B-low")
                         ["archived"], True)

        code, d = self.post("/api/models/section/restore",
                            {"name": "Qwen3.8-27B-low"})
        self.assertEqual(code, 200)
        self.assert_roundtrip()
        self.assertFalse(self._section(self.get()[1], "Qwen3.8-27B-low")
                          ["archived"])

    def test_delete_active_and_archived(self):
        code, d = self.post("/api/models/section/delete",
                            {"name": "Qwen3-Coder-Next"})
        self.assertEqual(code, 200)
        self.assert_roundtrip()
        names = [s["name"] for s in self.get()[1]["models"]]
        self.assertNotIn("Qwen3-Coder-Next", names)

        code, d = self.post("/api/models/section/delete",
                            {"name": "Muse-Glimmer-30B", "archived": True})
        self.assertEqual(code, 200)
        self.assert_roundtrip()
        names = [s["name"] for s in self.get()[1]["models"]]
        self.assertNotIn("Muse-Glimmer-30B", names)

    def test_archive_error_is_400(self):
        code, d = self.post("/api/models/section/archive",
                            {"name": "no-such"})
        self.assertEqual(code, 400)

    def test_move_within_region(self):
        code, d = self.post("/api/models/section/move", {
            "name": "gemma-4-31B-Code",
            "target": "gemma-4-26B",
            "position": "after",
        })
        self.assertEqual(code, 200)
        self.assertTrue(d["ok"])
        self.assert_roundtrip()
        names = [s["name"] for s in self.get()[1]["models"]]
        self.assertEqual(names.index("gemma-4-31B-Code"),
                         names.index("gemma-4-26B") + 1)

    def test_move_region_head(self):
        # moving the region head down must keep the file parseable with
        # the marker on the new head
        code, d = self.post("/api/models/section/move", {
            "name": "General-Bot-small",
            "target": "Reasoning-Bot",
            "position": "after",
        })
        self.assertEqual(code, 200)
        self.assert_roundtrip()
        code, d = self.get()
        s = self._section(d, "General-Bot-small")
        self.assertEqual(s["region"], "profiles")
        names = [x["name"] for x in d["models"]]
        self.assertEqual(names.index("General-Bot-small"),
                         names.index("Reasoning-Bot") + 1)

    def test_move_noop_keeps_file_untouched(self):
        before = self.read_file()
        code, d = self.post("/api/models/section/move", {
            "name": "gemma-4-31B-Code",
            "target": "gemma-4-26B",
            "position": "before",
        })
        self.assertEqual(code, 200)
        self.assertEqual(self.read_file(), before)

    def test_move_rejected_cross_region_and_bad_args(self):
        for body in (
            {"name": "Qwen3.5-4B", "target": "General-Bot-small",
             "position": "after"},
            {"name": "Qwen3.5-4B", "target": "gemma-4-31B", "position": "sideways"},
            {"name": "no-such", "target": "gemma-4-31B", "position": "after"},
            {"name": "bad name", "target": "gemma-4-31B", "position": "after"},
        ):
            with self.subTest(body=body):
                code, d = self.post("/api/models/section/move", body)
                self.assertEqual(code, 400)
        self.assertEqual(self.read_file(), ORIGINAL)

    def test_move_archived(self):
        code, d = self.post("/api/models/section/move", {
            "name": "Muse-Glimmer-30B",
            "target": "gemma-4-31B-Instruct",
            "position": "after",
            "archived": True,
        })
        self.assertEqual(code, 200)
        self.assert_roundtrip()
        code, d = self.get()
        s = self._section(d, "Muse-Glimmer-30B")
        self.assertEqual(s["region"], "archived_models")
        names = [x["name"] for x in d["models"]]
        self.assertEqual(names.index("Muse-Glimmer-30B"),
                         names.index("gemma-4-31B-Instruct") + 1)

    # ── git endpoints ────────────────────────────────────────

    def test_git_commit_no_repo(self):
        code, d = self.post("/api/models/git", {"action": "commit"})
        self.assertEqual(code, 500)
        self.assertFalse(d["ok"])

    def test_git_unknown_action(self):
        code, d = self.post("/api/models/git", {"action": "nope"})
        self.assertEqual(code, 400)

    def _install_fake_git(self):
        """Put a logging shim named `git` first on PATH."""
        bin_dir = Path(self.tmp) / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "git"
        # app always invokes as: git -C <dir> <cmd> ...  → cmd is $3
        shim.write_text(
            "#!/bin/sh\n"
            'echo "$@" >> "$GIT_FAKE_LOG"\n'
            'cmd=$3; a1=$4\n'
            'if [ "$cmd" = "rev-parse" ]; then\n'
            '  [ "$a1" = "--abbrev-ref" ] && { echo main; exit 0; }\n'
            "fi\n"
            'if [ "$cmd" = "status" ]; then\n'
            '  [ "$GIT_FAKE_STATUS" = 1 ] && echo " M models.ini"\n'
            "fi\n"
            'if [ "$cmd" = "log" ]; then echo "abc123 fake commit"; fi\n'
            'if [ "$cmd" = "rev-list" ]; then echo 1; fi\n'
            'if [ "$cmd" = "pull" ]; then echo "Already up to date."; fi\n'
            'if [ "$cmd" = "push" ]; then echo "To remote (pushed)"; fi\n'
            "exit 0\n",
            encoding="utf-8")
        shim.chmod(0o755)
        self.fake_git_env = {
            "PATH": os.environ.get("PATH"),
            "GIT_FAKE_LOG": None,
            "GIT_FAKE_STATUS": None,
        }
        os.environ["PATH"] = str(bin_dir) + os.pathsep + (os.environ["PATH"] or "")
        os.environ["GIT_FAKE_LOG"] = str(Path(self.tmp) / "git.log")
        os.environ["GIT_FAKE_STATUS"] = self.fake_status

    def _git_calls(self):
        calls = [line.split() for line in
                 (Path(self.tmp) / "git.log").read_text().splitlines()]
        return [c[2:] for c in calls if c and c[0] == "-C"]

    def test_git_commit_flow(self):
        self.fake_status = "1"
        self._install_fake_git()
        code, d = self.post("/api/models/git",
                            {"action": "commit", "message": "from ui"})
        self.assertEqual(code, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(d["commit"], "abc123 fake commit")
        calls = self._git_calls()
        seq = [c[0] for c in calls if c]
        i_add, i_commit = seq.index("add"), seq.index("commit")
        self.assertLess(i_add, i_commit)
        self.assertEqual(calls[i_add], ["add", "models.ini"])
        self.assertEqual(calls[i_commit][:2], ["commit", "-m"])
        self.assertEqual(" ".join(calls[i_commit][2:]), "from ui")

    def test_git_commit_clean_tree(self):
        self.fake_status = "1"
        self._install_fake_git()
        os.environ["GIT_FAKE_STATUS"] = "0"      # tree clean mid-request
        code, d = self.post("/api/models/git", {"action": "commit"})
        self.assertEqual(code, 400)
        self.assertIn("no changes", d["error"])

    def test_git_pull_push_flow(self):
        self.fake_status = "0"
        self._install_fake_git()
        code, d = self.post("/api/models/git", {"action": "pull"})
        self.assertEqual(code, 200)
        self.assertEqual(d["output"], "Already up to date.")
        code, d = self.post("/api/models/git", {"action": "push"})
        self.assertEqual(code, 200)
        self.assertIn("pushed", d["output"])
        calls = self._git_calls()
        self.assertIn(["pull", "--ff-only", "origin", "main"], calls)
        self.assertIn(["push", "origin", "main"], calls)

    def test_git_status_in_get(self):
        self.fake_status = "1"
        self._install_fake_git()
        code, d = self.get()
        self.assertEqual(code, 200)
        g = d["git"]
        self.assertTrue(g["ok"])
        self.assertEqual(g["branch"], "main")
        self.assertTrue(g["dirty"])
        self.assertEqual(g["ahead"], 1)
        self.assertEqual(g["behind"], 1)
        self.assertEqual(g["last_commit"], "abc123 fake commit")

    def test_git_status_refreshes_remote_ref(self):
        # GET must trigger a throttled `git fetch origin <branch>` so the
        # ahead/behind numbers track GitHub without a full pull
        self.fake_status = "0"
        self._install_fake_git()
        self.app_mod._fetch_state["last_ts"] = 0.0
        code, d = self.get()
        self.assertEqual(code, 200)
        self.assertIn(["fetch", "origin", "main"], self._git_calls())


class ConfigTestCase(unittest.TestCase):
    """Config externalization: env > config.json > built-in default."""

    CFG_KEYS = (
        "CONFIG_FILE", "PORT", "BIND_HOST", "RAM_WARN_PCT", "RAM_CRIT_PCT",
        "SWAP_WARN_PCT", "SWAP_CRIT_PCT", "LOG_TAIL_LINES",
        "LLAMA_SERVER_SERVICE", "LLAMA_SERVER_PORT", "LLAMA_SERVER_HOST",
        "MODELS_DIR", "MODELS_INI_FILE", "MODELS_INI_DIR",
        "RESTART_COOLDOWN_S", "PROBE_INTERVAL_S",
    )

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="modelsini-cfg-")
        self.prev = {k: os.environ.get(k) for k in self.CFG_KEYS}
        for k in self.CFG_KEYS:
            os.environ.pop(k, None)
        import app as app_mod
        self.app_mod = app_mod
        self.client = app_mod.app.test_client()

    def tearDown(self):
        for k, v in self.prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # drop any test config file, as a fresh import would see it
        self.app_mod._FILE_CFG = self.app_mod._load_config_file()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_cfg(self, data) -> None:
        p = Path(self.tmp) / "config.json"
        p.write_text(data if isinstance(data, str) else json.dumps(data),
                     encoding="utf-8")
        os.environ["CONFIG_FILE"] = str(p)
        # the app loads the file at import time; pick up test changes
        self.app_mod._FILE_CFG = self.app_mod._load_config_file()

    # ── file loading ────────────────────────────────────────
    def test_file_beats_default(self):
        self._write_cfg({"port": 8888, "log_tail_lines": 42})
        self.assertEqual(self.app_mod._cfg("port", 1), 8888)
        self.assertEqual(self.app_mod._cfg_int("log_tail_lines", 500), 42)

    def test_env_beats_file(self):
        self._write_cfg({"port": 8888})
        os.environ["PORT"] = "9999"
        self.assertEqual(self.app_mod._cfg("port", 1), "9999")

    def test_empty_env_falls_through_to_file(self):
        self._write_cfg({"port": 8888})
        os.environ["PORT"] = ""
        self.assertEqual(self.app_mod._cfg("port", 1), 8888)

    def test_empty_file_value_falls_to_default(self):
        # "llama_server_port": "" (disabled by default) must not warn or
        # coerce to 0 — it means "unset".
        self._write_cfg({"llama_server_port": ""})
        self.assertEqual(self.app_mod._cfg("llama_server_port", 0), 0)
        self.assertEqual(self.app_mod._cfg_int("llama_server_port", 0), 0)

    def test_default_when_nothing_set(self):
        os.environ["CONFIG_FILE"] = os.path.join(self.tmp, "absent.json")
        self.app_mod._FILE_CFG = self.app_mod._load_config_file()
        self.assertEqual(self.app_mod._cfg("port", 8080), 8080)
        self.assertEqual(self.app_mod._cfg_int("history_len", 120), 120)

    def test_missing_file_is_ignored(self):
        os.environ["CONFIG_FILE"] = os.path.join(self.tmp, "absent.json")
        self.assertEqual(self.app_mod._load_config_file(), {})

    def test_broken_file_is_ignored(self):
        self._write_cfg("{ not json")
        self.assertEqual(self.app_mod._FILE_CFG, {})
        self.assertEqual(self.app_mod._cfg("port", 8080), 8080)
        self._write_cfg("[1, 2]")
        self.assertEqual(self.app_mod._FILE_CFG, {})

    def test_coercion_and_bad_values(self):
        self._write_cfg({"port": "9191", "ram_warn_pct": "82.5"})
        self.assertEqual(self.app_mod._cfg_int("port", 8080), 9191)
        self.assertEqual(self.app_mod._cfg_float("ram_warn_pct", 85.0), 82.5)
        os.environ["PORT"] = "not-a-number"
        self.assertEqual(self.app_mod._cfg_int("port", 8080), 8080)

    # ── consumers read config ───────────────────────────────
    def test_severity_uses_configured_thresholds(self):
        self.assertEqual(self.app_mod._severity(84.0, 0.0), "ok")
        os.environ["RAM_WARN_PCT"] = "50"
        self.assertEqual(self.app_mod._severity(84.0, 0.0), "warn")
        os.environ["RAM_CRIT_PCT"] = "80"
        self.assertEqual(self.app_mod._severity(84.0, 0.0), "critical")
        os.environ["SWAP_CRIT_PCT"] = "10"
        self.assertEqual(self.app_mod._severity(0.0, 20.0), "critical")

    def test_journalctl_cmd_uses_config(self):
        cmd = self.app_mod._journalctl_cmd()
        self.assertIn("llama-server.service", cmd)
        self.assertEqual(cmd[cmd.index("-n") + 1], "500")
        os.environ["LLAMA_SERVER_SERVICE"] = "my-llama.service"
        os.environ["LOG_TAIL_LINES"] = "77"
        cmd = self.app_mod._journalctl_cmd()
        self.assertIn("my-llama.service", cmd)
        self.assertEqual(cmd[cmd.index("-n") + 1], "77")

    def test_restart_cooldown_from_config(self):
        os.environ["RESTART_COOLDOWN_S"] = "60"
        self.app_mod._restart_state["last_ts"] = time.time()
        r = self.client.post("/api/restart-llama")
        self.assertEqual(r.status_code, 429)
        self.assertIn("Cooldown", r.get_json()["error"])

    def test_payload_carries_thresholds(self):
        class FakeJetson:
            memory = {
                "RAM": {"tot": 1000, "used": 500, "free": 400, "shared": 50},
                "SWAP": {"tot": 100, "used": 10},
            }
            stats = {
                "CPU0": 12.0, "CPU1": 34.0, "GPU": 55.0, "Temp tj": 61.5,
                "Power TOT": 42000, "Fan pwmfan0": 30.0, "nvp model": "max",
            }

        p = self.app_mod._build_payload(FakeJetson())
        self.assertEqual(p["thresholds"],
                         {"ram_warn": 85.0, "ram_crit": 93.0,
                          "swap_warn": 25.0, "swap_crit": 50.0})
        os.environ["SWAP_WARN_PCT"] = "33"
        self.assertEqual(
            self.app_mod._build_payload(FakeJetson())["thresholds"]["swap_warn"],
            33.0)

    # ── models dir derivation ───────────────────────────────
    def test_models_dir_explicit_wins(self):
        os.environ["MODELS_DIR"] = "/data/models"
        self.assertEqual(self.app_mod._models_dir(), "/data/models")

    def test_models_dir_derived_from_ini_file_env(self):
        os.environ["MODELS_INI_FILE"] = os.path.join(self.tmp, "models.ini")
        self.assertEqual(self.app_mod._models_dir(), self.tmp)

    def test_models_dir_default_is_ini_parent(self):
        self.assertEqual(self.app_mod._models_dir(),
                         os.path.dirname(
                             self.app_mod.MODELS_INI_PATH))


class ProbesTestCase(unittest.TestCase):
    """Phase 2: llama-server status + model-disk probes (mocked)."""

    ENV_KEYS = (
        "CONFIG_FILE", "MODELS_INI_FILE", "MODELS_INI_DIR", "MODELS_DIR",
        "LLAMA_SERVER_PORT", "LLAMA_SERVER_HOST", "LLAMA_SERVER_SERVICE",
        "PROBE_INTERVAL_S",
    )

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="modelsini-probe-")
        self.prev = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["MODELS_INI_FILE"] = os.path.join(self.tmp, "models.ini")
        import app as app_mod
        self.app_mod = app_mod
        self.client = app_mod.app.test_client()
        self._saved: dict = {}

    def tearDown(self):
        for k, v in self.prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for name, val in self._saved.items():
            if name == "shutil.disk_usage":
                self.app_mod.shutil.disk_usage = val
            else:
                setattr(self.app_mod, name, val)
        self.app_mod._FILE_CFG = self.app_mod._load_config_file()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch(self, name, value):
        self._saved.setdefault(name, getattr(self.app_mod, name))
        setattr(self.app_mod, name, value)

    # ── llama-server probe ───────────────────────────────────
    def test_probe_active_with_model(self):
        self._patch("_systemctl_is_active", lambda unit: "active")
        self._patch("_systemctl_active_mono_us",
                    lambda unit: int((time.monotonic() - 3700) * 1_000_000))
        os.environ["LLAMA_SERVER_PORT"] = "11435"
        self._patch("_llama_model_name",
                    lambda host, port: "test-model.gguf")
        out = self.app_mod._probe_llama_server()
        self.assertEqual(out["state"], "active")
        self.assertTrue(3690 <= out["uptime_s"] <= 3710)
        self.assertEqual(out["model"], "test-model.gguf")
        self.assertTrue(out["model_configured"])

    def test_probe_inactive_skips_http(self):
        self._patch("_systemctl_is_active", lambda unit: "inactive")
        def boom(host, port):
            raise AssertionError("HTTP probe must be skipped when inactive")
        self._patch("_llama_model_name", boom)
        os.environ["LLAMA_SERVER_PORT"] = "11435"
        out = self.app_mod._probe_llama_server()
        self.assertEqual(out["state"], "inactive")
        self.assertIsNone(out["uptime_s"])
        self.assertFalse(out["model_configured"])

    def test_probe_unknown_unit(self):
        self._patch("_systemctl_is_active", lambda unit: "unknown")
        out = self.app_mod._probe_llama_server()
        self.assertEqual(out["state"], "unknown")

    def test_probe_without_port_skips_model_probe(self):
        self._patch("_systemctl_is_active", lambda unit: "active")
        self._patch("_systemctl_active_mono_us", lambda unit: None)
        def boom(host, port):
            raise AssertionError("HTTP probe must be skipped without a port")
        self._patch("_llama_model_name", boom)
        out = self.app_mod._probe_llama_server()
        self.assertFalse(out["model_configured"])
        self.assertIsNone(out["model"])

    # ── HTTP model lookup (real local stub server) ──────────
    def _serve(self, routes):
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path in routes:
                    status, body = routes[self.path]
                else:
                    status, body = 404, b'{"error": "not found"}'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, srv.server_address[1]

    def test_model_name_from_v1_models(self):
        body = json.dumps({"data": [{"id": "test-model.gguf"}]}).encode()
        srv, port = self._serve({"/v1/models": (200, body)})
        try:
            self.assertEqual(
                self.app_mod._llama_model_name("127.0.0.1", port),
                "test-model.gguf")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_model_name_falls_back_to_props(self):
        body = json.dumps({"name": "fallback.gguf"}).encode()
        srv, port = self._serve({"/props": (200, body)})
        try:
            self.assertEqual(
                self.app_mod._llama_model_name("127.0.0.1", port),
                "fallback.gguf")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_model_name_unreachable(self):
        self.assertIsNone(self.app_mod._llama_model_name("127.0.0.1", 1))

    # ── disk probe ───────────────────────────────────────────
    def test_probe_disk(self):
        u = type("U", (), {})
        u.total = 931 * 1_073_741_824
        u.used  = 342 * 1_073_741_824
        u.free  = u.total - u.used
        self._saved["shutil.disk_usage"] = self.app_mod.shutil.disk_usage
        self.app_mod.shutil.disk_usage = lambda p: u
        out = self.app_mod._probe_disk()
        self.assertEqual(out["path"], self.tmp)
        self.assertEqual(out["used_gib"], 342.0)
        self.assertEqual(out["total_gib"], 931.0)
        self.assertAlmostEqual(out["pct"], 36.7, places=1)

    # ── SSE merge ────────────────────────────────────────────
    def test_stream_payload_includes_probes(self):
        m = self.app_mod
        saved_probe = (m._probe_data["llama"], m._probe_data["disk"])
        saved_conn, saved_latest, saved_board = (
            m._data["connected"], m._data["latest"], m._data["board"])

        class FakeJetson:
            memory = {
                "RAM":  {"tot": 32768, "used": 8192, "free": 24576,
                         "shared": 2048},
                "SWAP": {"tot": 16384, "used": 2048},
            }
            stats = {"CPU0": 1.0, "CPU1": 2.0, "GPU": 5.0,
                     "Temp tj": 50.0, "Power TOT": 1000, "Fan pwmfan0": 10.0}

        fake = m._build_payload(FakeJetson())
        m._probe_data["llama"] = {"state": "active", "uptime_s": 90,
                                  "model": "m.gguf",
                                  "model_configured": True}
        m._probe_data["disk"] = {"path": "/mnt/ssd", "used_gib": 1.0,
                                 "total_gib": 2.0, "pct": 50.0}
        with m._lock:
            m._data["connected"] = True
            m._data["latest"] = fake
            m._data["board"] = {"model": "T", "jetpack": "1", "python": "3"}
            m._data["history"]["ram_pct"].append(1.0)
            m._data["history"]["gpu_pct"].append(2.0)
        m._new_data.set()
        try:
            found, buf = None, ""
            with self.client.get("/stream") as resp:
                for chunk in resp.response:
                    buf += chunk.decode("utf-8")
                    while "\n\n" in buf:
                        evt, buf = buf.split("\n\n", 1)
                        if evt.startswith("data: ") and "thresholds" in evt:
                            found = json.loads(evt[6:])
                            break
                    if found:
                        break
            self.assertIsNotNone(found, "no payload event seen in stream")
            self.assertEqual(found["llama"]["model"], "m.gguf")
            self.assertEqual(found["disk"]["pct"], 50.0)
        finally:
            m._probe_data["llama"], m._probe_data["disk"] = saved_probe
            m._data["connected"], m._data["latest"], m._data["board"] = (
                saved_conn, saved_latest, saved_board)


if __name__ == "__main__":
    unittest.main()
