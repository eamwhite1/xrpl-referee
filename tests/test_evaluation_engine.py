"""Tests for Part B Phase 1 evaluation engine helpers."""
import hashlib
import json
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# We only import the pure functions — no FastAPI startup
from referee import _parse_criteria, _execute_code_gates, _build_audit_prompt, _validate_verdict


# ---------------------------------------------------------------------------
# _parse_criteria
# ---------------------------------------------------------------------------

def test_parse_criteria_plain_string():
    result = _parse_criteria("Write a summary")
    assert len(result) == 1
    assert result[0]["id"] == "c1"
    assert result[0]["type"] == "judgment"
    assert result[0]["description"] == "Write a summary"


def test_parse_criteria_list_of_strings():
    result = _parse_criteria(["criterion A", "criterion B"])
    assert len(result) == 2
    assert result[0]["id"] == "c1"
    assert result[1]["id"] == "c2"
    assert result[0]["type"] == "judgment"


def test_parse_criteria_structured_dicts():
    raw = [
        {"description": "Code compiles", "type": "code", "gate": "sha256_of", "expected_hash": "abc"},
        {"description": "PR merged", "type": "evidence"},
        {"description": "Reasonable approach"},
    ]
    result = _parse_criteria(raw)
    assert len(result) == 3
    assert result[0]["type"] == "code"
    assert result[1]["type"] == "evidence"
    assert result[2]["type"] == "judgment"


def test_parse_criteria_invalid_type_raises():
    with pytest.raises(ValueError, match="invalid type"):
        _parse_criteria([{"description": "test", "type": "unknown"}])


def test_parse_criteria_empty_raises():
    with pytest.raises(ValueError):
        _parse_criteria([])


def test_parse_criteria_none_raises():
    with pytest.raises(ValueError):
        _parse_criteria(None)


def test_parse_criteria_missing_description_raises():
    with pytest.raises(ValueError, match="missing 'description'"):
        _parse_criteria([{"type": "judgment"}])


# ---------------------------------------------------------------------------
# _execute_code_gates
# ---------------------------------------------------------------------------

def test_code_gate_sha256_pass():
    work = "hello world"
    expected = hashlib.sha256(work.strip().encode()).hexdigest()
    criteria = [{"id": "c1", "type": "code", "gate": "sha256_of", "expected_hash": expected}]
    failures = _execute_code_gates(work, criteria)
    assert failures == []


def test_code_gate_sha256_fail():
    criteria = [{"id": "c1", "type": "code", "gate": "sha256_of", "expected_hash": "deadbeef"}]
    failures = _execute_code_gates("anything", criteria)
    assert len(failures) == 1
    assert failures[0]["criterion_id"] == "c1"
    assert failures[0]["verdict"] == "FAIL"
    assert "sha256 mismatch" in failures[0]["reason"]


def test_code_gate_regex_pass():
    criteria = [{"id": "c1", "type": "code", "gate": "regex", "pattern": r"^[A-Z]{3}-\d{4}$"}]
    failures = _execute_code_gates("ABC-1234", criteria)
    assert failures == []


def test_code_gate_regex_fail():
    criteria = [{"id": "c1", "type": "code", "gate": "regex", "pattern": r"^[A-Z]{3}-\d{4}$"}]
    failures = _execute_code_gates("invalid", criteria)
    assert len(failures) == 1
    assert "regex" in failures[0]["reason"]


def test_code_gate_exact_match_pass():
    criteria = [{"id": "c1", "type": "code", "gate": "exact_match", "expected_value": "done"}]
    failures = _execute_code_gates("done", criteria)
    assert failures == []


def test_code_gate_exact_match_fail():
    criteria = [{"id": "c1", "type": "code", "gate": "exact_match", "expected_value": "done"}]
    failures = _execute_code_gates("not done", criteria)
    assert len(failures) == 1


def test_code_gate_skips_non_code_criteria():
    criteria = [{"id": "c1", "type": "judgment", "description": "looks good"}]
    failures = _execute_code_gates("anything", criteria)
    assert failures == []


# ---------------------------------------------------------------------------
# _build_audit_prompt
# ---------------------------------------------------------------------------

def test_build_audit_prompt_has_work_tags():
    prompt = _build_audit_prompt("task", "my work", [], [])
    assert "<WORK>" in prompt
    assert "my work" in prompt
    assert "</WORK>" in prompt


def test_build_audit_prompt_evidence_wrapped():
    evidence_items = [{"criterion_id": "c1", "url": "https://example.com", "content": "some text", "truncated": False}]
    prompt = _build_audit_prompt("task", "work", [{"id": "c1", "type": "evidence", "description": "check url"}], evidence_items)
    assert '<UNTRUSTED_EVIDENCE criterion_id="c1"' in prompt
    assert 'url="https://example.com"' in prompt
    assert "some text" in prompt
    assert "</UNTRUSTED_EVIDENCE>" in prompt


def test_build_audit_prompt_no_evidence_no_tags():
    prompt = _build_audit_prompt("task", "work", [{"id": "c1", "type": "judgment", "description": "check it"}], [])
    assert "UNTRUSTED_EVIDENCE" not in prompt


# ---------------------------------------------------------------------------
# _validate_verdict
# ---------------------------------------------------------------------------

def test_validate_verdict_evidence_quote_mismatch():
    criteria = [{"id": "c1", "type": "evidence", "description": "check url"}]
    evidence_items = [{"criterion_id": "c1", "url": "https://x.com", "content": "the real content here", "truncated": False}]
    verdict = {
        "overall_verdict": "PASS",
        "overall_score": 85,
        "criteria_results": [{"criterion_id": "c1", "verdict": "PASS", "score": 85, "reasoning": "ok", "evidence_quote": "not in the content"}],
    }
    errors = _validate_verdict(verdict, criteria, evidence_items)
    assert any("not a verbatim substring" in e for e in errors)


def test_validate_verdict_evidence_quote_match():
    criteria = [{"id": "c1", "type": "evidence", "description": "check url"}]
    evidence_items = [{"criterion_id": "c1", "url": "https://x.com", "content": "the real content here", "truncated": False}]
    verdict = {
        "overall_verdict": "PASS",
        "overall_score": 90,
        "criteria_results": [{"criterion_id": "c1", "verdict": "PASS", "score": 90, "reasoning": "ok", "evidence_quote": "real content"}],
    }
    errors = _validate_verdict(verdict, criteria, evidence_items)
    assert errors == []


def test_validate_verdict_pass_with_low_score():
    verdict = {"overall_verdict": "PASS", "overall_score": 30, "criteria_results": []}
    errors = _validate_verdict(verdict, [], [])
    assert any("score" in e for e in errors)


def test_validate_verdict_fail_with_high_score():
    verdict = {"overall_verdict": "FAIL", "overall_score": 80, "criteria_results": []}
    errors = _validate_verdict(verdict, [], [])
    assert any("score" in e for e in errors)


def test_validate_verdict_ok():
    verdict = {"overall_verdict": "PASS", "overall_score": 90, "criteria_results": []}
    errors = _validate_verdict(verdict, [], [])
    assert errors == []


def test_response_schema_no_list_type():
    """responseSchema must not use list-valued 'type' — Gemini rejects those with 400."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from referee import _build_response_schema

    def _check_no_list_type(obj, path=""):
        if isinstance(obj, dict):
            t = obj.get("type")
            assert not isinstance(t, list), f"List-valued 'type' at {path}: {t!r}"
            for k, v in obj.items():
                _check_no_list_type(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                _check_no_list_type(item, f"{path}[{i}]")

    schema = _build_response_schema()
    _check_no_list_type(schema)
