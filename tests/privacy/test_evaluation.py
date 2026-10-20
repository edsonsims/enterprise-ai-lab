"""Recorded-span integrity and denominator regressions for synthetic development data."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from enterprise_ai_lab.privacy_proxy import Span
from enterprise_ai_lab.privacy_proxy import evaluation as ev


class DevelopmentEvaluationTests(unittest.TestCase):
    def test_packaged_corpus_and_separate_exact_span_denominators(self):
        report = ev.evaluate_corpus()
        self.assertEqual(report['corpus']['split'], 'development')
        self.assertEqual(report['corpus']['sha256'], ev.CORPUS_SHA256)
        self.assertEqual(report['metrics']['cases'], {'total': 36, 'succeeded': 36, 'failed': 0})
        self.assertEqual(report['metrics']['supported']['gold'], 29)
        self.assertEqual(report['metrics']['all_annotations']['gold'], 40)
        self.assertEqual(report['metrics']['unsupported'], {'gold': 11, 'matched': 0, 'missed': 11, 'coverage': 0.0})
        self.assertEqual(ev.recompute(report), report['metrics'])
        by_id = {case['id']: case for case in report['cases']}
        self.assertEqual(by_id['dev-035']['gold'], [])
        self.assertEqual([s['kind'] for s in by_id['dev-035']['false_positives']], ['EMAIL'])
        self.assertEqual([s['kind'] for s in by_id['dev-036']['misses']], ['EMAIL'])
        self.assertEqual(len(by_id['dev-006']['gold']), 2)
        self.assertNotEqual(by_id['dev-006']['gold'][0]['start'], by_id['dev-006']['gold'][1]['start'])
        for field in ('supported', 'all_annotations'):
            metric = report['metrics'][field]
            self.assertEqual(metric['precision'], metric['true_positives'] / metric['predicted'])
            self.assertEqual(metric['recall'], metric['true_positives'] / metric['gold'])
            self.assertEqual(metric['f1'], 2 * metric['precision'] * metric['recall'] /
                             (metric['precision'] + metric['recall']))

    def test_gold_never_enters_detector_and_unicode_offsets_are_not_bytes(self):
        with patch.object(ev, 'detect', wraps=ev.detect) as calls:
            report = ev.evaluate_corpus()
        self.assertEqual(calls.call_count, 36)
        self.assertEqual([call.args for call in calls.call_args_list], [(case['text'],) for case in report['cases']])
        self.assertTrue(all(not call.kwargs for call in calls.call_args_list))
        case = report['cases'][3]
        span = case['gold'][0]
        self.assertEqual(case['text'][span['start']:span['end']], span['text'])
        self.assertNotEqual(span['start'], len(case['text'][:span['start']].encode('utf-8')))

    def test_failed_case_remains_and_gold_stays_in_denominator_without_error_details(self):
        real = ev.detect
        sentinel = 'private-sentinel-must-not-enter-report'
        def failing(text):
            if text == 'Write to cedar@inbox.invalid.':
                raise RuntimeError(sentinel)
            return real(text)
        with patch.object(ev, 'detect', side_effect=failing):
            report = ev.evaluate_corpus()
            self.assertEqual(ev.recompute(report), report['metrics'])
        self.assertEqual(report['metrics']['cases'], {'total': 36, 'succeeded': 35, 'failed': 1})
        self.assertEqual(report['metrics']['supported']['gold'], 29)
        self.assertEqual(report['metrics']['all_annotations']['gold'], 40)
        self.assertEqual(report['cases'][0]['error'], 'detector_failure')
        self.assertEqual(report['cases'][0]['predicted'], [])
        self.assertEqual(report['cases'][0]['misses'], report['cases'][0]['gold'])
        self.assertNotIn(sentinel, json.dumps(report))

    def test_invalid_or_duplicate_detector_spans_are_failures_not_silent_repairs(self):
        for result in ([Span('EMAIL', 0, 1)], (Span('EMAIL', True, 1),),
                       (Span('EMAIL', 0, 1), Span('EMAIL', 0, 1)),
                       (Span('NAME', 0, 1),), (Span('EMAIL', 0, 40000),)):
            with self.subTest(result=result), patch.object(ev, 'detect', return_value=result):
                report = ev.evaluate_corpus()
                self.assertEqual(report['metrics']['cases']['failed'], 36)
                self.assertEqual(report['metrics']['all_annotations']['gold'], 40)
                self.assertEqual(report['metrics']['all_annotations']['true_positives'], 0)
                self.assertTrue(all(c['error'] == 'invalid_detector_output' for c in report['cases']))
                ev.recompute(report)

    def assert_bad(self, report):
        with self.assertRaises(ev.EvaluationError):
            ev.recompute(report)

    def test_dropped_duplicate_extra_or_reordered_case_ids_are_rejected(self):
        original = ev.evaluate_corpus()
        for transform in [lambda cases: cases.pop(), lambda cases: cases.append(deepcopy(cases[0])),
                          lambda cases: cases[0].update(id='extra-001'), lambda cases: cases.reverse()]:
            report = deepcopy(original); transform(report['cases']); self.assert_bad(report)

    def test_gold_prediction_and_derived_span_manipulation_is_rejected(self):
        original = ev.evaluate_corpus()
        mutations = [lambda r: r['cases'][0]['gold'].clear(),
                     lambda r: r['cases'][0]['predicted'].clear(),
                     lambda r: r['cases'][0]['predicted'].append(deepcopy(r['cases'][0]['predicted'][0])),
                     lambda r: r['cases'][0]['predicted'][0].update(end=999),
                     lambda r: r['cases'][0]['gold'][0].update(start=True),
                     lambda r: r['cases'][0]['true_positives'].clear(),
                     lambda r: r['cases'][34]['false_positives'].clear(),
                     lambda r: r['cases'][35]['misses'].clear(),
                     lambda r: r['cases'][0].update(text='different text'),
                     lambda r: r['cases'][0].update(error='silently_skipped')]
        for mutation in mutations:
            report = deepcopy(original); mutation(report); self.assert_bad(report)
        # Even consistently edited counts cannot hide a deleted false positive.
        report = deepcopy(original)
        report['cases'][34]['predicted'] = []
        report['cases'][34]['false_positives'] = []
        report['metrics'] = ev._metrics(report['cases'])
        self.assert_bad(report)

    def test_nonfinite_values_boolean_counts_and_metrics_replacement_are_rejected(self):
        original = ev.evaluate_corpus()
        for field, value in [('precision', float('nan')), ('recall', float('inf')),
                             ('f1', -float('inf')), ('true_positives', True),
                             ('gold', 29.0), ('false_negatives', -1)]:
            report = deepcopy(original); report['metrics']['supported'][field] = value; self.assert_bad(report)
        report = deepcopy(original); report['metrics']['unsupported']['gold'] = 0; self.assert_bad(report)
        report = deepcopy(original); report['metrics']['cases']['failed'] = 1; self.assert_bad(report)

    def test_corpus_sources_protocol_schema_and_timestamp_are_bound(self):
        original = ev.evaluate_corpus()
        mutations = [lambda r: r.update(schema='future'), lambda r: r.update(extra='field'),
                     lambda r: r.update(created_at='2026-10-20'),
                     lambda r: r['corpus'].update(sha256='0' * 64),
                     lambda r: r['corpus'].update(split='heldout'),
                     lambda r: r['source_sha256'].update({'privacy_proxy/detector.py': '0' * 64}),
                     lambda r: r['formulae'].update(zero_denominator='1.0'),
                     lambda r: r['runtime'].update(machine='bad\nvalue')]
        for mutation in mutations:
            report = deepcopy(original); mutation(report); self.assert_bad(report)
        with patch.object(ev, '_source_hashes', return_value={}):
            self.assert_bad(original)

    def test_explicit_corpus_copy_must_match_packaged_hash_and_be_regular(self):
        source = ev.resources.files('enterprise_ai_lab').joinpath(ev.CORPUS_PATH).read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'copy.json'; path.write_bytes(source)
            report = ev.evaluate_corpus(path)
            self.assertEqual(report['corpus']['sha256'], ev.CORPUS_SHA256)
            for raw in [source + b' ', b'{"duplicate":1,"duplicate":2}', b'[]', b' ' * 262145]:
                path.write_bytes(raw)
                with self.assertRaises(ev.EvaluationError):
                    ev.evaluate_corpus(path)
            path.write_bytes(source)
            link = Path(directory) / 'link.json'; link.symlink_to(path)
            with self.assertRaises(ev.EvaluationError):
                ev.evaluate_corpus(link)
            with self.assertRaises(ev.EvaluationError):
                ev.evaluate_corpus(Path(directory))


if __name__ == '__main__':
    unittest.main()
