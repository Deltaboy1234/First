"""Regression cases for full-resolution zoom, crop persistence and recoverable culling."""
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from fieldroom import imaging as im
from fieldroom.library import Session, export
from fieldroom.samples import make_demo
from fieldroom.server import EditorServer


def make_roll(folder):
    folder.mkdir(parents=True, exist_ok=True)
    y, x = np.ogrid[:2000, :3200]
    fine = (((x // 1) % 2) * 35 + ((y // 2) % 2) * 18)
    rgb = np.stack([(x * 157 // 3200 + y * 24 // 2000 + fine) % 256,
                    (y * 171 // 2000 + x * 35 // 3200 + fine) % 256,
                    (x * 88 // 3200 + y * 71 // 2000 + fine) % 256], axis=2).astype(np.uint8)
    image = Image.fromarray(rgb)
    image.save(folder / "01-resolution.png")
    rotated = Image.Exif()
    rotated[274] = 6
    image.resize((940, 610)).save(folder / "02-portrait.jpg", quality=91, exif=rotated)
    (folder / "second folder").mkdir(exist_ok=True)
    image.resize((240, 160)).save(folder / "second folder" / "Other.jpg")


class EditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        make_roll(cls.root)

    @classmethod
    def tearDownClass(cls):
        im.preview_source.cache_clear()
        cls.temp.cleanup()

    def test_native_tile_matches_export_pixels_across_seams(self):
        source = im.decode(self.root / "01-resolution.png")
        settings = im.recipe({"crop": [.083, .11, .79, .82], "exposure": .4, "luminance": 19,
                              "highlights": -24, "color": 25, "sharpen": 65, "radius": 2.4})
        full = im.render(source, settings)
        left, top, right, bottom = im.pixel_box(source.size, settings["crop"])
        for col, row in ((0, 0), (1, 1), (4, 3)):
            tile = Image.open(io.BytesIO(im.detail_tile(source, settings, col, row, 1)))
            x0, y0 = max(left, col*512), max(top, row*512)
            x1, y1 = min(right, (col+1)*512), min(bottom, (row+1)*512)
            expected = full.crop((x0-left, y0-top, x1-left, y1-top))
            np.testing.assert_array_equal(np.asarray(tile), np.asarray(expected))
        self.assertEqual(Image.open(io.BytesIO(im.detail_tile(source, im.recipe(), 0, 0, 2))).size, (512, 512))

    def test_rotated_photo_and_fit_share_coordinates(self):
        path = self.root / "02-portrait.jpg"
        preview = im.decode(path, 400)
        full = im.decode(path)
        self.assertEqual(preview.size, (610, 940))
        self.assertEqual(full.size, preview.size)
        self.assertGreater(preview.pixels.height, preview.pixels.width)
        self.assertEqual(Image.open(io.BytesIO(im.detail_tile(full, im.recipe(), 1, 1, 1))).size, (98, 428))

    def test_default_demo_opens_six_disposable_photographs(self):
        folder = make_demo()
        try:
            photos = Session().browse(folder)["photos"]
            self.assertEqual(len(photos), 6)
            for path in folder.glob("*.jpg"):
                with Image.open(path) as image:
                    self.assertEqual(image.format, "JPEG")
                    image.verify()
        finally:
            shutil.rmtree(folder.parent)

    def test_edit_reopen_and_external_export_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            path = folder / "Portrait.jpg"
            shutil.copyfile(self.root / "02-portrait.jpg", path)
            original = path.read_bytes()
            session = Session()
            key = session.browse(folder)["photos"][0]["id"]
            settings = im.recipe({"crop": [.1, .2, .9, .8], "shadows": 20})
            first = export(session.get(key), settings)
            expected = im.pixel_box((610, 940), settings["crop"])
            with Image.open(first["path"]) as written:
                self.assertEqual(written.size, (expected[2]-expected[0], expected[3]-expected[1]))
            self.assertTrue(export(session.get(key), settings)["skipped"])
            self.assertEqual(Session().browse(folder)["photos"][0]["recipe"], settings)
            Path(first["path"]).write_bytes(b"externally replaced")
            second = export(session.get(key), settings)
            self.assertNotEqual(second["path"], first["path"])
            self.assertEqual(Path(first["path"]).read_bytes(), b"externally replaced")
            self.assertEqual(path.read_bytes(), original)
            receipt = session.trash(key)
            self.assertFalse(path.exists())
            self.assertTrue(Path(receipt["location"]).exists())
            session.undo(receipt["ticket"])
            self.assertEqual(path.read_bytes(), original)

    @unittest.skipUnless(os.getenv("FIELDROOM_TEST_NEF"), "Set FIELDROOM_TEST_NEF to exercise a real camera RAW")
    def test_real_nikon_raw_through_the_editor(self):
        self.assertIsNotNone(im.rawpy)
        with tempfile.TemporaryDirectory() as tmp, EditorServer() as server:
            source = Path(tmp) / "Nikon.NEF"
            shutil.copyfile(os.environ["FIELDROOM_TEST_NEF"], source)
            photo = server.dispatch("folder", {"path": tmp})["photos"][0]
            request = {"id": photo["id"], "recipe": im.recipe({"exposure": .2, "crop": [.15,.12,.82,.8]})}
            preview = server.dispatch("preview", request)
            self.assertGreater(max(preview["size"]), 2000)
            self.assertLessEqual(max(preview["pixels"]), 1440)
            full = server.detail_source(server.session.get(photo["id"]))
            self.assertEqual(tuple(preview["size"]), full.size)
            l, t, r, b = preview["box"]
            tile = server.dispatch("tile", {**request, "column": l//512, "row": t//512, "step": 1})
            self.assertEqual(Image.open(io.BytesIO(tile)).format, "PNG")
            output = server.dispatch("export", request)
            with Image.open(output["path"]) as saved:
                self.assertEqual(saved.size, (r-l, b-t))
            self.assertTrue(source.is_file())


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--make-roll":
        make_roll(Path(sys.argv[2]))
    else:
        unittest.main()
