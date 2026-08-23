from __future__ import annotations

import unittest
from pathlib import Path

from models_ini import ModelsIniError, parse

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "models.ini"
TEXT = FIXTURE.read_text(encoding="utf-8")


def reparse(text: str) -> str:
    """parse -> render must be byte-identical."""
    return parse(text).render()


class RoundTrip(unittest.TestCase):
    def test_fixture_round_trip(self):
        self.assertEqual(reparse(TEXT), TEXT)

    def test_fixture_structural(self):
        doc = parse(TEXT)
        active = [b for b in doc.blocks if not b.archived]
        archived = [b for b in doc.blocks if b.archived]
        self.assertEqual(len(active), 22)     # [*] + 4 profiles + 17 individual
        self.assertEqual(len(archived), 17)

        self.assertEqual(doc.block("*").region, "global")
        self.assertEqual(doc.block("General-Bot-small").region, "profiles")
        self.assertEqual(doc.block("Reasoning-Bot").region, "profiles")
        self.assertEqual(doc.block("Qwen3.5-4B").region, "individual")
        self.assertEqual(doc.block("Muse-Glimmer-30B", archived=True).region, "archived")
        self.assertEqual(doc.block("Qwen3-Coder-30B", archived=True).region, "archived")
        # in-archived-region fragment without a model line
        frag = doc.block("gemma-4-26B", archived=True)
        self.assertEqual(frag.keys(), [
            ("image-min-tokens", "300"), ("image-max-tokens", "512"),
        ])

        keys = dict(doc.block("Qwen3.6-35B").keys())
        self.assertEqual(keys["override-kv"], "qwen35moe.context_length=int:1000000")
        self.assertEqual(keys["chat-template-kwargs"], '{"preserve_thinking": true}')

    def test_inline_comment_kept(self):
        doc = parse(TEXT)
        keys = dict(doc.block("*").keys())
        self.assertEqual(keys["checkpoint-min-step"],
                         "0   ; place the checkpoint as close to the end as possible")

    def test_header_lines_preserved(self):
        doc = parse(TEXT)
        self.assertEqual(doc.header[0], "version = 1\n")
        self.assertIn("; (Optional) This section provides global settings shared across all presets.\n",
                      doc.header)
        # leading comments above a section follow that section
        leading = doc.block("General-Bot-small").leading
        self.assertIn("; If the key corresponds to an existing model on the server,\n", leading)
        self.assertIn("# == PROFILES ==\n", leading)
        self.assertEqual(doc.render()[:12], TEXT[:12])


class Aliases(unittest.TestCase):
    def test_group_aliases(self):
        doc = parse(TEXT)
        qwen = "/mnt/ssd/llamacpp_models/Qwen3.8-27B-Q6_K.gguf"
        self.assertEqual(
            sorted(doc.group_aliases()[qwen]),
            sorted(["Reasoning-Bot", "Coding-Bot", "Qwen3.8-27B-xhigh",
                    "Qwen3.8-27B-medium", "Qwen3.8-27B-low", "Qwen3.8-27B-Code"]),
        )
        gemma = "/mnt/ssd/llamacpp_models/gemma-4-31B-it-qat-UD-Q4_K_XL.gguf"
        self.assertEqual(
            sorted(doc.group_aliases()[gemma]),
            sorted(["General-Bot-large", "gemma-4-31B", "gemma-4-31B-Code"]),
        )
        # archived sections do not participate
        self.assertNotIn(
            "/mnt/ssd/llamacpp_models/gpt-oss-20b-UD-Q4_K_XL.gguf",
            doc.group_aliases(),
        )


class Edits(unittest.TestCase):
    def setUp(self):
        self.doc = parse(TEXT)

    def _stable(self):
        text = self.doc.render()
        self.assertEqual(parse(text).render(), text)
        return text

    def test_upsert_existing(self):
        self.doc.upsert_key("Qwen3.5-4B", "temp", "0.5")
        self.assertEqual(dict(self.doc.block("Qwen3.5-4B").keys())["temp"], "0.5")
        self._stable()

    def test_upsert_new_key_appends(self):
        self.doc.upsert_key("Qwen3.5-4B", "foo", "bar")
        keys = self.doc.block("Qwen3.5-4B").keys()
        self.assertEqual(keys[-1], ("foo", "bar"))
        text = self._stable()
        self.assertIn("[Qwen3.5-4B]", text)

    def test_remove_key(self):
        self.doc.remove_key("Qwen3.5-9B", "parallel")
        keys = dict(self.doc.block("Qwen3.5-9B").keys())
        self.assertNotIn("parallel", keys)
        self.assertIn("model", keys)
        self._stable()

    def test_remove_key_missing(self):
        with self.assertRaises(ModelsIniError):
            self.doc.remove_key("Qwen3.5-4B", "no-such-key")

    def test_rename(self):
        self.doc.rename_section("Qwen3.5-4B", "Qwen-Mini-4B")
        self.assertEqual(self.doc.block("Qwen-Mini-4B").name, "Qwen-Mini-4B")
        self._stable()

    def test_rename_collision(self):
        with self.assertRaises(ModelsIniError):
            self.doc.rename_section("Qwen3.5-4B", "gemma-4-31B")

    def test_round_trip_idempotent(self):
        self.doc.upsert_key("Qwen3.5-4B", "temp", "0.5")
        self.doc.archive_section("Qwen3.8-27B-low")
        text = self.doc.render()
        self.assertEqual(parse(text).render(), text)


