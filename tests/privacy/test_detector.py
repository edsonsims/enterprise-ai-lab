"""Authored synthetic examples exercise the explicitly narrow contract."""
import unittest

from enterprise_ai_lab.privacy_proxy import PrivacyError, detect
from enterprise_ai_lab.privacy_proxy.contracts import validate_id, validate_scope, validate_text


class DetectorTests(unittest.TestCase):
    def test_exact_unicode_offsets_and_supported_kinds(self):
        text = "é🙂 alice@example.invalid; +1 (416) 555-0123; 4111 1111 1111 1111."
        spans = detect(text)
        self.assertEqual([s.kind for s in spans], ["EMAIL", "PHONE", "PAN"])
        values = ["alice@example.invalid", "+1 (416) 555-0123", "4111 1111 1111 1111"]
        self.assertEqual([text[s.start:s.end] for s in spans], values)
        self.assertEqual(spans[0].start, text.index("alice"))
        self.assertNotEqual(spans[0].start, len(text[:spans[0].start].encode()))

    def test_email_punctuation_and_narrow_boundaries(self):
        self.assertEqual(len(detect("Reach alice+demo@example.invalid.")), 1)
        for text in ["éa@example.invalid", "a@éxample.invalid", ".a@example.invalid",
                     "a..b@example.invalid", "a@host", "a@host.invalid2", "a@host.invalid-", "a@host.invalid.2bad"]:
            with self.subTest(text=text):
                self.assertEqual(detect(text), ())

    def test_phone_requires_explicit_international_prefix(self):
        for text in ["+14165550123", "+1-416-555-0123", "+1 416 555 0123", "+1 (416)5550123"]:
            with self.subTest(text=text):
                self.assertEqual([(s.kind, text[s.start:s.end]) for s in detect(text)], [("PHONE", text)])
        for text in ["416-555-0123", "+44 20 7946 0958", "+141655501234", "x+14165550123", "+１4165550123"]:
            with self.subTest(text=text):
                self.assertEqual(detect(text), ())

    def test_pan_luhn_length_and_separators(self):
        for text in ["4111111111111111", "4111-1111-1111-1111", "4222222222222", "4000000000000000006"]:
            with self.subTest(text=text):
                self.assertEqual([s.kind for s in detect(text)], ["PAN"])
        for text in ["4111111111111112", "111111111111", "00000000000000000000", "1234 5678 9012 3456 7890"]:
            with self.subTest(text=text):
                self.assertEqual(detect(text), ())

    def test_email_wins_over_pan_inside_local_part(self):
        text = "4111111111111111@example.invalid"
        self.assertEqual([(s.kind, s.start, s.end) for s in detect(text)], [("EMAIL", 0, len(text))])

    def test_input_bound_is_utf8_and_reserved_prefix_is_rejected(self):
        self.assertEqual(detect("é" * 16384), ())
        for text in ["é" * 16385, "x" * 32769, "\ud800", None, 1, "ordinary [[PII:anything]]"]:
            with self.subTest(type=type(text).__name__):
                with self.assertRaises(PrivacyError) as error:
                    detect(text)
                self.assertEqual(error.exception.code, "invalid_input")

    def test_scope_and_identifier_are_exact_ascii(self):
        validate_scope("tenant:a", "client-1")
        validate_id("a" * 32)
        for value in ["", "a\n", "é", "a" * 65, True, None]:
            with self.subTest(value=value):
                with self.assertRaises(PrivacyError):
                    validate_scope(value, "client")
        for value in ["A" * 32, "0" * 31, "0" * 32 + "\n", None]:
            with self.assertRaises(PrivacyError):
                validate_id(value)
        self.assertEqual(validate_text("[[PII:bad]]"), "[[PII:bad]]")

    def test_errors_never_echo_unknown_code(self):
        error = PrivacyError("secret@example.invalid")
        self.assertEqual(str(error), "storage_unavailable")
        self.assertEqual(repr(error), "PrivacyError('storage_unavailable')")


if __name__ == "__main__":
    unittest.main()
