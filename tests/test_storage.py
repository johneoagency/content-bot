"""State machine, idempotency and atomic-claim tests (no network)."""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import storage as S  # noqa: E402


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = S.Storage(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        self.db.close()

    def test_update_idempotency(self):
        self.assertTrue(self.db.mark_update_seen(100))
        self.assertFalse(self.db.mark_update_seen(100))  # replay suppressed

    def test_callback_idempotency(self):
        self.assertTrue(self.db.mark_callback_seen("cb-1"))
        self.assertFalse(self.db.mark_callback_seen("cb-1"))  # double click

    def test_expected_state_guard(self):
        self.db.create_job("j1", S.KIND_IMAGE, state=S.S_QUEUED)
        # Wrong expectation -> no transition.
        self.assertFalse(self.db.set_state("j1", S.S_POLLING, expect=S.S_SUBMITTING))
        # Correct expectation -> transition.
        self.assertTrue(self.db.set_state("j1", S.S_PREPARING, expect=S.S_QUEUED))

    def test_atomic_claim_single_winner(self):
        self.db.create_job("j2", S.KIND_IMAGE, state=S.S_QUEUED)
        winners = []

        def claim():
            job = self.db.claim_next([S.KIND_IMAGE], [S.S_QUEUED], f"w{threading.get_ident()}")
            if job:
                winners.append(job.job_id)

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(winners, ["j2"])  # exactly one worker claimed it

    def test_caption_job_unique_while_queued(self):
        self.db.create_job("out1", S.KIND_IMAGE, state=S.S_DONE)
        first = self.db.create_caption_job("out1", "123", "instagram_content")
        second = self.db.create_caption_job("out1", "123", "instagram_content")
        self.assertIsNotNone(first)
        self.assertIsNone(second)  # no duplicate active caption job

    def test_pending_prompt_latest_wins(self):
        self.db.add_pending_prompt(7, 10, 5, "regen_prompt", job_id="jA")
        self.db.add_pending_prompt(7, 10, 5, "regen_prompt", job_id="jB")
        taken = self.db.take_pending_prompt(7, 10, 5)
        self.assertEqual(taken["job_id"], "jB")
        self.assertIsNone(self.db.take_pending_prompt(7, 10, 5))  # consumed

    def test_backup_roundtrip(self):
        self.db.create_job("jb", S.KIND_IMAGE, state=S.S_DONE)
        dest = os.path.join(self.tmp, "backup.db")
        self.db.backup(dest)
        restored = S.Storage(dest)
        try:
            self.assertEqual(restored.counts_by_state().get(S.S_DONE), 1)
        finally:
            restored.close()


if __name__ == "__main__":
    unittest.main()
