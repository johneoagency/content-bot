"""Instagram URL canonicalization tests (no network)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import instagram  # noqa: E402


class InstagramTests(unittest.TestCase):
    def test_reel_canonicalization(self):
        ref = instagram.canonicalize("https://instagram.com/reel/ABC123/?igshid=xyz")
        self.assertIsNotNone(ref)
        self.assertEqual(ref.shortcode, "ABC123")
        self.assertEqual(ref.kind, "reel")
        self.assertEqual(ref.url, "https://www.instagram.com/reel/ABC123/")

    def test_reels_alias(self):
        ref = instagram.canonicalize("https://www.instagram.com/reels/ZZ9/")
        self.assertEqual(ref.kind, "reel")

    def test_rejects_other_domain(self):
        self.assertIsNone(instagram.canonicalize("https://example.com/reel/ABC123/"))
        self.assertIsNone(instagram.canonicalize("https://instagram.com/accounts/login/"))

    def test_parse_message_dedup_and_reject(self):
        text = (
            "check these\n"
            "https://instagram.com/reel/AAA/\n"
            "https://www.instagram.com/reel/AAA/  (dup)\n"
            "https://tiktok.com/@x/video/1 random words"
        )
        refs, rejected = instagram.parse_message(text)
        self.assertEqual(len(refs), 1)  # de-duplicated by shortcode
        self.assertEqual(refs[0].shortcode, "AAA")
        self.assertIn("https://tiktok.com/@x/video/1", rejected)


if __name__ == "__main__":
    unittest.main()
