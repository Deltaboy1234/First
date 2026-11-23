"""Persistent AI results are optional, bounded, and safe across replacement."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import numpy as np

from fieldroom import ai_cache as cache


class AiCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root/"cache"
        self.environment = patch.dict(os.environ, {"FIELDROOM_AI_CACHE_DIR": str(self.directory)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.key = ("/home/photographer/Private portraits/original.NEF", (123456789, 12345), .5, .4, "verified-model-signature")
        self.pixels = np.arange(10*12*3, dtype=np.uint16).reshape((10, 12, 3))

    def entry(self, key=None):
        return self.directory/cache._filename(self.key if key is None else key)

    def owned(self):
        return [path for path in self.directory.iterdir() if cache._OWNED.fullmatch(path.name)]

    def test_round_trip_is_readonly_memory_map_and_survives_fresh_process(self):
        self.assertTrue(cache.save(self.key, self.pixels))
        loaded = cache.load(self.key, (12, 10))
        self.assertIsInstance(loaded, np.memmap)
        self.assertFalse(loaded.flags.writeable)
        np.testing.assert_array_equal(loaded, self.pixels)
        with self.assertRaises(ValueError):
            loaded[0, 0, 0] = 0
        self.assertTrue(cache.has(self.key, (12, 10)))
        script = "\n".join([
            "import json,sys,numpy as np",
            "from fieldroom import ai_cache",
            "a=ai_cache.load(tuple(json.loads(sys.argv[1])), (12,10))",
            "assert isinstance(a,np.memmap) and not a.flags.writeable",
            "print(int(a[-1,-1,-1]))",
        ])
        child = subprocess.run([sys.executable, "-c", script, json.dumps(self.key)],
                               cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                               env=dict(os.environ), timeout=15)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(int(child.stdout.strip()), int(self.pixels[-1, -1, -1]))
        self.assertNotIn("Private", self.entry().name)
        self.assertNotIn("original", self.entry().name)

    def test_missing_cache_is_a_miss_without_creating_directories(self):
        self.assertIsNone(cache.load(self.key, (12, 10)))
        self.assertFalse(cache.has(self.key, (12, 10)))
        self.assertFalse(self.directory.exists())

    def test_fingerprint_strength_or_model_change_is_a_different_key(self):
        self.assertTrue(cache.save(self.key, self.pixels))
        for index, value in ((1, (999, 12345)), (2, .7), (3, .8), (4, "another-model")):
            changed = list(self.key)
            changed[index] = value
            self.assertIsNone(cache.load(tuple(changed), (12, 10)))
        self.assertTrue(cache.has(self.key, (12, 10)))

    def test_corrupt_dtype_shape_and_object_data_are_misses(self):
        self.directory.mkdir()
        self.entry().write_bytes(b"not a numpy array")
        self.assertIsNone(cache.load(self.key, (12, 10)))
        np.save(self.entry(), self.pixels, allow_pickle=False)
        with self.entry().open("ab") as stream:
            stream.write(b"unexpected trailing data")
        self.assertIsNone(cache.load(self.key, (12, 10)))
        for bad in (self.pixels.astype(np.uint8), self.pixels.astype(float)*float("nan"),
                    np.zeros((10, 12), dtype=np.uint16), np.zeros((10, 12, 4), dtype=np.uint16),
                    np.zeros((11, 12, 3), dtype=np.uint16), np.array([{"object": "must not unpickle"}], dtype=object)):
            with self.subTest(shape=bad.shape, dtype=bad.dtype):
                np.save(self.entry(), bad, allow_pickle=True)
                self.assertIsNone(cache.load(self.key, (12, 10)))

    def test_invalid_inputs_are_optional_cache_failures(self):
        for key in (["not-a-tuple"], (float("nan"),), (object(),)):
            self.assertFalse(cache.save(key, self.pixels))
            self.assertIsNone(cache.load(key, (12, 10)))
        for bad in (self.pixels.astype(np.float32), self.pixels[:, :, 0],
                    np.zeros((0, 3, 3), dtype=np.uint16), [1, 2, 3]):
            self.assertFalse(cache.save(self.key, bad))
        for size in ((0, 10), (-1, 10), (12,), (True, 10), (12., 10), "12,10"):
            self.assertIsNone(cache.load(self.key, size))
        self.assertFalse(self.directory.exists())

    def test_interrupted_write_preserves_previous_result_and_removes_partial_file(self):
        self.assertTrue(cache.save(self.key, self.pixels))
        old_bytes = self.entry().read_bytes()
        def fail(stream, *args, **kwargs):
            stream.write(b"incomplete")
            raise OSError("disk full")
        with patch.object(cache.np, "save", side_effect=fail):
            self.assertFalse(cache.save(self.key, self.pixels+1))
        self.assertEqual(self.entry().read_bytes(), old_bytes)
        self.assertFalse(any(path.name.endswith(".tmp") for path in self.directory.iterdir()))
        np.testing.assert_array_equal(cache.load(self.key, (12, 10)), self.pixels)

    def test_atomic_replace_failure_does_not_publish_partial_pixels(self):
        self.assertTrue(cache.save(self.key, self.pixels))
        with patch.object(cache.os, "replace", side_effect=OSError("write failed")):
            self.assertFalse(cache.save(self.key, self.pixels+7))
        np.testing.assert_array_equal(cache.load(self.key, (12, 10)), self.pixels)
        self.assertFalse(any(path.name.endswith(".tmp") for path in self.directory.iterdir()))

    def test_lru_cleanup_preserves_recent_load_new_result_and_foreign_files(self):
        pixels = np.zeros((10, 10, 3), dtype=np.uint16)
        keys = [("photo", value) for value in range(3)]
        with patch.object(cache, "MAX_CACHE_BYTES", 1600):
            self.assertTrue(cache.save(keys[0], pixels))
            self.assertTrue(cache.save(keys[1], pixels+1))
            os.utime(self.entry(keys[0]), (1, 1))
            os.utime(self.entry(keys[1]), (2, 2))
            self.assertTrue(cache.has(keys[0], (10, 10)))  # First is now most recently used.
            foreign = self.directory/"unrelated.npy"
            foreign.write_bytes(b"leave this file alone")
            self.assertTrue(cache.save(keys[2], pixels+2))
            self.assertTrue(self.entry(keys[0]).exists())
            self.assertFalse(self.entry(keys[1]).exists())
            self.assertTrue(self.entry(keys[2]).exists())
            self.assertLessEqual(sum(path.stat().st_size for path in self.owned()), 1600)
            self.assertEqual(foreign.read_bytes(), b"leave this file alone")

    def test_existing_mapping_survives_replacement_and_eviction(self):
        pixels = np.full((10, 10, 3), 17, dtype=np.uint16)
        self.assertTrue(cache.save(self.key, pixels))
        old = cache.load(self.key, (10, 10))
        self.assertTrue(cache.save(self.key, pixels+2))
        self.assertTrue(np.all(old == 17))
        current = cache.load(self.key, (10, 10))
        os.utime(self.entry(), (1, 1))
        with patch.object(cache, "MAX_CACHE_BYTES", 800):
            self.assertTrue(cache.save(("new photo",), pixels+3))
        self.assertFalse(self.entry().exists())
        self.assertTrue(np.all(current == 19))
        self.assertTrue(np.all(old == 17))

    def test_concurrent_replacements_never_return_mixed_or_partial_arrays(self):
        key = ("concurrent photo",)
        def write_and_read(value):
            pixels = np.full((48, 64, 3), value, dtype=np.uint16)
            self.assertTrue(cache.save(key, pixels))
            loaded = cache.load(key, (64, 48))
            self.assertIsNotNone(loaded)
            self.assertEqual(len(np.unique(loaded)), 1)
            self.assertFalse(loaded.flags.writeable)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(write_and_read, range(1, 13)))

    def test_invalid_directory_paths_and_symlinks_cannot_escape(self):
        outside = self.root/"outside"
        outside.mkdir()
        sentinel = outside/"sentinel"
        sentinel.write_bytes(b"unchanged")
        link = self.root/"linked-cache"
        link.symlink_to(outside, target_is_directory=True)
        blocked = self.root/"regular-file"
        blocked.write_bytes(b"not a directory")
        for location in ("relative-cache", str(link), str(link/"child"),
                         str(blocked/"child"), str(self.root/"cache"/".."/"escape"), "/"):
            with self.subTest(location=location), patch.dict(os.environ, {"FIELDROOM_AI_CACHE_DIR":location}):
                self.assertFalse(cache.save(self.key, self.pixels))
                self.assertIsNone(cache.load(self.key, (12, 10)))
        self.assertEqual(list(outside.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"unchanged")
        self.assertEqual(blocked.read_bytes(), b"not a directory")
        self.assertFalse((self.root/"escape").exists())

    def test_symlink_and_hardlink_entries_do_not_touch_external_targets(self):
        self.directory.mkdir()
        external = self.root/"external.npy"
        np.save(external, self.pixels)
        original = external.read_bytes()
        old_time = external.stat().st_mtime_ns
        self.entry().symlink_to(external)
        self.assertIsNone(cache.load(self.key, (12, 10)))
        self.assertFalse(cache.save(self.key, self.pixels+1))
        self.entry().unlink()
        os.link(external, self.entry())
        self.assertIsNone(cache.load(self.key, (12, 10)))
        self.assertFalse(cache.save(self.key, self.pixels+1))
        self.assertEqual(external.read_bytes(), original)
        self.assertEqual(external.stat().st_mtime_ns, old_time)

    def test_oversized_result_and_failed_eviction_keep_budget(self):
        with patch.object(cache, "MAX_CACHE_BYTES", 100):
            self.assertFalse(cache.save(self.key, self.pixels))
        pixels = np.zeros((10, 10, 3), dtype=np.uint16)
        first, second = ("first",), ("second",)
        with patch.object(cache, "MAX_CACHE_BYTES", 800):
            self.assertTrue(cache.save(first, pixels))
            unlink = cache.os.unlink
            def refuse(name, *args, **kwargs):
                if name == cache._filename(first):
                    raise PermissionError("file cannot be removed")
                return unlink(name, *args, **kwargs)
            with patch.object(cache.os, "unlink", side_effect=refuse):
                self.assertFalse(cache.save(second, pixels))
            self.assertTrue(self.entry(first).exists())
            self.assertFalse(self.entry(second).exists())
            self.assertLessEqual(sum(path.stat().st_size for path in self.owned()), 800)

    def test_xdg_cache_location_is_used_when_override_is_absent(self):
        xdg = self.root/"xdg-cache"
        with patch.dict(os.environ, {"XDG_CACHE_HOME":str(xdg)}):
            del os.environ["FIELDROOM_AI_CACHE_DIR"]
            self.assertTrue(cache.save(self.key, self.pixels))
            expected = xdg/"fieldroom"/"ai-detail"/cache._filename(self.key)
            self.assertTrue(expected.is_file())
            np.testing.assert_array_equal(cache.load(self.key, (12, 10)), self.pixels)


if __name__ == "__main__":
    unittest.main()
