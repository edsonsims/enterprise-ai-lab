"""Small, inspectable evaluator with fail-closed document authorization.

Input roles and tenant IDs are trusted fixture inputs, not authenticated identities.
No generated answer quality, LLM behavior, or production privacy is measured.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections import Counter
from pathlib import Path


class ValidationError(ValueError):
    """A dataset or prediction violates the documented JSONL schema."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _constant(value):
    raise ValidationError(f"nonfinite JSON number: {value}")


def read_jsonl(path):
    rows = []
    with Path(path).open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                raise ValidationError(f"{path}:{line_number}: blank JSONL record")
            try:
                row = json.loads(line, object_pairs_hook=_object, parse_constant=_constant)
            except (ValueError, TypeError, RecursionError) as exc:
                raise ValidationError(f"{path}:{line_number}: {exc}") from exc
            if not isinstance(row, dict):
                raise ValidationError(f"{path}:{line_number}: expected an object")
            rows.append(row)
    return rows


def _fields(row, required, optional=()):
    if not isinstance(row, dict) or not set(required) <= row.keys():
        raise ValidationError(f"required fields: {', '.join(sorted(required))}")
    extra = row.keys() - set(required) - set(optional)
    if extra:
        raise ValidationError(f"unknown fields: {', '.join(sorted(extra))}")


