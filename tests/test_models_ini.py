from __future__ import annotations

import unittest
from pathlib import Path

from models_ini import ModelsIniError, parse

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "models.ini"
TEXT = FIXTURE.read_text(encoding="utf-8")

# Paired layout: PROFILES / ARCHIVED PROFILES / MODELS / ARCHIVED MODELS.
# The ARCHIVED PROFILES region is non-empty in the fixture (Coding-Bot),
# so both marker-handing and anchor cases are covered.


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
        self.assertEqual(len(active), 21)     # [*] + 3 profiles + 17 models
        self.assertEqual(len(archived), 18)   # 1 profile + 17 models

        self.assertEqual(doc.block("*").region, "global")
        self.assertEqual(doc.block("General-Bot-small").region, "profiles")
        self.assertEqual(doc.block("Reasoning-Bot").region, "profiles")
        self.assertEqual(
            doc.block("Coding-Bot", archived=True).region, "archived_profiles")
        self.assertEqual(doc.block("Qwen3.5-4B").region, "models")
        self.assertEqual(
            doc.block("Muse-Glimmer-30B", archived=True).region,
            "archived_models")
        self.assertEqual(
            doc.block("Qwen3-Coder-30B", archived=True).region,
            "archived_models")
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
        # Coding-Bot is archived, so it no longer participates
        self.assertEqual(
            sorted(doc.group_aliases()[qwen]),
            sorted(["Reasoning-Bot", "Qwen3.8-27B-xhigh",
                    "Qwen3.8-27B-medium", "Qwen3.8-27B-low",
                    "Qwen3.8-27B-Code"]),
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

    def test_rename_collision_archived(self):
        # name exists only in ARCHIVED MODELS — must not create a duplicate
        with self.assertRaises(ModelsIniError):
            self.doc.rename_section("Qwen3.5-4B", "gemma-4-31B-Instruct")
        self._stable()

    def test_rename_archived(self):
        # [Coding-Bot] exists only in ARCHIVED PROFILES
        self.doc.rename_section("Coding-Bot", "Coding-Bot-x", archived=True)
        self.assertEqual(
            self.doc.block("Coding-Bot-x", archived=True).name,
            "Coding-Bot-x")
        # the '#' header prefix must survive, or reparse reclassifies the
        # section as active with all its keys still commented out
        text = self.doc.render()
        self.assertIn("# [Coding-Bot-x]", text)
        self.assertTrue(parse(text).block("Coding-Bot-x", archived=True)
                        .archived)
        self._stable()

    def test_upsert_archived_existing_updates_commented_in_place(self):
        self.doc.upsert_key("gemma-4-26B", "image-min-tokens", "301",
                            archived=True)
        lines = self.doc.render().splitlines()
        self.assertIn("# image-min-tokens = 301", lines)
        # no uncommented duplicate leaking into the file (a real INI parser
        # would attribute it to the active section above)
        self.assertNotIn("image-min-tokens = 301", lines)
        self.assertEqual(len([l for l in lines
                              if "image-min-tokens" in l]), 1)
        keys = dict(self.doc.block("gemma-4-26B", archived=True).keys())
        self.assertEqual(keys["image-min-tokens"], "301")
        self._stable()

    def test_upsert_archived_new_key_stays_commented(self):
        self.doc.upsert_key("gemma-4-26B", "zz_test", "1", archived=True)
        lines = self.doc.render().splitlines()
        self.assertIn("# zz_test = 1", lines)
        self.assertNotIn("zz_test = 1", lines)
        self._stable()

    def test_remove_key_archived_existing(self):
        self.doc.remove_key("gemma-4-26B", "image-min-tokens",
                            archived=True)
        keys = dict(self.doc.block("gemma-4-26B", archived=True).keys())
        self.assertNotIn("image-min-tokens", keys)
        self._stable()

    def test_rename_collision_from_archived(self):
        with self.assertRaises(ModelsIniError):
            self.doc.rename_section("Coding-Bot", "Qwen3.5-4B", archived=True)
        self._stable()

    def test_upsert_archived_twin_isolated(self):
        self.doc.upsert_key("gemma-4-26B", "zz_test", "1", archived=True)
        active = self.doc.block("gemma-4-26B")
        archived = self.doc.block("gemma-4-26B", archived=True)
        self.assertNotIn("zz_test", dict(active.keys()))
        self.assertEqual(dict(archived.keys())["zz_test"], "1")
        self._stable()

    def test_remove_key_archived(self):
        self.doc.upsert_key("gemma-4-26B", "zz_test", "1", archived=True)
        self.doc.remove_key("gemma-4-26B", "zz_test", archived=True)
        self.assertNotIn(
            "zz_test",
            dict(self.doc.block("gemma-4-26B", archived=True).keys()))
        self._stable()

    def test_round_trip_idempotent(self):
        self.doc.upsert_key("Qwen3.5-4B", "temp", "0.5")
        self.doc.archive_section("Qwen3.8-27B-low")
        text = self.doc.render()
        self.assertEqual(parse(text).render(), text)


# Single-block regions on both sides of MODELS; the PROFILES marker is
# owned by OnlyProfile's leading (there is a block before it), so every
# delete/move edge case around it is exercised.
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
    "# == ARCHIVED PROFILES ==\n"
    "\n"
    "# == MODELS ==\n"
    "\n"
    "[solo]\n"
    "model = /x/b.gguf\n"
    "\n"
    "# == ARCHIVED MODELS ==\n"
    "\n"
    "# [old]\n"
    "# model = /x/old.gguf\n"
    "\n"
)

