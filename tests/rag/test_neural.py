"""Pure evidence-contract tests; these do not download or invoke a model."""
import json
import tempfile
import unittest
from pathlib import Path

from enterprise_ai_lab.rag.neural import (
    GenerationError, LocalQwenGenerator, validate_generation, verify_snapshot,
)


class GeneratedEvidenceTests(unittest.TestCase):
    chunks = [{"chunk_id": "alpha-guide:0", "text": "Alpha allows 25 jobs per minute. Café access is restricted."}]

    def valid(self):
        return {"answer": "Alpha allows 25 jobs per minute.",
                "evidence": [{"chunk_id": "alpha-guide:0", "quote": "25 jobs per minute"}]}

    def parse(self, value):
        return validate_generation(json.dumps(value), self.chunks)

    def test_valid_exact_quote_requires_human_review(self):
        result = self.parse(self.valid())
        self.assertTrue(result["human_review_required"])
        self.assertFalse(result["legal_advice"])
        self.assertEqual(result["evidence"][0]["quote"], "25 jobs per minute")

    def test_parser_accepts_literal_unicode(self):
        value = self.valid()
        value["evidence"][0]["quote"] = "Café"
        self.assertEqual(self.parse(value)["evidence"][0]["quote"], "Café")

    def test_exact_quote_check_does_not_claim_semantic_faithfulness(self):
        value = self.valid()
        value["answer"] = "The moon is made of cheese."
        self.assertEqual(self.parse(value)["answer"], value["answer"])

    def test_unknown_chunk_cannot_be_cited(self):
        value = self.valid()
        value["evidence"][0]["chunk_id"] = "beta-secret:0"
        with self.assertRaises(GenerationError):
            self.parse(value)

    def test_nonliteral_paraphrase_cannot_be_called_a_quote(self):
        value = self.valid()
        value["evidence"][0]["quote"] = "25 requests per minute"
        with self.assertRaises(GenerationError):
            self.parse(value)

    def test_duplicate_evidence_rejected(self):
        value = self.valid()
        value["evidence"] *= 2
        with self.assertRaises(GenerationError):
            self.parse(value)

    def test_absent_evidence_rejected(self):
        value = self.valid()
        value["evidence"] = []
        with self.assertRaises(GenerationError):
            self.parse(value)

    def test_blank_quote_rejected(self):
        value = self.valid()
        value["evidence"][0]["quote"] = " "
        with self.assertRaises(GenerationError):
            self.parse(value)

    def test_quote_from_different_chunk_rejected(self):
        chunks = self.chunks + [{"chunk_id": "alpha-second:0", "text": "Exceptional processing requires explicit approval."}]
        value = self.valid()
        value["evidence"][0]["quote"] = "explicit approval"
        with self.assertRaises(GenerationError):
            validate_generation(json.dumps(value), chunks)

    def test_model_cannot_add_action_or_override_safety_fields(self):
        for field in ["action", "human_review_required", "tenant_id"]:
            value = self.valid()
            value[field] = "ignored"
            with self.subTest(field=field), self.assertRaises(GenerationError):
                self.parse(value)

    def test_model_cannot_add_evidence_fields(self):
        value = self.valid()
        value["evidence"][0]["url"] = "http://127.0.0.1/private"
        with self.assertRaises(GenerationError):
            self.parse(value)

    def test_json_only_contract_rejects_markdown_and_trailing_text(self):
        text = json.dumps(self.valid())
        for output in [f"```json\n{text}\n```", f"{text}\nMore explanation", "null", "[]", "bad"]:
            with self.subTest(output=output[:30]), self.assertRaises(GenerationError):
                validate_generation(output, self.chunks)

    def test_duplicate_json_fields_rejected(self):
        with self.assertRaises(GenerationError):
            validate_generation('{"answer":"a","answer":"b","evidence":[]}', self.chunks)

    def test_output_size_and_nonfinite_values_rejected(self):
        for output in [" " * 16385, '{"answer":NaN,"evidence":[]}', '{"answer":"a","evidence":Infinity}']:
            with self.subTest(output=output[:30]), self.assertRaises(GenerationError):
                validate_generation(output, self.chunks)

    def test_no_context_rejected_before_model(self):
        with self.assertRaises(GenerationError):
            validate_generation(json.dumps(self.valid()), [])

    def test_duplicate_and_blank_context_rejected(self):
        for chunks in [self.chunks * 2, [{"chunk_id": "a", "text": " "}],
                       [{"chunk_id": "", "text": "text"}], [{"chunk_id": [], "text": "text"}]]:
            with self.subTest(chunks=chunks), self.assertRaises(GenerationError):
                validate_generation(json.dumps(self.valid()), chunks)

    def test_context_size_is_bounded_without_truncation(self):
        with self.assertRaises(GenerationError):
            validate_generation(json.dumps(self.valid()), [{"chunk_id": "a", "text": "x" * 24001}])

    def test_escaped_surrogates_rejected(self):
        with self.assertRaises(GenerationError):
            validate_generation(json.dumps(self.valid()), [{"chunk_id": "a", "text": "\ud800"}])
        value = self.valid()
        value["answer"] = "\ud800"
        with self.assertRaises(GenerationError):
            self.parse(value)

    def test_missing_model_files_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(GenerationError):
            verify_snapshot(Path(directory))

    def test_modified_model_config_rejected_before_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "LICENSE").write_text("not the pinned model license")
            with self.assertRaisesRegex(GenerationError, "hash mismatch"):
                verify_snapshot(directory)

    def test_unpinned_auxiliary_model_file_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "added_tokens.json").write_text("{}")
            with self.assertRaisesRegex(GenerationError, "unexpected files"):
                verify_snapshot(directory)

    def test_generation_options_bounded_before_import(self):
        for options in [{"max_new_tokens": True}, {"max_new_tokens": 257}, {"threads": 0}, {"threads": 9}]:
            with self.subTest(options=options), self.assertRaises(GenerationError):
                LocalQwenGenerator("not-used", **options)

    def test_bad_question_fails_before_any_model_access(self):
        generator = LocalQwenGenerator.__new__(LocalQwenGenerator)
        for question in [None, " ", "x" * 2049, "\ud800"]:
            with self.subTest(question=repr(question)[:30]), self.assertRaises(GenerationError):
                generator.generate(question, self.chunks)


if __name__ == "__main__":
    unittest.main()
