"""Exact-span development evaluation over a frozen, fictional local corpus."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from importlib import resources
import json
import math
import os
from pathlib import Path
import platform
import re
import stat

from .contracts import KINDS, Span
from .detector import detect

CORPUS_SHA256 = '38974426ef1c84ae6899de815164df51e72cbeface00dc4abc7015e36fbf9279'
CORPUS_PATH = 'privacy_proxy/data/synthetic-v1.json'
REPORT_SCHEMA = 'privacy-development-report-v1'
_UNSUPPORTED = ('NAME', 'ADDRESS', 'LOCAL_PHONE', 'UNICODE_EMAIL')
_ALL_KINDS = tuple(KINDS) + _UNSUPPORTED
_MAX_CORPUS_BYTES = 262144
_SOURCE_FILES = ('privacy_proxy/contracts.py', 'privacy_proxy/detector.py',
                 'privacy_proxy/evaluation.py')
_FORMULAE = {
    'match': 'exact equality of (kind, start, end), using Python Unicode code-point offsets',
    'precision': 'true_positives / (true_positives + false_positives)',
    'recall': 'true_positives / (true_positives + false_negatives)',
    'f1': '2 * precision * recall / (precision + recall)',
    'zero_denominator': '0.0',
    'supported': 'Only gold/predicted EMAIL, PAN and PHONE kinds; unhandled formats remain in the denominator.',
    'all_annotations': 'All supported and unsupported gold kinds; every predicted supported span remains in precision.',
    'unsupported_coverage': 'Exact matched unsupported gold spans / all unsupported gold spans; not general privacy coverage.',
    'failure_policy': 'Keep every case; failed/invalid predictions are empty, gold remains in recall denominators and failures are counted.',
    'integrity': 'Recompute validates frozen corpus and current source hashes, re-runs detect(text) to verify predictions, then recomputes metrics.',
}


class EvaluationError(ValueError):
    """A corpus or recorded report does not satisfy this versioned protocol."""


def _invalid():
    raise EvaluationError('Invalid frozen development corpus or evaluation report.')


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _invalid()
            result[key] = value
        return result
    def constant(_value):
        _invalid()
    try:
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
        _json_tree(result)
        return result
    except (UnicodeError, ValueError, RecursionError, TypeError):
        _invalid()


def _json_tree(value, depth=0, budget=None):
    if budget is None:
        budget = [20000]
    budget[0] -= 1
    if depth > 24 or budget[0] < 0:
        _invalid()
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                _invalid()
            _json_tree(key, depth + 1, budget)
            _json_tree(item, depth + 1, budget)
    elif type(value) is list:
        for item in value:
            _json_tree(item, depth + 1, budget)
    elif type(value) is str:
        try:
            if len(value.encode('utf-8')) > 32768:
                _invalid()
        except UnicodeError:
            _invalid()
    elif type(value) is float:
        if not math.isfinite(value):
            _invalid()
    elif value is not None and type(value) not in (bool, int):
        _invalid()


def _same(actual, expected):
    """JSON equality that cannot confuse booleans, integral floats and counts."""
    if type(actual) is not type(expected):
        _invalid()
    if type(expected) is dict:
        if actual.keys() != expected.keys():
            _invalid()
        for key, value in expected.items():
            _same(actual[key], value)
    elif type(expected) is list:
        if len(actual) != len(expected):
            _invalid()
        for item, value in zip(actual, expected):
            _same(item, value)
    elif actual != expected:
        _invalid()


def _read_path(path):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _MAX_CORPUS_BYTES:
            _invalid()
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            raw = stream.read(_MAX_CORPUS_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (len(raw) != info.st_size or after.st_size != info.st_size
                or after.st_mtime_ns != info.st_mtime_ns or len(raw) > _MAX_CORPUS_BYTES):
            _invalid()
        return raw
    except (OSError, TypeError, ValueError):
        _invalid()
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _load_corpus(path=None):
    try:
        raw = (resources.files('enterprise_ai_lab').joinpath(CORPUS_PATH).read_bytes()
               if path is None else _read_path(path))
        if not 0 < len(raw) <= _MAX_CORPUS_BYTES or _digest(raw) != CORPUS_SHA256:
            _invalid()
        corpus = _strict_json(raw)
        if (corpus['schema'] != 'privacy-development-corpus-v1'
                or corpus['corpus_id'] != 'synthetic-v1' or corpus['split'] != 'development'
                or len(corpus['cases']) != 36):
            _invalid()
        for index, case in enumerate(corpus['cases'], 1):
            if case['id'] != f'dev-{index:03d}':
                _invalid()
            seen = set()
            for span in case['gold']:
                key = _key(span)
                if (span['kind'] not in _ALL_KINDS or key in seen
                        or type(span['start']) is not int or type(span['end']) is not int
                        or not 0 <= span['start'] < span['end'] <= len(case['text'])
                        or case['text'][span['start']:span['end']] != span['text']):
                    _invalid()
                seen.add(key)
        return corpus
    except (OSError, KeyError, TypeError, ValueError, RecursionError):
        _invalid()


def _source_hashes():
    root = resources.files('enterprise_ai_lab')
    return {path: _digest(root.joinpath(path).read_bytes()) for path in _SOURCE_FILES}


def _key(span):
    return span['kind'], span['start'], span['end']


def _prediction(text):
    try:
        # Gold labels, IDs and context tags never cross the runtime boundary.
        spans = detect(text)
    except Exception:
        return [], 'detector_failure'
    if type(spans) is not tuple:
        return [], 'invalid_detector_output'
    result = []
    previous_end = 0
    for span in spans:
        if (type(span) is not Span or type(span.kind) is not str or span.kind not in KINDS
                or type(span.start) is not int or type(span.end) is not int
                or not previous_end <= span.start < span.end <= len(text)):
            return [], 'invalid_detector_output'
        result.append({'kind': span.kind, 'start': span.start, 'end': span.end,
                       'text': text[span.start:span.end]})
        previous_end = span.end
    return result, None


def _observations(corpus):
    observations = []
    for case in corpus['cases']:
        predicted, error = _prediction(case['text'])
        gold_keys = {_key(s) for s in case['gold']}
        predicted_keys = {_key(s) for s in predicted}
        observations.append({**deepcopy(case), 'predicted': predicted,
            'true_positives': [s for s in predicted if _key(s) in gold_keys],
            'misses': [deepcopy(s) for s in case['gold'] if _key(s) not in predicted_keys],
            'false_positives': [s for s in predicted if _key(s) not in gold_keys], 'error': error})
    return observations


def _score(gold, predicted):
    tp = len(gold & predicted)
    fp = len(predicted - gold)
    fn = len(gold - predicted)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {'gold': len(gold), 'predicted': len(predicted), 'true_positives': tp,
            'false_positives': fp, 'false_negatives': fn, 'precision': precision,
            'recall': recall, 'f1': 2 * precision * recall / (precision + recall)
            if precision + recall else 0.0}


def _metrics(observations):
    gold = {(c['id'], *_key(s)) for c in observations for s in c['gold']}
    predicted = {(c['id'], *_key(s)) for c in observations for s in c['predicted']}
    supported_gold = {s for s in gold if s[1] in KINDS}
    unsupported_gold = {s for s in gold if s[1] in _UNSUPPORTED}
    unsupported_matched = unsupported_gold & predicted
    return {'cases': {'total': len(observations),
                      'succeeded': sum(c['error'] is None for c in observations),
                      'failed': sum(c['error'] is not None for c in observations)},
            'supported': _score(supported_gold, {s for s in predicted if s[1] in KINDS}),
            'all_annotations': _score(gold, predicted),
            'per_kind': {kind: _score({s for s in gold if s[1] == kind},
                                    {s for s in predicted if s[1] == kind}) for kind in _ALL_KINDS},
            'unsupported': {'gold': len(unsupported_gold), 'matched': len(unsupported_matched),
                            'missed': len(unsupported_gold - predicted),
                            'coverage': len(unsupported_matched) / len(unsupported_gold)
                            if unsupported_gold else 0.0}}


def _corpus_metadata(corpus):
    return {'id': corpus['corpus_id'], 'split': corpus['split'], 'schema': corpus['schema'],
            'package_path': CORPUS_PATH, 'sha256': CORPUS_SHA256, 'case_count': len(corpus['cases']),
            'created_date': corpus['created_date'], 'source': deepcopy(corpus['source']),
            'protocol': deepcopy(corpus['protocol'])}


def evaluate_corpus(path=None) -> dict:
    """Run the fixed development corpus; an explicit path must match its SHA256."""
    corpus = _load_corpus(path)
    observations = _observations(corpus)
    return {'schema': REPORT_SCHEMA, 'created_at': datetime.now(timezone.utc).isoformat(),
            'corpus': _corpus_metadata(corpus), 'formulae': deepcopy(_FORMULAE),
            'source_sha256': _source_hashes(),
            'runtime': {'implementation': platform.python_implementation(),
                        'python': platform.python_version(), 'system': platform.system(),
                        'machine': platform.machine()},
            'cases': observations, 'metrics': _metrics(observations)}


def recompute(report) -> dict:
    """Verify pinned source/predictions, then recompute every recorded metric.

    This intentionally re-runs the deterministic detector with text only. A
    historical report requires its matching source checkout; source differences
    are rejected rather than silently relabeling earlier predictions.
    """
    try:
        _json_tree(report)
        if type(report) is not dict or set(report) != {
                'schema', 'created_at', 'corpus', 'formulae', 'source_sha256',
                'runtime', 'cases', 'metrics'}:
            _invalid()
        _same(report['schema'], REPORT_SCHEMA)
        timestamp = report['created_at']
        if type(timestamp) is not str or len(timestamp) > 40:
            _invalid()
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            _invalid()
        runtime = report['runtime']
        if type(runtime) is not dict or set(runtime) != {'implementation', 'python', 'system', 'machine'}:
            _invalid()
        for value in runtime.values():
            if type(value) is not str or not 1 <= len(value) <= 128 or re.search(r'[\x00-\x1f\x7f]', value):
                _invalid()
        corpus = _load_corpus()
        _same(report['corpus'], _corpus_metadata(corpus))
        _same(report['formulae'], _FORMULAE)
        _same(report['source_sha256'], _source_hashes())
        expected = _observations(corpus)
        _same(report['cases'], expected)
        metrics = _metrics(expected)
        _same(report['metrics'], metrics)
        return deepcopy(metrics)
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError, AttributeError):
        _invalid()
