"""Caption validation tests (no network)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import captions  # noqa: E402


class CaptionTests(unittest.TestCase):
    def test_good_caption(self):
        self.assertEqual(
            captions.validate_caption("Sunny afternoon strolls feel like pure magic 🌸"),
            "Sunny afternoon strolls feel like pure magic 🌸",
        )

    def test_no_caption_sentinel(self):
        self.assertIsNone(captions.validate_caption("NO_CAPTION"))

    def test_rejects_hashtags_and_links(self):
        self.assertIsNone(captions.validate_caption("Cute day out 🌸 #vibes"))
        self.assertIsNone(captions.validate_caption("Look here https://x.com lovely 🌸"))

    def test_rejects_wrong_length(self):
        self.assertIsNone(captions.validate_caption("Hi 🌸"))  # too few words
        long = " ".join(["word"] * 25) + " 🌸"
        self.assertIsNone(captions.validate_caption(long))

    def test_requires_exactly_one_emoji(self):
        self.assertIsNone(captions.validate_caption("Soft morning light and coffee love"))
        self.assertIsNone(captions.validate_caption("Soft morning light and coffee 🌸🌸"))

    def test_rejects_refusal(self):
        self.assertIsNone(captions.validate_caption("I'm sorry, I can't help with that 🌸"))


if __name__ == "__main__":
    unittest.main()
