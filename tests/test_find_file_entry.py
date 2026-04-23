"""Regression tests for :func:`core.pamt_parser.find_file_entry`.

Before v1.22.6 this function was defined twice in the same module
— the second definition silently shadowed the first, which
dropped the basename-matching behaviour and broke Ship-to-App for
every language's paloc file ("'localizationstring_eng.paloc' not
in PAMT").

These tests pin down the full canonical-lookup contract so the
bug cannot reappear regardless of which caller-side form they
pass (bare basename, full path, mixed case, Windows slashes).
"""

from __future__ import annotations

import os
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.pamt_parser import (   # noqa: E402
    PamtData,
    PamtFileEntry,
    find_file_entry,
)


def _entry(path: str) -> PamtFileEntry:
    return PamtFileEntry(
        path=path, paz_file="", offset=0, comp_size=0,
        orig_size=0, flags=0, paz_index=0, record_offset=0,
    )


def _pamt(paths: list[str]) -> PamtData:
    return PamtData(
        path="", self_crc=0, paz_count=0, paz_table=[],
        file_entries=[_entry(p) for p in paths],
        folder_prefix="",
    )


class ModuleHasSingleDefinition(unittest.TestCase):
    """Guard against the shadowing bug re-appearing. If someone adds
    a second ``def find_file_entry`` to the module, the import-level
    identity check will fail."""

    def test_only_one_definition_in_module(self):
        import core.pamt_parser as mod
        import inspect
        src = inspect.getsource(mod)
        # Count function definition headers. There must be exactly one.
        count = src.count("\ndef find_file_entry(")
        self.assertEqual(
            count, 1,
            f"find_file_entry defined {count} times — second shadows "
            f"first (pre-v1.22.6 regression).",
        )


class BareBasenameLookup(unittest.TestCase):
    """Every shipping language's paloc must resolve from its bare
    basename form (which is what LANG_TO_PALOC in the Ship-to-App
    dialog generates)."""

    def setUp(self):
        self.pamt = _pamt([
            "localizationstring/localizationstring_eng.paloc",
            "localizationstring/localizationstring_kor.paloc",
            "localizationstring/localizationstring_jpn.paloc",
            "localizationstring/localizationstring_rus.paloc",
            "localizationstring/localizationstring_tur.paloc",
            "localizationstring/localizationstring_spa-es.paloc",
            "localizationstring/localizationstring_spa-mx.paloc",
            "localizationstring/localizationstring_fre.paloc",
            "localizationstring/localizationstring_ger.paloc",
            "localizationstring/localizationstring_ita.paloc",
            "localizationstring/localizationstring_pol.paloc",
            "localizationstring/localizationstring_por-br.paloc",
            "localizationstring/localizationstring_zho-tw.paloc",
            "localizationstring/localizationstring_zho-cn.paloc",
            "localizationstring/localizationstring_tha.paloc",
            "localizationstring/localizationstring_vie.paloc",
            "localizationstring/localizationstring_ara.paloc",
        ])

    def test_english_bare_basename(self):
        e = find_file_entry(self.pamt, "localizationstring_eng.paloc")
        self.assertIsNotNone(e)
        self.assertIn("_eng.paloc", e.path)

    def test_korean_bare_basename(self):
        e = find_file_entry(self.pamt, "localizationstring_kor.paloc")
        self.assertIsNotNone(e)
        self.assertIn("_kor.paloc", e.path)

    def test_arabic_bare_basename(self):
        # Arabic is the specific failure case the reporter hit.
        e = find_file_entry(self.pamt, "localizationstring_ara.paloc")
        self.assertIsNotNone(e)

    def test_chinese_simplified_bare_basename(self):
        # Hyphenated language codes are the edge case most likely to
        # trip naive string matching.
        e = find_file_entry(self.pamt, "localizationstring_zho-cn.paloc")
        self.assertIsNotNone(e)

    def test_spanish_mexico_bare_basename(self):
        e = find_file_entry(self.pamt, "localizationstring_spa-mx.paloc")
        self.assertIsNotNone(e)

    def test_portuguese_brazil_bare_basename(self):
        e = find_file_entry(self.pamt, "localizationstring_por-br.paloc")
        self.assertIsNotNone(e)

    def test_every_shipping_language(self):
        """Parametric guard — every one of the 17 shipping languages
        must resolve from its bare basename."""
        codes = [
            "eng", "kor", "jpn", "rus", "tur", "spa-es", "spa-mx",
            "fre", "ger", "ita", "pol", "por-br", "zho-tw", "zho-cn",
            "tha", "vie", "ara",
        ]
        for code in codes:
            with self.subTest(code=code):
                filename = f"localizationstring_{code}.paloc"
                e = find_file_entry(self.pamt, filename)
                self.assertIsNotNone(
                    e, f"failed to resolve bare basename for {code!r}",
                )