# Same, but with no block before PROFILES: the marker then lives in the
# document header.
SINGLE_FIRST = SINGLE_MID.replace("threads = 2\n\n", "", 1) \
    .replace("[*]\n", "", 1)


def _between(text: str, a: str, b: str) -> str:
    """Lines between two markers, excluding the markers themselves."""
    return text[text.index(a) + len(a):text.index(b)]


class Sections(unittest.TestCase):
    def setUp(self):
        self.doc = parse(TEXT)

    def _canonical(self, doc=None):
        # regions are homogeneous: active regions hold only active blocks,
        # archived regions only archived ones
        for b in (doc or self.doc).blocks:
            if b.region in ("profiles", "models"):
                self.assertFalse(b.archived, b.name)
            elif b.region in ("archived_profiles", "archived_models"):
                self.assertTrue(b.archived, b.name)

    # ── add ───────────────────────────────────────────────────

    def test_add_section(self):
        self.doc.add_section("Test-Model",
                             [("model", "/mnt/ssd/x.gguf"), ("temp", "0.7")])
        b = self.doc.block("Test-Model")
        self.assertFalse(b.archived)
        self.assertEqual(b.region, "models")
        # appended at the bottom of the models region
        last_model = max(i for i, x in enumerate(self.doc.blocks)
                         if x.region == "models" and x.name != "Test-Model")
        self.assertEqual(self.doc.blocks.index(b), last_model + 1)
        self.assertEqual(self.doc.blocks[self.doc.blocks.index(b) + 1].region,
                         "archived_models")
        text = self.doc.render()
        self.assertIn("[Test-Model]\n", text)
        # all region markers intact
        for m in ("# == PROFILES ==", "# == ARCHIVED PROFILES ==",
                  "# == MODELS ==", "# == ARCHIVED MODELS =="):
            self.assertEqual(text.count(m), 1)
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
        self.assertEqual(self.doc.blocks[idx + 1].region,
                         "archived_profiles")
        self.assertEqual(parse(self.doc.render()).render(), self.doc.render())

    def test_add_section_invalid_region(self):
        with self.assertRaises(ModelsIniError):
            self.doc.add_section("X", [("model", "/x")], region="global")
        with self.assertRaises(ModelsIniError):
            self.doc.add_section("X", [("model", "/x")],
                                 region="archived_models")

    def test_add_section_collision(self):
        with self.assertRaises(ModelsIniError):
            self.doc.add_section("Qwen3.5-4B", [("model", "x")])

    def test_add_section_into_emptied_pair(self):
        # both markers of the models pair were dropped when the pair was
        # emptied; adding a model re-creates them
        doc = parse(SINGLE_MID)
        doc.delete_section("solo")
        doc.delete_section("old", archived=True)
        doc.add_section("New-Model", [("model", "/x/n.gguf")], region="models")
        text = doc.render()
        self.assertEqual(text.count("# == MODELS =="), 1)
        self.assertEqual(parse(text).block("New-Model").region, "models")
        self.assertEqual(parse(text).render(), text)

    def test_add_section_into_empty_active_region(self):
        # profiles pair empty (anchors gone) but MODELS marker present:
        # the PROFILES marker is re-created above it
        doc = parse(SINGLE_MID)
        doc.delete_section("OnlyProfile")
        doc.add_section("New-Profile", [("model", "/x/n.gguf")],
                        region="profiles")
        text = doc.render()
        self.assertEqual(text.count("# == PROFILES =="), 1)
        self.assertEqual(parse(text).block("New-Profile").region, "profiles")
        self.assertLess(text.index("[New-Profile]"), text.index("[solo]"))
        self.assertEqual(parse(text).render(), text)

    # ── archive / restore ─────────────────────────────────────

    def test_archive_model_sinks_to_twin(self):
        self.doc.archive_section("Qwen3.8-27B-low")
        with self.assertRaises(ModelsIniError):
            self.doc.block("Qwen3.8-27B-low")
        b = self.doc.block("Qwen3.8-27B-low", archived=True)
        # moved to the bottom of ARCHIVED MODELS (below gpt-oss-20b)
        self.assertEqual(b.region, "archived_models")
        i = self.doc.blocks.index(b)
        self.assertEqual(i, len(self.doc.blocks) - 1)
        text = self.doc.render()
        self.assertNotIn("\n[Qwen3.8-27B-low]\n", text)
        self.assertIn("# [Qwen3.8-27B-low]\n", text)
        # archived body fully commented
        for line in b.body:
            s = line.strip()
            if s:
                self.assertTrue(s.startswith("#"), line)
        # all region markers intact
        for m in ("# == PROFILES ==", "# == ARCHIVED PROFILES ==",
                  "# == MODELS ==", "# == ARCHIVED MODELS =="):
            self.assertEqual(text.count(m), 1)
        self._canonical()
        self.assertEqual(parse(text).render(), text)

    def test_archive_profile_sinks_to_twin(self):
        self.doc.archive_section("General-Bot-small")
        b = self.doc.block("General-Bot-small", archived=True)
        self.assertEqual(b.region, "archived_profiles")
        # sunk below the existing archived profile
        i = self.doc.blocks.index(b)
        i_cb = self.doc.blocks.index(
            self.doc.block("Coding-Bot", archived=True))
        self.assertGreater(i, i_cb)
        text = self.doc.render()
        # PROFILES marker handed to the new region head
        gbl = parse(text).block("General-Bot-large")
        self.assertEqual(gbl.region, "profiles")
        self.assertEqual(gbl.leading[1], "# == PROFILES ==\n")
        # the docs above the section travelled with it
        self.assertLess(
            text.index("; this will be used as the default config for that model"),
            text.index("# [General-Bot-small]"))
        self._canonical()
        self.assertEqual(parse(text).render(), text)

    def test_archive_last_active_profile_anchors_marker(self):
        # archiving the last active profile empties PROFILES: its marker
        # stays as an anchor directly above the ARCHIVED PROFILES marker
        doc = parse(SINGLE_MID)
        doc.archive_section("OnlyProfile")
        text = doc.render()
        self.assertEqual(text.count("# == PROFILES =="), 1)
        self.assertEqual(text.count("# == ARCHIVED PROFILES =="), 1)
        seg = _between(text, "# == PROFILES ==\n", "# == ARCHIVED PROFILES ==\n")
        self.assertNotIn("[", seg)
        b = doc.block("OnlyProfile", archived=True)
        self.assertEqual(b.region, "archived_profiles")
        self._canonical(doc)
        self.assertEqual(parse(text).render(), text)

    def test_archive_global_rejected(self):
        with self.assertRaises(ModelsIniError):
            self.doc.archive_section("*")

    def test_restore_model_raises_to_active_twin(self):
        doc = parse(TEXT)
        doc.restore_section("gpt-oss-20b")
        text = doc.render()
        b = doc.block("gpt-oss-20b")
        self.assertFalse(b.archived)
        self.assertEqual(b.region, "models")
        # raised to the bottom of the models region
        self.assertTrue(b.body)
        self.assertEqual(dict(b.keys())["parallel"], "2")
        i = doc.blocks.index(b)
        self.assertEqual(doc.blocks[i + 1].region, "archived_models")
        # ARCHIVED MODELS now empty: marker anchored at end of file
        self.assertEqual(text.count("# == ARCHIVED MODELS =="), 1)
        self.assertGreater(text.index("# == ARCHIVED MODELS =="),
                           text.index("[gpt-oss-20b]"))
        self._canonical(doc)
        self.assertEqual(parse(text).render(), text)

    def test_restore_profile_raises_to_active_twin(self):
        doc = parse(TEXT)
        doc.restore_section("Coding-Bot")
        text = doc.render()
        b = doc.block("Coding-Bot")
        self.assertFalse(b.archived)
        self.assertEqual(b.region, "profiles")
        i = doc.blocks.index(b)
        self.assertEqual(doc.blocks[i + 1].region, "models")
        # ARCHIVED PROFILES now empty: marker anchored right above MODELS
        self.assertEqual(text.count("# == ARCHIVED PROFILES =="), 1)
        seg = _between(text, "# == ARCHIVED PROFILES ==\n",
                       "# == MODELS ==\n")
        self.assertNotIn("[", seg)
        self._canonical(doc)
        self.assertEqual(parse(text).render(), text)

    def test_archive_restore_converges(self):
        # archive+restore keeps content and the paired layout: the
        # section ends up at the bottom of its active region
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

    # ── delete ────────────────────────────────────────────────

    def test_delete(self):
        self.doc.delete_section("Qwen3-Coder-Next")
        text = self.doc.render()
        self.assertNotIn("[Qwen3-Coder-Next]", text)
        self.assertIn("# == ARCHIVED MODELS ==", text)
        self.assertEqual(parse(text).render(), text)

    def test_delete_archived_marker_owner(self):
        # deleting the first archived block must keep the marker on the
        # new region head
        self.doc.delete_section("Muse-Glimmer-30B", archived=True)
        text = self.doc.render()
        self.assertIn("# == ARCHIVED MODELS ==", text)
        self.assertNotIn("[Muse-Glimmer-30B]", text)
        head = parse(text).block("gemma-4-31B-Instruct", archived=True)
        self.assertEqual(head.leading[1], "# == ARCHIVED MODELS ==\n")
        self.assertEqual(parse(text).render(), text)

    def test_delete_last_block_marker_kept(self):
        # deleting the last block of a non-empty pair keeps the region
        # marker (still owned by the region head)
        doc = parse(TEXT)
        doc.delete_section("gpt-oss-20b", archived=True)
        text = doc.render()
        self.assertNotIn("[gpt-oss-20b]", text)
        self.assertEqual(text.count("# == ARCHIVED MODELS =="), 1)
        self._canonical(doc)
        self.assertEqual(parse(text).render(), text)

    def test_delete_last_block_of_nonfinal_region_drops_pair(self):
        # OnlyProfile is the only block of its pair: deleting it empties
        # both regions, so BOTH markers must be dropped, not migrated onto
        # the next region's head.
        doc = parse(SINGLE_MID)
        doc.delete_section("OnlyProfile")
        text = doc.render()
        self.assertNotIn("# == PROFILES ==", text)
        self.assertNotIn("# == ARCHIVED PROFILES ==", text)
        self.assertEqual(text.count("# == MODELS =="), 1)
        self.assertEqual(text.count("# == ARCHIVED MODELS =="), 1)
        reparsed = parse(text)
        self.assertEqual(reparsed.block("solo").region, "models")
        self.assertEqual(reparsed.block("old", archived=True).region,
                         "archived_models")
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

    def test_delete_all_active_profiles_keeps_twin(self):
        # emptying the active side of the pair keeps both markers as
        # adjacent anchors above the surviving archived blocks
        doc = parse(TEXT)
        for name in ("Reasoning-Bot", "General-Bot-large",
                     "General-Bot-small"):
            doc.delete_section(name)
        text = doc.render()
        self.assertEqual(text.count("# == PROFILES =="), 1)
        self.assertEqual(text.count("# == ARCHIVED PROFILES =="), 1)
        seg = _between(text, "# == PROFILES ==\n",
                       "# == ARCHIVED PROFILES ==\n")
        self.assertNotIn("[", seg)
        self.assertEqual(
            parse(text).block("Coding-Bot", archived=True).region,
            "archived_profiles")
        self.assertEqual(parse(text).render(), text)

    def test_delete_first_block_header_marker_dropped(self):
        # No block before PROFILES: the marker lives in the document
        # header and must be dropped too when the pair empties.
        doc = parse(SINGLE_FIRST)
        self.assertEqual(doc.block("OnlyProfile").region, "profiles")
        doc.delete_section("OnlyProfile")
        text = doc.render()
        self.assertNotIn("# == PROFILES ==", text)
        self.assertNotIn("# == ARCHIVED PROFILES ==", text)
        self.assertEqual(parse(text).block("solo").region, "models")
        self.assertEqual(parse(text).render(), text)

    def test_unknown_section(self):
        with self.assertRaises(ModelsIniError):
            self.doc.block("no-such-section")
        with self.assertRaises(ModelsIniError):
            self.doc.archive_section("no-such-section")


