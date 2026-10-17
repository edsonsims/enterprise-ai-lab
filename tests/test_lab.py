import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from enterprise_ai_lab.core import (
    ValidationError, authorized, evaluate, predict, read_jsonl, retrieve,
    synthetic_dataset, unsafe_global_retrieve, validate_dataset, validate_predictions,
    write_bundle,
)


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.documents = [
            {"id": "a-finance", "tenant_id": "a", "allowed_roles": ["finance"], "text": "budget budget budget"},
            {"id": "a-staff", "tenant_id": "a", "allowed_roles": ["staff"], "text": "budget"},
            {"id": "b-staff", "tenant_id": "b", "allowed_roles": ["staff"], "text": "budget budget budget"},
        ]
        self.case = {"id": "q", "tenant_id": "a", "roles": ["staff"], "query": "budget", "relevant_doc_ids": ["a-staff"]}

    def test_tenant_and_role_filters_precede_ranking(self):
        self.assertEqual(retrieve(self.documents, self.case, 1), [(1, "a-staff")])
        global_first = unsafe_global_retrieve(self.documents, self.case, 1)
        postfiltered = [row for row in global_first if authorized(next(d for d in self.documents if d["id"] == row[1]), self.case)]
        self.assertEqual(postfiltered, [])  # Postfiltering top-1 loses accessible recall.

    def test_fail_closed_identity_and_acl(self):
        for field in ("tenant_id", "roles"):
            broken = dict(self.case)
            broken.pop(field)
            self.assertEqual(retrieve(self.documents, broken), [])
        for roles in ([], "staff", [None]):
            self.assertFalse(authorized(self.documents[1], {**self.case, "roles": roles}))
        for field in ("tenant_id", "allowed_roles"):
            broken = dict(self.documents[1])
            broken.pop(field)
            self.assertFalse(authorized(broken, self.case))

    def test_unicode_and_deterministic_tie_break(self):
        doc = {**self.documents[1], "text": "CAFÉ RÉSERVATION"}
        self.assertEqual(retrieve([doc], {**self.case, "query": "café réservation"}), [(2, "a-staff")])
        tied = [{**doc, "id": "z"}, {**doc, "id": "a"}]
        self.assertEqual([row[1] for row in retrieve(tied, {**self.case, "query": "café"})], ["a", "z"])

    def test_no_match_and_invalid_k(self):
        self.assertEqual(retrieve(self.documents, {**self.case, "query": "unknown"}), [])
        for k in (0, -1, True, 1.5):
            with self.assertRaises(ValidationError):
                retrieve(self.documents, self.case, k)


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.documents, self.cases = synthetic_dataset(42)
        self.predictions = predict(self.documents, self.cases)

    def test_synthetic_cases_are_distinct_and_reproducible(self):
        self.assertEqual(len(self.documents), 50)
        self.assertEqual(len(self.cases), 100)
        self.assertEqual(len({case["query"] for case in self.cases}), 100)
        self.assertEqual((self.documents, self.cases), synthetic_dataset(42))
        self.assertNotEqual(self.documents, synthetic_dataset(43)[0])
        self.assertEqual({case["category"] for case in self.cases}, {"answerable_shared_terms", "role_denied_anchor", "role_denied_shared_terms", "cross_tenant_denied", "no_match"})

    def test_safe_and_unsafe_baselines_expose_acl_difference(self):
        report, raw = evaluate(self.documents, self.cases, self.predictions)
        metrics = report["metrics"]
        self.assertEqual(metrics["query_leakage_rate"]["numerator"], 0)
        self.assertEqual(metrics["retrieval_hit_rate_answerable"]["value"], 1)
        self.assertEqual(metrics["refusal_accuracy_unanswerable"]["value"], 0.8)
        self.assertEqual(report["categories"]["role_denied_shared_terms"]["correct_unanswerable_refusals"], 0)
        self.assertEqual(report["categories"]["role_denied_shared_terms"]["queries_with_leakage"], 0)
        self.assertEqual(len(raw), 100)
        unsafe, _ = evaluate(self.documents, self.cases, predict(self.documents, self.cases, unsafe_global=True))
        self.assertGreater(unsafe["metrics"]["query_leakage_rate"]["value"], 0)
        self.assertLess(unsafe["metrics"]["refusal_accuracy_unanswerable"]["value"], 1)

    def test_empty_results_and_denominators(self):
        empty = [{"case_id": case["id"], "retrieved_doc_ids": [], "refused": True} for case in self.cases]
        report, _ = evaluate(self.documents, self.cases, empty)
        metrics = report["metrics"]
        self.assertEqual(metrics["query_leakage_rate"]["value"], 0)
        self.assertEqual(metrics["query_leakage_rate"]["denominator"], 100)
        self.assertIsNone(metrics["returned_reference_leakage_rate"]["value"])
        self.assertIsNone(metrics["returned_reference_leakage_rate"]["wilson_95"])
        self.assertEqual(metrics["refusal_accuracy_unanswerable"]["denominator"], 50)
        self.assertEqual(metrics["abstention_classification_accuracy"]["value"], 0.5)
        answerable = [case for case in self.cases if case["relevant_doc_ids"]]
        selected = [row for row in empty if row["case_id"] in {case["id"] for case in answerable}]
        report, _ = evaluate(self.documents, answerable, selected)
        self.assertIsNone(report["metrics"]["refusal_accuracy_unanswerable"]["value"])

    def test_recall_supports_multiple_relevant_documents(self):
        case = {**self.cases[0], "id": "multi", "tenant_id": "alder", "roles": ["employee"],
                "relevant_doc_ids": ["alder-vpn", "alder-travel"]}
        prediction = {"case_id": "multi", "retrieved_doc_ids": ["alder-vpn"], "refused": False}
        report, _ = evaluate(self.documents, [case], [prediction])
        self.assertEqual(report["metrics"]["macro_recall_answerable"]["value"], 0.5)

    def test_rejects_invalid_dataset(self):
        mutations = [
            lambda d, c: d.append(d[0]),
            lambda d, c: d.append("not an object"),
            lambda d, c: c.append(c[0]),
            lambda d, c: c[0].update(relevant_doc_ids=["missing"]),
            lambda d, c: c[0].update(relevant_doc_ids=["alder-payroll"], roles=["visitor"]),
            lambda d, c: d[0].update(allowed_roles=[]),
            lambda d, c: c[0].update(unrecognized=True),
        ]
        for mutate in mutations:
            docs, cases = copy.deepcopy((self.documents, self.cases))
            mutate(docs, cases)
            with self.assertRaises(ValidationError):
                validate_dataset(docs, cases)

    def test_rejects_invalid_predictions(self):
        mutations = [
            lambda p: p.append(p[0]),
            lambda p: p.append("not an object"),
            lambda p: p.pop(),
            lambda p: p[0].update(case_id="unknown"),
            lambda p: p[0].update(retrieved_doc_ids=["unknown"]),
            lambda p: p[0].update(retrieved_doc_ids=["alder-vpn", "alder-vpn"]),
            lambda p: p[0].update(refused="false"),
            lambda p: p[0].update(scores=[float("nan")] * len(p[0]["retrieved_doc_ids"])),
            lambda p: p[0].update(scores=[float("inf")] * len(p[0]["retrieved_doc_ids"])),
            lambda p: p[0].update(scores=[True] * len(p[0]["retrieved_doc_ids"])),
            lambda p: p[0].update(scores=[10 ** 1000] * len(p[0]["retrieved_doc_ids"])),
            lambda p: p[0].update(scores=[1, 2, 3, 4]),
            lambda p: p[0].update(extra="unsupported"),
        ]
        # Ensure the score mutations operate on a record with returned results.
        self.predictions.sort(key=lambda row: not bool(row["retrieved_doc_ids"]))
        for mutate in mutations:
            predictions = copy.deepcopy(self.predictions)
            mutate(predictions)
            with self.assertRaises(ValidationError):
                validate_predictions(predictions, self.documents, self.cases)

    def test_jsonl_rejects_duplicate_fields_nonfinite_and_blank_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.jsonl"
            for value in ('{"id":"a","id":"b"}\n', '{"score":NaN}\n', '\n', '[]\n'):
                path.write_text(value, encoding="utf-8")
                with self.assertRaises(ValidationError):
                    read_jsonl(path)

    def test_bundle_is_reproducible_and_hashes_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "first", Path(directory) / "second"
            for path in (first, second):
                write_bundle(path, self.documents, self.cases, self.predictions, label="test", seed=42, k=3)
            for path in first.iterdir():
                self.assertEqual(path.read_bytes(), (second / path.name).read_bytes())
            manifest = json.loads((first / "manifest.json").read_text())
            for name, digest in manifest["files"].items():
                self.assertEqual(hashlib.sha256((first / name).read_bytes()).hexdigest(), digest)

    def test_deeply_nested_input_is_a_cli_validation_error(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "nested.jsonl"
            source.write_text('{"id":' + '[' * 2000 + '0' + ']' * 2000 + '}\n', encoding="utf-8")
            output = Path(directory) / "output"
            result = subprocess.run(
                [sys.executable, "-m", "enterprise_ai_lab", "evaluate",
                 "--documents", str(source), "--cases", str(source),
                 "--predictions", str(source), "--output", str(output)],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(output.exists())

    def test_cli_demo_and_external_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            demo = Path(directory) / "demo"
            command = [sys.executable, "-m", "enterprise_ai_lab"]
            result = subprocess.run(command + ["demo", "--output", str(demo), "--seed", "42"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((demo / "unsafe_global_baseline" / "report.json").is_file())
            result = subprocess.run(command + ["evaluate", "--documents", str(demo / "documents.jsonl"),
                                              "--cases", str(demo / "cases.jsonl"), "--predictions", str(demo / "predictions.jsonl"),
                                              "--output", str(Path(directory) / "external")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            external = json.loads((Path(directory) / "external" / "report.json").read_text())
            self.assertEqual(external["dataset_provenance"], "caller_supplied_unverified")
            result = subprocess.run(command + ["demo", "--output", str(demo), "--k", "0"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("positive integer", result.stderr)


if __name__ == "__main__":
    unittest.main()
