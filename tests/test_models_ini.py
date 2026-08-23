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


# Single-block regions on both sides of INDIVIDUAL MODELS; the PROFILES
# marker is owned by OnlyProfile's leading (there is a block before it),
# so every delete/relocation edge case around it is exercised.
SINGLE_MID = (
    "version = 1\n"
    "\n"
    "[*]\n"
    "threads = 2\n"
    "\n"
    "# == PROFILES ==\n"
    "\n"
    "[OnlyProfile]\n"
    "model = /x/a.gguf\n"
    "\n"
    "# == INDIVIDUAL MODELS ==\n"
    "\n"
    "[solo]\n"
    "model = /x/b.gguf\n"
    "\n"
    "# == ARCHIVED ==\n"
    "\n"
    "# [old]\n"
    "# model = /x/old.gguf\n"
    "\n"
)

# Same, but with no block before PROFILES: the marker then lives in the
# document header.
SINGLE_FIRST = SINGLE_MID.replace("threads = 2\n\n", "", 1) \
    .replace("[*]\n", "", 1)


class Sections(unittest.TestCase):
    def setUp(self):
        self.doc = parse(TEXT)

    def test_add_section(self):
        self.doc.add_section("Test-Model", [("model", "/mnt/ssd/x.gguf"), ("temp", "0.7")])
        b = self.doc.block("Test-Model")
        self.assertFalse(b.archived)
        self.assertEqual(b.region, "individual")
        # inserted at the bottom of the individual active group
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

    def _canonical(self, doc=None):
        # within every region: active blocks on top, archived below
        doc = doc or self.doc
        seen = {}
        for b in doc.blocks:
            if b.archived:
                seen[b.region] = True
            else:
                self.assertFalse(seen.get(b.region), b.name)

    def test_archive(self):
        self.doc.archive_section("Qwen3.8-27B-low")
        with self.assertRaises(ModelsIniError):
            self.doc.block("Qwen3.8-27B-low")
        b = self.doc.block("Qwen3.8-27B-low", archived=True)
        # sunk to the bottom of the individual region (not into ARCHIVED)
        self.assertEqual(b.region, "individual")
        i = self.doc.blocks.index(b)
        self.assertEqual(self.doc.blocks[i + 1].region, "archived")
        self.assertEqual(self.doc.blocks[i - 1].region, "individual")
        text = self.doc.render()
        self.assertNotIn("\n[Qwen3.8-27B-low]\n", text)
        self.assertIn("# [Qwen3.8-27B-low]\n", text)
        # archived body fully commented
        for line in b.body:
            s = line.strip()
            if s:
                self.assertTrue(s.startswith("#"), line)
        # all region markers intact
        for m in ("# == PROFILES ==", "# == INDIVIDUAL MODELS ==",
                  "# == ARCHIVED =="):
            self.assertEqual(text.count(m), 1)
        self._canonical()
        self.assertEqual(parse(text).render(), text)

    def test_archive_region_head_keeps_marker(self):
        doc = parse(TEXT)
        doc.archive_section("General-Bot-small")  # owns the PROFILES marker
        text = doc.render()
        self.assertEqual(text.count("# == PROFILES =="), 1)
        # marker stays at the region head, above General-Bot-large
        self.assertLess(text.index("# == PROFILES =="),
                         text.index("[General-Bot-large]"))
        # the docs above the section travelled with it
        self.assertLess(
            text.index("; this will be used as the default config for that model"),
            text.index("# [General-Bot-small]"))
        # and it sits at the bottom of profiles, above INDIVIDUAL MODELS
        self.assertLess(text.index("# [General-Bot-small]"),
                         text.index("# == INDIVIDUAL MODELS =="))
        self._canonical(doc)
        self.assertEqual(parse(text).render(), text)

    def test_archive_restore_converges(self):
        # archive+restore keeps content and the canonical layout: the
        # section ends up at the bottom of its region's active group
        for name in ("Qwen3.8-27B-low", "Qwen3-Coder-Next",
                     "General-Bot-small"):
            with self.subTest(name=name):
                doc = parse(TEXT)
                doc.archive_section(name)
                doc.restore_section(name)
                b = doc.block(name)
                nxt = doc.blocks[doc.blocks.index(b) + 1]
                self.assertTrue(
                    nxt.archived or nxt.region != b.region, name)
                self._canonical(doc)
                self.assertEqual(parse(doc.render()).render(), doc.render())

    def test_restore_moves_above_first_archived(self):
        doc = parse(TEXT)
        doc.archive_section("Coding-Bot")        # last profile: at region end
        doc.archive_section("Reasoning-Bot")     # sinks below Coding-Bot
        doc.restore_section("Reasoning-Bot")     # raises above Coding-Bot
        i_rb = doc.blocks.index(doc.block("Reasoning-Bot"))
        i_cb = doc.blocks.index(doc.block("Coding-Bot", archived=True))
        self.assertLess(i_rb, i_cb)
        self._canonical(doc)
        self.assertEqual(parse(doc.render()).render(), doc.render())

    def test_restore_after_region_head_archive(self):
        doc = parse(TEXT)
        doc.archive_section("General-Bot-small")
        doc.restore_section("General-Bot-small")
        text = doc.render()
        self.assertEqual(text.count("# == PROFILES =="), 1)
        self.assertLess(text.index("# == PROFILES =="),
                         text.index("[General-Bot-large]"))
        self.assertEqual(parse(text).render(), text)

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

    def test_delete_last_block_of_nonfinal_region_drops_marker(self):
        # OnlyProfile is the region head (owns the PROFILES marker) and
        # the region's last block: deleting it empties the region, so the
        # marker must be dropped, NOT migrated onto the next region's head.
        doc = parse(SINGLE_MID)
        doc.delete_section("OnlyProfile")
        text = doc.render()
        self.assertNotIn("# == PROFILES ==", text)
        self.assertEqual(text.count("# == INDIVIDUAL MODELS =="), 1)
        self.assertEqual(text.count("# == ARCHIVED =="), 1)
        reparsed = parse(text)
        self.assertEqual(reparsed.block("solo").region, "individual")
        self.assertEqual(reparsed.block("old", archived=True).region,
                         "archived")
        self.assertEqual(parse(text).render(), text)

    def test_delete_region_head_same_region_keeps_marker(self):
        # GBS owns the PROFILES marker but the region keeps other blocks:
        # General-Bot-large becomes the region head and must take the
        # marker, above it, with its seam blank intact.
        doc = parse(TEXT)
        doc.delete_section("General-Bot-small")
        text = doc.render()
        self.assertEqual(text.count("# == PROFILES =="), 1)
        gbl = parse(text).block("General-Bot-large")
        self.assertEqual(gbl.region, "profiles")
        self.assertEqual(gbl.leading[0], "\n")
        self.assertEqual(gbl.leading[1], "# == PROFILES ==\n")
        self.assertLess(text.index("# == PROFILES =="),
                        text.index("[General-Bot-large]"))
        self.assertEqual(parse(text).render(), text)

    def test_delete_first_block_header_marker_dropped(self):
        # No block before PROFILES: the marker lives in the document
        # header and must be dropped too when the region empties.
        doc = parse(SINGLE_FIRST)
        self.assertEqual(doc.block("OnlyProfile").region, "profiles")
        doc.delete_section("OnlyProfile")
        text = doc.render()
        self.assertNotIn("# == PROFILES ==", text)
        self.assertEqual(parse(text).block("solo").region, "individual")
        self.assertEqual(parse(text).render(), text)

    def test_archive_restore_single_block_region_noop_pos(self):
        # single-block region: archive/restore is pos-keeping (no move),
        # so the marker never moves and the file is byte-identical.
        doc = parse(SINGLE_MID)
        doc.archive_section("OnlyProfile")
        doc.restore_section("OnlyProfile")
        self.assertFalse(doc.block("OnlyProfile").archived)
        self.assertEqual(doc.render(), SINGLE_MID)

    def test_unknown_section(self):
        with self.assertRaises(ModelsIniError):
            self.doc.block("no-such-section")
        with self.assertRaises(ModelsIniError):
            self.doc.archive_section("no-such-section")


if __name__ == "__main__":
    unittest.main()