# PROFILES region sits at the top of the file, so its marker lives in
# the document header; two-block regions exercise every head-change path.
TINY = (
    "# == PROFILES ==\n"
    "\n"
    "[Alpha]\n"
    "model = /x/a.gguf\n"
    "\n"
    "[Beta]\n"
    "model = /x/b.gguf\n"
    "\n"
    "# == ARCHIVED PROFILES ==\n"
    "\n"
    "# == MODELS ==\n"
    "\n"
    "[Solo]\n"
    "model = /x/s.gguf\n"
    "\n"
    "# == ARCHIVED MODELS ==\n"
    "\n"
)


class MoveSection(unittest.TestCase):
    def setUp(self):
        self.doc = parse(TEXT)

    def _names(self):
        return [b.name for b in self.doc.blocks]

    def _stable(self):
        text = self.doc.render()
        self.assertEqual(parse(text).render(), text)
        for m in ("# == PROFILES ==", "# == ARCHIVED PROFILES ==",
                  "# == MODELS ==", "# == ARCHIVED MODELS =="):
            self.assertEqual(text.count(m), 1)
        return text

    def test_move_middle_block_down(self):
        self.doc.move_section("gemma-4-31B-Code", "gemma-4-26B", "after")
        names = self._names()
        self.assertLess(names.index("gemma-4-26B"),
                        names.index("gemma-4-31B-Code"))
        self.assertEqual(names.index("gemma-4-31B-Code"),
                         names.index("gemma-4-26B") + 1)
        text = self._stable()
        reparsed = parse(text)
        self.assertEqual(reparsed.block("gemma-4-31B-Code").region, "models")
        self.assertEqual(reparsed.block("gemma-4-26B").region, "models")

    def test_move_middle_block_up(self):
        self.doc.move_section("gemma-4-26B", "gemma-4-31B", "before")
        names = self._names()
        self.assertEqual(names.index("gemma-4-26B"),
                         names.index("gemma-4-31B") - 1)
        text = self._stable()
        self.assertEqual(parse(text).block("gemma-4-26B").region, "models")

    def test_move_noop_current_slot(self):
        # gemma-4-31B-Code already sits directly before gemma-4-26B
        self.doc.move_section("gemma-4-31B-Code", "gemma-4-26B", "before")
        self.assertEqual(self.doc.render(), TEXT)
        self.doc2 = parse(TEXT)
        self.doc2.move_section("gemma-4-26B", "gemma-4-31B-Code", "after")
        self.assertEqual(self.doc2.render(), TEXT)

    def test_move_self_noop(self):
        self.doc.move_section("Qwen3.5-4B", "Qwen3.5-4B", "after")
        self.assertEqual(self.doc.render(), TEXT)

    def test_move_region_head_marker_handoff(self):
        # General-Bot-small owns the PROFILES marker; moving it off the
        # top hands the marker to General-Bot-large, and the doc comments
        # above the section travel with it
        self.doc.move_section("General-Bot-small", "Reasoning-Bot", "after")
        text = self._stable()
        names = self._names()
        self.assertEqual(names.index("General-Bot-small"),
                         names.index("Reasoning-Bot") + 1)
        reparsed = parse(text)
        gbl = reparsed.block("General-Bot-large")
        self.assertEqual(gbl.region, "profiles")
        self.assertEqual(gbl.leading[1], "# == PROFILES ==\n")
        gbs = reparsed.block("General-Bot-small")
        self.assertEqual(gbs.region, "profiles")
        self.assertNotIn("# == PROFILES ==\n", gbs.leading)
        cmt = "; this will be used as the default config for that model"
        self.assertLess(text.index("[Reasoning-Bot]"), text.index(cmt))
        self.assertLess(text.index(cmt), text.index("[General-Bot-small]"))

    def test_move_before_head_takes_marker(self):
        # gemma-4-31B-Code jumps ahead of the region head and must take
        # the MODELS marker with it
        self.doc.move_section("gemma-4-31B-Code", "gemma-4-31B", "before")
        text = self._stable()
        reparsed = parse(text)
        gc = reparsed.block("gemma-4-31B-Code")
        self.assertEqual(gc.region, "models")
        self.assertEqual(gc.leading[1], "# == MODELS ==\n")
        g31 = reparsed.block("gemma-4-31B")
        self.assertEqual(g31.region, "models")
        self.assertNotIn("# == MODELS ==\n", g31.leading)
        names = [b.name for b in reparsed.blocks]
        self.assertEqual(names.index("gemma-4-31B-Code"),
                         names.index("gemma-4-31B") - 1)

    def test_move_archived_head_marker_handoff(self):
        # Muse-Glimmer-30B owns the ARCHIVED MODELS marker; moving it
        # down hands the marker to gemma-4-31B-Instruct
        self.doc.move_section("Muse-Glimmer-30B", "gemma-4-31B-Instruct",
                              "after", archived=True)
        text = self._stable()
        reparsed = parse(text)
        gi = reparsed.block("gemma-4-31B-Instruct", archived=True)
        self.assertEqual(gi.region, "archived_models")
        self.assertEqual(gi.leading[1], "# == ARCHIVED MODELS ==\n")
        mg = reparsed.block("Muse-Glimmer-30B", archived=True)
        self.assertEqual(mg.region, "archived_models")
        names = [b.name for b in reparsed.blocks]
        self.assertEqual(names.index("Muse-Glimmer-30B"),
                         names.index("gemma-4-31B-Instruct") + 1)

    def test_move_first_region_header_marker(self):
        # marker lives in the document header; Beta moves to the top and
        # takes it, Alpha (formerly first) gains a seam blank
        doc = parse(TINY)
        doc.move_section("Beta", "Alpha", "before")
        text = doc.render()
        self.assertEqual(text.count("# == PROFILES =="), 1)
        self.assertFalse(text.startswith("\n"))
        reparsed = parse(text)
        self.assertEqual(reparsed.block("Beta").region, "profiles")
        self.assertEqual(reparsed.block("Alpha").region, "profiles")
        self.assertEqual(reparsed.block("Solo").region, "models")
        # a file-first block never owns the marker: it stays in the header
        self.assertIn("# == PROFILES ==\n", reparsed.header)
        self.assertEqual(reparsed.block("Beta").leading, [])
        self.assertEqual(reparsed.block("Alpha").leading[0], "\n")
        self.assertEqual(parse(text).render(), text)

    def test_move_two_block_swap_header_marker(self):
        # "Alpha after Beta" in a two-block top region: Beta becomes the
        # head, taking the marker out of the document header
        doc = parse(TINY)
        doc.move_section("Alpha", "Beta", "after")
        text = doc.render()
        self.assertEqual(text.count("# == PROFILES =="), 1)
        self.assertFalse(text.startswith("\n"))
        reparsed = parse(text)
        # a file-first block never owns the marker: it stays in the header
        self.assertIn("# == PROFILES ==\n", reparsed.header)
        self.assertEqual(reparsed.block("Beta").leading, [])
        self.assertEqual(reparsed.block("Beta").region, "profiles")
        self.assertEqual(reparsed.block("Alpha").region, "profiles")
        names = [b.name for b in reparsed.blocks]
        self.assertEqual(names.index("Alpha"), names.index("Beta") + 1)
        self.assertEqual(parse(text).render(), text)

    def test_move_two_block_noop(self):
        doc = parse(TINY)
        doc.move_section("Beta", "Alpha", "after")     # already there
        self.assertEqual(doc.render(), TINY)

    def test_move_cross_region_rejected(self):
        with self.assertRaises(ModelsIniError):
            self.doc.move_section("Qwen3.5-4B", "General-Bot-small",
                                  "after")
        with self.assertRaises(ModelsIniError):
            self.doc.move_section("General-Bot-small", "Qwen3.5-4B",
                                  "before")

    def test_move_invalid_args(self):
        with self.assertRaises(ModelsIniError):
            self.doc.move_section("Qwen3.5-4B", "gemma-4-31B", "above")
        with self.assertRaises(ModelsIniError):
            self.doc.move_section("no-such", "gemma-4-31B", "after")
        with self.assertRaises(ModelsIniError):
            self.doc.move_section("Qwen3.5-4B", "no-such", "after")


if __name__ == "__main__":
    unittest.main()
