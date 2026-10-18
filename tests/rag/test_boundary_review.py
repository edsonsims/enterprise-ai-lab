"""Independent actual-Qdrant boundary counterexamples with controlled vectors.

These vectors deliberately control neighbor order. They are not model inference
or a semantic retrieval benchmark; the separate end-to-end suite runs MiniLM.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from enterprise_ai_lab.rag.contracts import Document, RagPrincipal
from enterprise_ai_lab.rag.pipeline import RagPipeline
from enterprise_ai_lab.rag.store import VectorStore


class ControlledEmbeddings:
    dimensions = 2
    fingerprint = "controlled-review-vectors-not-a-neural-model"

    def embed(self, texts):
        # Beta is the global nearest neighbor, private Alpha is next, and the
        # permitted Alpha reader passage remains a lower but valid neighbor.
        return [[1.0, 0.0] if text == "query" or "BETA_SECRET" in text else
                [0.9, 0.1] if "ALPHA_PRIVATE" in text else [0.6, 0.8]
                for text in texts]


class GeneratorProbe:
    name = "review-probe"

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def generate(self, question, chunks):
        self.calls.append((question, chunks))
        if self.result is not None:
            return self.result
        return {"answer": chunks[0]["text"], "generator": self.name,
                "evidence": [{"chunk_id": chunks[0]["chunk_id"], "quote": chunks[0]["text"]}]}


class VectorBoundaryReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "index"
        self.store = VectorStore(self.path, ControlledEmbeddings())
        self.reader = RagPrincipal("alpha", ("reader",), "reader-alpha")
        self.store.index([
            Document("alpha-general", "alpha", ("reader", "analyst"), "Synthetic general", "Alpha public processing limit is 20."),
            Document("alpha-private", "alpha", ("analyst",), "Synthetic restricted", "ALPHA_PRIVATE reserve buffer is 18."),
            Document("beta-general", "beta", ("reader", "analyst"), "Other synthetic tenant", "BETA_SECRET processing limit is 75."),
        ])

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_prefilter_preserves_allowed_neighbor_below_global_top_one(self):
        global_hit = self.store.client.query_points(self.store.collection_name, query=[1.0, 0.0],
                                                    limit=1, with_payload=True).points[0]
        self.assertEqual(global_hit.payload["document_id"], "beta-general")
        allowed = self.store.search("query", self.reader, top_k=1)
        self.assertEqual([row.document_id for row in allowed], ["alpha-general"])
        self.assertLess(allowed[0].score, global_hit.score)

    def test_multi_role_is_intersection_and_never_removes_tenant_predicate(self):
        principal = RagPrincipal("alpha", ("reader", "analyst"), "multi-role-alpha")
        allowed = self.store.search("query", principal, top_k=1)
        self.assertEqual([row.document_id for row in allowed], ["alpha-private"])
        self.assertEqual(allowed[0].tenant_id, "alpha")

    def test_generator_sees_only_the_filtered_context(self):
        probe = GeneratorProbe()
        result = RagPipeline(self.store, probe).query("query", self.reader, top_k=1)
        serialized_context = str(probe.calls)
        self.assertNotIn("BETA_SECRET", serialized_context)
        self.assertNotIn("ALPHA_PRIVATE", serialized_context)
        self.assertEqual(result["citations"][0]["document_id"], "alpha-general")

    def test_empty_authorized_context_skips_generator(self):
        probe = GeneratorProbe()
        no_acl = RagPrincipal("alpha", ("observer",), "observer-alpha")
        result = RagPipeline(self.store, probe).query("query", no_acl)
        self.assertTrue(result["abstained"])
        self.assertEqual(result["citations"], [])
        self.assertEqual(probe.calls, [])

    def test_unknown_citation_and_changed_quote_fail_instead_of_falling_back(self):
        permitted = self.store.search("query", self.reader, top_k=1)[0]
        for evidence in [[{"chunk_id": "beta-general:invented", "quote": "BETA_SECRET"}],
                         [{"chunk_id": permitted.chunk_id, "quote": "Alpha permits 9999"}]]:
            probe = GeneratorProbe({"answer": "unsupported assertion", "generator": "review-probe", "evidence": evidence})
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                RagPipeline(self.store, probe).query("query", self.reader, top_k=1)

    def test_interrupted_reindex_cannot_serve_old_acl_on_current_or_reopened_store(self):
        replacement = Document("alpha-general", "alpha", ("analyst",), "Changed synthetic ACL", "Changed processing limit is 30.")
        with patch.object(self.store.client, "upsert", side_effect=RuntimeError("injected upsert failure")):
            with self.assertRaises(RuntimeError):
                self.store.index([replacement])
        with self.assertRaises((ValueError, RuntimeError)):
            self.store.search("query", self.reader)
        self.store.close()
        with self.assertRaises((ValueError, RuntimeError)):
            VectorStore(self.path, ControlledEmbeddings())

    def test_partially_persisted_acl_change_requires_rebuild_after_restart(self):
        # The real Qdrant local engine commits payload updates one point at a time.
        # Persist one chunk, then fail; a pending mutation marker must protect all
        # remaining old-reader chunks both now and after process restart.
        self.store.index([Document("multi-chunk", "alpha", ("reader", "analyst"), "Two chunks",
                                   "First synthetic processing policy.\n\nSecond synthetic processing policy.")])
        collection = self.store.client._client._get_collection(self.store.collection_name)
        persist = collection.storage.persist
        committed = []

        def persist_then_fail(point):
            persist(point)
            committed.append(point.id)
            raise RuntimeError("injected failure after first actual SQLite point commit")

        with patch.object(collection.storage, "persist", side_effect=persist_then_fail):
            with self.assertRaises(RuntimeError):
                self.store.update_acl("multi-chunk", "alpha", ["analyst"])
        self.assertEqual(len(committed), 1)
        with self.assertRaises((ValueError, RuntimeError)):
            self.store.search("query", self.reader)
        self.store.close()
        with self.assertRaises((ValueError, RuntimeError)):
            VectorStore(self.path, ControlledEmbeddings())


if __name__ == "__main__":
    unittest.main()
