"""Finding the index store a build wrote, whatever layout the toolchain's SwiftPM uses."""
import os
import tempfile
import time
import unittest

from reflective_index.indexstore import find_store


class FindStoreTest(unittest.TestCase):
    def test_layouts(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(find_store(d))
            old = os.path.join(d, "arm64-apple-macosx", "debug", "index", "store")
            os.makedirs(os.path.join(old, "v5"))
            self.assertEqual(find_store(d), old)
            time.sleep(0.01)
            new = os.path.join(d, "out", "Products", "Debug", "index", "store")
            os.makedirs(os.path.join(new, "v5"))
            os.utime(new)
            self.assertEqual(find_store(d), new, "the most recently written store")
            os.makedirs(os.path.join(d, "other", "index", "store"))   # no versioned store inside: not one
            self.assertEqual(find_store(d), new)


if __name__ == "__main__":
    unittest.main()