class Sections(unittest.TestCase):
    def setUp(self):
        self.doc = parse(TEXT)

    def test_add_section(self):
        self.doc.add_section("Test-Model", [("model", "/mnt/ssd/x.gguf"), ("temp", "0.7")])
        b = self.doc.block("Test-Model")
        self.assertFalse(b.archived)
        self.assertEqual(b.region, "individual")
        # inserted at the end of the individual area (after its last block)
        last_individual = max(i for i, x in enumerate(self.doc.blocks)
                               if x.region == "individual" and x.name != "Test-Model")
        self.assertEqual(self.doc.blocks.index(b), last_individual + 1)
        first_archived = next(i for i, x in enumerate(self.doc.blocks) if x.archived)
        self.assertLess(self.doc.blocks.index(b), first_archived)
        text = self.doc.render()
        self.assertIn("[Test-Model]\n", text)
        # still exactly one archived marker
        self.assertEqual(text.count("# == ARCHIVED =="), 1)
        self.assertEqual(parse(text).render(), text)
        # original text untouched (mutation worked on parsed copy)
        b2 = parse(TEXT)
        self.assertNotIn("Test-Model", [x.name for x in b2.blocks])

    def test_add_section_region_profiles(self):
        self.doc.add_section("New-Profile", [("model", "/x.gguf")],
                             region="profiles")
        b = self.doc.block("New-Profile")
        idx = self.doc.blocks.index(b)
        self.assertEqual(b.region, "profiles")
        self.assertEqual(self.doc.blocks[idx - 1].region, "profiles")
        self.assertEqual(self.doc.blocks[idx + 1].region, "individual")
        self.assertEqual(parse(self.doc.render()).render(), self.doc.render())

    def test_add_section_collision(self):
        with self.assertRaises(ModelsIniError):
            self.doc.add_section("Qwen3.5-4B", [("model", "x")])

    def test_archive(self):
        idx = self.doc.blocks.index(self.doc.block("Qwen3.8-27B-low"))
        self.doc.archive_section("Qwen3.8-27B-low")
        with self.assertRaises(ModelsIniError):
            self.doc.block("Qwen3.8-27B-low")
        b = self.doc.block("Qwen3.8-27B-low", archived=True)
        # in place: same index, same region (does NOT move to ARCHIVED)
        self.assertEqual(self.doc.blocks.index(b), idx)
        self.assertEqual(b.region, "individual")
        text = self.doc.render()
        self.assertNotIn("\n[Qwen3.8-27B-low]\n", text)
        self.assertIn("# [Qwen3.8-27B-low]\n", text)
        # archived body fully commented
        for line in b.body:
            s = line.strip()
            if s:
                self.assertTrue(s.startswith("#"), line)
        # marker intact, section still sits above it
        self.assertEqual(text.count("# == ARCHIVED =="), 1)
        self.assertLess(text.index("# [Qwen3.8-27B-low]"),
                         text.index("# == ARCHIVED =="))
        self.assertEqual(parse(text).render(), text)

    def test_restore_after_archive_is_byte_noop(self):
        # archive+restore is the exact inverse for any active section
        for name in ("Qwen3-Coder-Next", "Qwen3.8-27B-low", "General-Bot-small"):
            with self.subTest(name=name):
                doc = parse(TEXT)
                doc.archive_section(name)
                doc.restore_section(name)
                self.assertEqual(doc.render(), TEXT)

    def test_restore(self):
        doc = parse(TEXT)
        doc.archive_section("Qwen3.8-27B-low")
        doc.restore_section("Qwen3.8-27B-low")
        text = doc.render()
        b = doc.block("Qwen3.8-27B-low")
        self.assertTrue(b.body)          # keys came back uncommented
        self.assertEqual(dict(b.keys())["temp"], "1.0")
        self.assertEqual(text.count("# == ARCHIVED =="), 1)
        self.assertEqual(parse(text).render(), text)

    def test_delete(self):
        self.doc.delete_section("Qwen3-Coder-Next")
        text = self.doc.render()
        self.assertNotIn("[Qwen3-Coder-Next]", text)
        self.assertIn("# == ARCHIVED ==", text)
        self.assertEqual(parse(text).render(), text)

    def test_delete_archived_marker_owner(self):
        # deleting the first archived block must keep the ARCHIVED marker
        self.doc.delete_section("Muse-Glimmer-30B", archived=True)
        text = self.doc.render()
        self.assertIn("# == ARCHIVED ==", text)
        self.assertNotIn("[Muse-Glimmer-30B]", text)
        self.assertEqual(parse(text).render(), text)

    def test_delete_last_block_marker_dropped(self):
        # deleting the only remaining last block that labels an empty area
        doc = parse(TEXT)
        doc.delete_section("gpt-oss-20b", archived=True)
        text = doc.render()
        self.assertNotIn("[gpt-oss-20b]", text)
        self.assertEqual(parse(text).render(), text)

    def test_unknown_section(self):
        with self.assertRaises(ModelsIniError):
            self.doc.block("no-such-section")
        with self.assertRaises(ModelsIniError):
            self.doc.archive_section("no-such-section")


if __name__ == "__main__":
    unittest.main()
