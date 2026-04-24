"""Regression tests for the April-2026 game-patch extension rename.

The game's April-2026 patch renamed three compound extensions:

  .app.xml         -> .app_xml         (appearance XML)
  .pac.xml         -> .pac_xml         (mesh property sidecar)
  .prefabdata.xml  -> .prefabdata_xml  (prefab supplementary data)

Before we picked up the rename, ``PamtFileEntry.encrypted`` only
recognised the old names so VFS reads returned ChaCha20 ciphertext
for the new names. These tests pin down:

  * All three new extensions report ``encrypted == True``.
  * All three old extensions still report ``encrypted == True``
    (backward compat with pre-patch installs).
  * The SIDECAR_KINDS tuple contains BOTH forms so mesh repack
    picks them up regardless of which game build the user is on.
  * asset_catalog's ``.app.xml`` detection accepts both forms.
"""

from __future__ import annotations

import os
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.pamt_parser import PamtFileEntry


def _entry(path: str) -> PamtFileEntry:
    return PamtFileEntry(
        path=path, paz_file="", offset=0, comp_size=0,
        orig_size=0, flags=0, paz_index=0, record_offset=0,
    )


class NewExtensionsAreEncrypted(unittest.TestCase):
    """The three new extensions must be ChaCha20-decrypted on read.

    Symptom pre-fix: ``vfs.read_entry_data`` returned raw ciphertext
    for every .app_xml / .pac_xml / .prefabdata_xml entry, breaking
    the character catalog and silently skipping sidecars on repack.
    """

    def test_app_xml_is_encrypted(self):
        self.assertTrue(_entry("character/foo.app_xml").encrypted)

    def test_pac_xml_is_encrypted(self):
        self.assertTrue(_entry("character/foo.pac_xml").encrypted)

    def test_prefabdata_xml_is_encrypted(self):
        self.assertTrue(_entry("character/foo.prefabdata_xml").encrypted)

    def test_case_insensitive_app_xml(self):
        self.assertTrue(_entry("Character/Foo.APP_XML").encrypted)

    def test_case_insensitive_pac_xml(self):
        self.assertTrue(_entry("Character/Foo.PAC_XML").encrypted)

    def test_mixed_case_prefabdata_xml(self):
        self.assertTrue(_entry("Character/Foo.PrefabData_XML").encrypted)


class LegacyExtensionsStillEncrypted(unittest.TestCase):
    """Pre-patch game installs still ship the old dotted-compound
    names. Those must continue to decrypt so existing mod workflows
    on unpatched installs don't regress."""

    def test_app_dot_xml_still_encrypted(self):
        # .app.xml splitext() -> '.xml' which is in the encrypted set.
        self.assertTrue(_entry("character/foo.app.xml").encrypted)

    def test_pac_dot_xml_still_encrypted(self):
        self.assertTrue(_entry("character/foo.pac.xml").encrypted)

    def test_prefabdata_dot_xml_still_encrypted(self):
        self.assertTrue(_entry("character/foo.prefabdata.xml").encrypted)


class UnrelatedExtensionsStillUnencrypted(unittest.TestCase):
    """The encrypted list should NOT be so loose that unrelated
    extensions get mis-flagged. These must all remain unencrypted."""

    def test_pac_not_encrypted(self):
        self.assertFalse(_entry("character/foo.pac").encrypted)

    def test_pam_not_encrypted(self):
        self.assertFalse(_entry("character/foo.pam").encrypted)

    def test_pab_not_encrypted(self):
        self.assertFalse(_entry("character/foo.pab").encrypted)

    def test_dds_not_encrypted(self):
        self.assertFalse(_entry("character/foo.dds").encrypted)

    def test_wem_not_encrypted(self):
        self.assertFalse(_entry("sound/foo.wem").encrypted)

    def test_paa_not_encrypted(self):
        self.assertFalse(_entry("character/foo.paa").encrypted)

    def test_pabgb_not_encrypted(self):
        self.assertFalse(_entry("gamedata/foo.pabgb").encrypted)


class SidecarKindsIncludesBothForms(unittest.TestCase):
    """The sidecar service must discover both old- and new-form
    files so the mesh repack flow picks up the correct sidecars
    on either game build."""

    def test_contains_both_pac_forms(self):
        from core.mesh_sidecar_service import SIDECAR_KINDS
        suffixes = {s for s, _, _ in SIDECAR_KINDS}
        self.assertIn(".pac_xml", suffixes)
        self.assertIn(".pac.xml", suffixes)

    def test_contains_both_app_forms(self):
        from core.mesh_sidecar_service import SIDECAR_KINDS
        suffixes = {s for s, _, _ in SIDECAR_KINDS}
        self.assertIn(".app_xml", suffixes)
        self.assertIn(".app.xml", suffixes)

    def test_contains_both_prefabdata_forms(self):
        from core.mesh_sidecar_service import SIDECAR_KINDS
        suffixes = {s for s, _, _ in SIDECAR_KINDS}
        self.assertIn(".prefabdata_xml", suffixes)
        self.assertIn(".prefabdata.xml", suffixes)

    def test_physics_still_listed(self):
        from core.mesh_sidecar_service import SIDECAR_KINDS
        suffixes = {s for s, _, _ in SIDECAR_KINDS}
        self.assertIn(".hkx", suffixes)


class CheckumMemMapped(unittest.TestCase):
    """_checksum_paz_file must match the pre-v1.22.7 implementation
    byte-for-byte across a range of file sizes."""

    def _make_tempfile(self, data: bytes) -> str:
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".paz")
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        self.addCleanup(lambda: os.unlink(path) if os.path.isfile(path) else None)
        return path

    def test_empty_file(self):
        from core.repack_engine import _checksum_paz_file
        from core.checksum_engine import pa_checksum
        p = self._make_tempfile(b"")
        crc, size = _checksum_paz_file(p)
        self.assertEqual(size, 0)
        self.assertEqual(crc, pa_checksum(b""))

    def test_small_file(self):
        from core.repack_engine import _checksum_paz_file
        from core.checksum_engine import pa_checksum
        data = b"PAZsmall-test-payload" * 3
        p = self._make_tempfile(data)
        crc, size = _checksum_paz_file(p)
        self.assertEqual(size, len(data))
        self.assertEqual(crc, pa_checksum(data))

    def test_one_megabyte_file(self):
        from core.repack_engine import _checksum_paz_file
        from core.checksum_engine import pa_checksum
        data = b"x" * (1024 * 1024)
        p = self._make_tempfile(data)
        crc, size = _checksum_paz_file(p)
        self.assertEqual(size, len(data))
        self.assertEqual(crc, pa_checksum(data))

    def test_non_multiple_of_twelve_length(self):
        """Bob Jenkins Lookup3 has special handling for 0-11 tail
        bytes — make sure the mmap path handles the tail correctly."""
        from core.repack_engine import _checksum_paz_file
        from core.checksum_engine import pa_checksum
        # Length that's not a multiple of 12 to exercise the tail code.
        data = bytes(range(256)) * 37   # 9472 bytes, 9472 % 12 = 4
        self.assertNotEqual(len(data) % 12, 0)
        p = self._make_tempfile(data)
        crc, size = _checksum_paz_file(p)
        self.assertEqual(crc, pa_checksum(data))


if __name__ == "__main__":
    unittest.main()
