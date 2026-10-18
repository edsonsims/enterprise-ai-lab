"""A query must not wait behind publication after its JWT was verified."""
import threading
import time
from types import SimpleNamespace
import unittest

from enterprise_ai_lab.publication.__main__ import PublishedPipeline
from enterprise_ai_lab.publication.activities import PublicationBlocked


class PublicationAdmissionTests(unittest.TestCase):
    def test_held_publication_lock_refuses_query_before_fence_or_retrieval(self):
        lock, held, release = threading.RLock(), threading.Event(), threading.Event()
        calls = []
        def holding_publication():
            with lock:
                held.set()
                release.wait(5)
        owner = threading.Thread(target=holding_publication)
        owner.start()
        try:
            self.assertTrue(held.wait(2))
            pipeline = PublishedPipeline(SimpleNamespace(lock=lock),
                SimpleNamespace(assert_readable=lambda: calls.append("fence")))
            pipeline.pipeline = SimpleNamespace(query=lambda *a, **kw: calls.append("retrieval"))
            started = time.monotonic()
            with self.assertRaises(PublicationBlocked):
                pipeline.query("question", None)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(calls, [])
        finally:
            release.set(); owner.join(2)
        self.assertFalse(owner.is_alive())

    def test_failed_integrity_check_releases_query_lock(self):
        lock = threading.RLock()
        def refuse():
            raise PublicationBlocked("unfinished publication")
        pipeline = PublishedPipeline(SimpleNamespace(lock=lock), SimpleNamespace(assert_readable=refuse))
        with self.assertRaises(PublicationBlocked):
            pipeline.query("question", None)
        acquired = []
        def another_thread():
            if lock.acquire(blocking=False):
                acquired.append(True); lock.release()
        caller = threading.Thread(target=another_thread)
        caller.start(); caller.join(2)
        self.assertEqual(acquired, [True])
