from __future__ import annotations

import json
import os
import shutil
import tempfile
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


if __name__ == "__main__":
    unittest.main()
