"""Image geometry tests (requires Pillow, no network)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from PIL import Image
    from app import images
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False


@unittest.skipUnless(HAVE_PIL, "Pillow not installed")
class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _make(self, name, w, h, fmt="PNG", color=(120, 30, 200)):
        p = os.path.join(self.tmp, name)
        Image.new("RGB", (w, h), color).save(p, format=fmt)
        return p

    def test_inspect_dimensions_and_format(self):
        p = self._make("a.png", 640, 480)
        info = images.inspect(p)
        self.assertEqual((info.width, info.height), (640, 480))
        self.assertEqual(info.fmt, "PNG")
        self.assertEqual(len(info.sha256), 64)

    def test_same_aspect(self):
        self.assertTrue(images.same_aspect(1920, 1080, 1280, 720))
        self.assertFalse(images.same_aspect(1000, 1000, 1000, 500))

    def test_pad_and_crop_roundtrip(self):
        src = self._make("tall.png", 400, 800)  # 1:2 into a 1000x1000 canvas
        padded = os.path.join(self.tmp, "padded.png")
        box = images.pad_to_size(src, padded, 1000, 1000)
        with Image.open(padded) as im:
            self.assertEqual(im.size, (1000, 1000))
        self.assertEqual(box["inner_w"], 500)  # 400*scale, scale=min(1000/400,1000/800)=1.25
        self.assertEqual(box["inner_h"], 1000)
        cropped = os.path.join(self.tmp, "cropped.png")
        images.crop_padding(padded, cropped, box)
        with Image.open(cropped) as im:
            self.assertEqual(im.size, (500, 1000))

    def test_enforce_output_geometry_matches_target(self):
        produced = self._make("produced.png", 900, 650)
        final = os.path.join(self.tmp, "final.png")
        info = images.enforce_output_geometry(produced, final, 640, 480)
        self.assertEqual((info.width, info.height), (640, 480))

    def test_rejects_unsupported_format(self):
        p = os.path.join(self.tmp, "a.gif")
        Image.new("RGB", (10, 10)).save(p, format="GIF")
        with self.assertRaises(images.ImageError):
            images.inspect(p)


if __name__ == "__main__":
    unittest.main()