def _string(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a nonempty string")


def _strings(value, field, allow_empty=True):
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ValidationError(f"{field} must be a list" + (" with entries" if not allow_empty else ""))
    for item in value:
        _string(item, field)
    if len(value) != len(set(value)):
        raise ValidationError(f"{field} contains duplicate IDs or values")


def _unique(rows, id_field):
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValidationError("each record must be an object")
        _string(row.get(id_field), id_field)
        if row[id_field] in seen:
            raise ValidationError(f"duplicate {id_field}: {row[id_field]}")
        seen.add(row[id_field])
    return seen


def authorized(document, case):
    """Missing identity/ACL attributes deny access, including empty role lists."""
    tenant = case.get("tenant_id")
    roles = case.get("roles")
    allowed = document.get("allowed_roles")
    return (isinstance(tenant, str) and bool(tenant.strip())
            and tenant == document.get("tenant_id")
            and isinstance(roles, list) and isinstance(allowed, list)
            and all(isinstance(role, str) and role.strip() for role in roles + allowed)
            and bool(set(roles) & set(allowed)))


def validate_dataset(documents, cases):
    if not documents or not cases:
        raise ValidationError("documents and cases must both contain records")
    _unique(documents, "id")
    _unique(cases, "id")
    by_id = {doc["id"]: doc for doc in documents}
    for doc in documents:
        _fields(doc, ("id", "tenant_id", "allowed_roles", "text"))
        _string(doc["tenant_id"], "tenant_id")
        _string(doc["text"], "text")
        _strings(doc["allowed_roles"], "allowed_roles", allow_empty=False)
    for case in cases:
        _fields(case, ("id", "tenant_id", "roles", "query", "relevant_doc_ids"), ("category",))
        _string(case["tenant_id"], "tenant_id")
        _string(case["query"], "query")
        _strings(case["roles"], "roles")
        _strings(case["relevant_doc_ids"], "relevant_doc_ids")
        if "category" in case:
            _string(case["category"], "category")
        for doc_id in case["relevant_doc_ids"]:
            if doc_id not in by_id:
                raise ValidationError(f"unknown relevant document: {doc_id}")
            if not authorized(by_id[doc_id], case):
                raise ValidationError(f"relevance includes an unauthorized document: {doc_id}")


def validate_predictions(predictions, documents, cases):
    case_ids = {case["id"] for case in cases}
    doc_ids = {doc["id"] for doc in documents}
    prediction_ids = _unique(predictions, "case_id")
    if prediction_ids != case_ids:
        raise ValidationError(f"prediction IDs differ: missing={sorted(case_ids - prediction_ids)}, "
                              f"unknown={sorted(prediction_ids - case_ids)}")
    for prediction in predictions:
        _fields(prediction, ("case_id", "retrieved_doc_ids", "refused"), ("scores",))
        _strings(prediction["retrieved_doc_ids"], "retrieved_doc_ids")
        unknown = set(prediction["retrieved_doc_ids"]) - doc_ids
        if unknown:
            raise ValidationError(f"unknown retrieved document IDs: {sorted(unknown)}")
        if not isinstance(prediction["refused"], bool):
            raise ValidationError("refused must be a boolean")
        if "scores" in prediction:
            scores = prediction["scores"]
            if not isinstance(scores, list) or len(scores) != len(prediction["retrieved_doc_ids"]):
                raise ValidationError("scores must align with retrieved_doc_ids")
            try:
                valid = all(not isinstance(score, bool) and isinstance(score, (int, float))
                            and math.isfinite(score) for score in scores)
            except OverflowError:
                valid = False
            if not valid:
                raise ValidationError("scores must contain finite numbers")


def _tokens(text):
    return re.findall(r"\w+", text.casefold(), flags=re.UNICODE)


def _rank(documents, query, k):
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValidationError("k must be a positive integer")
    terms = set(_tokens(query))
    scored = []
    for doc in documents:
        counts = Counter(_tokens(doc["text"]))
        score = sum(min(counts[term], 3) for term in terms)
        if score:
            scored.append((score, doc["id"]))
    return sorted(scored, key=lambda item: (-item[0], item[1]))[:k]


def retrieve(documents, case, k=3):
    """Apply tenant AND any-role policy before scoring; only return authorized context."""
    return _rank([doc for doc in documents if authorized(doc, case)], case["query"], k)


def unsafe_global_retrieve(documents, case, k=3):
    """LAB ONLY: deliberately omit ACL checks to make leakage measurable."""
    return _rank(documents, case["query"], k)


def predict(documents, cases, k=3, *, unsafe_global=False):
    ranker = unsafe_global_retrieve if unsafe_global else retrieve
    predictions = []
    for case in cases:
        ranked = ranker(documents, case, k)
        predictions.append({"case_id": case["id"], "retrieved_doc_ids": [row[1] for row in ranked],
                            "scores": [row[0] for row in ranked], "refused": not bool(ranked)})
    return predictions


def _rate(successes, total):
    """Descriptive Wilson 95% interval; repeated synthetic templates are not IID trials."""
    if not total:
        return {"numerator": successes, "denominator": total, "value": None, "wilson_95": None}
    z = 1.959963984540054
    p = successes / total
    scale = 1 + z * z / total
    center = (p + z * z / (2 * total)) / scale
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / scale
    return {"numerator": successes, "denominator": total, "value": p,
            "wilson_95": [max(0.0, center - half), min(1.0, center + half)]}


def evaluate(documents, cases, predictions):
    validate_dataset(documents, cases)
    validate_predictions(predictions, documents, cases)
    by_doc = {doc["id"]: doc for doc in documents}
    by_case = {row["case_id"]: row for row in predictions}
    raw = []
    hits = answerable = unanswerable = refusals = leaks = refs = leaked_refs = correct = 0
    recalls = []
    for case in cases:
        prediction = by_case[case["id"]]
        returned = prediction["retrieved_doc_ids"]
        denied = [doc_id for doc_id in returned if not authorized(by_doc[doc_id], case)]
        relevant = set(case["relevant_doc_ids"])
        found = len(relevant & set(returned))
        can_answer = bool(relevant)
        answerable += can_answer
        unanswerable += not can_answer
        hits += can_answer and found > 0
        refusals += not can_answer and prediction["refused"]
        correct += prediction["refused"] == (not can_answer)
        if can_answer:
            recalls.append(found / len(relevant))
        leaks += bool(denied)
        refs += len(returned)
        leaked_refs += len(denied)
        raw.append({**case, "prediction": prediction, "unauthorized_doc_ids": denied,
                    "answerable": can_answer, "hit": bool(found),
                    "recall": found / len(relevant) if relevant else None})
    metrics = {
        "retrieval_hit_rate_answerable": _rate(hits, answerable),
        "macro_recall_answerable": {"value": sum(recalls) / len(recalls) if recalls else None,
                                     "denominator": answerable},
        "query_leakage_rate": _rate(leaks, len(cases)),
        "returned_reference_leakage_rate": _rate(leaked_refs, refs),
        "refusal_accuracy_unanswerable": _rate(refusals, unanswerable),
        "abstention_classification_accuracy": _rate(correct, len(cases)),
    }
    categories = {}
    for category in sorted({row.get("category", "unspecified") for row in raw}):
        group = [row for row in raw if row.get("category", "unspecified") == category]
        unanswerable_group = [row for row in group if not row["answerable"]]
        categories[category] = {
            "case_count": len(group), "answerable_count": sum(row["answerable"] for row in group),
            "queries_with_leakage": sum(bool(row["unauthorized_doc_ids"]) for row in group),
            "correct_unanswerable_refusals": sum(row["prediction"]["refused"] for row in unanswerable_group),
            "unanswerable_count": len(unanswerable_group),
        }
    report = {"schema_version": 1, "case_count": len(cases), "document_count": len(documents),
              "categories": categories,
              "metrics": metrics,
              "limitations": ["Input provenance and relevance labels are not independently verified.",
                              "Wilson intervals are descriptive binomial calculations only; sampling independence is not established, including within-query document references.",
                              "Retrieval and abstention only; no generated answer quality is measured.",
                              "ACL fields are supplied inputs; identity authentication is out of scope."],
              "definitions": {"answerable": "At least one authorized relevant_doc_id in the fixture.",
                              "query_leakage_rate": "Queries returning >=1 unauthorized ID / all queries; empty retrieval counts as no leak.",
                              "returned_reference_leakage_rate": "Unauthorized returned IDs / all returned IDs; null if none returned.",
                              "refusal_accuracy_unanswerable": "refused=true / cases with no authorized relevant document.",
                              "retrieval_hit_rate_answerable": "Answerable cases retrieving >=1 relevant ID / answerable cases.",
                              "macro_recall_answerable": "Mean per-answerable-case fraction of relevant IDs retrieved."}}
    return report, raw


def synthetic_dataset(seed=42):
    """50 distinct policy docs; 100 distinct queries across 10 synthetic tenants.

    Seed changes reference anchors and case ordering, never fabricates independent
    observations. Policies and query phrasing remain deliberately templated.
    """
    rng = random.Random(seed)
    tenants = ["alder", "birch", "cedar", "dogwood", "elm", "fir", "ginkgo", "hazel", "iris", "juniper"]
    topics = [
        ("vpn", ["employee", "ops"], "vpn access setup", "Install the VPN client and enroll a hardware security key."),
        ("payroll", ["finance"], "payroll correction deadline", "Submit payroll corrections before the monthly cutoff."),
        ("incident", ["ops"], "incident escalation procedure", "Page the duty engineer and record the incident timeline."),
        ("travel", ["employee", "finance"], "travel reimbursement receipt", "Upload an itemized receipt for approved business travel."),
        ("cafe", ["employee"], "café accessibility réservation", "The café offers accessible seating; réservation requests use the staff portal."),
    ]
    documents, cases = [], []
    anchors = {}
    for tenant in tenants:
        for topic, roles, phrase, instruction in topics:
            doc_id = f"{tenant}-{topic}"
            anchor = "ref" + format(rng.getrandbits(64), "016x")
            anchors[doc_id] = anchor
            documents.append({"id": doc_id, "tenant_id": tenant, "allowed_roles": roles,
                              "text": f"{tenant.title()} {phrase}. {instruction} Reference {anchor}."})
            cases.append({"id": f"answer-{doc_id}", "tenant_id": tenant, "roles": [roles[0]],
                          "query": f"{tenant.title()} {phrase}", "relevant_doc_ids": [doc_id],
                          "category": "answerable_shared_terms"})
    for index, tenant in enumerate(tenants):
        for topic in ("payroll", "incident"):
            # Shared tenant terms retrieve irrelevant authorized material: ACL safety
            # alone cannot make lexical retrieval judge whether it can answer.
            query = f"When is {tenant.title()} payroll correction deadline?" if topic == "payroll" else anchors[f"{tenant}-{topic}"]
            category = "role_denied_shared_terms" if topic == "payroll" else "role_denied_anchor"
            cases.append({"id": f"denied-{tenant}-{topic}", "tenant_id": tenant, "roles": ["employee"],
                          "query": query, "relevant_doc_ids": [], "category": category})
        other_tenant = tenants[(index + 1) % len(tenants)]
        for topic in ("vpn", "cafe"):
            cases.append({"id": f"cross-{tenant}-{topic}", "tenant_id": tenant, "roles": ["employee"],
                          "query": anchors[f"{other_tenant}-{topic}"], "relevant_doc_ids": [],
                          "category": "cross_tenant_denied"})
        cases.append({"id": f"empty-{tenant}", "tenant_id": tenant, "roles": ["employee"],
                      "query": f"unlisted{index} 星雲{index}", "relevant_doc_ids": [], "category": "no_match"})
    rng.shuffle(cases)
    return documents, cases


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def write_jsonl(path, rows):
    Path(path).write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
                                  for row in rows), encoding="utf-8")


def write_bundle(output, documents, cases, predictions, *, label, seed=None, k=None):
    """Write only after complete validation. No timestamps or machine-specific paths."""
    report, raw = evaluate(documents, cases, predictions)
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=True)
    report["experiment"] = {"label": label, "seed": seed, "k": k}
    report["dataset_provenance"] = "packaged_synthetic_fixture" if seed is not None else "caller_supplied_unverified"
    if seed is not None:
        report["limitations"][0] = "Packaged synthetic, repeated-template fixture; no real customer or deployment evidence."
        report["limitations"].append("Correlated synthetic templates do not support confidence or generalization claims.")
    _write_json(directory / "report.json", report)
    for name, rows in (("documents", documents), ("cases", cases), ("predictions", predictions), ("results", raw)):
        write_jsonl(directory / f"{name}.jsonl", rows)
    names = ["report.json", "documents.jsonl", "cases.jsonl", "predictions.jsonl", "results.jsonl"]
    hashes = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in names}
    _write_json(directory / "manifest.json", {"algorithm": "sha256", "files": hashes})
    return report
