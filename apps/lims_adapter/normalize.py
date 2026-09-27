"""Normalize vendor LIMS payloads into AI Compliance evidence rows."""

from __future__ import annotations

from typing import Any, Iterable, List, Optional


def _match_details(text_value: Any, keywords: Iterable[str]) -> List[str]:
    text = str(text_value or "").lower()
    return [kw for kw in keywords if kw in text]


def _first(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def normalize_lims_record(
    raw: dict,
    *,
    req_code: Optional[str] = None,
    keywords: Optional[List[str]] = None,
) -> Optional[dict]:
    """
    Map a vendor record (or already-normalized evidence dict) to the shape
    expected by gap analysis / evidence lists.
    """
    if not isinstance(raw, dict):
        return None

    keywords = keywords or []
    lims_type = str(
        _first(raw.get("lims_type"), raw.get("type"), raw.get("record_type")) or ""
    ).strip().lower()

    # Already in evidence shape
    if raw.get("from_lims") and raw.get("snippet"):
        out = dict(raw)
        out.setdefault("source_type", "lims")
        out.setdefault("requirement_query", req_code)
        out.setdefault("link", raw.get("link"))
        return out

    # Infer type from common field names when vendor omits lims_type
    if not lims_type:
        if _first(raw.get("result"), raw.get("test_result"), raw.get("sample_name")):
            lims_type = "test_result"
        elif _first(raw.get("action_type"), raw.get("audit_type")):
            lims_type = "audit_action"
        else:
            lims_type = "record"

    rid = _first(raw.get("id"), raw.get("record_id"), raw.get("external_id"), "?")
    link = _first(raw.get("link"), raw.get("url"), raw.get("permalink"))
    match_reason = raw.get("match_reason")
    snippet = raw.get("snippet")
    document_name = raw.get("document_name")
    title = raw.get("title")

    if lims_type in ("test_result", "test", "result"):
        lims_type = "test_result"
        sample_name = _first(raw.get("sample_name"), raw.get("sample"), "") or ""
        test_name = _first(raw.get("test_id"), raw.get("test_name"), raw.get("test"), "") or ""
        result_val = _first(raw.get("result"), raw.get("test_result"), raw.get("value"), "") or ""
        notes_val = _first(raw.get("notes"), raw.get("comment"), "") or ""
        timestamp = _first(raw.get("commit_timestamp"), raw.get("timestamp"), raw.get("updated_at"), "") or ""
        if not snippet:
            snippet = (
                f"Test Result (ID: {rid}) - Sample: {sample_name}, "
                f"Test: {test_name}, Result: {result_val}"
            )
            if timestamp:
                snippet += f" [{timestamp}]"
        if not document_name:
            document_name = f"LIMS Test Result - ID {rid}"
        if not title:
            title = "Laboratory Test Result"
        if not match_reason:
            match_fields = []
            if _match_details(result_val, keywords):
                match_fields.append("result")
            if _match_details(notes_val, keywords):
                match_fields.append("notes")
            match_reason = (
                "Matched requirement keywords in "
                + (", ".join(match_fields) if match_fields else "test result text")
            )
        evidence_id = f"lims_test_{rid}"
    elif lims_type in ("audit_action", "action", "audit", "audit_log"):
        lims_type = "audit_action"
        action_type = _first(raw.get("action_type"), raw.get("audit_type"), "") or ""
        user_id = _first(raw.get("user_id"), raw.get("user"), raw.get("actor"), "") or ""
        timestamp = _first(raw.get("commit_timestamp"), raw.get("timestamp"), raw.get("updated_at"), "") or ""
        details = _first(raw.get("description"), raw.get("payload"), raw.get("details"), "") or ""
        if not snippet:
            snippet = f"Audit Action (ID: {rid}) - Type: {action_type}, User: {user_id}"
            if timestamp:
                snippet += f" [{timestamp}]"
            if details:
                snippet += f" - {str(details)[:120]}"
        if not document_name:
            document_name = f"LIMS Audit Action - ID {rid}"
        if not title:
            title = "Audit/Action Log Entry"
        if not match_reason:
            match_fields = []
            if _match_details(action_type, keywords):
                match_fields.append("action_type")
            if _match_details(details, keywords):
                match_fields.append("description")
            match_reason = (
                "Matched requirement keywords in "
                + (", ".join(match_fields) if match_fields else "audit action text")
            )
        evidence_id = f"lims_action_{rid}"
    else:
        if not snippet:
            snippet = str(
                _first(raw.get("summary"), raw.get("text"), raw.get("content"), rid) or rid
            )[:500]
        if not document_name:
            document_name = f"LIMS Record - ID {rid}"
        if not title:
            title = "LIMS Record"
        if not match_reason:
            match_reason = "Returned by external LIMS API"
        evidence_id = f"lims_record_{rid}"

    return {
        "id": str(raw.get("evidence_id") or evidence_id),
        "document_name": str(document_name),
        "title": str(title),
        "snippet": str(snippet),
        "link": link,
        "requirement_query": req_code,
        "from_lims": True,
        "lims_type": lims_type,
        "source_type": "lims",
        "match_reason": str(match_reason),
    }


def normalize_lims_payload(
    payload: Any,
    *,
    req_code: Optional[str] = None,
    keywords: Optional[List[str]] = None,
    max_records: int = 3,
) -> List[dict]:
    """Accept list or {records|results|data|items: [...]} and return evidence rows."""
    if payload is None:
        return []

    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = (
            payload.get("records")
            or payload.get("results")
            or payload.get("data")
            or payload.get("items")
            or []
        )
        if isinstance(rows, dict):
            rows = rows.get("records") or rows.get("results") or []
    else:
        return []

    out: List[dict] = []
    for raw in rows:
        normalized = normalize_lims_record(raw, req_code=req_code, keywords=keywords)
        if normalized:
            out.append(normalized)
        if len(out) >= max_records:
            break
    return out