class FullPathLookup(unittest.TestCase):
    def setUp(self):
        self.pamt = _pamt([
            "localizationstring/localizationstring_eng.paloc",
            "sound/pc/en/voice.wem",
            "gamedata/iteminfo.pabgb",
        ])

    def test_exact_full_path(self):
        e = find_file_entry(
            self.pamt, "localizationstring/localizationstring_eng.paloc",
        )
        self.assertIsNotNone(e)

    def test_full_path_uppercase(self):
        e = find_file_entry(
            self.pamt, "LOCALIZATIONSTRING/LOCALIZATIONSTRING_ENG.PALOC",
        )
        self.assertIsNotNone(e)

    def test_full_path_windows_slashes(self):
        e = find_file_entry(
            self.pamt, "localizationstring\\localizationstring_eng.paloc",
        )
        self.assertIsNotNone(e)

    def test_full_path_mixed_case(self):
        e = find_file_entry(
            self.pamt, "LocalizationString/LocalizationString_ENG.paloc",
        )
        self.assertIsNotNone(e)

    def test_sound_full_path(self):
        e = find_file_entry(self.pamt, "sound/pc/en/voice.wem")
        self.assertIsNotNone(e)

    def test_sound_basename_only(self):
        # Non-paloc files also work via basename match.
        e = find_file_entry(self.pamt, "voice.wem")
        self.assertIsNotNone(e)

    def test_gamedata_full_path(self):
        e = find_file_entry(self.pamt, "gamedata/iteminfo.pabgb")
        self.assertIsNotNone(e)


class MissingAndInvalidInputs(unittest.TestCase):
    def setUp(self):
        self.pamt = _pamt([
            "localizationstring/localizationstring_eng.paloc",
        ])

    def test_missing_file_returns_none(self):
        self.assertIsNone(find_file_entry(self.pamt, "nonexistent.paloc"))

    def test_empty_filename_returns_none(self):
        self.assertIsNone(find_file_entry(self.pamt, ""))

    def test_empty_pamt_returns_none(self):
        empty = _pamt([])
        self.assertIsNone(find_file_entry(empty, "anything.paloc"))

    def test_wrong_basename_returns_none(self):
        # A filename whose basename doesn't exist anywhere in the PAMT.
        self.assertIsNone(find_file_entry(self.pamt, "foo/bar/baz.wem"))


class AmbiguousBasenames(unittest.TestCase):
    """When two entries share a basename, the first-declared wins.

    This is a Pearl Abyss convention: paloc / paz / pamt files are
    unique by basename within a group's PAMT. Cross-folder basename
    collisions do not occur in shipping archives, but if a test
    fixture creates one we document the behaviour."""

    def test_duplicate_basenames_returns_first(self):
        pamt = _pamt([
            "a/shared.wem",
            "b/shared.wem",
        ])
        e = find_file_entry(pamt, "shared.wem")
        self.assertIsNotNone(e)
        self.assertEqual(e.path, "a/shared.wem")

    def test_exact_path_beats_basename(self):
        """When the caller provides a full path that EXACTLY matches
        one entry, they get that entry even if another entry shares
        the basename."""
        pamt = _pamt([
            "a/shared.wem",
            "b/shared.wem",
        ])
        e = find_file_entry(pamt, "b/shared.wem")
        self.assertEqual(e.path, "b/shared.wem")


if __name__ == "__main__":
    unittest.main()
