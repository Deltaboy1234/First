"""Accepted edits survive restarts without changing originals or stale sources."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from fieldroom import imaging as im
from fieldroom.library import Session, export
from fieldroom.recovery import RecoverySession, RecoveryStore, state_directory


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "Photo roll"
        self.folder.mkdir()
        for name in ("01.jpg", "02.jpg"):
            Image.new("RGB", (80, 60), (90, 130, 170)).save(self.folder / name)
        self.directory = self.root / "state"
        self.recovery = RecoverySession(Session(), self.directory)
        self.roll, self.boot = self.recovery.boot(self.folder)

    def snapshot(self, sequence=1, recipe=None, identifier=None):
        identifier = identifier or self.roll["photos"][1]["id"]
        return {"client": self.boot["client"], "sequence": sequence, "path": str(self.folder),
                "id": identifier, "changes": [{"id": identifier, "recipe": im.recipe(recipe or {"exposure": .6})}]}

    def test_new_process_restores_edits_and_selected_photo_without_export(self):
        source = (self.folder / "02.jpg").read_bytes()
        self.assertTrue(self.recovery.save(self.snapshot())["saved"])
        child = subprocess.check_output([sys.executable, "-c",
            "import json,sys; from fieldroom.library import Session; from fieldroom.recovery import RecoverySession; "
            "print(json.dumps(RecoverySession(Session(), sys.argv[1]).boot()))", str(self.directory)], text=True)
        roll, boot = json.loads(child)
        photo = roll["photos"][1]
        self.assertEqual(roll["path"], str(self.folder))
        self.assertEqual(boot["selected_id"], photo["id"])
        self.assertEqual(photo["recipe"]["exposure"], .6)
        self.assertEqual(photo["saved_recipe"], im.recipe())
        self.assertTrue(photo["recovered"])
        self.assertEqual(boot["restored_count"], 1)
        self.assertFalse((self.folder / "Edited").exists())
        self.assertEqual((self.folder / "02.jpg").read_bytes(), source)

    def test_reset_to_defaults_replaces_previous_recovery(self):
        self.recovery.save(self.snapshot())
        self.recovery.save(self.snapshot(2, im.recipe()))
        roll, _ = RecoverySession(Session(), self.directory).boot()
        self.assertEqual(roll["photos"][1]["recipe"], im.recipe())

    def test_exported_baseline_stays_distinct_from_new_unsaved_edits(self):
        identifier = self.roll["photos"][1]["id"]
        saved = im.recipe({"exposure": .2})
        export(self.recovery.session.get(identifier), saved)
        self.recovery.save(self.snapshot(recipe={"exposure": 1.2}))
        roll, _ = RecoverySession(Session(), self.directory).boot()
        self.assertEqual(roll["photos"][1]["saved_recipe"], saved)
        self.assertEqual(roll["photos"][1]["recipe"]["exposure"], 1.2)
        self.assertFalse(roll["photos"][1]["exported"])

    def test_changed_source_never_receives_stale_recovery(self):
        self.recovery.save(self.snapshot())
        Image.new("RGB", (100, 40), "red").save(self.folder / "02.jpg")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.recovery.save(self.snapshot(2))
        roll, _ = RecoverySession(Session(), self.directory).boot()
        self.assertEqual(roll["photos"][1]["recipe"], im.recipe())
        self.assertFalse(roll["photos"][1]["recovered"])

    def test_malformed_batch_cannot_partially_replace_good_recovery(self):
        self.recovery.save(self.snapshot())
        bad = self.snapshot(2, {"exposure": 1.8})
        bad["changes"].append({"id": self.roll["photos"][0]["id"], "recipe": {"exposure": float("nan")}})
        with self.assertRaises(ValueError):
            self.recovery.save(bad)
        roll, _ = RecoverySession(Session(), self.directory).boot()
        self.assertEqual(roll["photos"][1]["recipe"]["exposure"], .6)
        self.assertEqual(self.recovery.sequence, 1)

    def test_late_snapshot_and_old_window_cannot_overwrite_new_edits(self):
        old = self.snapshot(1, {"exposure": .1})
        self.recovery.save(self.snapshot(2, {"exposure": 1.2}))
        self.assertTrue(self.recovery.save(old)["stale"])
        self.recovery.boot(self.folder)
        self.assertTrue(self.recovery.save(self.snapshot(3, {"exposure": -.8}))["stale"])
        roll, _ = RecoverySession(Session(), self.directory).boot()
        self.assertEqual(roll["photos"][1]["recipe"]["exposure"], 1.2)

    def test_explicit_folder_wins_but_recovery_applies_when_browsing_back(self):
        self.recovery.save(self.snapshot())
        other = self.root / "Other roll"
        other.mkdir()
        Image.new("RGB", (20, 20), "green").save(other / "Other.jpg")
        reopened = RecoverySession(Session(), self.directory)
        roll, boot = reopened.boot(other)
        self.assertEqual(roll["path"], str(other))
        self.assertIsNone(boot["selected_id"])
        self.assertEqual(reopened.browse(self.folder)["photos"][1]["recipe"]["exposure"], .6)

    def test_disconnected_drive_keeps_state_and_recovers_after_reconnection(self):
        self.recovery.save(self.snapshot())
        unplugged = self.root / "Disconnected roll"
        self.folder.rename(unplugged)
        self.assertTrue(self.recovery.save(self.snapshot(2, {"exposure": .9}))["saved"])
        fallback = self.root / "Demo"
        fallback.mkdir()
        Image.new("RGB", (20, 20), "blue").save(fallback / "Demo.jpg")
        session = Session()
        reopened = RecoverySession(session, self.directory)
        with patch.object(session, "demo", side_effect=lambda: session.browse(fallback)):
            roll, boot = reopened.boot()
        self.assertIn("unavailable", boot["warning"])
        reopened.save({"client": boot["client"], "sequence": 1, "path": roll["path"],
                       "id": roll["photos"][0]["id"], "changes": []})
        self.assertEqual(reopened.store.location()["path"], str(self.folder))
        unplugged.rename(self.folder)
        roll, boot = RecoverySession(Session(), self.directory).boot()
        self.assertEqual(roll["path"], str(self.folder))
        self.assertEqual(roll["photos"][1]["recipe"]["exposure"], .9)

    def test_corrupt_database_is_not_replaced(self):
        self.directory.mkdir()
        path = self.directory / "recovery.sqlite3"
        path.write_bytes(b"Existing unrecognized recovery data")
        before = path.read_bytes()
        store = RecoveryStore(self.directory)
        self.assertIsNone(store.location())
        with self.assertRaises(ValueError):
            store.save([])
        self.assertEqual(path.read_bytes(), before)

    def test_state_override_isolated_and_read_only_start_does_not_create_files(self):
        with patch.dict(os.environ, {"FIELDROOM_STATE_DIR": str(self.directory)}):
            self.assertEqual(state_directory(), self.directory)
            store = RecoveryStore()
            self.assertIsNone(store.location())
            self.assertFalse(self.directory.exists())


if __name__ == "__main__":
    unittest.main()
