"""Optional material metadata must not prevent preparing or restoring a card."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from misaka.core.network import worker


class CardRestorePromptContextTests(unittest.TestCase):

    def test_prepare_keeps_material_snapshot_despite_corrupt_markdown(self):
        with tempfile.TemporaryDirectory() as directory, closing(sqlite3.connect(":memory:")) as con:
            root = Path(directory)
            (root / "downloads").mkdir()
            (root / "downloads" / "notes.txt").write_text("Existing source material", encoding="utf-8")
            (root / "downloads" / "plain.md").write_text("Markdown without provenance", encoding="utf-8")
            (root / "downloads" / "broken.md").write_bytes(b"\xff")
            row = {"id": "card-1", "workspace": directory, "generation": 1,
                   "claim_lock": "fixture", "assignee": "10032"}
            prepared = worker.card_extras(con, row)
            self.assertIn("downloads/notes.txt", prepared["_materials"])
            self.assertIn("- `downloads/plain.md`", prepared["_materials"].splitlines())
            self.assertIn("- `downloads/broken.md`", prepared["_materials"].splitlines())
            self.assertIn("snapshot", prepared["_materials"])
            self.assertNotIn("Before any new fetch", prepared["_materials"])
            self.assertIsNone(prepared["_research"])

    def test_material_snapshot_retains_path_if_file_disappears_during_metadata_read(self):
        from misaka.core.web import evidence

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "downloads" / "removed.md"
            path.parent.mkdir()
            path.write_text("---\nsource_url: \"https://example.invalid/source\"\n---\n", encoding="utf-8")
            read_provenance = evidence.read_provenance

            def remove_before_read(filename):
                Path(filename).unlink()
                return read_provenance(filename)

            with patch.object(evidence, "read_provenance", side_effect=remove_before_read):
                materials = worker.materials_on_hand(directory)
            self.assertFalse(path.exists())
            self.assertIn("- `downloads/removed.md`", materials.splitlines())


if __name__ == "__main__":
    unittest.main()
