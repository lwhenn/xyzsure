import os
import json
import logging
import re
import itertools
import pandas as pd
import urllib.request
import csv
import io
import time
import copy
import threading
from html import escape
from flask import Blueprint, render_template, jsonify, request, redirect, url_for, make_response, send_file, current_app
from flask_login import current_user, login_required
from datetime import datetime, UTC, timedelta
from concurrent.futures import ThreadPoolExecutor
from apps import Google_API
from ai_service import ai_service
from .ai_utils import (
    CAP_SHEET_ID, CAP_FILE_NAME, CAP_SHEET_NAME, MODULE_ROOT,
    AI_SEARCH_REPORTS_FILE, AI_GAP_ANALYSIS_REPORTS_FILE, GCS_PATH_MAPPING_FILE,
    _load_ai_reports, _save_ai_reports,
    _load_gap_analysis_reports, _save_gap_analysis_reports,
    _load_gcs_path_mapping, _save_gcs_path_mapping,
    _extract_gcs_top_level_folder, _doc_filename, _normalize_doc_name, _extract_doc_code
)
from apps.Admin_Panel.shared_utils import get_val_fuzzy, get_column_letter, find_header_index


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

ai_engine = Blueprint(
    "ai_engine",
    __name__,
    template_folder="templates",
    url_prefix="/ai-engine",
)

# Global cycling index for distributing multiple documents to different links
_link_cycling_index = 0


def _format_evidence_list(res_list):
    """Format a list of evidence objects into a single string for Google Sheets.
    
    Extracts document names from evidence items and creates a clean, readable list.
    Handles both dict objects and plain string URLs.
    Uses semicolon separator to match UI page display.
    """
    if not res_list:
        return ""
    
    if isinstance(res_list, str):
        return res_list
    
    if not isinstance(res_list, (list, tuple)):
        return str(res_list)
    
    try:
        # Load inspection sheet links with caching to enable reverse lookup
        inspection_links = _get_inspection_sheet_links()
        logger.debug(f"[_format_evidence_list] Processing {len(res_list)} evidence items")
        
        doc_names = []
        
        for idx, item in enumerate(res_list):
            doc_name = None
            doc_link = None
            
            try:
                logger.debug(f"  [{idx}] Item type: {type(item).__name__}")
                
                # Handle dict objects (preferred format from Vertex AI search)
                if isinstance(item, dict):
                    # Strategy 1: Get document_name directly (best source)
                    doc_name = item.get("document_name")
                    if doc_name and str(doc_name).strip():
                        doc_name = str(doc_name).strip()
                        logger.debug(f"      ✓ Using document_name field: '{doc_name}'")
                        doc_names.append(doc_name)
                        continue
                    
                    # Strategy 2: Try alternative name fields
                    doc_name = item.get("title") or item.get("filename") or item.get("name")
                    if doc_name:
                        logger.debug(f"      ✓ Using alternative field: '{doc_name}'")
                        doc_names.append(doc_name)
                        continue
                    
                    # Strategy 3: If we have a link, try to extract name or do reverse lookup
                    doc_link = item.get("link") or item.get("url")
                    if doc_link:
                        # Try reverse lookup in inspection sheet
                        for sheet_doc_name, sheet_url in inspection_links.items():
                            if sheet_url and (sheet_url.strip() == doc_link.strip() or sheet_url in doc_link or doc_link in sheet_url):
                                logger.debug(f"      ✓ Reverse lookup found: '{sheet_doc_name}'")
                                doc_names.append(sheet_doc_name)
                                doc_name = sheet_doc_name
                                break
                        
                        # If reverse lookup didn't work, extract from URL
                        if not doc_name:
                            if "gs://" in doc_link:
                                doc_name = doc_link.split("/")[-1].replace("-", " ").strip()
                            elif "drive.google.com" in doc_link:
                                doc_name = "Google Drive Document"
                            else:
                                doc_name = doc_link.split("/")[-1] if "/" in doc_link else "Document"
                            logger.debug(f"      ✓ Extracted from URL: '{doc_name}'")
                            doc_names.append(doc_name)
                            continue
                
                # Handle string URLs (from previous storage or direct link format)
                elif isinstance(item, str):
                    item_str = item.strip()
                    logger.debug(f"      String value: {item_str[:60]}...")
                    
                    if item_str.startswith("http") or item_str.startswith("gs://"):
                        # It's a link - try reverse lookup first
                        doc_link = item_str
                        for sheet_doc_name, sheet_url in inspection_links.items():
                            if sheet_url and (sheet_url.strip() == doc_link.strip() or sheet_url in doc_link or doc_link in sheet_url):
                                logger.debug(f"      ✓ Reverse lookup found (from URL): '{sheet_doc_name}'")
                                doc_names.append(sheet_doc_name)
                                doc_name = sheet_doc_name
                                break
                        
                        # Extract from URL if reverse lookup failed
                        if not doc_name:
                            if "gs://" in item_str:
                                doc_name = item_str.split("/")[-1].replace("-", " ").strip()
                            elif "drive.google.com" in item_str:
                                doc_name = "Google Drive Document"
                            else:
                                doc_name = item_str.split("/")[-1] if "/" in item_str else "Document Link"
                            logger.debug(f"      ✓ Extracted from URL string: '{doc_name}'")
                            doc_names.append(doc_name)
                    else:
                        # Plain text - use as-is
                        logger.debug(f"      ✓ Plain text: '{item_str}'")
                        doc_names.append(item_str)
                
                else:
                    # Unknown type - convert to string
                    doc_names.append(str(item))
                    logger.debug(f"      Converted to string: {str(item)[:60]}")
            
            except Exception as e:
                logger.debug(f"    Error processing item {idx}: {e}")
                try:
                    doc_names.append(str(item))
                except Exception:
                    pass
        
        result = "; ".join(doc_names) if doc_names else ""
        logger.debug(f"[_format_evidence_list] Final result with {len(doc_names)} items: '{result[:150] if len(result) > 150 else result}'")
        return result
        
    except Exception as e:
        logger.error(f"[_format_evidence_list] Error: {e}", exc_info=True)
        return ""


_AI_SEARCH_EVIDENCE_SHEET_MAX_DOCS = 5
# Vertex Discovery ``AnswerQuery`` SearchSpec ``max_return_results`` (and Search ``page_size``);
# the API rejects values above 25 (INVALID_ARGUMENT).
_VERTEX_ANSWER_QUERY_MAX_RETURN_RESULTS = 25
# Discovery Search iterators may paginate far beyond page_size; never process unbounded hits.
_FALLBACK_SEARCH_HIT_HARD_MAX = 200
_TOP_AI_SEARCH_EVIDENCE_CAP_ARG_MISSING = object()


def _collect_discovery_search_hits(search_response, limit: int) -> list:
    """Collect Search API hits with an explicit cap (iterator may return every page)."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = _VERTEX_ANSWER_QUERY_MAX_RETURN_RESULTS
    if n <= 0:
        n = _VERTEX_ANSWER_QUERY_MAX_RETURN_RESULTS
    n = min(n, _FALLBACK_SEARCH_HIT_HARD_MAX)
    return list(itertools.islice(search_response or [], n))


def _effective_evidence_process_cap(max_evidence_param: int, search_only: bool) -> int:
    """Max evidence rows to extract links for per requirement (avoids hanging on huge fallback lists)."""
    if search_only:
        return 0
    if max_evidence_param and max_evidence_param > 0:
        return min(int(max_evidence_param), _FALLBACK_SEARCH_HIT_HARD_MAX)
    return _VERTEX_ANSWER_QUERY_MAX_RETURN_RESULTS


def _parse_max_ai_search_evidence_from_payload(payload):
    """Return positive int cap, or 0 meaning unlimited (all hits returned by search, up to API cap)."""
    if not isinstance(payload, dict) or "max_ai_search_evidence" not in payload:
        return _AI_SEARCH_EVIDENCE_SHEET_MAX_DOCS
    raw = payload["max_ai_search_evidence"]
    if raw is None:
        return 0
    if isinstance(raw, str) and not str(raw).strip():
        return 0
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _AI_SEARCH_EVIDENCE_SHEET_MAX_DOCS
    if n <= 0:
        return 0
    return min(max(n, 1), 200)


def _top_ai_search_evidence_for_sheet(evidence_list, max_docs=_TOP_AI_SEARCH_EVIDENCE_CAP_ARG_MISSING):
    """Keep only the top N matched documents for the AI Search Evidence column (sheet/export).

    Preserves API ranking order and skips duplicate dict items that share the same ``id``.

    ``max_docs``: omit second argument for default (3); ``0`` = no cap
    (all items in ``evidence_list``, still deduped by id).
    """
    if evidence_list is None or evidence_list is False:
        return evidence_list
    if isinstance(evidence_list, str):
        return evidence_list
    if not isinstance(evidence_list, (list, tuple)):
        return evidence_list
    if max_docs is _TOP_AI_SEARCH_EVIDENCE_CAP_ARG_MISSING:
        cap = _AI_SEARCH_EVIDENCE_SHEET_MAX_DOCS
    else:
        try:
            cap = int(max_docs)
        except (TypeError, ValueError):
            cap = _AI_SEARCH_EVIDENCE_SHEET_MAX_DOCS
    seen_ids = set()
    out = []
    for item in evidence_list:
        if cap > 0 and len(out) >= cap:
            break
        if isinstance(item, dict):
            doc_id = item.get("id")
            if doc_id is not None and str(doc_id).strip():
                key = str(doc_id).strip()
                if key in seen_ids:
                    continue
                seen_ids.add(key)
        out.append(item)
    return out


def _sheet_evidence_cap_from_obj(obj):
    """Resolve sheet/export cap: missing key → default top-3; ``null`` → unlimited (0)."""
    if not isinstance(obj, dict) or "max_ai_search_evidence" not in obj:
        return _TOP_AI_SEARCH_EVIDENCE_CAP_ARG_MISSING
    v = obj["max_ai_search_evidence"]
    if v is None:
        return 0
    try:
        return max(1, min(int(v), 500))
    except (TypeError, ValueError):
        return _TOP_AI_SEARCH_EVIDENCE_CAP_ARG_MISSING


def _sheet_evidence_cap_from_chk_or_report(chk, rpt):
    cap = _sheet_evidence_cap_from_obj(chk)
    if cap is not _TOP_AI_SEARCH_EVIDENCE_CAP_ARG_MISSING:
        return cap
    if isinstance(rpt, dict):
        return _sheet_evidence_cap_from_obj(rpt)
    return _TOP_AI_SEARCH_EVIDENCE_CAP_ARG_MISSING


def _cap_evidence_results_per_requirement(items, max_per_req):
    """Keep at most ``max_per_req`` evidence rows per ``requirement_query`` (order preserved)."""
    if not isinstance(items, list) or max_per_req <= 0:
        return items
    counts = {}
    out = []
    for item in items:
        if not isinstance(item, dict):
            out.append(item)
            continue
        rq = str(item.get("requirement_query") or "").strip() or "__none__"
        n = counts.get(rq, 0)
        if n >= max_per_req:
            continue
        counts[rq] = n + 1
        out.append(item)
    return out


# Max words for AI Search Summary / compliance Discovery answers (aligned with AI Gap Analysis).
_COMPLIANCE_SUMMARY_MAX_WORDS = 200

REMEDIATION_COLUMN_HEADER = "Remediation"
REMEDIATION_COLUMN_CANDIDATES = [
    "remediation",
    "action completion",
    "action_completion",
    "action complete",
    "completion status",
    "completion notes",
]


def _coalesce_remediation_value(source, default=""):
    """Read remediation text from a dict-like payload (supports legacy action_completion)."""
    if not isinstance(source, dict):
        return str(default or "").strip()
    return str(source.get("remediation") or source.get("action_completion") or default or "").strip()

# Appended to every compliance-mode Discovery preamble — require citations and grounded audit prose.
_COMPLIANCE_DISCOVERY_ANSWER_SUFFIX = (
    "\n\n[Required answer style — compliance search] HARD LIMIT: "
    f"{_COMPLIANCE_SUMMARY_MAX_WORDS} words or fewer — count before you finish. "
    "One or two concise paragraphs (at most 8 short sentences). Use only retrieved sources. "
    "After each phrase grounded in a source, add bracketed indices in source order: [1], [2], [3] "
    '(example: "The scope-of-service table [1] lists tests and turnaround times [2]."). '
    "When naming a file, include its extension. If records or operational evidence were requested "
    "but not found in retrieved sources, state explicitly what was not verified — never invent "
    "document IDs or record numbers. Complete sentences only—no bullets, lists, or markdown."
)

# Vertex Discovery ``answer_query`` rejects ``query.text`` longer than this (API error INVALID_ARGUMENT).
_VERTEX_ANSWER_QUERY_TEXT_MAX = 2000


def _strip_stored_ai_result_tags_from_query(q_text: str) -> str:
    """Remove persisted AI Search Summary/Evidence blocks from compliance query text."""
    text = str(q_text or "")
    for label in (
        "AI Search Summary",
        "Prior AI Search Summary",
        "AI Search Evidence",
    ):
        text = re.sub(rf"\[{re.escape(label)}:\s*[^\]]*\]", " ", text, flags=re.DOTALL)
    return re.sub(r"\s{2,}", " ", text).strip()


def _normalize_vertex_query_ai_search_summaries(q_text: str) -> str:
    """Strip stored AI result columns from compliance queries before Vertex search."""
    return _strip_stored_ai_result_tags_from_query(q_text)


def _fit_vertex_answer_query_text(q_text: str, max_len: int = _VERTEX_ANSWER_QUERY_TEXT_MAX) -> str:
    """Return ``q_text`` within Vertex ``answer_query`` query length limits."""
    text = _normalize_vertex_query_ai_search_summaries(str(q_text or ""))
    if not text or len(text) <= max_len:
        return text

    def _shrink_tagged_block(s: str, label: str, inner_cap: int) -> str:
        pat = re.compile(rf"\[{re.escape(label)}:\s*([^\]]*)\]", re.DOTALL)

        def repl(m):
            inner = (m.group(1) or "").strip()
            if inner_cap <= 0:
                return f"[{label}: (omitted; query length limit)]"
            if len(inner) <= inner_cap:
                return f"[{label}: {inner}]"
            return f"[{label}: {inner[:inner_cap].rstrip()} …]"

        return pat.sub(repl, s)

    for cap in (1200, 800, 500, 300, 150, 0):
        text = _shrink_tagged_block(text, "Note", cap)
        if len(text) <= max_len:
            return text
    for cap in (500, 300, 150, 0):
        text = _shrink_tagged_block(text, "Policy/Procedure", cap)
        if len(text) <= max_len:
            return text
    for cap in (400, 200, 80, 0):
        text = _shrink_tagged_block(text, "Onsite Note", cap)
        if len(text) <= max_len:
            return text

    suffix = "\n… [truncated]"
    keep = max_len - len(suffix)
    if keep < 1:
        return text[:max_len]
    return text[:keep].rstrip() + suffix


def _compliance_summary_word_count(text) -> int:
    if not text:
        return 0
    return len(str(text).split())


def _strip_retrieved_docs_evidence_preamble_paragraph(text: str) -> str:
    """
    Remove boilerplate the model or legacy pipelines added: a leading filename list,
    an echoed 'AI Search Summary:' label, the 'Supporting excerpts from retrieved documents:'
    section (anywhere in the string), and trailing lines that are only bracketed citation
    markers like [1].
    """
    def _drop_trailing_citation_only_lines(s: str) -> str:
        lines = s.splitlines()
        while lines:
            st = lines[-1].strip()
            if not st:
                lines.pop()
                continue
            if re.match(r"^\[\d+\]\s*$", st):
                lines.pop()
                continue
            break
        return "\n".join(lines).rstrip()

    raw = str(text or "").strip()
    if not raw:
        return raw
    # Header may appear after narrative on a new line or the same line; cut from first match to EOF.
    m = re.search(r"(?is)\s*Supporting excerpts(?: from retrieved documents)?\s*:", raw)
    if m:
        raw = raw[: m.start()].rstrip()
    t = re.sub(r"^AI Search Summary:\s*\n?", "", raw, count=1, flags=re.IGNORECASE).strip()
    lines = t.splitlines()
    if not lines:
        return _drop_trailing_citation_only_lines(t if t != raw else raw)
    first = lines[0].strip()
    if re.match(r"(?is)^Retrieved documents\b", first):
        rest = lines[1:]
        while rest and not rest[0].strip():
            rest = rest[1:]
        out = "\n".join(rest).strip()
        return _drop_trailing_citation_only_lines(out if out else "")
    if t != raw:
        return _drop_trailing_citation_only_lines(t)
    return _drop_trailing_citation_only_lines(raw)


def _hard_cap_summary_words(text: str, max_words: int) -> str:
    """Last-resort word cap (may clip mid-thought; avoids multi-hundred-word UI text)."""
    t = (text or "").strip()
    if not t:
        return t
    words = t.split()
    if len(words) <= max_words:
        return t
    clipped = " ".join(words[:max_words]).rstrip(",;:")
    if clipped and clipped[-1] not in ".!?":
        clipped += "."
    return clipped


def _shorten_compliance_summary_preserving_citations(text: str, max_words=None) -> str:
    """If Discovery returns a long answer, compress with LLM while keeping [1]/[2] citations."""
    lim = max_words if max_words is not None else _COMPLIANCE_SUMMARY_MAX_WORDS
    if not text or not str(text).strip():
        return text
    t = str(text).strip()
    if _compliance_summary_word_count(t) <= lim:
        return t
    if not getattr(ai_service, "api_key", None):
        logger.debug("Skipping compliance summary shorten: no AI API key configured")
        return _hard_cap_summary_words(t, lim)
    try:
        messages = [
            {
                "role": "system",
                "content": (
                    "You shorten laboratory audit evidence summaries. Reply with only the rewritten summary "
                    "paragraph, no title or preface."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Rewrite the following into at most {lim} words as ONE concise paragraph. "
                    "Rules: (1) Keep every bracketed numeric citation exactly as in the source — same numbers and "
                    "brackets like [1] [2]. (2) Do not add facts, documents, or conclusions not already stated. "
                    "(3) Remove repetition and combine sentences. (4) No bullets, headings, or markdown. "
                    "(5) Do not add a 'Supporting excerpts from retrieved documents' section or trailing lines "
                    "that are only citation markers like [1] or [2].\n\n"
                    f"{t}"
                ),
            },
        ]
        model = getattr(ai_service, "model", None)
        out = ai_service._call_llm(messages, max_tokens=900, temperature=0.1, model=model)
        out = (out or "").strip()
        ol = out.lower()
        if (
            not out
            or "ai analysis unavailable" in ol
            or "could not be parsed" in ol
            or ("truncated" in ol and "token" in ol)
        ):
            return _hard_cap_summary_words(t, lim)
        if _compliance_summary_word_count(out) > lim + 25:
            out = _hard_cap_summary_words(out, lim)
        return out
    except Exception as exc:
        logger.debug("Compliance summary shorten failed: %s", exc)
        return _hard_cap_summary_words(t, lim)


# Cache for inspection sheet document links (loaded once per request via _get_inspection_sheet_links)
_inspection_sheet_cache = None
_inspection_sheet_cache_time = None


def _get_google_sheets_retry_count():
    """Return the configured retry count for Google Sheets API requests."""
    try:
        return max(0, int(os.getenv("GOOGLE_SHEETS_NUM_RETRIES", "3")))
    except (TypeError, ValueError):
        logger.warning("Invalid GOOGLE_SHEETS_NUM_RETRIES value; defaulting to 3")
        return 3


def _is_retryable_google_api_error(error):
    """Return True for transient Google API errors worth surfacing as upstream failures."""
    status = getattr(getattr(error, "resp", None), "status", None)
    return status in {429, 500, 502, 503, 504}


def _build_google_sheets_service():
    """Build an authenticated Google Sheets API client and return it with the sheet id."""
    from googleapiclient.discovery import build
    from google.oauth2 import service_account

    sheet_id = os.getenv("INSPECTION_DOCUMENT_ID")
    if not sheet_id:
        raise RuntimeError("INSPECTION_DOCUMENT_ID not configured")

    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT") or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        raise RuntimeError("Google Service Account not configured")

    sa_info = json.loads(sa_json)
    credentials = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )

    return build('sheets', 'v4', credentials=credentials, cache_discovery=False), sheet_id


def _execute_google_sheets_request(request_obj, action_description):
    """Execute a Google Sheets request with configured retries for transient failures."""
    retry_count = _get_google_sheets_retry_count()
    logger.debug(
        "Executing Google Sheets request for %s with %s retries",
        action_description,
        retry_count,
    )
    return request_obj.execute(num_retries=retry_count)


def _format_corrective_actions_for_sheet(value):
    """Format corrective actions for a Google Sheets cell (newline-separated)."""
    if isinstance(value, (list, tuple)):
        parts = [str(item).strip() for item in value if str(item or "").strip()]
        return "\n".join(parts)
    return str(value or "").strip()


def _normalize_gap_analysis_status(status_value):
    """Normalize status values to the underscore format expected by AI gap analysis."""
    status_text = str(status_value or "").strip().upper().replace(" ", "_")
    status_text = status_text.replace("-", "_")
    if status_text in {"NONCOMPLIANT", "NON_COMPLIANT"}:
        return "NON_COMPLIANT"
    if status_text in {"NOT_APPLICABLE", "N_A", "NA"}:
        return "NOT_APPLICABLE"
    if status_text in {"WARNING", "CAUTION", "WARN", "PARTIAL"}:
        return "PARTIAL"
    return status_text or "UNKNOWN"


def _export_ai_search_report_to_sheet(data, target_tab=None, sheet_id=None):
    """Persist AI search report rows to the CAP compliance results sheet (background worker)."""
    target_tab = target_tab or os.getenv("CAP_RESULTS_TAB", "CUS AI Reports")
    sheet_id = sheet_id or os.getenv("CAP_COMPLIANCE_SHEET_ID") or CAP_SHEET_ID
    if not sheet_id:
        logger.warning("CAP_COMPLIANCE_SHEET_ID not configured; skipping report export")
        return False
    logger.debug("Exporting AI search report to sheet %s tab %s", sheet_id, target_tab)
    use_tab = target_tab
    rows_written = 0
    try:
        headers = [
            "Date",
            "Requirement (ID)",
            "Requirement",
            "Status",
            "Details",
            "Findings",
            "AI Search Evidence",
            "AI Recommendations",
            "AI Search Summary",
            "Records",
            "Follow-up",
            "Follow-up Evidence",
            "Gap Analysis",
            "Corrective Actions",
            "AI Gap Analysis",
            "Priority Actions",
            "Regulatory Impact",
        ]

        values = []
        rpt = data.get("report") or (data if "checks" in data else {})
        report_date = rpt.get("report_date") or rpt.get("timestamp") or datetime.now(UTC).isoformat()
        checks = rpt.get("checks") or []
        ai_gap = rpt.get("ai_gap_analysis") or {}

        gap_target_ids = set()
        try:
            raw_gap_targets = ai_gap.get("analyzed_check_ids") or ai_gap.get("requirement_ids") or []
            if isinstance(raw_gap_targets, (list, tuple, set)):
                gap_target_ids = {
                    str(item).strip().upper()
                    for item in raw_gap_targets
                    if str(item or "").strip()
                }
        except Exception:
            gap_target_ids = set()

        if not gap_target_ids and checks:
            try:
                fallback_targets = []
                for chk in checks:
                    status_value = _normalize_gap_analysis_status(chk.get("status"))
                    req_id = chk.get("checklist_item") or chk.get("check") or chk.get("checklistItem") or chk.get("code") or ""
                    if status_value in ("NON_COMPLIANT", "PARTIAL", "WARNING") and str(req_id).strip():
                        fallback_targets.append(str(req_id).strip().upper())
                if len(fallback_targets) == 1:
                    gap_target_ids = set(fallback_targets)
            except Exception:
                gap_target_ids = set()

        try:
            gap_analysis_str = str(ai_gap.get("analysis") or "").strip()
        except Exception:
            gap_analysis_str = ""

        try:
            regulatory_impact_str = str(ai_gap.get("regulatory_impact") or "").strip()
        except Exception:
            regulatory_impact_str = ""

        try:
            priority_actions = ai_gap.get("priority_actions") or []
            if isinstance(priority_actions, list):
                priority_actions_str = "\n".join(
                    [
                        " | ".join(
                            [
                                str(action.get("impact") or "").strip(),
                                str(action.get("timeframe") or "").strip(),
                                str(action.get("action") or "").strip(),
                                str(action.get("rationale") or "").strip(),
                            ]
                        ).strip(" |")
                        for action in priority_actions
                        if isinstance(action, dict) and any(action.values())
                    ]
                )
            else:
                priority_actions_str = str(priority_actions).strip()
        except Exception:
            priority_actions_str = ""

        if not checks:
            try:
                ai_evidence_summary = rpt.get("summary") or rpt.get("ai_evidence_summary") or ""
            except Exception:
                ai_evidence_summary = ""
            try:
                res_list = _top_ai_search_evidence_for_sheet(rpt.get("results") or [], _sheet_evidence_cap_from_obj(rpt))
                docs_str = _format_evidence_list(res_list)
            except Exception:
                docs_str = ""

            values.append([
                report_date,
                "",
                rpt.get("query") or "",
                rpt.get("compliance_rate") or "",
                "",
                "",
                Google_API.truncate_for_sheets(docs_str),
                "; ".join(rpt.get("ai_recommendations") or []) if isinstance(rpt.get("ai_recommendations"), list) else "",
                Google_API.truncate_for_sheets(ai_evidence_summary),
                "",
                "",
                "",
                Google_API.truncate_for_sheets(gap_analysis_str),
                Google_API.truncate_for_sheets(priority_actions_str),
                Google_API.truncate_for_sheets(gap_analysis_str),
                Google_API.truncate_for_sheets(priority_actions_str),
                Google_API.truncate_for_sheets(regulatory_impact_str),
            ])
        else:
            for chk in checks:
                try:
                    req_id = chk.get("checklist_item") or chk.get("check") or chk.get("checklistItem") or chk.get("requirement") or chk.get("code") or ""
                    normalized_req_id = str(req_id).strip().upper()
                    req_text = chk.get("requirement") or chk.get("requirement_text") or ""
                    status = chk.get("status") or ""
                    details = chk.get("details") or ""
                    findings = chk.get("findings") or []
                    ai_recs = chk.get("ai_recommendations") or chk.get("recommendations") or []
                    ai_evidence_summary = chk.get("ai_search_summary") or chk.get("ai_evidence_summary") or ""
                    records_evidence = chk.get("records_evidence") or []
                    try:
                        res_list = _top_ai_search_evidence_for_sheet(
                            chk.get("ai_search_evidence")
                            or chk.get("results")
                            or chk.get("policy_evidence")
                            or rpt.get("results")
                            or [],
                            _sheet_evidence_cap_from_chk_or_report(chk, rpt),
                        )
                        docs_str = _format_evidence_list(res_list)
                    except Exception:
                        docs_str = ""

                    records_str = (
                        "; ".join([str(x) for x in records_evidence])
                        if isinstance(records_evidence, (list, tuple))
                        else str(records_evidence)
                    )
                    row_gap = str(chk.get("gap_analysis") or "").strip()
                    row_ca = _format_corrective_actions_for_sheet(chk.get("corrective_actions") or [])
                    apply_gap_to_row = bool(normalized_req_id and normalized_req_id in gap_target_ids)
                    if not row_gap and apply_gap_to_row:
                        row_gap = gap_analysis_str
                    if not row_ca and apply_gap_to_row:
                        row_ca = priority_actions_str
                    rollup_gap = Google_API.truncate_for_sheets(gap_analysis_str) if apply_gap_to_row else ""
                    rollup_actions = Google_API.truncate_for_sheets(priority_actions_str) if apply_gap_to_row else ""
                    rollup_impact = Google_API.truncate_for_sheets(regulatory_impact_str) if apply_gap_to_row else ""
                    values.append([
                        report_date,
                        req_id,
                        req_text,
                        status,
                        details,
                        "; ".join([str(x) for x in findings]) if isinstance(findings, (list, tuple)) else str(findings),
                        Google_API.truncate_for_sheets(docs_str),
                        "; ".join([str(x) for x in ai_recs]) if isinstance(ai_recs, (list, tuple)) else str(ai_recs),
                        Google_API.truncate_for_sheets(ai_evidence_summary),
                        records_str,
                        "",
                        "",
                        Google_API.truncate_for_sheets(row_gap),
                        Google_API.truncate_for_sheets(row_ca),
                        rollup_gap,
                        rollup_actions,
                        rollup_impact,
                    ])
                except Exception:
                    continue

        use_tab, created_tab = Google_API.ensure_sheet_tab(sheet_id, target_tab)

        try:
            if created_tab:
                header_range = f"{use_tab}!A1"
                header_body = {"valueInputOption": "RAW", "data": [{"range": header_range, "values": [headers]}]}
                try:
                    Google_API.batchupdate_values_sheets(sheet_id, header_body, spoof=True)
                except Exception:
                    pass
            else:
                try:
                    existing_data = Google_API.get_values_sheets(sheet_id, use_tab, spoof=True)
                    existing_values = existing_data.get("values", [])
                    if not existing_values or len(existing_values) == 0:
                        header_range = f"{use_tab}!A1"
                        header_body = {"valueInputOption": "RAW", "data": [{"range": header_range, "values": [headers]}]}
                        Google_API.batchupdate_values_sheets(sheet_id, header_body, spoof=True)
                except Exception:
                    pass
        except Exception:
            pass

        safe_tab = str(use_tab).replace("'", "''")
        try:
            existing_data = Google_API.get_values_sheets(sheet_id, use_tab, spoof=True)
            existing_values = existing_data.get("values", [])
        except Exception:
            existing_values = []

        target_sheet_headers = None
        if existing_values:
            h_row_idx = 0
            max_cols_local = 0
            for i, r in enumerate(existing_values[:10]):
                non_empty = sum(1 for c in r if c and str(c).strip())
                if non_empty > max_cols_local:
                    max_cols_local = non_empty
                    h_row_idx = i
            target_sheet_headers = [str(x) for x in existing_values[h_row_idx]]

        if created_tab or not target_sheet_headers:
            target_sheet_headers = headers
        else:
            required_new = [
                "AI Search Summary",
                "AI Search Evidence",
                "Gap Analysis",
                "Corrective Actions",
                "AI Gap Analysis",
                "Priority Actions",
                "Regulatory Impact",
            ]
            legacy_map = {
                "AI Search Results": "AI Search Evidence",
                "Policy_Evidence": "AI Search Evidence",
                "AI Evidence": "AI Search Summary",
            }
            replaced = []
            for li, lh in enumerate(list(target_sheet_headers)):
                if lh in legacy_map and lh != legacy_map[lh]:
                    target_sheet_headers[li] = legacy_map[lh]
                    replaced.append((lh, legacy_map[lh]))
            if replaced:
                header_range = f"{use_tab}!A{h_row_idx + 1}"
                header_body = {"valueInputOption": "RAW", "data": [{"range": header_range, "values": [target_sheet_headers]}]}
                try:
                    Google_API.batchupdate_values_sheets(sheet_id, header_body, spoof=True)
                except Exception:
                    pass
            missing = [h for h in required_new if h not in target_sheet_headers]
            if missing:
                target_sheet_headers = list(target_sheet_headers) + missing
                header_range = f"{use_tab}!A{h_row_idx + 1}"
                header_body = {"valueInputOption": "RAW", "data": [{"range": header_range, "values": [target_sheet_headers]}]}
                try:
                    Google_API.batchupdate_values_sheets(sheet_id, header_body, spoof=True)
                except Exception:
                    pass

        full_rows = []
        canonical = headers
        def _find_hdr_index(hdr_name):
            for idx, h in enumerate(target_sheet_headers):
                if str(h or "").strip().lower() == hdr_name.strip().lower():
                    return idx
            return None

        for compact_row in values:
            full = [""] * len(target_sheet_headers)
            for ci, val in enumerate(compact_row[: len(canonical)]):
                hdr = canonical[ci]
                found = _find_hdr_index(hdr)
                if found is None:
                    if ci < len(full):
                        full[ci] = val
                else:
                    full[found] = val
            full_rows.append(full)

        next_row = (len(existing_values) + 1) if existing_values else 2
        rng = f"'{safe_tab}'!A{next_row}"
        if full_rows:
            Google_API.append_sheets(
                sheet_id, rng, {"values": full_rows},
                valueInputOption="RAW", insertDataOption="INSERT_ROWS", spoof=True,
            )
            rows_written = len(full_rows)
    except Exception as exc:
        logger.error("Report export to sheet failed: %s", exc, exc_info=True)
        return False
    if rows_written:
        logger.info("Saved AI search report to sheet %s tab %s (%s row(s))", sheet_id, use_tab, rows_written)
    return True


def _enqueue_ai_search_report_export(data, target_tab=None, sheet_id=None):
    sheet_id = sheet_id or os.getenv("CAP_COMPLIANCE_SHEET_ID") or CAP_SHEET_ID
    if not sheet_id:
        return False
    app = current_app._get_current_object()
    payload = copy.deepcopy(data)
    tab = target_tab or os.getenv("CAP_RESULTS_TAB", "CUS AI Reports")

    def _worker():
        with app.app_context():
            _export_ai_search_report_to_sheet(payload, target_tab=tab, sheet_id=sheet_id)

    threading.Thread(target=_worker, name=f"ai-report-export-{tab}", daemon=True).start()
    logger.info("Queued background export of AI search report to sheet %s tab %s", sheet_id, tab)
    return True


def _get_inspection_sheet_links():
    """Load document links from inspection sheet and return as dict for lookup.
    
    Returns dict mapping normalized document names to working links.
    """
    global _inspection_sheet_cache, _inspection_sheet_cache_time
    try:
        # Use cache if available (within 5 minutes)
        now = time.time()
        if _inspection_sheet_cache is not None and _inspection_sheet_cache_time is not None:
            if now - _inspection_sheet_cache_time < 300:  # 5 minutes
                return _inspection_sheet_cache
        
        try:
            service, sheet_id = _build_google_sheets_service()
        except RuntimeError as config_error:
            logger.debug("%s, skipping inspection sheet lookup", config_error)
            return {}

        result = _execute_google_sheets_request(
            service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range='Sheet1'
            ),
            "inspection sheet lookup",
        )
        
        rows = result.get('values', [])
        logger.debug(f"Loaded {len(rows)} rows from inspection sheet")
        
        # Build lookup dict supporting multiple variations per document code
        # Sheet structure: [0]=File Name, [1]=Type, [2]=Size, [3]=Created, [4]=Modified, [5]=Folder Link, [6]=Document Link, [7]=Timestamp
        doc_links = {}
        code_to_rows = {}  # Track rows by document code for multiple matches
        
        if rows and len(rows) > 1:
            for row in rows[1:]:  # Skip header
                try:
                    doc_name = (row[0] if len(row) > 0 else "").strip()
                    doc_link = (row[6] if len(row) > 6 else "").strip()
                    
                    if doc_name and doc_link:
                        row_data = {
                            "name": doc_name,
                            "link": doc_link,
                        }
                        
                        # Store by exact name
                        if doc_name not in doc_links:
                            doc_links[doc_name] = []
                        
                        # Only append if not already in the list for this key (handle duplicates in sheet)
                        if not any(d.get("link") == doc_link for d in doc_links[doc_name]):
                            doc_links[doc_name].append(row_data)
                        
                        # Store by normalized name
                        norm_name = _normalize_doc_name(doc_name)
                        if norm_name and norm_name != doc_name:
                            if norm_name not in doc_links:
                                doc_links[norm_name] = []
                            if not any(d.get("link") == doc_link for d in doc_links[norm_name]):
                                doc_links[norm_name].append(row_data)
                        
                        # Store by document code (e.g., "K1003.8")
                        code = _extract_doc_code(doc_name)
                        if code:
                            if code not in code_to_rows:
                                code_to_rows[code] = []
                            if not any(d.get("link") == doc_link for d in code_to_rows[code]):
                                code_to_rows[code].append(row_data)
                            
                            # Also add by code to main lookup
                            if code not in doc_links:
                                doc_links[code] = []
                            if not any(d.get("link") == doc_link for d in doc_links[code]):
                                doc_links[code].append(row_data)
                
                except Exception as e:
                    import traceback
                    logger.debug(f"Error processing inspection sheet row: {e}\n{traceback.format_exc()}")
        
        # Cache the result
        _inspection_sheet_cache = doc_links
        _inspection_sheet_cache_time = now
        
        # Debug: Log what codes were extracted and their folder context
        codes_in_links = [k for k in doc_links.keys() if re.match(r'^[A-Z0-9\.]+$', k)]
        logger.debug(f"Cached {len(doc_links)} document lookups from {len(rows)-1} sheet rows")
        logger.debug(f"  Document codes extracted: {codes_in_links[:20]}")  # Show first 20 codes
        logger.debug(f"  Sample keys: {list(doc_links.keys())[:10]}")  # Show first 10 keys overall
        
        return doc_links
    except Exception as e:
        if _inspection_sheet_cache is not None and _is_retryable_google_api_error(e):
            logger.warning(
                "Failed to refresh inspection sheet links due to transient Google API error; using stale cache: %s",
                e,
            )
            return _inspection_sheet_cache
        logger.debug(f"Failed to load inspection sheet links: {e}")
        return {}


def _get_working_link(doc_name, gcs_path=None, doc_id=None):
    """Get working link from inspection sheet for a document name.
    
    Matching strategies (in order):
    1. Exact name match
    2. Normalized name match
    3. Document code extraction (e.g., K1003.8) - PREFERRED for duplicates
    4. Substring matching (fallback)
    
    When multiple links match (e.g., multiple K1003.8 forms), uses GCS folder-path
    to select the correct version.
    
    Args:
        doc_name: Document name from Vertex AI
        gcs_path: Optional GCS path containing folder structure for disambiguation
        doc_id: Optional document ID for link assignment tracking
    
    Returns the working link if found, else returns None.
    """
    if not doc_name:
        return None
    
    doc_links = _get_inspection_sheet_links()
    if not doc_links:
        return None
    
    logger.debug(f"[_get_working_link] Looking up '{doc_name}' (doc_id={doc_id})")
    logger.debug(f"[_get_working_link] Available keys in doc_links: {list(doc_links.keys())[:15]}")
    
    # Try exact match first
    if doc_name in doc_links:
        logger.debug(f"[_get_working_link] ✓ EXACT NAME MATCH: {doc_name}")
        link = doc_links[doc_name]
        if isinstance(link, list):
            return _select_link_from_multiple(link, gcs_path, doc_id, doc_name)
        return link
    else:
        logger.debug(f"[_get_working_link] ✗ No exact match for '{doc_name}'")
    
    # Try normalized name
    norm_name = _normalize_doc_name(doc_name)
    if norm_name and norm_name in doc_links:
        logger.debug(f"[_get_working_link] ✓ NORMALIZED NAME MATCH: '{norm_name}' (from '{doc_name}')")
        link = doc_links[norm_name]
        if isinstance(link, list):
            return _select_link_from_multiple(link, gcs_path, doc_id, norm_name)
        return link
    else:
        logger.debug(f"[_get_working_link] ✗ No normalized match for '{norm_name}' (from '{doc_name}')")
    
    # Try document code extraction (e.g., "K1003.8" from "K1003.8_Annual Laboratory...")
    # This is CRITICAL for handling multiple forms with the same code.
    code = _extract_doc_code(doc_name)
    if code:
        logger.debug(f"[_get_working_link] Extracted code '{code}' from '{doc_name}'")
        if code in doc_links:
            logger.debug(f"[_get_working_link] ✓ CODE MATCH FOUND: '{code}' has {len(doc_links[code]) if isinstance(doc_links[code], list) else 1} entries")
            link = doc_links[code]
            if isinstance(link, list):
                logger.debug(f"[_get_working_link] Code match for '{code}': {len(link)} entries - using folder-path disambiguation")
                return _select_link_from_multiple(link, gcs_path, doc_id, code)
            return link
        else:
            available_codes = [k for k in doc_links.keys() if re.match(r'^[A-Z0-9\.]+$', k)]
            logger.debug(f"[_get_working_link] ✗ CODE NOT FOUND: '{code}' (available codes: {available_codes[:10]})")
            # For coded docs, avoid broad fallback matching that can mis-link unrelated files.
            return None
    else:
        logger.debug(f"[_get_working_link] ✗ No code extraction match for '{doc_name}'")
    
    # Try substring matching (fallback, but avoid matching partial words)
    # ONLY match if the entire document name or a significant portion appears
    logger.debug(f"[_get_working_link] ✗ Falling back to substring matching for '{doc_name}'")
    
    # NEW: Safety check - if doc_name contains a code (like K1003.8) but we reached here,
    # it means the code-match failed. Do NOT fall back to generic substring matching
    # for coded documents as it often hits unrelated files like "MP.jpg".
    if code:
        logger.debug(f"[_get_working_link] ✗ Skipping substring fallback for coded document '{doc_name}' to prevent mislinking")
        return None

    doc_lower = doc_name.lower()
    best_match = None
    best_match_key = None
    best_match_score = 0
    
    doc_tokens = {t for t in re.split(r'[^a-z0-9]+', doc_lower) if len(t) >= 3}

    for key, link in doc_links.items():
        key_lower = key.lower()
        # Skip very short keys (like "mp", "qa") to avoid spurious matches
        # and ignore common image extensions that are often unrelated defaults.
        if len(key_lower) < 3 or key_lower.endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp')):
            logger.debug(f"[_get_working_link]   Skipping short/irrelevant key '{key}' (length={len(key_lower)})")
            continue

        key_tokens = {t for t in re.split(r'[^a-z0-9]+', key_lower) if len(t) >= 3}
        token_overlap = len(doc_tokens & key_tokens)
        
        # Check for meaningful substring matches
        if key_lower in doc_lower or doc_lower in key_lower:
            # Score: longer matches are better
            match_score = max(len(key_lower), len(doc_lower) - len(doc_lower.replace(key_lower, ''))) + (token_overlap * 10)
            logger.debug(f"[_get_working_link]   Substring match candidate: '{key}' (score={match_score})")
            if match_score > best_match_score:
                best_match_score = match_score
                best_match = link
                best_match_key = key
        elif token_overlap >= 2:
            # Allow token-based fallback only when there is substantial semantic overlap.
            match_score = token_overlap * 10
            logger.debug(f"[_get_working_link]   Token overlap candidate: '{key}' (score={match_score})")
            if match_score > best_match_score:
                best_match_score = match_score
                best_match = link
                best_match_key = key
    
    if best_match:
        logger.debug(f"[_get_working_link] ✓ SUBSTRING MATCH: '{best_match_key}' (score={best_match_score})")
        if isinstance(best_match, list):
            return _select_link_from_multiple(best_match, gcs_path, doc_id, best_match_key)
        return best_match
    
    return None


def _select_link_from_multiple(links, gcs_path, doc_id, key):
    """Select the best link when multiple options exist.
    
    CRITICAL: Folder links in the inspection sheet are unreliable. 
    Instead of using them, we use document name keywords and GCS path context
    to select the best matching document.
    
    Strategy priority:
    1. Check cached mapping (persistence)
    2. Intelligent keyword matching between GCS path and document name
    3. Fallback: Distributed cycling based on document ID
    """
    if not links:
        return None
    
    if len(links) == 1:
        # Only one option, return it directly
        link_data = links[0]
        logger.debug(f"[_select_link_from_multiple] Only 1 option for '{key}', returning directly")
        if isinstance(link_data, dict):
            return link_data.get("link")
        return link_data
    
    logger.debug(f"[_select_link_from_multiple] Selecting from {len(links)} options for '{key}' (doc_id={doc_id})")
    

    # Fallback Strategy: Distributed cycling
    # This ensures that different doc_ids get distributed across different versions 
    # of the same document name if no better context is found.
    global _link_cycling_index
    idx = _link_cycling_index % len(links)
    _link_cycling_index += 1
    
    link_data = links[idx]
    logger.debug(f"[_select_link_from_multiple] No context found, using cycling index {idx} for '{key}'")
    logger.debug(f"[_select_link_from_multiple] No context found, link_data {link_data} for '{key}'")
    if isinstance(link_data, dict):
        return link_data.get("link")
    return link_data


def _get_val_fuzzy(row, keys):
    """Retrieve value from row dict looking up keys case-insensitively.
    
    Args:
        row: Dictionary (e.g. from csv.DictReader or API)
        keys: List of string keys to look for
    """
    if not isinstance(row, dict):
        return ""
    # Fast path: exact match
    for k in keys:
        if k in row and row[k]:
            return row[k]
    
    # Slow path: case insensitive
    row_map = {str(k).lower().strip(): k for k in row.keys()}
    for k in keys:
        kl = str(k).lower().strip()
        if kl in row_map:
            val = row[row_map[kl]]
            if val: return val
    return ""


def get_column_letter(col_index):
    """Convert column index to Excel-style letter (0=A, 1=B, etc.)"""
    if col_index is None:
        return None
    letter = ""
    while col_index >= 0:
        letter = chr(col_index % 26 + 65) + letter
        col_index = col_index // 26 - 1
    return letter


def find_header_index(candidates, headers):
    """Find the index of a header that matches any of the candidate names."""
    if not headers:
        return None
    lower_headers = [str(h).strip().lower() for h in headers]
    for cand in candidates:
        cand_low = str(cand).strip().lower()
        if cand_low in lower_headers:
            return lower_headers.index(cand_low)
    return None


@ai_engine.route("/cus-gen-requirements", methods=["GET"])
@login_required
def cus_gen_requirements():
    """Load requirements (checklist items) from the CAP sheet, specialized for AI Search Engine.
    
    Prioritizes AI Search Summary and AI Search Evidence columns.
    Falls back to local CSV if Google Sheet is unavailable.
    """
    try:
        sheet_name = request.args.get("sheet") or CAP_SHEET_NAME
        logger.debug(f"AI Engine cus_gen_requirements called with sheet: {sheet_name}")

        # Try Google Sheet first
        active_sheet_id = os.getenv("CAP_SHEET_ID") or CAP_SHEET_ID
        raw_values = None
        source = None
        source_info = None
        
        if active_sheet_id:
            try:
                logger.debug(f"Fetching requirements from Google Sheet: {active_sheet_id} (Tab: {sheet_name})")
                rows_data = Google_API.get_values_sheets(
                    active_sheet_id,
                    sheet_name,
                    dateTimeRenderOption="FORMATTED_STRING",
                    spoof=False,
                )
                raw_values = rows_data.get("values", []) if rows_data else []
                if raw_values:
                    source = "google_sheet"
                    source_info = f"{CAP_FILE_NAME}/{sheet_name}"
                    logger.debug(f"Successfully loaded {len(raw_values)} rows from Google Sheet")
                else:
                    logger.warning(f"Google Sheet returned no values for tab: {sheet_name}")
            except Exception as e:
                logger.warning(f"Google Sheet load failed: {e}. Trying CSV fallback...")
                raw_values = None
        
        # Fallback to local CSV if Google Sheet failed or returned no data
        if not raw_values:
            logger.debug("Attempting CSV fallback...")
            module_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
            local_csv = os.path.abspath(os.path.join(module_root, f"{CAP_FILE_NAME}.csv"))
            
            if os.path.exists(local_csv):
                try:
                    logger.info(f"Loading from local CSV: {local_csv}")
                    df = pd.read_csv(local_csv, dtype=str).fillna("")
                    # Convert dataframe back to list of lists format
                    headers = df.columns.tolist()
                    raw_values = [headers] + df.values.tolist()
                    source = "local_csv"
                    source_info = os.path.basename(local_csv)
                    logger.debug(f"Loaded {len(raw_values)-1} rows from CSV")
                except Exception as e:
                    logger.warning(f"CSV load failed: {e}")
                    raw_values = None
            else:
                logger.warning(f"CSV fallback file not found: {local_csv}")
        
        if not raw_values:
            logger.error(f"Could not load requirements from Google Sheet or CSV for tab: {sheet_name}")
            return jsonify({"success": False, "error": f"Could not load requirements for '{sheet_name}'. Please check that the Google Sheet is accessible or the CSV file exists."}), 404

        # Heuristic to find the header row
        header_row_index = 0
        max_cols = 0
        for i, row in enumerate(raw_values):
            non_empty = sum(1 for cell in row if cell and str(cell).strip())
            if non_empty > max_cols:
                max_cols = non_empty
                header_row_index = i
        
        logger.debug(f"Selected header row index: {header_row_index} with {max_cols} columns")

        headers = raw_values[header_row_index]
        headers = [h if h is not None else "" for h in headers]
        lower_map = {str(h).strip().lower(): idx for idx, h in enumerate(headers)}
        logger.debug(f"Lowercased headers: {list(lower_map.keys())}")

        # Direct search for specific header patterns
        def find_col(candidates):
            for cand in candidates:
                if str(cand).lower() in lower_map:
                    return lower_map[str(cand).lower()]
            return None

        code_candidates = [
            "requirement (id)",
            "requirement id",
            "requirement_id",
            "requirement (ID)",
            "requirement code",
            "requirement_code",
            "code",
            "gen code",
            "id",
            "checklist item",
            "checklist_item",
            "requirement",
            "checklist",
        ]
        status_candidates = ["status", "status value", "status_value", "state", "compliance", "compliance status"]
        desc_candidates = [
            "requirement (text)",
            "requirement text",
            "requirement_text",
            "requirement",
            "description",
            "title",
            "label",
            "text",
            "subject",
        ]
        subject_candidates = [
            "subject header",
            "subject",
            "subject_header",
            "topic",
            "discipline",
            "section",
            "header",
            "category",
        ]
        revision_candidates = [
            "revision",
            "rev",
            "revision no",
            "revision number",
            "rev no",
            "rev number",
            "document revision",
            "current revision",
        ]
        
        # SEARCH SPECIALIZED CANDIDATES
        ai_search_summary_candidates = [
            "ai search summary",
            "ai_search_summary",
            "ai search summary (vertex ai)",
            "search summary",
            "ai search summary (vertex)",
        ]
        ai_search_evidence_candidates = [
            "ai search evidence",
            "ai_search_evidence",
            "ai search results",
            "ai search evidence (docs)",
            "search evidence",
            "ai search evidence (summary)",
        ]
        
        # LEGACY FALLBACKS
        legacy_ai_candidates = [
            "ai evidence",
            "ai_evidence",
            "ai search summary",
            "ai evidence (summary)",
            "ai_evidence_summary",
        ]
        
        onsite_note_candidates = ["onsite note", "onsite_note", "onsite-note", "onsite note (if any)"]
        policy_procedure_candidates = [
            "policy/procedure",
            "policy / procedure",
            "policy_procedures",
            "policy procedures",
            "policy procedure",
        ]
        note_general_candidates = ["note", "notes", "comments", "internal note"]
        evidence_compliance_candidates = [
            "evidence of compliance",
            "evidence_of_compliance",
            "compliance evidence",
            "evidence (compliance)",
        ]
        gap_analysis_candidates = [
            "gap analysis",
            "gap_analysis",
        ]
        corrective_actions_candidates = [
            "corrective actions",
            "corrective_actions",
            "corrective action",
        ]
        remediation_candidates = list(REMEDIATION_COLUMN_CANDIDATES)

        found_code_col = find_col(code_candidates)
        found_status_col = find_col(status_candidates)
        found_desc_col = find_col(desc_candidates)
        found_subject_col = find_col(subject_candidates)
        found_revision_col = find_col(revision_candidates)
        
        found_ai_col = find_col(ai_search_summary_candidates)
        if found_ai_col is None:
            found_ai_col = find_col(legacy_ai_candidates)
            
        found_ai_docs_col = find_col(ai_search_evidence_candidates)
        found_onsite_note_col = find_col(onsite_note_candidates)
        found_policy_col = find_col(policy_procedure_candidates)
        found_note_general_col = find_col(note_general_candidates)
        found_eoc_col = find_col(evidence_compliance_candidates)
        found_gap_analysis_col = find_col(gap_analysis_candidates)
        found_corrective_actions_col = find_col(corrective_actions_candidates)
        found_remediation_col = find_col(remediation_candidates)
        # Avoid using the Onsite Note column as the general Note column
        if (
            found_note_general_col is not None
            and found_onsite_note_col is not None
            and found_note_general_col == found_onsite_note_col
        ):
            found_note_general_col = None

        # Compliance Details "Comments" — distinct from checklist Note when possible
        compliance_comments_candidates = [
            "compliance comments",
            "compliance comment",
            "ai compliance comments",
            "cus comments",
            "review comments",
            "comments (compliance)",
        ]
        found_compliance_comments_col = find_col(compliance_comments_candidates)
        if found_compliance_comments_col is None:
            for cc_key in ("comments", "comment"):
                if cc_key in lower_map:
                    ccol = lower_map[cc_key]
                    if found_note_general_col is None or ccol != found_note_general_col:
                        found_compliance_comments_col = ccol
                    break
        
        logger.debug(
            f"Identified columns: code={found_code_col}, status={found_status_col}, desc={found_desc_col}, "
            f"subject={found_subject_col}, revision={found_revision_col}"
        )

        # Ensure code and desc are not the same if we have other options
        if found_code_col == found_desc_col and found_code_col is not None:
            # If they are the same, try to find another candidate for description
            for cand in ["requirement text", "description", "label", "text"]:
                if cand in lower_map and lower_map[cand] != found_code_col:
                    found_desc_col = lower_map[cand]
                    logger.debug(f"Separated code and desc. New desc col: {found_desc_col}")
                    break

        # Additional fallback logic from compliance_review.py
        if found_code_col is None:
            # Try to find any column with 'requirement' and 'id'
            for idx, h in enumerate(headers):
                low_h = str(h).lower()
                if "requirement" in low_h and "id" in low_h:
                    found_code_col = idx
                    logger.debug(f"Fuzzy match for code column: {h} at index {idx}")
                    break

        if found_desc_col is None:
             # use second column if available, or just same as code
             if len(headers) > 1:
                 found_desc_col = 1 if found_code_col != 1 else 0
             else:
                 found_desc_col = found_code_col
             logger.debug(f"Defaulted desc column: {found_desc_col}")

        if found_subject_col is None:
             # try to find any column containing 'subject'
             for idx, h in enumerate(headers):
                 if "subject" in str(h).lower():
                     found_subject_col = idx
                     logger.debug(f"Fuzzy match for subject column: {h} at index {idx}")
                     break

        if found_revision_col is None:
             # try to find any column containing 'revision' or common rev shorthand
             for idx, h in enumerate(headers):
                 low_h = str(h).lower()
                 if "revision" in low_h or re.search(r"\brev\b", low_h):
                     found_revision_col = idx
                     logger.debug(f"Fuzzy match for revision column: {h} at index {idx}")
                     break

        # Heuristic fallback for code column if still not found
        if found_code_col is None:
            try:
                # Pattern for common requirement codes like MIC.1234 or MIC123
                # Expanded to match codes with dot or no dot, and 2-7 digits
                pattern = re.compile(r"[A-Za-z]{1,6}\.?\d{2,7}")
                col_scores = {}
                sample_rows = raw_values[header_row_index + 1 : header_row_index + 51]
                logger.debug(f"Analyzing {len(sample_rows)} sample rows for code patterns")
                for ci in range(len(headers)):
                    score = 0
                    for row in sample_rows:
                        if ci < len(row):
                            v = str(row[ci]).strip()
                            if v:
                                if pattern.search(v) or re.match(r"^\d{3,7}$", v):
                                    score += 1
                    col_scores[ci] = score
                
                best_col = None
                max_score = 0
                for ci, score in col_scores.items():
                    if score > max_score:
                        max_score = score
                        best_col = ci
                if best_col is not None and max_score > 0:
                    found_code_col = best_col
                    logger.info(f"Heuristically selected code column {headers[best_col]} (index {best_col}) with score {max_score}")
            except Exception as e:
                logger.error(f"Heuristic code detection failed: {e}")

        if found_code_col is None:
            logger.error(f"Could not identify Requirement Code column in headers: {headers}")
            return jsonify({"success": False, "error": f"Could not identify Requirement Code column in sheet '{sheet_name}'. Available columns: {', '.join(map(str, headers[:10]))}..."}), 400

        requirements = []
        logger.debug(f"Extracting requirements from {len(raw_values) - header_row_index - 1} data rows...")
        for i, row in enumerate(raw_values[header_row_index + 1 :]):
            abs_row = header_row_index + i + 2
            
            # Extract common fields
            code_val = str(row[found_code_col]).strip() if found_code_col is not None and found_code_col < len(row) else ""
            if not code_val:
                continue
                
            status_val = str(row[found_status_col]).strip() if found_status_col is not None and found_status_col < len(row) else ""
            label_val = str(row[found_desc_col]).strip() if found_desc_col is not None and found_desc_col < len(row) else ""
            subject_val = str(row[found_subject_col]).strip() if found_subject_col is not None and found_subject_col < len(row) else ""
            revision_val = str(row[found_revision_col]).strip() if found_revision_col is not None and found_revision_col < len(row) else ""
            
            # Safety checks for AI columns
            ai_val = str(row[found_ai_col]).strip() if found_ai_col is not None and found_ai_col < len(row) else ""
            ai_docs_val = str(row[found_ai_docs_col]).strip() if found_ai_docs_col is not None and found_ai_docs_col < len(row) else ""
            onsite_note_val = str(row[found_onsite_note_col]).strip() if found_onsite_note_col is not None and found_onsite_note_col < len(row) else ""
            policy_procedure_val = (
                str(row[found_policy_col]).strip()
                if found_policy_col is not None and found_policy_col < len(row)
                else ""
            )
            note_general_val = (
                str(row[found_note_general_col]).strip()
                if found_note_general_col is not None and found_note_general_col < len(row)
                else ""
            )
            eoc_val = str(row[found_eoc_col]).strip() if found_eoc_col is not None and found_eoc_col < len(row) else ""
            compliance_comments_val = (
                str(row[found_compliance_comments_col]).strip()
                if found_compliance_comments_col is not None
                and found_compliance_comments_col < len(row)
                else ""
            )
            gap_analysis_val = (
                str(row[found_gap_analysis_col]).strip()
                if found_gap_analysis_col is not None and found_gap_analysis_col < len(row)
                else ""
            )
            corrective_actions_val = (
                str(row[found_corrective_actions_col]).strip()
                if found_corrective_actions_col is not None and found_corrective_actions_col < len(row)
                else ""
            )
            remediation_val = (
                str(row[found_remediation_col]).strip()
                if found_remediation_col is not None and found_remediation_col < len(row)
                else ""
            )

            requirements.append({
                "row_index": abs_row,
                "code": code_val,
                "status": status_val,
                "label": label_val,
                "subject": subject_val,
                "revision": revision_val,
                "policy_procedure": policy_procedure_val,
                "note": note_general_val,
                "evidence_of_compliance": eoc_val,
                "ai_evidence": ai_val,
                "ai_evidence_docs": ai_docs_val,
                "onsite_note": onsite_note_val,
                "compliance_comments": compliance_comments_val,
                "gap_analysis": gap_analysis_val,
                "corrective_actions": corrective_actions_val,
                "remediation": remediation_val,
                "is_ai_search": True
            })
        
        logger.debug(f"Successfully extracted {len(requirements)} requirements")

        return jsonify({
            "success": True, 
            "requirements": requirements, 
            "headers": headers,
            "source": source,
            "source_info": source_info,
            "column_indices": {
                "code": found_code_col,
                "status": found_status_col,
                "description": found_desc_col,
                "subject": found_subject_col,
                "revision": found_revision_col,
                "policy_procedure": found_policy_col,
                "note": found_note_general_col,
                "evidence_of_compliance": found_eoc_col,
                "ai_evidence": found_ai_col,
                "ai_search_results": found_ai_docs_col,
                "compliance_comments": found_compliance_comments_col,
                "gap_analysis": found_gap_analysis_col,
                "corrective_actions": found_corrective_actions_col,
                "remediation": found_remediation_col,
            }
        })


    except Exception as e:
        logger.exception(f"cus_gen_requirements failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@ai_engine.route("/update-results", methods=["POST"])
@login_required
def update_results():
    """Batch update AI Engine search/gap results to the CAP requirements Google Sheet."""
    from apps import Google_API
    try:
        data = request.get_json() or {}
        checks = data.get("checks") or data.get("updates") or []
        if not checks:
            return jsonify({"error": "No updates provided"}), 400

        sheet_name = request.args.get("sheet") or CAP_SHEET_NAME
        # Use the most specific sheet ID available
        requirements_sheet_id = os.getenv("CAP_SHEET_ID") or CAP_SHEET_ID
        
        if not requirements_sheet_id:
            logger.error("No CAP_SHEET_ID configured for updates")
            return jsonify({"error": "CAP_SHEET_ID not configured"}), 500

        # Load current sheet state
        logger.debug(f"Fetching sheet '{sheet_name}' from {requirements_sheet_id} for update...")
        rows_data = Google_API.get_values_sheets(requirements_sheet_id, sheet_name)
        raw_values = rows_data.get("values", [])
        if not raw_values:
            logger.warning(f"Sheet '{sheet_name}' appears to be empty or inaccessible")
            return jsonify({"error": "Sheet empty"}), 400

        # Discover columns using same heuristic as loading logic
        header_row_index = 0
        max_cols = 0
        for i, row in enumerate(raw_values[:30]): # Check up to 30 rows for headers
            non_empty = sum(1 for cell in row if cell and str(cell).strip())
            if non_empty > max_cols:
                max_cols = non_empty
                header_row_index = i
        
        headers = raw_values[header_row_index]
        logger.debug(f"Identified headers at row {header_row_index}: {headers[:10]}...")
        
        # Column candidates - synchronized with loading logic
        code_candidates = [
            "requirement (id)", "requirement id", "requirement_id", "requirement (ID)",
            "requirement code", "requirement_code", "code", "gen code", "id",
            "checklist item", "checklist_item", "requirement", "checklist"
        ]
        ai_search_summary_candidates = [
            "ai search summary", "ai_search_summary", "ai search summary (vertex ai)",
            "search summary", "ai search summary (vertex)", "ai evidence",
            "ai_evidence", "ai search summary", "ai evidence (summary)", 
            "ai_evidence_summary"
        ]
        ai_search_evidence_candidates = [
            "ai search evidence", "ai_search_evidence", "ai search results",
            "ai search evidence (docs)", "search evidence", "ai search evidence (summary)",
            "ai_evidence_docs", "ai evidence docs"
        ]
        status_candidates = ["status", "status value", "status_value", "state", "compliance", "compliance status"]
        gap_analysis_candidates = ["gap analysis", "gap_analysis"]
        corrective_actions_candidates = ["corrective actions", "corrective_actions", "corrective action"]
        remediation_candidates = list(REMEDIATION_COLUMN_CANDIDATES)

        code_col = find_header_index(code_candidates, headers)
        
        # Robust fallback for code column
        if code_col is None:
            for idx, h in enumerate(headers):
                low_h = str(h).strip().lower()
                if ("requirement" in low_h and "id" in low_h) or low_h == "code" or low_h == "checklist":
                    code_col = idx
                    break

        summary_col = find_header_index(ai_search_summary_candidates, headers)
        evidence_col = find_header_index(ai_search_evidence_candidates, headers)
        status_col = find_header_index(status_candidates, headers)
        gap_analysis_col = find_header_index(gap_analysis_candidates, headers)
        corrective_actions_col = find_header_index(corrective_actions_candidates, headers)
        remediation_col = find_header_index(remediation_candidates, headers)

        # Handle sheet names with spaces/special characters
        quoted_sheet = f"'{sheet_name}'" if " " in sheet_name or "(" in sheet_name or "-" in sheet_name else sheet_name

        # Create columns if missing (Summary and Evidence)
        header_updates = False
        if summary_col is None:
            summary_col = len(headers)
            headers.append("AI Search Summary")
            header_updates = True
            logger.info(f"Adding missing 'AI Search Summary' column at index {summary_col}")
            
        if evidence_col is None:
            evidence_col = len(headers)
            headers.append("AI Search Evidence")
            header_updates = True
            logger.info(f"Adding missing 'AI Search Evidence' column at index {evidence_col}")

        if gap_analysis_col is None:
            gap_analysis_col = len(headers)
            headers.append("Gap Analysis")
            header_updates = True
            logger.info(f"Adding missing 'Gap Analysis' column at index {gap_analysis_col}")

        if corrective_actions_col is None:
            corrective_actions_col = len(headers)
            headers.append("Corrective Actions")
            header_updates = True
            logger.info(f"Adding missing 'Corrective Actions' column at index {corrective_actions_col}")

        if remediation_col is None:
            remediation_col = len(headers)
            headers.append(REMEDIATION_COLUMN_HEADER)
            header_updates = True
            logger.info(f"Adding missing '{REMEDIATION_COLUMN_HEADER}' column at index {remediation_col}")
            
        if header_updates:
            Google_API.batchupdate_values_sheets(requirements_sheet_id, {
                "valueInputOption": "RAW",
                "data": [{"range": f"{quoted_sheet}!A{header_row_index+1}", "values": [headers]}]
            })

        # Map codes to rows
        code_to_row = {}
        for idx, row in enumerate(raw_values[header_row_index+1:]):
            if code_col is not None and code_col < len(row):
                c = str(row[code_col]).strip().lower()
                if c:
                    code_to_row[c] = header_row_index + idx + 2

        # Prepare updates
        updates = []
        logger.debug(f"")
        logger.debug(f"{'='*80}")
        logger.debug(f"PROCESSING {len(checks)} CHECKS FOR GOOGLE SHEET UPDATE")
        logger.debug(f"{'='*80}")
        
        for chk in checks:
            code = str(chk.get("code") or chk.get("checklist_item") or "").strip().lower()
            target_row = code_to_row.get(code)
            if not target_row:
                logger.debug(f"Skipping check '{code}': no row found in sheet")
                continue

            logger.debug(f"")
            logger.debug(f"PROCESSING CHECK: {code.upper()}")
            logger.debug(f"  Row number: {target_row}")
            logger.debug(f"  Check dict keys: {list(chk.keys())}")
            
            # Map flexible key names from different report formats
            summary_val = chk.get("ai_search_summary") or chk.get("ai_evidence_summary") or chk.get("ai_evidence")
            evidence_list = chk.get("ai_search_evidence") or chk.get("policy_evidence") or chk.get("results")
            status_val = chk.get("status")
            gap_analysis_val = chk.get("gap_analysis")
            corrective_actions_val = chk.get("corrective_actions")
            remediation_val = chk.get("remediation") or chk.get("action_completion")
            
            # Enhanced logging for evidence processing
            code_val = str(chk.get("code") or chk.get("checklist_item") or "UNKNOWN").strip()
            if evidence_list:
                logger.debug(f"")
                logger.debug(f"{'='*80}")
                logger.debug(f"PROCESSING EVIDENCE FOR: {code_val}")
                logger.debug(f"{'='*80}")
                logger.debug(f"Evidence list type: {type(evidence_list)}")
                logger.debug(f"Evidence list length: {len(evidence_list) if isinstance(evidence_list, (list, tuple)) else 'N/A'}")
                
                if isinstance(evidence_list, (list, tuple)):
                    logger.debug(f"Evidence items ({len(evidence_list)} total):")
                    for i, item in enumerate(evidence_list):
                        if isinstance(item, dict):
                            doc_name = item.get('document_name', 'NO_NAME')
                            doc_title = item.get('title', '')
                            doc_link = item.get('link', 'NO_LINK')
                            logger.debug(f"  [{i}] {doc_name} (title: {doc_title})")
                            logger.debug(f"       Full dict keys: {list(item.keys())}")
                            logger.debug(f"       Link: {doc_link[:80] if doc_link else 'None'}")
                        else:
                            logger.debug(f"  [{i}] {type(item).__name__}: {str(item)[:100]}")
            
            if summary_val:
                logger.debug(f"Summary value: {str(summary_val)[:100]}")
                updates.append({"range": f"{quoted_sheet}!{get_column_letter(summary_col)}{target_row}", "values": [[Google_API.truncate_for_sheets(str(summary_val))]]})
            
            if evidence_list:
                logger.debug(f"Calling _format_evidence_list() for '{code_val}'...")
                ev_str = _format_evidence_list(_top_ai_search_evidence_for_sheet(evidence_list, _sheet_evidence_cap_from_obj(chk)))
                logger.debug(f"Formatted evidence result: '{ev_str}'")
                if ev_str:
                    logger.debug(f"Adding to sheet column {evidence_col}, row {target_row}")
                    updates.append({"range": f"{quoted_sheet}!{get_column_letter(evidence_col)}{target_row}", "values": [[Google_API.truncate_for_sheets(ev_str)]]})
                else:
                    logger.warning(f"⚠ Evidence list produced EMPTY formatted string!")
            if status_val and status_col is not None:
                updates.append({"range": f"{quoted_sheet}!{get_column_letter(status_col)}{target_row}", "values": [[str(status_val)]]})

            if gap_analysis_val is not None and str(gap_analysis_val).strip() and gap_analysis_col is not None:
                updates.append({
                    "range": f"{quoted_sheet}!{get_column_letter(gap_analysis_col)}{target_row}",
                    "values": [[Google_API.truncate_for_sheets(str(gap_analysis_val))]],
                })

            if corrective_actions_val is not None and corrective_actions_col is not None:
                ca_str = _format_corrective_actions_for_sheet(corrective_actions_val)
                if ca_str:
                    updates.append({
                        "range": f"{quoted_sheet}!{get_column_letter(corrective_actions_col)}{target_row}",
                        "values": [[Google_API.truncate_for_sheets(ca_str)]],
                    })

            if remediation_val is not None and remediation_col is not None:
                ac_str = str(remediation_val).strip()
                if ac_str:
                    updates.append({
                        "range": f"{quoted_sheet}!{get_column_letter(remediation_col)}{target_row}",
                        "values": [[Google_API.truncate_for_sheets(ac_str)]],
                    })

        if updates:
            try:
                Google_API.batchupdate_values_sheets(requirements_sheet_id, {
                    "valueInputOption": "RAW",
                    "data": updates
                })
                # Proceed to optional report export if checks were provided as part of a report
            except Exception as e:
                logger.error(f"Failed to batch update sheet: {e}")

        target_tab = request.args.get("sheet") or data.get("sheet") or os.getenv("CAP_RESULTS_TAB", "CUS AI Reports")
        sheet_id = os.getenv("CAP_COMPLIANCE_SHEET_ID") or CAP_SHEET_ID
        report_export_queued = _enqueue_ai_search_report_export(
            data, target_tab=target_tab, sheet_id=sheet_id
        )

        return jsonify({
            "success": True,
            "updated": len(updates),
            "updated_count": len(updates),
            "report_export_queued": report_export_queued,
        })

    except Exception as e:
        logger.exception(f"Engine update_results fatal failure: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@ai_engine.route("/save-cus-compliance-comments", methods=["POST"])
@login_required
def save_cus_compliance_comments():
    """Save Compliance Comments (Compliance Details column) back to Google Sheet."""
    try:
        data = request.get_json() or {}
        row_index = data.get("row_index")
        comments = data.get("comments") or ""
        sheet_name = data.get("sheet") or request.args.get("sheet") or CAP_SHEET_NAME

        if not row_index:
            return jsonify({"error": "row_index required"}), 400

        sheet_id = os.getenv("CAP_SHEET_ID") or CAP_SHEET_ID
        if not sheet_id:
            return jsonify({"success": False, "error": "CAP_SHEET_ID not configured"}), 400

        rows_data = Google_API.get_values_sheets(sheet_id, sheet_name)
        raw_values = rows_data.get("values", [])
        if not raw_values:
            return jsonify({"success": False, "error": "Sheet appears empty"}), 400

        header_row_index = 0
        max_cols = 0
        for i, row in enumerate(raw_values):
            non_empty = sum(1 for cell in row if cell and str(cell).strip())
            if non_empty > max_cols:
                max_cols = non_empty
                header_row_index = i

        headers = raw_values[header_row_index]
        headers = [h if h is not None else "" for h in headers]
        lower_map = {str(h).strip().lower(): idx for idx, h in enumerate(headers)}

        def find_col(candidates):
            for cand in candidates:
                if str(cand).lower() in lower_map:
                    return lower_map[str(cand).lower()]
            return None

        note_candidates = ["note", "notes", "comments", "internal note"]
        onsite_candidates = ["onsite note", "onsite_note", "onsite-note", "onsite note (if any)"]
        note_general_col = find_col(note_candidates)
        onsite_note_col = find_col(onsite_candidates)
        if (
            note_general_col is not None
            and onsite_note_col is not None
            and note_general_col == onsite_note_col
        ):
            note_general_col = None

        compliance_comments_candidates = [
            "compliance comments",
            "compliance comment",
            "ai compliance comments",
            "cus comments",
            "review comments",
            "comments (compliance)",
        ]
        col_idx = find_col(compliance_comments_candidates)
        if col_idx is None:
            for cc_key in ("comments", "comment"):
                if cc_key in lower_map:
                    ccol = lower_map[cc_key]
                    if note_general_col is None or ccol != note_general_col:
                        col_idx = ccol
                    break

        if col_idx is None:
            col_idx = len(headers)
            headers.append("Compliance Comments")
            header_range = f"{sheet_name}!A{header_row_index + 1}"
            Google_API.batchupdate_values_sheets(
                sheet_id,
                {
                    "valueInputOption": "RAW",
                    "data": [{"range": header_range, "values": [headers]}],
                },
            )

        cell_range = f"{sheet_name}!{get_column_letter(col_idx)}{row_index}"
        Google_API.batchupdate_values_sheets(
            sheet_id,
            {"valueInputOption": "RAW", "data": [{"range": cell_range, "values": [[comments]]}]},
        )
        return jsonify({"success": True, "range": cell_range})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@ai_engine.route("/save-cus-action-completion", methods=["POST"])
@login_required
def save_cus_action_completion():
    """Save Remediation (gap workbook column) back to Google Sheet."""
    try:
        data = request.get_json() or {}
        row_index = data.get("row_index")
        remediation = _coalesce_remediation_value(data)
        sheet_name = data.get("sheet") or request.args.get("sheet") or CAP_SHEET_NAME

        if not row_index:
            return jsonify({"error": "row_index required"}), 400

        sheet_id = os.getenv("CAP_SHEET_ID") or CAP_SHEET_ID
        if not sheet_id:
            return jsonify({"success": False, "error": "CAP_SHEET_ID not configured"}), 400

        rows_data = Google_API.get_values_sheets(sheet_id, sheet_name)
        raw_values = rows_data.get("values", [])
        if not raw_values:
            return jsonify({"success": False, "error": "Sheet appears empty"}), 400

        header_row_index = 0
        max_cols = 0
        for i, row in enumerate(raw_values):
            non_empty = sum(1 for cell in row if cell and str(cell).strip())
            if non_empty > max_cols:
                max_cols = non_empty
                header_row_index = i

        headers = raw_values[header_row_index]
        headers = [h if h is not None else "" for h in headers]
        lower_map = {str(h).strip().lower(): idx for idx, h in enumerate(headers)}

        def find_col(candidates):
            for cand in candidates:
                if str(cand).lower() in lower_map:
                    return lower_map[str(cand).lower()]
            return None

        col_idx = find_col(REMEDIATION_COLUMN_CANDIDATES)
        if col_idx is None:
            col_idx = len(headers)
            headers.append(REMEDIATION_COLUMN_HEADER)
            header_range = f"{sheet_name}!A{header_row_index + 1}"
            Google_API.batchupdate_values_sheets(
                sheet_id,
                {
                    "valueInputOption": "RAW",
                    "data": [{"range": header_range, "values": [headers]}],
                },
            )

        cell_range = f"{sheet_name}!{get_column_letter(col_idx)}{row_index}"
        Google_API.batchupdate_values_sheets(
            sheet_id,
            {"valueInputOption": "RAW", "data": [{"range": cell_range, "values": [[remediation]]}]},
        )
        return jsonify({"success": True, "range": cell_range})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@ai_engine.route("/save-cus-evidence-checks", methods=["POST"])
@login_required
def save_cus_evidence_checks():
    """Save onsite verification notes (and optional onsite-only status) to the Google Sheet.

    Expects JSON body: { row_index: <int>, evidence_checks: {onsite: bool, onsite_note: str}, sheet: <optional> }
    """
    try:
        data = request.get_json(silent=True) or {}
        row_index = int(data.get("row_index")) if data.get("row_index") else None
        evidence_checks = data.get("evidence_checks") or {}
        sheet_name = data.get("sheet") or request.args.get("sheet") or CAP_SHEET_NAME

        logger.info(
            "[save_cus_evidence_checks] row_index=%s sheet=%s onsite_note_length=%s",
            row_index,
            sheet_name,
            len(str(evidence_checks.get("onsite_note") or "")),
        )

        if not CAP_SHEET_ID:
            return jsonify({"success": False, "error": "CAP_SHEET_ID not configured"}), 400
        if not row_index:
            return jsonify({"success": False, "error": "row_index required"}), 400

        rows_data = Google_API.get_values_sheets(
            CAP_SHEET_ID,
            sheet_name,
            dateTimeRenderOption="FORMATTED_STRING",
            spoof=False,
        )
        raw_values = rows_data.get("values", [])
        if not raw_values:
            return jsonify({"success": False, "error": "Sheet appears empty"}), 400

        header_row_index = 0
        max_cols = 0
        for i, row in enumerate(raw_values):
            non_empty = sum(1 for cell in row if cell and str(cell).strip())
            if non_empty > max_cols:
                max_cols = non_empty
                header_row_index = i

        headers = raw_values[header_row_index]
        headers = [h if h is not None else "" for h in headers]

        onsite_note_col_idx = None
        status_col_idx = None
        for idx, h in enumerate(headers):
            header_norm = str(h).strip().lower()
            if header_norm in ("onsite note", "onsite_note", "onsite-note"):
                onsite_note_col_idx = idx
            if header_norm in ("status", "compliance status", "compliance_status"):
                status_col_idx = idx

        header_updated = False
        if onsite_note_col_idx is None:
            onsite_note_col_idx = len(headers)
            headers.append("Onsite Note")
            header_updated = True
        if status_col_idx is None:
            status_col_idx = len(headers)
            headers.append("Status")
            header_updated = True

        if header_updated:
            header_range = f"{sheet_name}!A{header_row_index+1}"
            Google_API.batchupdate_values_sheets(
                CAP_SHEET_ID,
                {"valueInputOption": "RAW", "data": [{"range": header_range, "values": [headers]}]},
                spoof=False,
            )

        onsite_note_val = evidence_checks.get("onsite_note")
        data_updates = []
        if onsite_note_val is not None and onsite_note_col_idx is not None:
            onsite_col_letter = get_column_letter(onsite_note_col_idx)
            onsite_cell_range = f"{sheet_name}!{onsite_col_letter}{row_index}"
            data_updates.append({"range": onsite_cell_range, "values": [[str(onsite_note_val)]]})

        is_only_onsite = bool(evidence_checks.get("onsite"))
        if is_only_onsite and status_col_idx is not None and onsite_note_val:
            note_lower = str(onsite_note_val).lower()
            compliant_keywords = [
                "compliant", "verified", "checked", "confirmed", "passed", "okay", "ok",
                "acceptable", "adequate", "satisfactory",
            ]
            non_compliant_keywords = [
                "non-compliant", "noncompliant", "failed", "not compliant", "not verified",
                "not checked", "issue", "deficiency", "gap", "not found", "no information",
                "not available", "missing", "not mention", "no evidence", "insufficient",
                "lack of", "absence of", "not provided",
            ]
            has_compliant = any(keyword in note_lower for keyword in compliant_keywords)
            has_non_compliant = any(keyword in note_lower for keyword in non_compliant_keywords)
            new_status = None
            if has_compliant and not has_non_compliant:
                new_status = "COMPLIANT"
            elif has_non_compliant:
                new_status = "NON-COMPLIANT"
            elif str(onsite_note_val).strip():
                new_status = "COMPLIANT"
            if new_status:
                status_col_letter = get_column_letter(status_col_idx)
                status_cell_range = f"{sheet_name}!{status_col_letter}{row_index}"
                data_updates.append({"range": status_cell_range, "values": [[new_status]]})

        if not data_updates:
            return jsonify({"success": True, "updates": 0})

        try:
            body = {"valueInputOption": "RAW", "data": data_updates}
            resp = Google_API.batchupdate_values_sheets(CAP_SHEET_ID, body, spoof=False)
            logger.info("Saved onsite note updates: %s", str(resp)[:400])
            return jsonify({"success": True, "updates": len(data_updates)})
        except Exception as e:
            logger.exception("Error writing onsite note to sheet %s: %s", CAP_SHEET_ID, e)
            return jsonify({"success": False, "error": str(e)}), 500

    except Exception as e:
        logger.exception("Error saving onsite note: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@ai_engine.route("/cap-cus-sheet-tabs")
@login_required
def cap_cus_sheet_tabs():
    """Get list of tabs in the CAP sheet."""
    try:
        # Force live check from environment
        active_sheet_id = os.getenv("CAP_SHEET_ID") or CAP_SHEET_ID
        if not active_sheet_id:
            return jsonify({"success": False, "error": "CAP_SHEET_ID not set"})
        
        logger.debug(f"Fetching sheet tabs for {active_sheet_id}")
        metadata = Google_API.get_sheets(active_sheet_id, [], includeGridData=False)
        sheets = []
        for s in metadata.get("sheets", []):
            props = s.get("properties", {})
            sheets.append({
                "sheetId": props.get("sheetId"),
                "title": props.get("title"),
                "index": props.get("index")
            })
            
        return jsonify({
            "success": True, 
            "sheets": sheets, 
            "default": CAP_SHEET_NAME
        })
    except Exception as e:
        logger.error(f"Failed to fetch tabs: {e}")
        return jsonify({"success": False, "error": str(e)})



@ai_engine.route("/", methods=["GET"])
@login_required
def home():
    return redirect(url_for("admin.ai_engine.ai_gap_analysis"))


@ai_engine.route("/ai-compliance-search", methods=["GET"])
@login_required
def ai_compliance_search():
    """Legacy Compliance Check URL — redirect to AI Gap Analysis."""
    return redirect(url_for("admin.ai_engine.ai_gap_analysis"))


@ai_engine.route("/ai-gap-analysis", methods=["GET"])
@login_required
def ai_gap_analysis():
    """Render the dedicated AI Gap Analysis page (CAP workbook-style assessments)."""
    try:
        ai_engines = []
        try:
            if os.getenv("GEMINI_API_KEY"):
                ai_engines.append(("gemini-3.1-flash-lite", "Google Gemini — gemini-3.1-flash-lite"))
                ai_engines.append(("gemini-3.1-pro-preview", "Google Gemini — gemini-3.1-pro-preview"))
                ai_engines.append(("gemini-flash-latest", "Google Gemini — gemini-flash-latest"))

            if os.getenv("OPENAI_API_KEY"):
                ai_engines.append(("gpt-5.5", "OpenAI — gpt-5.5"))
                ai_engines.append(("gpt-5.4-mini", "OpenAI — gpt-5.4-mini"))
                ai_engines.append(("gpt-5.4-nano", "OpenAI — gpt-5.4-nano"))

            if os.getenv("ANTHROPIC_API_KEY"):
                ai_engines.append(("claude-sonnet-4-6", "Anthropic — claude-sonnet-4-6"))
                ai_engines.append(("claude-opus-4-7", "Anthropic — claude-opus-4-7"))

            default_model = ai_engines[0][0] if ai_engines else getattr(ai_service, "model", None)
        except Exception:
            ai_engines = []
            default_model = None

        try:
            from apps.lims_adapter import get_lims_availability

            lims_status = get_lims_availability()
            lims_available = bool(lims_status.get("available"))
            lims_hint = lims_status.get("hint") or (
                "Include laboratory records from the configured LIMS source."
            )
        except Exception:
            lims_available = False
            lims_hint = "No LIMS source available — checkbox disabled."

        return render_template(
            "admin.ai_compliance_search.html",
            page_mode="gap",
            ai_engines=ai_engines,
            default_model=default_model,
            lims_data_available=lims_available,
            lims_data_source_hint=lims_hint,
        )
    except Exception as e:
        logger.error(f"Error rendering AI Gap Analysis template: {e}", exc_info=True)
        return render_template("admin.home.html")

@ai_engine.route("/api/google-service-account-email", methods=["GET"])
@login_required
def get_google_service_account_email():
    """Return the Google service account email for help/documentation."""
    try:
        sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT") or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        if not sa_json:
            return jsonify({"email": None, "error": "Not configured"}), 400
        
        sa_info = json.loads(sa_json)
        email = sa_info.get("client_email")
        return jsonify({"email": email})
    except Exception as e:
        logger.error(f"Error getting service account email: {e}")
        return jsonify({"email": None, "error": str(e)}), 500

@ai_engine.route("/inspection-sheet", methods=["GET"])
@login_required
def get_inspection_sheet():
    """Fetch current data from the inspection Google Sheet."""
    try:
        service, sheet_id = _build_google_sheets_service()
        result = _execute_google_sheets_request(
            service.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range='Sheet1'
            ),
            "inspection sheet fetch",
        )
        
        rows = result.get('values', [])
        logger.info(f"Fetched {len(rows)} rows from inspection sheet")
        return jsonify({"success": True, "data": rows, "count": len(rows)})
        
    except Exception as e:
        if _is_retryable_google_api_error(e):
            logger.warning("Transient Google Sheets error fetching inspection sheet: %s", e)
            return jsonify({
                "success": False,
                "error": "Google Sheets is temporarily unavailable. Please retry shortly.",
            }), 503
        logger.error(f"Error fetching inspection sheet: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500

@ai_engine.route("/inspection-sheet/save", methods=["POST"])
@login_required
def save_inspection_sheet():
    """Save files to the inspection Google Sheet (replaces existing rows)."""
    try:
        from googleapiclient.errors import HttpError
        from datetime import datetime
        
        data = request.get_json() or {}
        files = data.get("files", [])
        folder_name = data.get("folder_name", "Untitled Folder")
        
        if not files:
            return jsonify({"success": False, "error": "No files to save"}), 400
        
        service, sheet_id = _build_google_sheets_service()
        
        # Clear existing data (keep header in row 1)
        _execute_google_sheets_request(
            service.spreadsheets().values().clear(
                spreadsheetId=sheet_id,
                range='Sheet1!A2:H'
            ),
            "inspection sheet clear",
        )
        
        # Prepare rows for insertion
        rows_to_add = []
        for f in files:
            row = [
                f.get('name', ''),
                f.get('mimeType', ''),
                f.get('size', ''),
                f.get('createdTime', ''),
                f.get('modifiedTime', ''),
                folder_name,
                f.get('link', ''),
                datetime.now().isoformat()
            ]
            rows_to_add.append(row)
        
        # Update with new rows (replace existing data)
        body = {
            'values': rows_to_add
        }
        
        result = _execute_google_sheets_request(
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range='Sheet1!A2:H',
                valueInputOption='RAW',
                body=body
            ),
            "inspection sheet update",
        )
        
        updates = result.get('updatedRows', 0)
        logger.info(f"Replaced with {updates} rows in inspection sheet")
        return jsonify({"success": True, "message": f"Saved {updates} file(s) to inspection sheet", "count": updates})
        
    except HttpError as e:
        error_msg = str(e)
        if _is_retryable_google_api_error(e):
            logger.warning("Transient Google Sheets error saving inspection sheet: %s", error_msg)
            return jsonify({
                "success": False,
                "error": "Google Sheets is temporarily unavailable. Please retry shortly.",
            }), 503
        logger.error(f"Google Sheets API error: {error_msg}")
        return jsonify({"success": False, "error": f"Sheet API error: {error_msg}"}), 500


@ai_engine.route("/api/inspection-links", methods=["GET"])
@login_required
def get_inspection_links_api():
    """API endpoint to get document links from the inspection sheet."""
    try:
        links = _get_inspection_sheet_links()
        
        # Return the full list of links for each document name
        # to allow the frontend to cycle through duplicates
        serializable_links = {}
        for key, value_list in links.items():
            if value_list:
                serializable_links[key] = [v.get("link") for v in value_list if v.get("link")]
                
        return jsonify({"success": True, "links": serializable_links})
    except Exception as e:
        logger.error(f"Error in get_inspection_links_api: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    except Exception as e:
        logger.error(f"Error saving inspection sheet: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@ai_engine.route("/document-list", methods=["GET", "POST"])
@login_required
def document_list():
    """Browse and list files from Google Drive paths."""
    try:
        if request.method == "GET":
            # Render the document list page
            return render_template("admin.document_list.html")
        
        # POST request: load files from Google Drive
        data = request.get_json() or {}
        drive_path = (data.get("drive_path") or "").strip()
        recursive = data.get("recursive", False)
        
        if not drive_path:
            return jsonify({"success": False, "error": "Please enter a Google Drive path or folder ID"}), 400
        
        # Extract folder ID from various formats
        folder_id = drive_path
        
        # If it's a full URL, extract the folder ID
        if 'drive.google.com' in drive_path:
            # URL format: https://drive.google.com/drive/u/1/folders/FOLDER_ID or https://drive.google.com/drive/folders/FOLDER_ID
            import re
            match = re.search(r'/folders/([a-zA-Z0-9-_]+)', drive_path)
            if match:
                folder_id = match.group(1)
                logger.debug(f"Extracted folder ID from URL: {folder_id}")
            else:
                return jsonify({"success": False, "error": "Could not extract folder ID from URL. Please check the format."}), 400
        
        # Validate folder ID looks reasonable (long alphanumeric string with dashes/underscores)
        if not folder_id or len(folder_id) < 20:
            return jsonify({"success": False, "error": "Invalid folder ID format. Please use a valid Google Drive folder ID (usually 25-30 characters)."}), 400
        
        files = []
        
        def get_drive_files_recursive(folder_id, service, path_prefix=""):
            """Recursively get all files and folders from a Google Drive folder."""
            local_files = []
            try:
                query = f"'{folder_id}' in parents and trashed=false"
                page_token = None
                
                while True:
                    results = service.files().list(
                        q=query,
                        fields='nextPageToken, files(id, name, mimeType, webViewLink, createdTime, modifiedTime, size)',
                        pageSize=100,
                        pageToken=page_token,
                        orderBy='name',
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True
                    ).execute()
                    
                    items = results.get('files', [])
                    if not items:
                        break
                    
                    for item in items:
                        # Build folder path
                        item_path = f"{path_prefix}/{item.get('name')}" if path_prefix else item.get('name')
                        
                        if item.get('mimeType') == 'application/vnd.google-apps.folder':
                            # If recursive and it's a folder, get its contents
                            if recursive:
                                subfolder_files = get_drive_files_recursive(item['id'], service, item_path)
                                local_files.extend(subfolder_files)
                            else:
                                # Add folder itself if not recursive
                                local_files.append({
                                    'id': item.get('id'),
                                    'name': item.get('name'),
                                    'mimeType': item.get('mimeType'),
                                    'link': item.get('webViewLink'),
                                    'createdTime': item.get('createdTime'),
                                    'modifiedTime': item.get('modifiedTime'),
                                    'size': item.get('size'),
                                    'folder_path': path_prefix
                                })
                        else:
                            # It's a file
                            local_files.append({
                                'id': item.get('id'),
                                'name': item.get('name'),
                                'mimeType': item.get('mimeType'),
                                'link': item.get('webViewLink'),
                                'createdTime': item.get('createdTime'),
                                'modifiedTime': item.get('modifiedTime'),
                                'size': item.get('size'),
                                'folder_path': path_prefix
                            })
                    
                    page_token = results.get('nextPageToken')
                    if not page_token:
                        break
                        
            except Exception as e:
                logger.error(f"Error in recursive folder listing: {e}", exc_info=True)
                raise
            
            return local_files
        
        try:
            # Use folder_id to fetch files from that folder
            logger.debug(f"Loading files from Google Drive folder: {folder_id} (recursive={recursive})")
            
            # Use Google Drive API to list files in this folder
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account
            
            # Get service account credentials
            sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT") or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
            if not sa_json:
                return jsonify({"success": False, "error": "Google Service Account not configured"}), 500
            
            try:
                sa_info = json.loads(sa_json)
                credentials = service_account.Credentials.from_service_account_info(
                    sa_info,
                    scopes=['https://www.googleapis.com/auth/drive.readonly']
                )
                
                from googleapiclient.discovery import build
                service = build('drive', 'v3', credentials=credentials)
                
                # Get files (recursively if requested)
                files = get_drive_files_recursive(folder_id, service)
                logger.info(f"Successfully loaded {len(files)} files from Google Drive folder (recursive={recursive})")
                
            except Exception as api_err:
                logger.error(f"Google Drive API error: {api_err}", exc_info=True)
                return jsonify({"success": False, "error": f"Failed to load files: {str(api_err)}"}), 500
        except Exception as e:
            logger.error(f"Error loading files from Google Drive: {e}", exc_info=True)
            return jsonify({"success": False, "error": f"Error loading files: {str(e)}"}), 500
        
        return jsonify({"success": True, "files": files, "count": len(files)})
    
    except Exception as e:
        logger.error(f"document_list route error: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


def _current_user_report_identity():
    """Return (user_display, user_id) for saved report metadata."""
    try:
        user_display = None
        user_id = None
        if current_user and getattr(current_user, "is_authenticated", False):
            user_id = getattr(current_user, "id", None) or getattr(current_user, "user_id", None)
            user_display = (
                getattr(current_user, "username", None)
                or getattr(current_user, "display_name", None)
                or getattr(current_user, "email", None)
                or getattr(current_user, "name", None)
            )
        if not user_display:
            user_display = "anonymous"
        return user_display, user_id
    except Exception:
        return "anonymous", None


def _unwrap_saved_report_payload(report_obj, max_depth=5):
    """Unwrap nested saved-report wrappers until the CUS analysis payload is found."""
    if not isinstance(report_obj, dict):
        return report_obj
    current = report_obj
    seen = set()
    for _ in range(max_depth):
        if not isinstance(current, dict):
            return current
        if isinstance(current.get("checks"), list) or current.get("report_date"):
            return current
        nxt = (
            current.get("gap_report")
            or current.get("search_report")
            or current.get("report")
        )
        if not isinstance(nxt, dict) or nxt is current:
            return current
        marker = id(nxt)
        if marker in seen:
            return current
        seen.add(marker)
        current = nxt
    return current


def _filter_reports_for_current_user(reports):
    """Return reports visible to the current user (shared or owned)."""
    filtered = []
    curr_name = None
    curr_id = None
    if current_user and getattr(current_user, "is_authenticated", False):
        curr_name = (
            getattr(current_user, "name", None)
            or getattr(current_user, "username", None)
            or getattr(current_user, "display_name", None)
            or getattr(current_user, "email", None)
        )
        curr_id = getattr(current_user, "id", None) or getattr(current_user, "user_id", None)

    for report in reports or []:
        if report.get("shared"):
            filtered.append(report)
            continue
        if curr_id is not None and str(report.get("user_id")) == str(curr_id):
            filtered.append(report)
            continue
        if curr_name and str(report.get("user")) == str(curr_name):
            filtered.append(report)
            continue
    return filtered


def _current_user_is_admin():
    return bool(
        current_user
        and getattr(current_user, "is_authenticated", False)
        and "admin" in (getattr(current_user, "roles_list", None) or [])
    )


def _current_user_can_delete_report(report):
    """Admins may delete any report; everyone else only reports they saved."""
    if not current_user or not getattr(current_user, "is_authenticated", False):
        return False
    if _current_user_is_admin():
        return True
    user_display, user_id = _current_user_report_identity()
    if report.get("user_id") is not None:
        return user_id is not None and str(report.get("user_id")) == str(user_id)
    return user_display != "anonymous" and str(report.get("user")) == str(user_display)


def _delete_saved_report(report_id, load_reports, save_reports):
    reports = load_reports() or []
    target = next((r for r in reports if str(r.get("id")) == str(report_id)), None)
    if target is None or not (_current_user_is_admin() or _filter_reports_for_current_user([target])):
        return jsonify({"success": False, "error": "Report not found"}), 404
    if not _current_user_can_delete_report(target):
        return jsonify({"success": False, "error": "You can only delete reports you saved"}), 403
    new_reports = [r for r in reports if str(r.get("id")) != str(report_id)]
    if not save_reports(new_reports):
        return jsonify({"success": False, "error": "Failed to delete report"}), 500
    return jsonify({"success": True})


def _dedupe_saved_report_documents(report_obj, *, preserve_gap_fields=False):
    """Normalize document lists on a saved report payload."""

    def _doc_key(doc):
        try:
            if not isinstance(doc, dict):
                return str(doc)
            return doc.get("id") or doc.get("document_name") or doc.get("link") or json.dumps(doc, sort_keys=True)
        except Exception:
            return str(doc)

    def _dedupe_list(items):
        seen = set()
        out = []
        if not isinstance(items, (list, tuple)):
            return items
        for item in items:
            key = _doc_key(item)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    if not isinstance(report_obj, dict):
        return report_obj

    if "results" in report_obj:
        try:
            report_obj["results"] = _dedupe_list(report_obj.get("results") or [])
        except Exception:
            pass

    checks = report_obj.get("checks") or []
    if isinstance(checks, (list, tuple)):
        for check in checks:
            if not isinstance(check, dict):
                continue
            try:
                res_list = check.get("results") or []
                pol_list = check.get("policy_evidence") or []
                combined = []
                seen_keys = set()
                for item in (res_list if isinstance(res_list, (list, tuple)) else []):
                    key = _doc_key(item)
                    if key and key not in seen_keys:
                        seen_keys.add(key)
                        combined.append(item)
                for item in (pol_list if isinstance(pol_list, (list, tuple)) else []):
                    key = _doc_key(item)
                    if key and key not in seen_keys:
                        seen_keys.add(key)
                        combined.append(item)
                if not combined:
                    if res_list:
                        combined = _dedupe_list(res_list)
                    elif pol_list:
                        combined = _dedupe_list(pol_list)
                check["results"] = combined if isinstance(combined, list) else []
                check["policy_evidence"] = combined if isinstance(combined, list) else []
            except Exception:
                pass

            if not check.get("ai_evidence_summary") and check.get("ai_search_summary"):
                check["ai_evidence_summary"] = check.get("ai_search_summary")

            if preserve_gap_fields:
                if check.get("cited_documents"):
                    check["cited_documents"] = _dedupe_list(check.get("cited_documents") or [])
                continue

            try:
                if "results" in check:
                    del check["results"]
                if "ai_search_evidence" in check:
                    del check["ai_search_evidence"]
                if "ai_search_summary" in check:
                    del check["ai_search_summary"]
            except Exception:
                pass

    if preserve_gap_fields:
        return report_obj

    try:
        if "results" in report_obj:
            del report_obj["results"]
        if "ai_search_evidence" in report_obj:
            del report_obj["ai_search_evidence"]
        if "ai_search_summary" in report_obj:
            del report_obj["ai_search_summary"]
    except Exception:
        pass
    return report_obj


@ai_engine.route("/ai-gap-analysis-reports", methods=["GET", "POST"])
@login_required
def ai_gap_analysis_reports():
    """List or create AI gap analysis reports (separate from compliance search reports).

    Stored as a JSON array in `AI_GAP_ANALYSIS_REPORTS_FILE`.
    """
    try:
        if request.method == "GET":
            reports = _filter_reports_for_current_user(_load_gap_analysis_reports())
            return jsonify({"success": True, "reports": reports})

        data = request.get_json() or {}
        name = data.get("name") or data.get("title") or f"Gap Analysis {datetime.now(UTC).isoformat()}"
        report_obj = (
            data.get("gap_report")
            or data.get("report")
            or data.get("payload")
            or data.get("search_report")
            or data
        )
        user_display, user_id = _current_user_report_identity()
        reports = _load_gap_analysis_reports()
        new_id = int(datetime.now(UTC).timestamp() * 1000)
        shared_flag = bool(data.get("shared", False))
        if isinstance(report_obj, dict):
            report_obj = _unwrap_saved_report_payload(copy.deepcopy(report_obj))
            report_obj["report_type"] = "gap_analysis"
            if data.get("sheet"):
                report_obj["sheet"] = data.get("sheet")
            _dedupe_saved_report_documents(report_obj, preserve_gap_fields=True)

        entry = {
            "id": new_id,
            "name": str(name),
            "date": datetime.now(UTC).isoformat(),
            "user": user_display,
            "user_id": user_id,
            "shared": shared_flag,
            "report_type": "gap_analysis",
            "report": report_obj,
        }
        reports.append(entry)
        if not _save_gap_analysis_reports(reports):
            return jsonify({"success": False, "error": "Failed to persist gap analysis report on server"}), 500
        return jsonify({"success": True, "report": entry})
    except Exception as e:
        logger.error(f"Error in ai_gap_analysis_reports endpoint: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@ai_engine.route("/ai-gap-analysis-reports/<int:report_id>", methods=["DELETE"])
@login_required
def delete_gap_analysis_report(report_id):
    try:
        return _delete_saved_report(report_id, _load_gap_analysis_reports, _save_gap_analysis_reports)
    except Exception as e:
        logger.error(f"Error in delete_gap_analysis_report endpoint: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@ai_engine.route("/ai-search-reports", methods=["GET", "POST"])
@login_required
def ai_search_reports():
    """List or create AI search reports used by CAP AI Compliance pages.

    Stored as a JSON array in `AI_SEARCH_REPORTS_FILE` with objects: {id, name, date, report}
    """
    try:
        if request.method == "GET":
            reports = _load_ai_reports()
            try:
                filtered = []
                curr_name = None
                curr_id = None
                if current_user and getattr(current_user, "is_authenticated", False):
                    curr_name = (
                        getattr(current_user, "name", None)
                        or getattr(current_user, "username", None)
                        or getattr(current_user, "display_name", None)
                        or getattr(current_user, "email", None)
                    )
                    curr_id = getattr(current_user, "id", None) or getattr(current_user, "user_id", None)

                for r in reports:
                    if r.get("shared"):
                        filtered.append(r)
                        continue
                    if curr_id is not None and str(r.get("user_id")) == str(curr_id):
                        filtered.append(r)
                        continue
                    if curr_name and str(r.get("user")) == str(curr_name):
                        filtered.append(r)
                        continue
                return jsonify({"success": True, "reports": filtered})
            except Exception:
                return jsonify({"success": True, "reports": reports})

        # POST - save new report
        data = request.get_json() or {}
        name = data.get("name") or data.get("title") or f"AI Report {datetime.now(UTC).isoformat()}"
        
        # Accept `report`, `payload`, or `search_report` as what we store in the JSON array
        report_obj = data.get("report") or data.get("payload") or data.get("search_report") or data
        if isinstance(report_obj, dict):
            report_obj = _unwrap_saved_report_payload(copy.deepcopy(report_obj))
        
        try:
            user_display = None
            user_id = None
            if current_user and getattr(current_user, "is_authenticated", False):
                user_id = getattr(current_user, "id", None) or getattr(current_user, "user_id", None)
                user_display = (
                    getattr(current_user, "username", None)
                    or getattr(current_user, "display_name", None)
                    or getattr(current_user, "email", None)
                    or getattr(current_user, "name", None)
                )
            if not user_display:
                user_display = "anonymous"
        except Exception:
            user_display = "anonymous"
            user_id = None

        reports = _load_ai_reports()
        new_id = int(datetime.now(UTC).timestamp() * 1000)
        shared_flag = bool(data.get("shared", False))

        entry = {
            "id": new_id,
            "name": str(name),
            "date": datetime.now(UTC).isoformat(),
            "user": user_display,
            "user_id": user_id,
            "shared": shared_flag,
            "report": report_obj,
        }
        # Deduplicate top-level and per-check results, normalize fields, then remove top-level results
        try:
            def _doc_key(d):
                try:
                    if not isinstance(d, dict):
                        return str(d)
                    return d.get("id") or d.get("document_name") or d.get("link") or json.dumps(d, sort_keys=True)
                except Exception:
                    return str(d)

            def _dedupe_list(lst):
                seen = set()
                out = []
                if not isinstance(lst, (list, tuple)):
                    return lst
                for it in lst:
                    k = _doc_key(it)
                    if k in seen:
                        continue
                    seen.add(k)
                    out.append(it)
                return out

            if isinstance(report_obj, dict):
                if "results" in report_obj:
                    try:
                        report_obj["results"] = _dedupe_list(report_obj.get("results") or [])
                    except Exception:
                        pass
                checks = report_obj.get("checks") or []
                if isinstance(checks, (list, tuple)):
                    for ck in checks:
                        try:
                            if isinstance(ck, dict):
                                # Combine and deduplicate per-check document lists (results + policy_evidence)
                                try:
                                    res_list = ck.get("results") or []
                                    pol_list = ck.get("policy_evidence") or []
                                    combined = []
                                    seen_keys = set()
                                    # preserve first-seen order from results then policy_evidence
                                    for it in (res_list if isinstance(res_list, (list, tuple)) else []):
                                        k = _doc_key(it)
                                        if k and k not in seen_keys:
                                            seen_keys.add(k)
                                            combined.append(it)
                                    for it in (pol_list if isinstance(pol_list, (list, tuple)) else []):
                                        k = _doc_key(it)
                                        if k and k not in seen_keys:
                                            seen_keys.add(k)
                                            combined.append(it)
                                    # fallback: dedupe individually if nothing combined
                                    if not combined:
                                        if res_list:
                                            combined = _dedupe_list(res_list)
                                        elif pol_list:
                                            combined = _dedupe_list(pol_list)
                                    # write back deduped results
                                    ck["results"] = combined if isinstance(combined, list) else []
                                    ck["policy_evidence"] = combined if isinstance(combined, list) else []
                                except Exception:
                                    # non-fatal: ensure individual lists are at least deduped
                                    try:
                                        if "results" in ck:
                                            ck["results"] = _dedupe_list(ck.get("results") or [])
                                    except Exception:
                                        pass
                                    try:
                                        if "policy_evidence" in ck:
                                            ck["policy_evidence"] = _dedupe_list(ck.get("policy_evidence") or [])
                                    except Exception:
                                        pass
                                # normalize summary key
                                if not ck.get("ai_evidence_summary") and ck.get("ai_search_summary"):
                                    ck["ai_evidence_summary"] = ck.get("ai_search_summary")
                                # Clean up duplicate fields for AI Engine reports - keep only policy_evidence
                                try:
                                    if "results" in ck:
                                        del ck["results"]
                                    if "ai_search_evidence" in ck:
                                        del ck["ai_search_evidence"]
                                    if "ai_search_summary" in ck:
                                        del ck["ai_search_summary"]
                                except Exception:
                                    pass
                        except Exception:
                            continue
                try:
                    if "results" in report_obj:
                        del report_obj["results"]
                    # Clean up duplicate fields for AI Engine reports
                    if "ai_search_evidence" in report_obj:
                        del report_obj["ai_search_evidence"]
                    if "ai_search_summary" in report_obj:
                        del report_obj["ai_search_summary"]
                except Exception:
                    pass
        except Exception:
            pass

        reports.append(entry)
        ok = _save_ai_reports(reports)
        if not ok:
            return (
                jsonify({"success": False, "error": "Failed to persist AI report on server"}),
                500,
            )

        return jsonify({"success": True, "report": entry})

    except Exception as e:
        logger.error(f"Error in ai_search_reports endpoint: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@ai_engine.route("/ai-search-reports/<int:report_id>", methods=["DELETE"])
@login_required
def delete_ai_report(report_id):
    try:
        return _delete_saved_report(report_id, _load_ai_reports, _save_ai_reports)
    except Exception as e:
        logger.error(f"Error in delete_ai_report endpoint: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


def _build_discovery_engine_runtime_context():
    """Build shared auth and resource context for Discovery Engine clients."""
    creds = None
    sa_info = None
    GOOGLE_SERVICE_ACCOUNT_SUBJECT = os.environ.get("GOOGLE_SERVICE_ACCOUNT_SUBJECT", None)
    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT") or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    location = (os.getenv("LOCATION") or os.getenv("DATASTORE_LOCATION") or "global").strip() or "global"
    client_options = None

    try:
        if location and location.lower() != "global":
            api_endpoint = f"{location.lower()}-discoveryengine.googleapis.com"
            try:
                from google.api_core.client_options import ClientOptions

                client_options = ClientOptions(api_endpoint=api_endpoint)
            except Exception:
                client_options = {"api_endpoint": api_endpoint}
    except Exception:
        client_options = None

    if sa_json:
        try:
            sa_info = json.loads(sa_json)
            try:
                from google.oauth2 import service_account as sa

                creds = sa.Credentials.from_service_account_info(sa_info, scopes=scopes)
                if GOOGLE_SERVICE_ACCOUNT_SUBJECT:
                    creds = creds.with_subject(GOOGLE_SERVICE_ACCOUNT_SUBJECT)
            except Exception as sce:
                logger.exception(f"Failed to create service account credentials: {sce}")
        except Exception as jex:
            logger.exception(f"Failed to parse GOOGLE_SERVICE_ACCOUNT JSON: {jex}")

    project = (
        os.getenv("PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or os.getenv("GCP_PROJECT")
        or (sa_info.get("project_id") if isinstance(sa_info, dict) and sa_info.get("project_id") else None)
        or ""
    )
    app_id = os.getenv("APP_ID") or os.getenv("APPLICATION_ID") or None
    serving_config_id = (
        os.getenv("SERVING_CONFIG_ID")
        or os.getenv("SERVINGCONFIG_ID")
        or os.getenv("SEARCH_SERVING_CONFIG_ID")
        or "default_search"
    )
    project = str(project).strip()
    app_id = str(app_id).strip() if app_id is not None else None
    serving_config_id = str(serving_config_id).strip() or "default_search"

    if not app_id:
        raise ValueError("APP_ID (or APPLICATION_ID) is required for Discovery Engine search")

    engine_parent = (
        f"projects/{project}/"
        f"locations/{location}/"
        f"collections/default_collection/"
        f"engines/{app_id}"
    )
    serving_config = f"{engine_parent}/servingConfigs/{serving_config_id}"

    return {
        "credentials": creds,
        "client_options": client_options,
        "location": location,
        "project": project,
        "app_id": app_id,
        "engine_parent": engine_parent,
        "serving_config": serving_config,
    }


def _build_discovery_engine_client(client_cls, creds=None, client_options=None):
    """Instantiate a Discovery Engine client with shared auth options."""
    if creds is not None:
        if client_options:
            return client_cls(credentials=creds, client_options=client_options)
        return client_cls(credentials=creds)
    if client_options:
        return client_cls(client_options=client_options)
    return client_cls()


def _struct_to_plain_dict(struct_value):
    """Return a plain dict from protobuf Struct or map-like metadata."""
    if not struct_value:
        return {}
    try:
        return dict(struct_value)
    except Exception:
        try:
            from google.protobuf.json_format import MessageToDict

            return MessageToDict(struct_value)
        except Exception:
            return {}


def _extract_answer_reference_result(reference, req_code=None):
    """Normalize an Answer reference into the existing search-result shape."""
    doc_id = None
    doc_title = None
    raw_link = None
    snippet = ""
    struct_data = {}
    page_identifier = None

    try:
        unstructured_info = getattr(reference, "unstructured_document_info", None)
        chunk_info = getattr(reference, "chunk_info", None)
        structured_info = getattr(reference, "structured_document_info", None)

        if unstructured_info and (getattr(unstructured_info, "document", None) or getattr(unstructured_info, "title", None) or getattr(unstructured_info, "uri", None)):
            doc_id = getattr(unstructured_info, "document", None)
            doc_title = getattr(unstructured_info, "title", None)
            raw_link = getattr(unstructured_info, "uri", None)
            struct_data = _struct_to_plain_dict(getattr(unstructured_info, "struct_data", None))
            chunk_contents = list(getattr(unstructured_info, "chunk_contents", None) or [])
            if chunk_contents:
                snippet = " ".join(str(getattr(chunk, "content", "") or "").strip() for chunk in chunk_contents if getattr(chunk, "content", None)).strip()
                page_identifier = getattr(chunk_contents[0], "page_identifier", None)
        elif chunk_info and (getattr(chunk_info, "chunk", None) or getattr(chunk_info, "content", None) or getattr(chunk_info, "document_metadata", None)):
            metadata = getattr(chunk_info, "document_metadata", None)
            doc_id = getattr(metadata, "document", None) if metadata else getattr(chunk_info, "chunk", None)
            doc_title = getattr(metadata, "title", None) if metadata else None
            raw_link = getattr(metadata, "uri", None) if metadata else None
            struct_data = _struct_to_plain_dict(getattr(metadata, "struct_data", None)) if metadata else {}
            snippet = str(getattr(chunk_info, "content", "") or "").strip()
            page_identifier = getattr(metadata, "page_identifier", None) if metadata else None
        elif structured_info and getattr(structured_info, "document", None):
            doc_id = getattr(structured_info, "document", None)
            struct_data = _struct_to_plain_dict(getattr(structured_info, "struct_data", None))
            doc_title = struct_data.get("title") or struct_data.get("document_name") or struct_data.get("name")
            raw_link = struct_data.get("link") or struct_data.get("url") or struct_data.get("uri")
    except Exception as ref_err:
        logger.debug(f"Failed to normalize answer reference: {ref_err}")

    actual_filename = None
    for key in ("filename", "file_name", "document_name", "name", "title", "display_title"):
        value = struct_data.get(key)
        if value:
            actual_filename = str(value).strip()
            break

    gcs_filename = None
    if raw_link and str(raw_link).startswith("gs://"):
        try:
            candidate = str(raw_link).split("/")[-1].split("?")[0]
            if candidate and "." in candidate:
                gcs_filename = candidate
        except Exception:
            gcs_filename = None

    doc_display_name = gcs_filename or actual_filename or doc_title or doc_id or "Document"
    working_link = _get_working_link(doc_display_name, gcs_path=raw_link, doc_id=doc_id)
    resolved_link = working_link or raw_link
    if not snippet and page_identifier:
        snippet = f"Referenced on page {page_identifier}"

    result = {
        "id": doc_id,
        "document_name": doc_display_name,
        "title": doc_title,
        "snippet": snippet,
        "link": resolved_link,
        "requirement_query": req_code,
    }
    if raw_link and raw_link != resolved_link:
        result["document_uri"] = raw_link
    return result


def _extract_search_result_item(search_result, req_code=None):
    """Normalize a Search API result into the existing search-result shape."""
    doc_id = None
    doc_title = None
    raw_link = None
    snippet = ""
    struct_data = {}
    derived_struct = {}

    try:
        document = getattr(search_result, "document", None)
        if document:
            doc_id = getattr(document, "id", None) or getattr(document, "name", None)
            struct_data = _struct_to_plain_dict(getattr(document, "struct_data", None))
            derived_struct = _struct_to_plain_dict(getattr(document, "derived_struct_data", None))
            doc_title = (
                struct_data.get("title")
                or struct_data.get("document_name")
                or struct_data.get("name")
                or derived_struct.get("title")
                or derived_struct.get("document_name")
            )
            raw_link = (
                struct_data.get("link")
                or struct_data.get("url")
                or struct_data.get("uri")
                or derived_struct.get("link")
                or derived_struct.get("url")
                or derived_struct.get("uri")
            )

            for snippet_key in ("snippets", "extractive_answers", "extractive_segments"):
                snippet_val = derived_struct.get(snippet_key)
                if isinstance(snippet_val, list) and snippet_val:
                    first_item = snippet_val[0]
                    if isinstance(first_item, dict):
                        snippet = (
                            str(first_item.get("snippet") or first_item.get("content") or "").strip()
                        )
                    else:
                        snippet = str(first_item).strip()
                    if snippet:
                        break
        if not snippet:
            snippet = str(getattr(search_result, "snippet", "") or "").strip()
    except Exception as ref_err:
        logger.debug(f"Failed to normalize search result item: {ref_err}")

    actual_filename = None
    for key in ("filename", "file_name", "document_name", "name", "title", "display_title"):
        value = struct_data.get(key) or derived_struct.get(key)
        if value:
            actual_filename = str(value).strip()
            break

    doc_display_name = actual_filename or doc_title or doc_id or "Document"
    working_link = _get_working_link(doc_display_name, gcs_path=raw_link, doc_id=doc_id)
    resolved_link = working_link or raw_link

    result = {
        "id": doc_id,
        "document_name": doc_display_name,
        "title": doc_title,
        "snippet": snippet,
        "link": resolved_link,
        "requirement_query": req_code,
    }
    if raw_link and raw_link != resolved_link:
        result["document_uri"] = raw_link
    return result


def _extract_answer_citations(answer_obj, results):
    """Convert answer citation metadata into the existing lightweight citation shape."""
    if not answer_obj:
        return []

    reference_index_map = {}
    for idx, result in enumerate(results or [], start=1):
        for key in (result.get("id"), result.get("document_uri"), result.get("link"), result.get("document_name")):
            if key and key not in reference_index_map:
                reference_index_map[str(key)] = idx

    citations = []
    for citation_idx, citation in enumerate(list(getattr(answer_obj, "citations", None) or [])):
        sources = []
        for source in list(getattr(citation, "sources", None) or []):
            reference_id = getattr(source, "reference_id", None)
            if reference_id and str(reference_id) in reference_index_map:
                sources.append({"reference_index": reference_index_map[str(reference_id)]})
        if sources:
            citations.append({"citation_index": citation_idx, "sources": sources})
    return citations


def _build_answer_skip_summary(answer_obj):
    """Return a readable fallback summary when AnswerQuery skips answer generation."""
    reasons = list(getattr(answer_obj, "answer_skipped_reasons", None) or [])
    if not reasons:
        return ""

    labels = []
    reason_enum = getattr(type(answer_obj), "AnswerSkippedReason", None)
    for reason in reasons:
        try:
            labels.append(reason_enum(reason).name.replace("_", " ").title())
        except Exception:
            labels.append(str(reason))

    if not labels:
        return "No results could be found. Try rephrasing the search query."

    if any("No Relevant Content" in label for label in labels):
        return "No results could be found. Try rephrasing the search query."
    return "Answer was skipped: " + ", ".join(labels)


_VERTEX_PLACEHOLDER_SUMMARY_SUBSTRINGS = (
    "summary could not be generated",
    "a summary could not be generated",
    "could not be generated for your search",
    "here are some search results",
)


def _is_vertex_placeholder_summary_text(text: str) -> bool:
    """True when Discovery returns generic boilerplate instead of a real compliance answer."""
    t = (text or "").strip().lower()
    if not t:
        return False
    return any(s in t for s in _VERTEX_PLACEHOLDER_SUMMARY_SUBSTRINGS)


def _synthesize_compliance_summary_from_evidence_rows(meta_rows, evidence_limit: int) -> str:
    """Build a short [COMPLIANT] summary from normalized evidence dicts (document_name / title)."""
    if not meta_rows:
        return ""
    cap = evidence_limit if evidence_limit and evidence_limit > 0 else _VERTEX_ANSWER_QUERY_MAX_RETURN_RESULTS
    try:
        cap = min(int(cap), len(meta_rows), _VERTEX_ANSWER_QUERY_MAX_RETURN_RESULTS)
    except (TypeError, ValueError):
        cap = min(len(meta_rows), _VERTEX_ANSWER_QUERY_MAX_RETURN_RESULTS)
    parts = []
    for i in range(cap):
        try:
            meta = meta_rows[i]
            if not isinstance(meta, dict):
                continue
            name = (meta.get("document_name") or meta.get("title") or "").strip()
            if not name:
                continue
            if len(name) > 90:
                name = name[:87] + "..."
            snippet = str(meta.get("snippet") or "").strip()
            if snippet:
                snippet = re.sub(r"\s+", " ", snippet)
                if len(snippet) > 160:
                    snippet = snippet[:157].rstrip() + "..."
                parts.append(f"{name} [{len(parts) + 1}]: {snippet}")
            else:
                parts.append(f"{name} [{len(parts) + 1}]")
        except Exception:
            continue
    if not parts:
        return ""
    inner = " Evidence includes " + "; ".join(parts) + "."
    return f"[COMPLIANT]: Search returned {len(parts)} matching document(s).{inner}"


def _synthesize_compliance_summary_from_vertex_references(answer_obj, req_code, evidence_limit: int) -> str:
    """Build [COMPLIANT] summary when AnswerQuery references exist but answer_text is placeholder or empty."""
    refs = list(getattr(answer_obj, "references", None) or []) if answer_obj else []
    if not refs:
        return ""
    rows = []
    for r in refs:
        try:
            rows.append(_extract_answer_reference_result(r, req_code=req_code))
        except Exception:
            continue
    return _synthesize_compliance_summary_from_evidence_rows(rows, evidence_limit)


def _synthesize_compliance_summary_from_search_fallback_hits(hits, req_code, evidence_limit: int) -> str:
    """Same as vertex-reference synthesis when documents came from Search API fallback only."""
    if not hits:
        return ""
    cap = _effective_evidence_process_cap(evidence_limit, search_only=False)
    rows = []
    for h in itertools.islice(hits, cap):
        try:
            rows.append(_extract_search_result_item(h, req_code=req_code))
        except Exception:
            continue
    return _synthesize_compliance_summary_from_evidence_rows(rows, evidence_limit)


def _prepend_retrieved_docs_to_summaries(all_summaries, evidence_rows):
    """Evidence filenames stay in the Evidences column; summaries are narrative-only (no retrieved-doc preamble)."""
    return list(all_summaries or [])


def _append_vertex_excerpts_to_summaries(all_summaries, evidence_rows):
    """Chunk excerpts are not appended into AI Search Summary text (evidence list + citations suffice)."""
    return list(all_summaries or [])


def _extract_lims_keywords(query_text, fallback_terms=None):
    """Extract a compact set of LIMS-friendly search keywords."""
    fallback_terms = fallback_terms or []
    keywords = set()
    text = str(query_text or "").lower()
    words = re.findall(r"\b[a-z]{4,}\b", text)
    common_words = {
        "that", "this", "from", "with", "have", "been", "such", "which", "their",
        "are", "for", "the", "and", "all", "not", "into", "more", "when", "only",
        "other", "same", "may", "any", "about", "also", "subject", "required",
        "evidences", "note", "prompt", "onsite", "revision", "include", "records",
        "prior", "search", "focus", "summary", "already", "cited", "above",
        "find", "please", "show", "list", "give",
    }
    for word in words:
        if word not in common_words and len(word) >= 4:
            keywords.add(word)

    for match in re.finditer(r"\b[jJ]\d+(?:\.\d+)*\b", str(query_text or "")):
        code = match.group(0).lower()
        keywords.add(code)
        keywords.add(code.replace(".", ""))
        base = code.split(".")[0]
        if base:
            keywords.add(base)

    if re.search(r"\brecords?\b", text):
        for term in (
            "investigation", "corrective", "incident", "nonconforming",
            "non-conforming", "nce", "sentinel", "rca", "root", "cause", "analysis",
        ):
            keywords.add(term)

    if not keywords:
        for term in fallback_terms:
            clean = re.sub(r"[^a-zA-Z0-9]", "", str(term or "")).lower()
            if len(clean) >= 3:
                keywords.add(clean)

    return list(keywords)[:8]


def _fetch_lims_records_from_orm(query_text, req_code=None, keywords=None, max_records=3):
    """Legacy path: read Test_Result / Action from the shared XYZ LIMS Postgres DB."""
    keywords = list(keywords or [])
    if not keywords:
        return []

    def _match_details(text_value, keywords_list):
        text = str(text_value or "").lower()
        hits = [kw for kw in keywords_list if kw in text]
        return hits

    try:
        from database import db_session
        from models.sample_control import Test_Result
        from models.general import Action
        from sqlalchemy import cast, String, or_
    except Exception as import_err:
        logger.debug(f"LIMS ORM import unavailable for req {req_code}: {import_err}")
        return []

    records = []
    cutoff = datetime.now() - timedelta(days=365)
    patterns = [f"%{kw}%" for kw in keywords]

    try:
        tr_clauses = []
        for pat in patterns:
            tr_clauses.extend([Test_Result.result.ilike(pat), cast(Test_Result.notes, String).ilike(pat)])

        test_results = []
        if tr_clauses:
            test_results = (
                db_session.query(Test_Result)
                .filter(Test_Result.commit_timestamp >= cutoff, or_(*tr_clauses))
                .order_by(Test_Result.commit_timestamp.desc())
                .limit(max_records)
                .all()
            )

        for row in test_results:
            rid = getattr(row, "id", "?")
            sample_name = getattr(row, "sample_name", "") or ""
            test_name = getattr(row, "test_id", "") or ""
            result_val = getattr(row, "result", "") or ""
            notes_val = getattr(row, "notes", "") or ""
            timestamp = getattr(row, "commit_timestamp", "") or ""
            matched_result = _match_details(result_val, keywords)
            matched_notes = _match_details(notes_val, keywords)
            match_fields = []
            if matched_result:
                match_fields.append("result")
            if matched_notes:
                match_fields.append("notes")
            snippet = f"Test Result (ID: {rid}) - Sample: {sample_name}, Test: {test_name}, Result: {result_val}"
            if timestamp:
                snippet += f" [{timestamp}]"
            records.append(
                {
                    "id": f"lims_test_{rid}",
                    "document_name": f"LIMS Test Result - ID {rid}",
                    "title": "Laboratory Test Result",
                    "snippet": snippet,
                    "link": None,
                    "requirement_query": req_code,
                    "from_lims": True,
                    "lims_type": "test_result",
                    "source_type": "lims",
                    "match_reason": (
                        "Matched requirement keywords in "
                        + (", ".join(match_fields) if match_fields else "test result text")
                    ),
                }
            )
    except Exception as test_err:
        logger.debug(f"LIMS test query failed for req {req_code}: {test_err}")
        try:
            db_session.rollback()
        except Exception:
            pass

    try:
        action_clauses = []
        for pat in patterns:
            action_clauses.extend([Action.action_type.ilike(pat), cast(Action.description, String).ilike(pat)])

        actions = []
        if action_clauses:
            actions = (
                db_session.query(Action)
                .filter(Action.commit_timestamp >= cutoff, or_(*action_clauses))
                .order_by(Action.commit_timestamp.desc())
                .limit(max_records)
                .all()
            )

        for row in actions:
            aid = getattr(row, "id", "?")
            action_type = getattr(row, "action_type", "") or ""
            user_id = getattr(row, "user_id", "") or ""
            timestamp = getattr(row, "commit_timestamp", "") or ""
            details = getattr(row, "description", "") or getattr(row, "payload", "") or ""
            matched_action_type = _match_details(action_type, keywords)
            matched_details = _match_details(details, keywords)
            match_fields = []
            if matched_action_type:
                match_fields.append("action_type")
            if matched_details:
                match_fields.append("description")
            snippet = f"Audit Action (ID: {aid}) - Type: {action_type}, User: {user_id}"
            if timestamp:
                snippet += f" [{timestamp}]"
            if details:
                snippet += f" - {str(details)[:120]}"
            records.append(
                {
                    "id": f"lims_action_{aid}",
                    "document_name": f"LIMS Audit Action - ID {aid}",
                    "title": "Audit/Action Log Entry",
                    "snippet": snippet,
                    "link": None,
                    "requirement_query": req_code,
                    "from_lims": True,
                    "lims_type": "audit_action",
                    "source_type": "lims",
                    "match_reason": (
                        "Matched requirement keywords in "
                        + (", ".join(match_fields) if match_fields else "audit action text")
                    ),
                }
            )
    except Exception as action_err:
        logger.debug(f"LIMS action query failed for req {req_code}: {action_err}")
        try:
            db_session.rollback()
        except Exception:
            pass

    return records[:max_records]


def _fetch_lims_records_for_query(query_text, req_code=None, fallback_terms=None, max_records=3):
    """
    Fetch LIMS evidence for a requirement query.

    Prefer external LIMS HTTP API when configured and available; optionally fall
    back to in-process ORM against the shared XYZ LIMS database.
    """
    keywords = _extract_lims_keywords(query_text, fallback_terms=fallback_terms)
    if not keywords and not (query_text or "").strip():
        return []

    api_ok = False
    orm_ok = False
    try:
        from apps.lims_adapter import get_lims_availability

        status = get_lims_availability()
        if not status.get("available"):
            return []
        api_ok = bool((status.get("api") or {}).get("available"))
        orm_ok = bool((status.get("orm") or {}).get("available"))
    except Exception:
        return []

    records = []
    try:
        from apps.lims_adapter import (
            external_lims_configured,
            fetch_lims_evidence_via_api,
        )
    except Exception as adapter_err:
        logger.debug(f"LIMS adapter import failed: {adapter_err}")
        external_lims_configured = lambda: False  # noqa: E731
        fetch_lims_evidence_via_api = None

    use_api = False
    try:
        use_api = bool(api_ok and external_lims_configured())
    except Exception:
        use_api = False

    if use_api and fetch_lims_evidence_via_api:
        try:
            records = fetch_lims_evidence_via_api(
                query_text,
                keywords=keywords,
                req_code=req_code,
                max_records=max_records,
            ) or []
        except Exception as api_err:
            logger.warning(
                "External LIMS fetch failed for req %s: %s",
                req_code,
                api_err,
            )
            records = []

    fallback_orm = str(os.environ.get("LIMS_API_FALLBACK_ORM", "1")).lower() in (
        "1",
        "true",
        "yes",
    )
    # When API is not available, try ORM if it passed the startup probe.
    # When API is available, only fall back if enabled and the API returned nothing.
    if orm_ok and ((not use_api) or (fallback_orm and not records)):
        orm_rows = _fetch_lims_records_from_orm(
            query_text,
            req_code=req_code,
            keywords=keywords or fallback_terms,
            max_records=max_records,
        )
        if orm_rows:
            if records:
                seen = {r.get("id") for r in records}
                for row in orm_rows:
                    if row.get("id") not in seen:
                        records.append(row)
            else:
                records = orm_rows

    return records[:max_records]


_SUMMARY_CITED_POLICY_RE = re.compile(
    r"(Policy|Record)\s+(?:No\.?\s+)?([A-Z][0-9]+(?:\.[0-9]+)*)\s*\(\s*AI\s+Match[:\s]*([^\)]*)\)",
    re.IGNORECASE,
)


def _append_summary_cited_policy_records(final_results, all_summaries):
    """Extract Policy/Record + AI Match citations from each **REQ**: summary block; append scoped synthetic rows."""
    if not all_summaries:
        return
    seen_pairs = set()
    for block in all_summaries:
        block = str(block or "").strip()
        if not block:
            continue
        m = re.match(r"^\*\*(?P<hdr>[^*]+)\*\*\s*:\s*(?P<body>.*)$", block, re.DOTALL | re.IGNORECASE)
        if not m:
            continue
        req_code = (m.group("hdr") or "").strip()
        body = m.group("body") or ""
        if not req_code:
            continue
        for match in _SUMMARY_CITED_POLICY_RE.finditer(body):
            code = match.group(2)
            score_text = (match.group(3) or "").strip()
            context = match.group(1).lower()
            code_lower = code.lower()
            pair = (req_code.lower(), code_lower)
            if pair in seen_pairs:
                continue
            already_present = any(
                str(r.get("requirement_query") or "").strip() == req_code
                and (
                    str(r.get("document_name") or "").lower() == code_lower
                    or str(r.get("document_name") or "").lower().startswith(code_lower + " ")
                )
                for r in final_results
            )
            if already_present:
                continue
            score = None
            if score_text and score_text.lower() != "not provided":
                score_match = re.search(r"([0-9]+(?:\.[0-9]+)?)", score_text)
                if score_match:
                    score = score_match.group(1)
            snippet_text = "Referenced in AI summary" + (f" with {score}% relevance match" if score else "")
            final_results.append(
                {
                    "id": f"policy_code_{code}",
                    "document_name": f"{code} (Policy)" if context == "policy" else f"{code} (Record)",
                    "title": f"Policy/Record {code}",
                    "snippet": snippet_text,
                    "link": None,
                    "requirement_query": req_code,
                    "from_summary": True,
                    "ai_match_score": score,
                }
            )
            seen_pairs.add(pair)
            logger.debug("Added cited policy/record code for %s: %s (%s)", req_code, code, score if score else "No score")


def _extract_requirement_summary_text(all_summaries_list, requirement_code):
    """Parse per-requirement answer text from merged **CODE**: summary blocks."""
    if not all_summaries_list or not requirement_code:
        return ""
    code = str(requirement_code).strip()
    for block in all_summaries_list:
        m = re.match(r"\*\*([A-Z0-9\.]+)\*\*:\s*(.*)\Z", block, re.DOTALL)
        if m and m.group(1) == code:
            return m.group(2).strip()
    return ""


def _summary_indicates_not_applicable(summary_text, note_text=None):
    """
    Detect N/A: conditional checklist notes (e.g. radionuclides — if the lab does not handle them,
    the requirement is skipped / Not Applicable) must not be scored as Compliant just because
    unrelated documents were retrieved.
    """
    st = (summary_text or "").strip().lower()
    note = (note_text or "").strip().lower()
    if not st and not note:
        return False
    if "[not applicable]" in st or re.match(r"^\s*not applicable\s*[:.]?", st):
        return True
    comb = f"{st} {note}"
    if "status should be not applicable" in comb or "should be not applicable" in st:
        return True
    conditional_note = any(
        phrase in note
        for phrase in (
            "skip this checklist",
            "if not, skip",
            "if not, status",
            "not, status should be not applicable",
            "if not, status should be not applicable",
        )
    ) or (
        "if not" in note
        and ("skip" in note or "not applicable" in note or "n/a" in note.split() or " na " in f" {note} ")
    )
    summary_suggests_na = any(
        phrase in st
        for phrase in (
            "checklist is skipped",
            "this checklist is skipped",
            "skip this checklist",
            "this checklist is skip",
            "not handle any specimens containing radionuclides",
            "does not handle any specimens containing radionuclides",
            "do not handle any specimens containing radionuclides",
            "no specimens containing radionuclides",
            "do not indicate that the lab handles",
            "none of the provided sources contain any information indicating that the lab handles",
            "condition for skipping the checklist is met",
            "therefore, this checklist is skipped",
        )
    )
    if conditional_note and summary_suggests_na:
        return True
    if "not applicable" in st and any(
        w in st for w in ("skip", "radionuclide", "radioactive", "checklist", "does not handle")
    ):
        return True
    # Skip stated in summary for radionuclide / radioactive conditional items (prompt may be omitted in follow-up)
    if any(
        phrase in st
        for phrase in (
            "therefore, this checklist is skipped",
            "this checklist is skipped",
            "checklist is skipped",
        )
    ) and (
        conditional_note
        or "radionuclide" in st
        or "radioactive" in st
        or "radionuclide" in note
        or "radioactive" in note
    ):
        return True
    return False


def _evidence_blob_for_records_heuristic(item):
    """Lowercased text used to decide whether a catalog hit looks like operational records (not only policy/SOP)."""
    parts = [
        str(item.get("document_name") or ""),
        str(item.get("title") or ""),
        str(item.get("snippet") or ""),
    ]
    return " ".join(parts).strip().lower()


def _evidence_item_supports_records_requirement(item):
    """
    True if this row counts toward an explicit Records requirement inferred from requirement/EOC text.

    LIMS rows always count. Vertex / Discovery rows count when they resemble records or
    operational artifacts (aligned with CAP checker record keywords), not policy-only hits.
    """
    if item.get("from_lims"):
        return True
    if str(item.get("source_type") or "").strip().lower() == "lims":
        return True
    lid = str(item.get("id") or "").lower()
    if lid.startswith("lims_"):
        return True
    blob = _evidence_blob_for_records_heuristic(item)
    if not blob:
        return False
    if (
        "test result (id:" in blob
        or "audit action (id:" in blob
        or "lims test result" in blob
        or "lims audit action" in blob
    ):
        return True
    # Record/document keyword stems for LIMS records heuristic
    record_tokens = (
        "form",
        "report",
        "log",
        "checklist",
        "worksheet",
        "quiz",
        "template",
        "certificate",
        "competency",
        "training",
        "inspection",
        "batch",
        "minutes",
    )
    return any(tok in blob for tok in record_tokens)


_ONSITE_VERIFICATION_KEYWORDS = (
    "onsite",
    "on-site",
    "on site",
    "site visit",
    "physical inspection",
    "in-person",
    "in person",
    "field visit",
    "walkthrough",
    "walk-through",
)

_DOCUMENT_EVIDENCE_KEYWORDS = (
    "policy",
    "sop",
    "standard operating",
    "procedure",
    "records",
    "documentation",
    "document",
    "written",
    "manual",
)


def _text_requires_onsite_verification(text):
    """True when requirement or EOC text calls for onsite verification."""
    t = str(text or "").lower()
    return any(keyword in t for keyword in _ONSITE_VERIFICATION_KEYWORDS)


def _text_mentions_document_evidence(text):
    """True when text explicitly references document-based evidence types."""
    t = str(text or "").lower()
    return any(keyword in t for keyword in _DOCUMENT_EVIDENCE_KEYWORDS)


def _query_core_text_without_tags(query):
    return re.sub(r"\[[^\]]+\]", " ", str(query or ""))


def _derive_evidence_tokens_from_query_part(q_part, requires_onsite=False, payload_raw=""):
    """Infer gap-analysis evidence tokens from requirement/EOC text."""
    raw = str(payload_raw or "").strip()
    if raw:
        return {
            token.strip().lower()
            for token in re.split(r"[,;/]", raw)
            if token and token.strip()
        }

    core = _query_core_text_without_tags(q_part).lower()
    if requires_onsite and not _text_mentions_document_evidence(core):
        return {"onsite"}

    tokens = {"policy", "sop", "records"}
    if requires_onsite:
        tokens.add("onsite")
    return tokens


def _evaluate_requirement_gaps(
    requirement_code,
    required_evidence_tokens,
    evidence_items,
    req_summary_text=None,
    note_text=None,
):
    """Assess missing evidence categories for a requirement."""
    if _summary_indicates_not_applicable(req_summary_text, note_text):
        items = evidence_items or []
        return {
            "requirement": requirement_code,
            "status": "NOT APPLICABLE",
            "missing_evidence": [],
            "evidence_count": len(items),
            "vertex_evidence_count": sum(1 for item in items if not item.get("from_lims")),
            "lims_evidence_count": sum(1 for item in items if item.get("from_lims")),
        }

    tokens = set(required_evidence_tokens or [])
    items = evidence_items or []
    has_vertex = any(not item.get("from_lims") for item in items)
    has_lims = any(item.get("from_lims") for item in items)
    has_onsite_note = any("onsite verification" in str(item.get("snippet", "")).lower() for item in items)

    missing = []
    if "onsite" in tokens and not has_onsite_note:
        missing.append("onsite")
    if {"policy", "sop", "policy_or_records"} & tokens and not has_vertex:
        missing.append("document")
    # Explicit "records" needs LIMS or Vertex items that look like records—not policy-only catalog hits.
    has_records_for_explicit = any(_evidence_item_supports_records_requirement(i) for i in items)
    if "records" in tokens and not has_records_for_explicit:
        missing.append("records")
    elif "policy_or_records" in tokens and "records" not in tokens and not (has_lims or has_vertex):
        missing.append("records")

    if missing:
        # Partial evidence (some required categories, not all) → PARTIAL; no evidence at all → not checked
        status = "PARTIAL" if items else "NOT CHECKED"
    elif items:
        status = "COMPLIANT"
    else:
        status = "NOT CHECKED"

    return {
        "requirement": requirement_code,
        "status": status,
        "missing_evidence": missing,
        "evidence_count": len(items),
        "vertex_evidence_count": sum(1 for item in items if not item.get("from_lims")),
        "lims_evidence_count": sum(1 for item in items if item.get("from_lims")),
    }

@ai_engine.route("/cap-ai-search", methods=["POST"])
@login_required
def cap_ai_search():
    """Proxy route to perform Discovery Engine searches for CAP AI page."""
    global _link_cycling_index
    
    # Reset link cycling for fresh distribution of documents across multiple links
    _link_cycling_index = 0
    
    try:
        payload = request.get_json(silent=True) or {}
        query = (payload.get("query") or "").strip()
        requirements = payload.get("requirements") or []
        ai_prompt = (payload.get("ai_prompt") or "").strip()  # General search tab only
        include_lims_data = bool(payload.get("include_data", True))
        try:
            from apps.lims_adapter import lims_data_available

            if include_lims_data and not lims_data_available():
                logger.info("Include LIMS data requested but no LIMS source available; ignoring.")
                include_lims_data = False
        except Exception:
            include_lims_data = False
        search_only = bool(payload.get("search_only", False)) # New: for general search queries
        followup_mode = bool(payload.get("followup_mode", False))
        existing_session = str(payload.get("session") or "").strip()
        user_pseudo_id = str(payload.get("user_pseudo_id") or "").strip()
        acl_user_id = str(payload.get("user_id") or payload.get("user_info") or "").strip()
        related_questions_enabled = bool(payload.get("related_questions", followup_mode or search_only))
        max_evidence_param = _parse_max_ai_search_evidence_from_payload(payload)

        if not query and requirements:
            query = " ".join([str(x) for x in requirements if x])

        if not search_only and query:
            query = _strip_stored_ai_result_tags_from_query(query)

        # Debug: Log what we received from frontend
        logger.debug("=== AI Search Debug Info (Server) ===")
        logger.debug(f"Received {len(requirements)} requirement(s): {requirements}")
        logger.debug(f"Processing query: {query[:100]}...") if len(query) > 100 else logger.debug(f"Processing query: {query}")
        logger.debug(f"Include LIMS data: {include_lims_data}")
        
        import re
        subjects_in_query = re.findall(r'\[Subject:\s*([^\]]+)\]', query)
        policy_procedure_in_query = re.findall(r'\[Policy/Procedure:\s*([^\]]+)\]', query)
        evidence_of_compliance_in_query = re.findall(
            r'\[Evidence of Compliance:\s*([^\]]+)\]', query
        )
        notes_in_query = re.findall(r'\[Note:\s*([^\]]+)\]', query)
        onsite_notes_in_query = re.findall(r'\[Onsite Note:\s*([^\]]+)\]', query)
        if subjects_in_query:
            logger.debug(f"Subjects in query: {subjects_in_query}")
        if policy_procedure_in_query:
            logger.debug("Policy/Procedure in query: %s", policy_procedure_in_query)
        if evidence_of_compliance_in_query:
            logger.debug(
                "Evidence of Compliance in query: %s",
                evidence_of_compliance_in_query,
            )
        if notes_in_query:
            logger.debug("Notes in query: %s", notes_in_query)
        if onsite_notes_in_query:
            logger.debug(f"Onsite Notes for Vertex AI: {onsite_notes_in_query}")
        if search_only and ai_prompt:
            logger.debug(
                "General search ai_prompt received: %s",
                ai_prompt[:100] if len(ai_prompt) > 100 else ai_prompt,
            )

        if existing_session:
            logger.debug(f"Using follow-up session: {existing_session}")
        if user_pseudo_id:
            logger.debug(f"Using user pseudo ID: {user_pseudo_id[:32]}")

        if not query:
            return jsonify({"success": False, "error": "Missing query or selected requirements"}), 400
        
        # Onsite-only: requirement/EOC text calls for onsite verification without document evidence.
        requires_onsite = bool(payload.get("requires_onsite_verification"))
        if not requires_onsite:
            requires_onsite = _text_requires_onsite_verification(
                _query_core_text_without_tags(query)
            )

        onsite_note_from_payload = str(payload.get("onsite_note") or "").strip()
        onsite_summary = onsite_note_from_payload
        if not onsite_summary:
            onsite_summary = ". ".join([note.strip() for note in onsite_notes_in_query if note.strip()])

        query_core = _query_core_text_without_tags(query)
        is_onsite_only = requires_onsite and not _text_mentions_document_evidence(query_core)

        if is_onsite_only:
            logger.debug(
                "Detected onsite-only requirement from text/payload - returning onsite-only result without document search"
            )

            if not onsite_summary:
                logger.debug("Onsite-only requirement has no onsite note - returning NON-COMPLIANT without document search")
                return jsonify({
                    "success": True,
                    "results": [],
                    "summary": "[NON-COMPLIANT]: Onsite verification required but not documented.",
                    "citations": [],
                    "result_count": 0,
                    "inferred_status": "NON-COMPLIANT",
                    "onsite_only": True,
                })

            # Onsite-only evaluation must rely only on onsite verification notes.
            combined_summary = f"[Onsite Verification]: {onsite_summary}"
            summary_lower = onsite_summary.lower()
            compliant_keywords = ['compliant', 'verified', 'checked', 'confirmed', 'passed', 'okay', 'ok', 'acceptable', 'adequate', 'satisfactory']
            non_compliant_keywords = ['non-compliant', 'noncompliant', 'failed', 'not compliant', 'not verified', 'not checked', 'issue', 'deficiency', 'gap', 'not found', 'no information', 'not available', 'missing', 'not mention', 'no evidence', 'insufficient', 'lack of', 'absence of', 'not provided']
            
            has_compliant = any(keyword in summary_lower for keyword in compliant_keywords)
            has_non_compliant = any(keyword in summary_lower for keyword in non_compliant_keywords)
            
            inferred_status = None
            if has_non_compliant:
                # Non-compliance takes precedence - if ANY part of the evidence indicates non-compliance
                inferred_status = "NON-COMPLIANT"
            elif has_compliant:
                inferred_status = "COMPLIANT"
            elif onsite_summary.strip():
                # If we have an onsite note but no clear indicators, assume compliant
                inferred_status = "COMPLIANT"
            else:
                inferred_status = "NON-COMPLIANT"
            
            logger.debug(f"Onsite evidence summary: {onsite_summary[:100]}...")
            logger.debug(f"Onsite-only summary for status check: {combined_summary[:200]}...")
            logger.debug(f"Status inference: has_compliant={has_compliant}, has_non_compliant={has_non_compliant}, inferred_status={inferred_status}")
            
            # Format return summary with status indicator (to be combined with requirement code by frontend)
            return_summary = f"[{inferred_status}]: {combined_summary}"
            
            
            return jsonify({
                "success": True,
                "results": [],  # No documents to return for onsite-only
                "summary": return_summary,
                "citations": [],
                "result_count": 0,
                "inferred_status": inferred_status,
                "onsite_only": True  # Flag to indicate this is onsite-only evidence
            })

        # Build preamble before Discovery queries
        query_parts = [p.strip() for p in query.split(" | ") if p.strip()]
        if not query_parts:
            query_parts = [query]

        preamble_text = None
        if search_only and ai_prompt:
            preamble_text = ai_prompt

        # Use default if no custom prompt (compliance search always uses default preamble)
        if not preamble_text:
            if search_only:
                preamble_text = (
                    "Provide a direct and comprehensive answer to the user's question about these documents. "
                    "DO NOT include any compliance status (e.g., [COMPLIANT], [NON-COMPLIANT], 'Pass', 'Fail'). "
                    "Focus purely on the content and purpose of the records found. Cite sources using [1], [2], etc. "
                    "Do not open with a separate paragraph that only lists retrieved filenames or "
                    "\"Retrieved documents (evidence basis):\"; begin with substantive findings. "
                    "Do not append a \"Supporting excerpts from retrieved documents\" section or bare lines that are "
                    "only [1], [2], etc. "
                    "ALWAYS include the full document file extension (e.g., .pdf, .docx) when mentioning a document name."
                )
            else:
                preamble_text = (
                    f"Summarize audit evidence in at most {_COMPLIANCE_SUMMARY_MAX_WORDS} words "
                    "(one or two concise paragraphs, ≤8 sentences). Ground every claim in retrieved documents. "
                    "After each statement supported by a retrieved document, cite with bracketed indices in source "
                    'order, e.g. "The Quality Manual [1] describes ...; SOPs are reviewed [2]." '
                    "When naming a file, include its full extension. If records or operational evidence were "
                    "requested but not found, state explicitly what was not verified rather than inventing "
                    "document IDs. Do not open with a filename list or \"Retrieved documents (evidence basis):\"; "
                    "begin with substantive findings. Do not append \"Supporting excerpts from retrieved documents\" "
                    'or bare "[1]" citation lines.'
                )

        # Add LIMS data context to the preamble if include_lims_data is specified
        if include_lims_data:
            preamble_text = preamble_text + " Include LIMS test results, audit logs, and laboratory records in your search results."
        else:
            if not search_only:
                preamble_text = preamble_text + " Focus on policies, SOPs, and documents. Exclude LIMS data and laboratory records."
            else:
                 preamble_text = preamble_text + " Focus on policies, SOPs, and documents."

        try:
            from google.cloud import discoveryengine_v1beta as discovery
        except Exception as e:
            logger.exception(f"Discovery Engine client import failed: {e}")
            return jsonify({"success": False, "error": "Discovery Engine client not available"}), 500

        runtime = _build_discovery_engine_runtime_context()
        serving_config = runtime["serving_config"]
        engine_parent = runtime["engine_parent"]
        creds = runtime["credentials"]
        client_options = runtime["client_options"]

        try:
            client = _build_discovery_engine_client(
                discovery.ConversationalSearchServiceClient,
                creds=creds,
                client_options=client_options,
            )
        except Exception as cex:
            logger.exception(f"Failed to create Discovery Engine conversational client: {cex}")
            return jsonify({"success": False, "error": "Discovery Engine conversational client unavailable"}), 500

        try:
            search_client = _build_discovery_engine_client(
                discovery.SearchServiceClient,
                creds=creds,
                client_options=client_options,
            )
        except Exception as sex:
            logger.warning(f"SearchServiceClient unavailable for fallback search: {sex}")
            search_client = None

        if not user_pseudo_id:
            try:
                if current_user and getattr(current_user, "is_authenticated", False):
                    user_pseudo_id = f"u-{getattr(current_user, 'id', None) or getattr(current_user, 'user_id', None) or 'authenticated'}"
            except Exception:
                user_pseudo_id = ""
        if not user_pseudo_id:
            user_pseudo_id = f"anon-{abs(hash((request.remote_addr or '') + query)) % 1000000000000}"

        if not acl_user_id:
            try:
                if current_user and getattr(current_user, "is_authenticated", False):
                    acl_user_id = (
                        str(getattr(current_user, "email", "") or "").strip()
                        or str(getattr(current_user, "id", "") or getattr(current_user, "user_id", "") or "").strip()
                    )
            except Exception:
                acl_user_id = ""
        if not acl_user_id:
            acl_user_id = str(os.getenv("GOOGLE_SERVICE_ACCOUNT_SUBJECT") or "").strip()

        if acl_user_id:
            logger.debug(f"Access-control search principal set: user_info.user_id='{acl_user_id}'")
        else:
            logger.warning("Access-control search principal missing; fallback search may return 0 with ACL enabled")

        use_followup_session = bool(followup_mode and len(query_parts) == 1)
        session_name = existing_session or (f"{engine_parent}/sessions/-" if use_followup_session else "")

        # Debug: Log query parts and Discovery Engine configuration
        logger.debug(f"Executing {len(query_parts)} query part(s) in parallel (max 5 workers)")
        logger.debug(f"Query parts to process:")
        for idx, qpart in enumerate(query_parts):
            logger.debug(f"  Part {idx}: length={len(qpart)}, first_100_chars='{qpart[:100]}'")
            has_subject = '[Subject:' in qpart
            has_eoc = '[Evidence of Compliance:' in qpart
            has_policy = '[Policy/Procedure:' in qpart
            has_ai_summary = '[AI Search Summary:' in qpart
            logger.debug(
                "           Has [Subject: %s, [Evidence of Compliance: %s, [Policy/Procedure: %s, [AI Search Summary: %s",
                has_subject,
                has_eoc,
                has_policy,
                has_ai_summary,
            )
        
        def _build_fallback_search_queries(q_part):
            """Build progressively broader search queries from a compliance query part."""
            text = str(q_part or "").strip()
            if not text:
                return []

            queries = [text]

            no_code = re.sub(r"^[A-Z0-9\.]+\s*:\s*", "", text).strip()
            if no_code and no_code not in queries:
                queries.append(no_code)

            no_tags = re.sub(r"\[[^\]]+\]", " ", no_code or text)
            no_tags = re.sub(r"\s+", " ", no_tags).strip()
            if no_tags and no_tags not in queries:
                queries.append(no_tags)

            core_only = re.split(r"\s*\[", no_code or text, maxsplit=1)[0].strip()
            if core_only and core_only not in queries:
                queries.append(core_only)

            return queries

        def _build_keyword_probe_queries(q_part):
            """Build very short keyword probes for low-recall engines/connectors."""
            text = str(q_part or "").lower()
            if not text:
                return []

            text = re.sub(r"\[[^\]]+\]", " ", text)
            text = re.sub(r"^[a-z0-9\.]+\s*:\s*", "", text)
            words = re.findall(r"\b[a-z]{4,}\b", text)
            stop_words = {
                "subject", "required", "evidences", "evidence", "records", "policy", "sop",
                "tests", "test", "that", "this", "with", "from", "have", "been", "which",
                "when", "where", "into", "also", "include", "verified", "monitor", "monitored",
            }
            tokens = []
            for w in words:
                if w not in stop_words and w not in tokens:
                    tokens.append(w)
            if not tokens:
                return []

            probes = []
            # phrase probe
            probes.append(" ".join(tokens[:4]))
            # pair probes
            if len(tokens) >= 2:
                probes.append(f"{tokens[0]} {tokens[1]}")
            if len(tokens) >= 3:
                probes.append(f"{tokens[1]} {tokens[2]}")
            if len(tokens) >= 4:
                probes.append(f"{tokens[2]} {tokens[3]}")

            cleaned = []
            for probe in probes:
                p = re.sub(r"\s+", " ", probe).strip()
                if p and p not in cleaned:
                    cleaned.append(p)
            return cleaned

        # Compliance runs: top-N matched docs per requirement (user-controlled); Ask AI (search_only) uses the API max.
        if search_only:
            _vertex_results_cap = _VERTEX_ANSWER_QUERY_MAX_RETURN_RESULTS
        elif max_evidence_param <= 0:
            _vertex_results_cap = _VERTEX_ANSWER_QUERY_MAX_RETURN_RESULTS
        else:
            _vertex_results_cap = min(_VERTEX_ANSWER_QUERY_MAX_RETURN_RESULTS, max_evidence_param)

        # Function to perform a single answer query part
        def perform_search(q_part):
            try:
                q_part_for_api = _fit_vertex_answer_query_text(q_part)
                if q_part_for_api != q_part:
                    logger.debug(
                        "Vertex answer_query text length reduced from %s to %s (max %s)",
                        len(q_part),
                        len(q_part_for_api),
                        _VERTEX_ANSWER_QUERY_TEXT_MAX,
                    )
                req_prompt = preamble_text
                if not search_only:
                    req_prompt = f"{(req_prompt or '').rstrip()}{_COMPLIANCE_DISCOVERY_ANSWER_SUFFIX}"
                if followup_mode:
                    req_prompt = (
                        f"{(req_prompt or '').rstrip()} "
                        "This is a follow-up question. Answer it directly and cite NEW evidence. "
                        "If the user asks for records, forms, or operational evidence, prioritize "
                        "completed lab operation records and investigation forms over policy/SOP documents "
                        "already discussed in the prior summary."
                    )

                answer_generation_spec = discovery.AnswerQueryRequest.AnswerGenerationSpec(
                    include_citations=True,
                    prompt_spec=discovery.AnswerQueryRequest.AnswerGenerationSpec.PromptSpec(
                        preamble=req_prompt
                    ),
                    ignore_non_answer_seeking_query=False,
                    ignore_adversarial_query=False,
                    ignore_low_relevant_content=False,
                )
                search_spec = discovery.AnswerQueryRequest.SearchSpec(
                    search_params=discovery.AnswerQueryRequest.SearchSpec.SearchParams(
                        max_return_results=_vertex_results_cap,
                    )
                )
                query_understanding_spec = discovery.AnswerQueryRequest.QueryUnderstandingSpec(
                    query_rephraser_spec=discovery.AnswerQueryRequest.QueryUnderstandingSpec.QueryRephraserSpec(
                        max_rephrase_steps=1,
                    )
                )
                req = discovery.AnswerQueryRequest(
                    serving_config=serving_config,
                    query=discovery.Query(text=q_part_for_api),
                    answer_generation_spec=answer_generation_spec,
                    search_spec=search_spec,
                    query_understanding_spec=query_understanding_spec,
                    user_pseudo_id=user_pseudo_id,
                )
                if related_questions_enabled and use_followup_session:
                    req.related_questions_spec = discovery.AnswerQueryRequest.RelatedQuestionsSpec(enable=True)
                if session_name and use_followup_session:
                    req.session = session_name

                response = client.answer_query(request=req)
                fallback_results = []
                answer_obj = getattr(response, "answer", None)
                references = list(getattr(answer_obj, "references", None) or []) if answer_obj else []

                if not references and search_client is not None:
                    try:
                        fallback_query_variants = _build_fallback_search_queries(q_part_for_api)
                        for fallback_query in fallback_query_variants:
                            search_request = discovery.SearchRequest(
                                serving_config=serving_config,
                                query=fallback_query,
                                page_size=_vertex_results_cap,
                                user_pseudo_id=user_pseudo_id,
                            )
                            if acl_user_id:
                                search_request.user_info = discovery.UserInfo(user_id=acl_user_id)
                            search_response = search_client.search(request=search_request)
                            fallback_results = _collect_discovery_search_hits(
                                search_response, _vertex_results_cap
                            )
                            logger.debug(
                                "Fallback search variant '%s' returned %s document hits (capped at %s)",
                                fallback_query[:120],
                                len(fallback_results),
                                _vertex_results_cap,
                            )
                            if fallback_results:
                                break
                        if not fallback_results:
                            keyword_probes = _build_keyword_probe_queries(q_part_for_api)
                            for probe_query in keyword_probes:
                                search_request = discovery.SearchRequest(
                                    serving_config=serving_config,
                                    query=probe_query,
                                    page_size=_vertex_results_cap,
                                    user_pseudo_id=user_pseudo_id,
                                )
                                if acl_user_id:
                                    search_request.user_info = discovery.UserInfo(user_id=acl_user_id)
                                search_response = search_client.search(request=search_request)
                                fallback_results = _collect_discovery_search_hits(
                                    search_response, _vertex_results_cap
                                )
                                logger.debug(
                                    "Keyword probe fallback '%s' returned %s document hits (capped at %s)",
                                    probe_query,
                                    len(fallback_results),
                                    _vertex_results_cap,
                                )
                                if fallback_results:
                                    break
                    except Exception as search_exc:
                        logger.warning(
                            "Fallback search failed for query part '%s': %s",
                            q_part[:80],
                            search_exc,
                        )
                
                # Log what we got back
                summary_text = getattr(answer_obj, 'answer_text', None) if answer_obj else None
                related_questions = list(getattr(answer_obj, 'related_questions', None) or []) if answer_obj else []
                logger.debug(
                    "Discovery answer response for query part: has_answer=%s, summary_length=%s, references_count=%s, fallback_search_count=%s, related_questions=%s",
                    bool(answer_obj),
                    len(summary_text) if summary_text else 0,
                    len(references),
                    len(fallback_results),
                    len(related_questions),
                )
                
                return q_part, response, fallback_results
            except Exception as exc:
                logger.error(f"Answer query for part '{q_part[:80]}' failed: {exc}", exc_info=True)
                return q_part, None, []

        def _parse_query_evidence_tokens(q_part, req_code=None, requires_onsite=False):
            return _derive_evidence_tokens_from_query_part(
                q_part, requires_onsite=requires_onsite, payload_raw=""
            )

        def _extract_note_for_requirement(q_part, req_code=None):
            match = re.search(r'\[Note:\s*([^\]]+)\]', q_part or '')
            return match.group(1).strip() if match and match.group(1) else ''

        def _extract_onsite_note_for_requirement(q_part, req_code=None):
            note = str(payload.get("onsite_note") or "").strip()
            if note:
                return note
            if isinstance(payload.get("onsite_note"), dict) and req_code:
                note = str(payload["onsite_note"].get(req_code) or "").strip()
                if note:
                    return note
            match = re.search(r'\[Onsite Note:\s*([^\]]+)\]', q_part or '')
            return match.group(1).strip() if match and match.group(1) else ''

        def _append_onsite_verification(summary_text_value, onsite_note_value):
            clean_summary = str(summary_text_value or '').strip()
            clean_note = str(onsite_note_value or '').strip()
            if not clean_note:
                return clean_summary

            onsite_line = f"[Onsite Verification]: {clean_note}"
            if onsite_line.lower() in clean_summary.lower():
                return clean_summary
            if re.search(r'\[Onsite Verification\]\s*:', clean_summary, flags=re.IGNORECASE):
                return clean_summary
            if not clean_summary:
                return onsite_line
            return f"{clean_summary}\n\n{onsite_line}"

        req_query_map = {}
        req_evidence_tokens = {}
        req_requires_onsite = {}
        for q_part in query_parts[:10]:
            req_match = re.match(r'^([A-Z0-9\.]+):', q_part)
            req_code = req_match.group(1) if req_match else q_part
            req_query_map[q_part] = req_code
            part_requires_onsite = _text_requires_onsite_verification(
                _query_core_text_without_tags(q_part)
            )
            if not part_requires_onsite and bool(payload.get("requires_onsite_verification")):
                part_requires_onsite = True
            req_requires_onsite[req_code] = part_requires_onsite
            req_evidence_tokens[req_code] = _parse_query_evidence_tokens(
                q_part, req_code, requires_onsite=part_requires_onsite
            )

        # Execute Vertex searches in parallel
        with ThreadPoolExecutor(max_workers=5) as executor:
            search_results = list(executor.map(perform_search, query_parts[:10]))

        all_results = []
        all_summaries = []
        all_citations = []
        all_related_questions = []
        resolved_session_name = session_name or None
        evidence_process_cap = _effective_evidence_process_cap(max_evidence_param, search_only)

        for q_part, resp, fallback_search_hits in search_results:
            # Create a fresh seen_ids set for EACH query part to avoid filtering duplicates across requirements
            # Each requirement should show all its results, even if some docs appear in other requirements
            seen_ids = set()
            if not resp:
                continue

            response_session = getattr(resp, "session", None)
            if response_session and getattr(response_session, "name", None):
                resolved_session_name = response_session.name

            # Extract requirement code from q_part (format: "CODE: requirement text...")
            req_code = req_query_map.get(q_part)

            q_part_evidence_tokens = req_evidence_tokens.get(req_code) or _parse_query_evidence_tokens(
                q_part, req_code, requires_onsite=req_requires_onsite.get(req_code, False)
            )
            q_part_onsite_note = _extract_onsite_note_for_requirement(q_part, req_code)
            include_onsite_in_summary = (
                req_requires_onsite.get(req_code)
                and q_part_evidence_tokens != {"onsite"}
                and bool(q_part_onsite_note)
            )
            

            # Extract summary
            try:
                answer_obj = getattr(resp, "answer", None)
                if answer_obj:
                    stext = getattr(answer_obj, "answer_text", None) or _build_answer_skip_summary(answer_obj)
                    refs_for_summary = list(getattr(answer_obj, "references", None) or [])
                    if not search_only and (
                        (not (stext or "").strip()) or _is_vertex_placeholder_summary_text(stext)
                    ):
                        syn = ""
                        if refs_for_summary:
                            syn = _synthesize_compliance_summary_from_vertex_references(
                                answer_obj, req_code, max_evidence_param
                            )
                        # Vertex often returns placeholder answer_text while documents only exist on
                        # Search API fallback hits (empty answer.references).
                        if not syn and fallback_search_hits:
                            syn = _synthesize_compliance_summary_from_search_fallback_hits(
                                fallback_search_hits, req_code, max_evidence_param
                            )
                        if syn:
                            stext = syn
                    if stext:
                        stext = _strip_retrieved_docs_evidence_preamble_paragraph(stext)
                        if include_onsite_in_summary:
                            stext = _append_onsite_verification(stext, q_part_onsite_note)
                        
                        # Do not use summarize_evidence() here (it strips [1]/[2]). Enforce max length with a
                        # citation-preserving compress when Discovery ignores the word-limit preamble.
                        if not search_only and stext:
                            stext = _shorten_compliance_summary_preserving_citations(
                                stext, _COMPLIANCE_SUMMARY_MAX_WORDS
                            )
                            stext = _strip_retrieved_docs_evidence_preamble_paragraph(stext)

                        # Determine what will be appended to all_summaries
                        # Use ONLY the requirement code (e.g., "GEN.77700"), not the full q_part definition
                        summary_header = f"**{req_code}**" if req_code else f"**{q_part.split(':')[0]}**"
                        to_append = f"{summary_header}: {stext}" if not search_only else stext
                        
                        
                        all_summaries.append(to_append)
                        logger.debug(f"Appended summary for requirement {req_code}; total summaries now: {len(all_summaries)}")
                    else:
                        logger.debug(f"No answer_text found for query part '{q_part[:40]}'")

                    related_questions = list(getattr(answer_obj, "related_questions", None) or [])
                    if related_questions:
                        for related_question in related_questions:
                            text = str(related_question or "").strip()
                            if text and text not in all_related_questions:
                                all_related_questions.append(text)
            except Exception as sum_err:
                logger.exception(f"Failed to extract summary for '{q_part}': {sum_err}")
            # Extract documents per requirement; cap at top N when running AI Compliance (not Ask AI).
            count = 0
            answer_obj = getattr(resp, "answer", None)
            has_summary = bool(answer_obj and (getattr(answer_obj, "answer_text", None) or getattr(answer_obj, "answer_skipped_reasons", None)))
            
            # Helper to normalize doc names for deduplication
            def _normalize_name(name):
                if not name:
                    return ""
                # Extract filename from path
                name = name.split('/')[-1].split('\\')[-1]
                # Keep extension - user wants filenames to show extensions
                # Normalize: lowercase, remove extra spaces and special chars for core comparison
                normalized = name.lower().strip()
                # Also remove parentheses content like (1), (2) that indicate file copies
                import re
                normalized = re.sub(r'\s*\(\d+\)\s*$', '', normalized)
                # Remove multiple spaces
                normalized = re.sub(r'\s+', ' ', normalized)
                return normalized
            
            try:
                results_list = list(getattr(answer_obj, "references", None) or []) if answer_obj else []
                use_fallback_results = not results_list and bool(fallback_search_hits)
                if use_fallback_results:
                    logger.debug(
                        "Using fallback Search API results for query '%s' because answer.references is empty",
                        q_part[:80],
                    )
                
                logger.debug(f"\n→ Processing requirement query: '{q_part[:100]}'")
                logger.debug(
                    "  Received %s %s from Vertex AI",
                    len(results_list) if not use_fallback_results else len(fallback_search_hits),
                    "references" if not use_fallback_results else "fallback search hits",
                )
                per_query_results = []
                iterable_results = fallback_search_hits if use_fallback_results else results_list
                for idx, reference in enumerate(iterable_results):
                    if evidence_process_cap > 0 and count >= evidence_process_cap:
                        break
                    if use_fallback_results:
                        result = _extract_search_result_item(reference, req_code=req_code if req_code else q_part)
                    else:
                        result = _extract_answer_reference_result(reference, req_code=req_code if req_code else q_part)
                    doc_id = result.get("id")
                    norm_name = _normalize_name(result.get("document_name"))
                    logger.debug(f"    Result [{idx}]: ID={doc_id}")
                    logger.debug(f"             Title: {result.get('title')}")
                    logger.debug(f"             Document Name: {result.get('document_name')}")
                    logger.debug(f"             Normalized: '{norm_name}'")

                    # Check if we've already seen this document by ID
                    # Only deduplicate by ID - allow same-named documents with different IDs
                    # This ensures quarterly/annual versions of same document are all included
                    if doc_id and str(doc_id) in seen_ids:
                        logger.debug(f"             → SKIP: Duplicate ID ({doc_id} already in seen_ids)")
                        continue
                    
                    # Not seen yet - mark as seen and process
                    logger.debug(f"             → INCLUDE: New document, adding to all_results")
                    if doc_id:
                        seen_ids.add(str(doc_id))
                    per_query_results.append(result)
                    all_results.append(result)
                    logger.debug(
                        "             ✓ Added result: document_name='%s', has_link=%s",
                        result.get("document_name"),
                        bool(result.get("link")),
                    )
                    count += 1
                    
                # If we have a summary but got no results, log the mismatch
                if count == 0 and has_summary:
                    stext = getattr(answer_obj, "answer_text", None)
                    if stext:
                        logger.warning(f"Query '{q_part[:80]}' returned AI summary but answer.references is empty/None")
                        logger.debug(f"Summary: {stext[:100]}...")

                query_citations = [] if use_fallback_results else _extract_answer_citations(answer_obj, per_query_results)
                if query_citations:
                    all_citations.extend(query_citations)
                    if count > 0 and len(query_citations) > count:
                        logger.debug(f"Query '{q_part[:80]}' has {len(query_citations)} citations but only {count} documents")
                        
            except Exception as e:
                logger.error(f"Error parsing results for '{q_part}': {e}")

        # Helper function to normalize document names for comparison (SAME as per-query function)
        def _normalize_doc_name(name):
            """Extract and normalize filename for comparison."""
            if not name:
                return ""
            # Extract filename from path if present
            name = name.split('/')[-1].split('\\')[-1]
            # Remove extension for more aggressive dedup
            name_no_ext = name.rsplit('.', 1)[0] if '.' in name else name
            # Normalize: lowercase, remove extra spaces
            normalized = name_no_ext.lower().strip()
            # Remove (1), (2) type copy indicators
            try:
                import re
                normalized = re.sub(r'\s*\(\d+\)\s*$', '', normalized)
                # Remove multiple spaces
                normalized = re.sub(r'\s+', ' ', normalized)
            except Exception:
                pass
            return normalized

        # Log all results
        logger.debug(f"\n=== ALL RESULTS ({len(all_results)} total) ===")
        for idx, result in enumerate(all_results):
            doc_id = result.get("id") or ""
            doc_name = result.get("document_name") or ""
            norm_name_temp = _normalize_doc_name(doc_name)
            logger.debug(f"  [{idx}] ID: {doc_id}, Document Name: {doc_name}")
            logger.debug(f"       Normalized Key: '{norm_name_temp}'")
        
        # NO final deduplication - each requirement gets ALL its results 
        # (per-query dedup already prevents duplicates within each requirement)
        # If a document is relevant to multiple requirements, it correctly appears for each
        final_results = all_results
        
        # Add LIMS records sequentially (same request db_session is not safe across threads).
        if include_lims_data:
            try:
                lims_records = []
                lims_max_records = 2
                if followup_mode and re.search(r"\brecords?\b", query, re.IGNORECASE):
                    lims_max_records = 5
                for qp in query_parts[:10]:
                    lims_records.extend(
                        _fetch_lims_records_for_query(
                            qp,
                            req_code=req_query_map.get(qp),
                            fallback_terms=requirements,
                            max_records=lims_max_records,
                        )
                        or []
                    )
                if lims_records:
                    final_results.extend(lims_records)
                    logger.info(
                        "Added %s LIMS evidence rows across %s requirement queries",
                        len(lims_records),
                        len(query_parts[:10]),
                    )
            except Exception as lims_err:
                logger.error(f"Failed to process LIMS records: {lims_err}", exc_info=True)

        # Synthetic policy/record codes from AI summaries (before gap assessment so counts align).
        if not search_only:
            try:
                _append_summary_cited_policy_records(final_results, all_summaries)
            except Exception as cited_err:
                logger.debug(f"Extracting cited codes from summaries: {cited_err}")

        if not search_only:
            per_req_cap = max_evidence_param if max_evidence_param > 0 else 0
            final_results = _cap_evidence_results_per_requirement(final_results, per_req_cap)

        if not search_only and all_summaries:
            try:
                all_summaries = _append_vertex_excerpts_to_summaries(all_summaries, final_results)
            except Exception as ex_err:
                logger.debug("Append Vertex excerpts to summaries failed: %s", ex_err)

        if not search_only and all_summaries:
            try:
                all_summaries = _prepend_retrieved_docs_to_summaries(all_summaries, final_results)
            except Exception as pre_err:
                logger.debug("Prepend retrieved docs to summaries failed: %s", pre_err)

        # Requirement-level gap assessment from merged evidence.
        requirement_assessments = []
        for q_part in query_parts[:10]:
            req_code = req_query_map.get(q_part)
            evidence_items = [
                item for item in final_results
                if str(item.get("requirement_query") or "").strip() == str(req_code).strip()
            ]
            if req_code:
                note_for_req = _extract_note_for_requirement(q_part, req_code)
                sum_for_req = _extract_requirement_summary_text(all_summaries, req_code)
                requirement_assessments.append(
                    _evaluate_requirement_gaps(
                        req_code,
                        req_evidence_tokens.get(req_code, set()),
                        evidence_items,
                        req_summary_text=sum_for_req,
                        note_text=note_for_req,
                    )
                )

        overall_missing = sorted(
            {
                missing
                for assessment in requirement_assessments
                for missing in (assessment.get("missing_evidence") or [])
            }
        )
        
        logger.debug(f"\n=== RESULTS READY: {len(final_results)} total documents ===")
        
        summary_text = "\n\n".join(all_summaries) if all_summaries else None
        
        # Infer status from AI summary text
        inferred_status = None
        if summary_text and not search_only:
            summary_lower = summary_text.lower()
            # Check for explicit status keywords in summary
            if "[compliant]" in summary_lower or "compliant:" in summary_lower:
                inferred_status = "COMPLIANT"
            elif "[non-compliant]" in summary_lower or "non-compliant:" in summary_lower:
                inferred_status = "NON-COMPLIANT"
            elif "[partial]" in summary_lower or "[warning]" in summary_lower or "warning:" in summary_lower:
                inferred_status = "PARTIAL"
            elif "[not applicable]" in summary_lower or _summary_indicates_not_applicable(summary_text, None):
                inferred_status = "NOT APPLICABLE"
            # Check for indicators of missing evidence or inability to determine
            elif any(phrase in summary_lower for phrase in [
                "cannot be completed",
                "cannot determine",
                "not explicitly",
                "not found",
                "no evidence",
                "does not state",
                "unable to determine",
                "missing information",
                "cannot assess",
                "insufficient information"
            ]):
                inferred_status = "NON-COMPLIANT"
                logger.debug(f"Inferred NON-COMPLIANT status due to missing evidence/information")
            # If results are empty or minimal, likely non-compliant
            elif not final_results or len(final_results) == 0:
                if any(phrase in summary_lower for phrase in ["no", "none", "missing", "not found"]):
                    inferred_status = "NOT CHECKED"
            elif (
                "could not be generated" in summary_lower
                or "summary could not" in summary_lower
                or "[not checked]" in summary_lower
            ):
                inferred_status = "NOT CHECKED"

        if requirement_assessments:
            all_na = all((a.get("status") or "") == "NOT APPLICABLE" for a in requirement_assessments)
            _stats = [str(a.get("status") or "") for a in requirement_assessments]
            if _stats and all(s == "NOT APPLICABLE" for s in _stats):
                inferred_status = "NOT APPLICABLE"
            elif _stats and all(s == "NOT CHECKED" for s in _stats):
                inferred_status = "NOT CHECKED"
            elif _stats and all(s == "COMPLIANT" for s in _stats):
                inferred_status = "COMPLIANT"
            elif any(s in ("PARTIAL", "WARNING") for s in _stats):
                inferred_status = "PARTIAL"
            elif any(s == "NOT CHECKED" for s in _stats):
                inferred_status = "NOT CHECKED"
            elif inferred_status is None and not all_na and _stats and len(_stats) == 1:
                inferred_status = _stats[0]
            elif inferred_status is None and not all_na:
                inferred_status = "COMPLIANT"
        
        return jsonify({
            "success": True, 
            "results": final_results, 
            "summary": summary_text, 
            "citations": all_citations, 
            "result_count": len(final_results),
            "inferred_status": inferred_status,  # Add inferred status to response
            "session": resolved_session_name,
            "related_questions": all_related_questions,
            "requirement_assessments": requirement_assessments,
            "missing_evidence_categories": overall_missing,
            "max_ai_search_evidence": max_evidence_param if max_evidence_param > 0 else None,
        })

    except Exception as e:
        logger.exception(f"cap_ai_search failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@ai_engine.route("/cap-gap-assessment", methods=["POST"])
@login_required
def cap_gap_assessment():
    """Generate CAP workbook gap analysis and corrective actions for one requirement."""
    try:
        payload = request.get_json(silent=True) or {}
        req_id = str(
            payload.get("requirement_id")
            or payload.get("check")
            or payload.get("checklist_item")
            or ""
        ).strip()
        if not req_id:
            return jsonify({"success": False, "error": "requirement_id is required"}), 400

        ai_model = (payload.get("ai_model") or "").strip() or None
        findings = payload.get("findings") or []
        if isinstance(findings, str):
            findings = [findings]
        missing = payload.get("missing_evidence") or []
        if isinstance(missing, str):
            missing = [missing]

        requirement_ctx = {
            "requirement_id": req_id,
            "subject": str(payload.get("subject") or "").strip(),
            "requirement": str(payload.get("requirement") or "").strip(),
            "note": str(payload.get("note") or payload.get("cap_note") or "").strip(),
            "policy_procedure": str(payload.get("policy_procedure") or "").strip(),
            "evidence_of_compliance": str(payload.get("evidence_of_compliance") or "").strip(),
            "findings": [str(item).strip() for item in findings if str(item).strip()],
            "missing_evidence": list(missing) if isinstance(missing, (list, tuple)) else [],
            "evidence_items": payload.get("evidence_items") or payload.get("results") or [],
            "ai_evidence_summary": str(
                payload.get("ai_evidence_summary") or payload.get("ai_search_summary") or ""
            ).strip(),
            "action_completion": _coalesce_remediation_value(payload),
            "remediation": _coalesce_remediation_value(payload),
            "onsite_note": str(payload.get("onsite_note") or "").strip(),
            "corrective_actions": payload.get("corrective_actions") or [],
        }

        assessment = ai_service.generate_cap_requirement_gap_assessment(
            requirement_ctx, model=ai_model
        )
        return jsonify({"success": True, "assessment": assessment, "requirement_id": req_id})
    except Exception as e:
        logger.exception(f"cap_gap_assessment failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@ai_engine.route("/gap-analysis", methods=["POST"])
@login_required
def generate_ai_gap_analysis():
    """Generate AI gap analysis for non-compliant and warning checks."""
    try:
        payload = request.get_json(silent=True) or {}
        raw_checks = payload.get("checks") or []
        ai_model = (payload.get("ai_model") or "").strip() or None

        if not isinstance(raw_checks, list):
            return jsonify({"success": False, "error": "Checks payload must be a list"}), 400

        normalized_checks = []
        for idx, raw_check in enumerate(raw_checks):
            if not isinstance(raw_check, dict):
                continue

            check_code = str(
                raw_check.get("check")
                or raw_check.get("checklist_item")
                or raw_check.get("code")
                or f"Check {idx + 1}"
            ).strip()
            status = _normalize_gap_analysis_status(raw_check.get("status"))

            findings = raw_check.get("findings") or []
            if isinstance(findings, str):
                findings = [findings]

            cleaned_findings = []
            for item in findings:
                text = str(item or "").strip()
                if text:
                    cleaned_findings.append(text)

            if not cleaned_findings:
                for fallback_key in ("ai_evidence_summary", "details", "requirement"):
                    fallback_text = str(raw_check.get(fallback_key) or "").strip()
                    if fallback_text:
                        cleaned_findings.append(fallback_text)
                        break

            normalized_checks.append(
                {
                    "check": check_code,
                    "requirement": str(raw_check.get("requirement") or "").strip(),
                    "status": status,
                    "findings": cleaned_findings[:3],
                    "ai_evidence_summary": str(raw_check.get("ai_evidence_summary") or "").strip(),
                    "details": str(raw_check.get("details") or "").strip(),
                }
            )

        target_checks = [
            check
            for check in normalized_checks
            if check.get("status") in ("NON_COMPLIANT", "PARTIAL", "WARNING")
        ]

        if not target_checks:
            return jsonify(
                {
                    "success": True,
                    "gap_analysis": {
                        "analysis": "No non-compliant or warning requirements were provided for gap analysis.",
                        "priority_actions": [],
                        "risk_level": "LOW",
                    },
                    "analyzed_checks": 0,
                    "analyzed_check_ids": [],
                }
            )

        gap_analysis = ai_service.analyze_compliance_gaps(target_checks, model=ai_model)
        return jsonify(
            {
                "success": True,
                "gap_analysis": gap_analysis,
                "analyzed_checks": len(target_checks),
                "analyzed_check_ids": [check.get("check") for check in target_checks if check.get("check")],
            }
        )
    except Exception as e:
        logger.exception(f"generate_ai_gap_analysis failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


def _load_cap_compliance_sheet_payload(sheet_name=None):
    """Load a CAP compliance tab and normalize rows to the detected header length."""
    sheet_name = sheet_name or CAP_SHEET_NAME
    sheet_id = os.getenv("CAP_COMPLIANCE_SHEET_ID") or CAP_SHEET_ID
    if not sheet_id:
        raise ValueError("CAP_COMPLIANCE_SHEET_ID not configured")

    rows_data = Google_API.get_values_sheets(
        sheet_id,
        sheet_name,
        dateTimeRenderOption="FORMATTED_STRING",
        spoof=False,
    )
    raw_values = rows_data.get("values", []) if isinstance(rows_data, dict) else []
    if not raw_values:
        return {
            "sheet_id": sheet_id,
            "sheet": sheet_name,
            "headers": [],
            "rows": [],
            "header_row_index": 0,
        }

    header_row_index = 0
    max_cols = 0
    for idx, row in enumerate(raw_values):
        non_empty = sum(1 for cell in row if cell and str(cell).strip())
        if non_empty > max_cols:
            max_cols = non_empty
            header_row_index = idx

    headers = raw_values[header_row_index] if len(raw_values) > header_row_index else []
    data_rows = raw_values[header_row_index + 1 :]
    normalized_rows = [row + [""] * (len(headers) - len(row)) for row in data_rows]

    return {
        "sheet_id": sheet_id,
        "sheet": sheet_name,
        "headers": headers,
        "rows": normalized_rows,
        "header_row_index": header_row_index,
    }


def _is_non_compliant_like_status(cell_value):
    if cell_value is None:
        return False
    status_text = str(cell_value).strip().lower()
    return bool(re.search(r"warn|caution|non[-_ ]?compli|noncompliant|fail", status_text))


def _filter_non_compliant_rows(headers, rows):
    status_idx = next(
        (idx for idx, header in enumerate(headers) if header and "status" in str(header).lower()),
        None,
    )

    if status_idx is not None:
        filtered_rows = []
        for row in rows:
            try:
                if _is_non_compliant_like_status(row[status_idx]):
                    filtered_rows.append(row)
            except Exception:
                continue
        return filtered_rows

    return [row for row in rows if any(_is_non_compliant_like_status(cell) for cell in row)]


def _normalize_report_status(status_value):
    status_text = str(status_value or "").strip().upper().replace("_", "-")
    if "NON" in status_text and "COMPLIANT" in status_text:
        return "NON-COMPLIANT"
    if "COMPLIANT" in status_text:
        return "COMPLIANT"
    if "WARNING" in status_text or "CAUTION" in status_text or "PARTIAL" in status_text:
        return "PARTIAL"
    if "NOT APPLICABLE" in status_text or status_text in {"N/A", "NA"}:
        return "NOT APPLICABLE"
    return status_text or "NOT APPLICABLE"


def _stringify_export_evidence(item):
    if item is None:
        return ""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        name = (
            item.get("document_name")
            or item.get("name")
            or item.get("title")
            or ""
        )
        snippet = (
            item.get("snippet")
            or item.get("excerpt")
            or item.get("summary")
            or item.get("match_reason")
            or ""
        )
        if name and snippet:
            return f"{name}: {snippet}"
        return str(name or snippet or item.get("id") or "")
    return str(item)


_EVIDENCE_LIST_KEYS = (
    "evidence",
    "sop_evidence",
    "policy_evidence",
    "records_evidence",
    "onsite_evidence",
    "ai_search_evidence",
    "source_of_evidence",
)


def _trim_report_for_export(report):
    """Keep only fields required for HTML/PDF export to avoid oversized POST bodies."""
    report = report or {}
    if not isinstance(report, dict):
        return {}

    export = {
        "report_date": report.get("report_date"),
        "total_checks": report.get("total_checks"),
        "compliant": report.get("compliant"),
        "non_compliant": report.get("non_compliant"),
        "partial": report.get("partial"),
        "warnings": report.get("warnings"),
        "compliance_rate": report.get("compliance_rate"),
        "ai_gap_analysis": report.get("ai_gap_analysis"),
        "checks": [],
    }

    check_scalar_keys = (
        "status",
        "status_value",
        "checklist_item",
        "check",
        "name",
        "requirement",
        "requirement_text",
        "details",
        "ai_evidence_summary",
        "ai_search_summary",
    )
    check_list_keys = ("findings", "recommendations", "ai_recommendations", "ai_recs")

    for raw in report.get("checks") or []:
        if not isinstance(raw, dict):
            continue
        check = {key: raw.get(key) for key in check_scalar_keys if raw.get(key) is not None}
        for key in check_list_keys:
            values = raw.get(key)
            if values:
                check[key] = [str(value) for value in values]
        for key in _EVIDENCE_LIST_KEYS:
            values = raw.get(key)
            if values:
                check[key] = [
                    _stringify_export_evidence(value)
                    for value in values
                    if value is not None
                ]
        if not check.get("ai_evidence_summary") and check.get("ai_search_summary"):
            check["ai_evidence_summary"] = check.get("ai_search_summary")
        export["checks"].append(check)

    return export


def _find_saved_report_by_id(report_id, report_type=None):
    report_type = (report_type or "search").strip().lower()
    if report_type in ("gap", "gap_analysis"):
        loaders = [_load_gap_analysis_reports]
    elif report_type == "search":
        loaders = [_load_ai_reports]
    else:
        loaders = [_load_ai_reports, _load_gap_analysis_reports]

    for loader in loaders:
        for entry in _filter_reports_for_current_user(loader()):
            try:
                if int(entry.get("id", -1)) == int(report_id):
                    return _unwrap_saved_report_payload(entry.get("report") or entry)
            except (TypeError, ValueError):
                continue
    return None


def _resolve_report_for_export(data):
    data = data or {}
    report_id = data.get("report_id")
    if report_id is not None:
        report = _find_saved_report_by_id(report_id, data.get("report_type"))
        if report is None:
            raise ValueError(f"Report {report_id} not found")
        return _trim_report_for_export(report)

    payload = data.get("report")
    if payload is None and any(key in data for key in ("checks", "report_date", "total_checks")):
        payload = data
    if payload is None:
        return _trim_report_for_export({})

    return _trim_report_for_export(_unwrap_saved_report_payload(payload))


def _generate_ai_engine_compliance_report_html(report):
    """Generate HTML for AI compliance report export without depending on compliance_review."""
    report = report or {}

    def _normalize_check(raw_check):
        if isinstance(raw_check, dict):
            return {
                "status": _normalize_report_status(raw_check.get("status") or raw_check.get("status_value")),
                "checklist_item": raw_check.get("checklist_item") or raw_check.get("check") or raw_check.get("name") or "",
                "requirement": raw_check.get("requirement") or raw_check.get("requirement_text") or "",
                "details": raw_check.get("details") or "",
                "findings": raw_check.get("findings") or [],
                "evidence": raw_check.get("evidence") or [],
                "sop_evidence": raw_check.get("sop_evidence") or [],
                "policy_evidence": raw_check.get("policy_evidence") or [],
                "records_evidence": raw_check.get("records_evidence") or [],
                "onsite_evidence": raw_check.get("onsite_evidence") or [],
                "source_of_evidence": raw_check.get("source_of_evidence") or [],
                "recommendations": raw_check.get("recommendations") or [],
                "ai_recommendations": raw_check.get("ai_recommendations") or raw_check.get("ai_recs") or [],
                "ai_evidence_summary": raw_check.get("ai_evidence_summary") or raw_check.get("ai_evidence") or None,
            }

        return {
            "status": _normalize_report_status(getattr(raw_check, "status", None)),
            "checklist_item": getattr(raw_check, "checklist_item", "") or getattr(raw_check, "check", ""),
            "requirement": getattr(raw_check, "requirement", ""),
            "details": getattr(raw_check, "details", ""),
            "findings": getattr(raw_check, "findings", []) or [],
            "evidence": getattr(raw_check, "evidence", []) or [],
            "sop_evidence": getattr(raw_check, "sop_evidence", []) or [],
            "policy_evidence": getattr(raw_check, "policy_evidence", []) or [],
            "records_evidence": getattr(raw_check, "records_evidence", []) or [],
            "onsite_evidence": getattr(raw_check, "onsite_evidence", []) or [],
            "source_of_evidence": getattr(raw_check, "source_of_evidence", []) or [],
            "recommendations": getattr(raw_check, "recommendations", []) or [],
            "ai_recommendations": getattr(raw_check, "ai_recommendations", []) or [],
            "ai_evidence_summary": getattr(raw_check, "ai_evidence_summary", None),
        }

    def _status_class(status_value):
        normalized = _normalize_report_status(status_value)
        if normalized == "COMPLIANT":
            return "compliant"
        if normalized == "NON-COMPLIANT":
            return "non-compliant"
        if normalized == "PARTIAL":
            return "warning"
        return ""

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset=\"utf-8\" />
        <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />
        <title>CAP Custom Compliance AI Analysis Report</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; color: #212529; }}
            h1 {{ color: #0d6efd; }}
            .summary {{ background: #f8f9fa; padding: 15px; margin: 20px 0; border-radius: 5px; }}
            .check {{ margin: 20px 0; padding: 15px; border: 1px solid #dee2e6; border-radius: 5px; }}
            .compliant {{ border-left: 4px solid #198754; }}
            .non-compliant {{ border-left: 4px solid #dc3545; }}
            .warning {{ border-left: 4px solid #ffc107; }}
            .ai-section {{ background: #e7f3ff; padding: 10px; margin: 10px 0; border-radius: 3px; }}
            .small-muted {{ color: #6c757d; font-size: 0.9em; }}
            ul {{ margin: 0 0 8px 20px; }}
        </style>
    </head>
    <body>
        <h1>CAP Custom Compliance AI Analysis Report</h1>
        <p class=\"small-muted\">Generated: {escape(str(report.get('report_date') or ''))}</p>
        <div class=\"summary\">
            <h2>Summary</h2>
            <p><strong>Total Checks:</strong> {report.get('total_checks') or 0}</p>
            <p><strong>Compliant:</strong> {report.get('compliant') or 0}</p>
            <p><strong>Non-Compliant:</strong> {report.get('non_compliant') or 0}</p>
            <p><strong>Partial:</strong> {report.get('partial') or report.get('warnings') or 0}</p>
            <p><strong>Compliance Rate:</strong> {float(report.get('compliance_rate') or 0):.1f}%</p>
        </div>
    """

    ai_gap = report.get("ai_gap_analysis") or {}
    if ai_gap:
        gap_summary = str(ai_gap.get("analysis") or ai_gap.get("summary") or "")
        risk_level = str(ai_gap.get("risk_level") or ai_gap.get("risk") or "")
        priority_actions = ai_gap.get("priority_actions") or ai_gap.get("priority") or []
        html += '<div class="check"><h3>AI Gap Analysis</h3>'
        if gap_summary:
            html += f"<p>{escape(gap_summary)}</p>"
        if risk_level:
            html += f"<p><strong>Risk Level:</strong> {escape(risk_level)}</p>"
        if priority_actions:
            html += "<div><strong>Priority Actions:</strong><ul>"
            html += "".join(f"<li>{escape(str(item))}</li>" for item in priority_actions)
            html += "</ul></div>"
        html += "</div>"

    html += "<h3>Detailed Findings</h3>"

    for raw_check in report.get("checks", []):
        check = _normalize_check(raw_check)
        html += f"""
        <div class=\"check {_status_class(check['status'])}\">
            <h3>{escape(str(check['checklist_item']))} - {escape(str(check['requirement']))}</h3>
            <p><strong>Status:</strong> {escape(str(check['status']))}</p>
            <p><strong>Details:</strong> {escape(str(check['details']))}</p>
        """

        list_sections = [
            ("Findings", check["findings"]),
            ("SOP Documents", check["sop_evidence"]),
            ("Policy/Procedure Documents", check["policy_evidence"]),
            ("Records/Supporting Documents", check["records_evidence"]),
            ("Onsite Findings", check["onsite_evidence"]),
            ("Evidence", check["evidence"]),
            ("Recommendations", check["recommendations"]),
        ]

        for title, values in list_sections:
            if values:
                html += f"<div><strong>{escape(title)}:</strong><ul>"
                html += "".join(f"<li>{escape(str(value))}</li>" for value in values)
                html += "</ul></div>"

        if check["source_of_evidence"]:
            html += "<div class=\"small-muted\"><strong>Evidence Sources:</strong> "
            html += escape(", ".join(str(value) for value in check["source_of_evidence"]))
            html += "</div>"

        if check["ai_evidence_summary"]:
            html += f"<div class=\"ai-section\"><strong>AI Search Summary:</strong><p>{escape(str(check['ai_evidence_summary']))}</p></div>"
        if check["ai_recommendations"]:
            html += "<div class=\"ai-section\"><strong>AI Recommendations:</strong><ul>"
            html += "".join(f"<li>{escape(str(value))}</li>" for value in check["ai_recommendations"])
            html += "</ul></div>"

        html += "</div>"

    html += "</body></html>"
    return html


@ai_engine.route("/cap-compliance-sheet", methods=["GET"])
@login_required
def cap_compliance_sheet():
    try:
        payload = _load_cap_compliance_sheet_payload(request.args.get("sheet") or CAP_SHEET_NAME)
        return jsonify(
            {
                "success": True,
                "headers": payload["headers"],
                "rows": payload["rows"],
                "sheet": payload["sheet"],
                "header_row_index": payload["header_row_index"],
            }
        )
    except Exception as e:
        logger.exception(f"Error returning CAP compliance sheet: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@ai_engine.route("/cap-noncompliant-report", methods=["GET"])
@login_required
def cap_noncompliant_report():
    sheet_name = request.args.get("sheet") or CAP_SHEET_NAME
    try:
        payload = _load_cap_compliance_sheet_payload(sheet_name)
        warning_rows = _filter_non_compliant_rows(payload["headers"], payload["rows"])
        return render_template(
            "admin.cap_noncompliant_report.html",
            sheet=sheet_name,
            headers=payload["headers"],
            rows=warning_rows,
            header_row_index=payload["header_row_index"],
            hide_navbar=True,
        )
    except Exception as e:
        logger.exception(f"Error generating non-compliant report: {e}")
        return render_template(
            "admin.cap_noncompliant_report.html",
            error=str(e),
            sheet=sheet_name,
            headers=[],
            rows=[],
            hide_navbar=True,
        )


@ai_engine.route("/cap-noncompliant-report.pdf", methods=["GET"])
@login_required
def cap_noncompliant_report_pdf():
    sheet_name = request.args.get("sheet") or CAP_SHEET_NAME
    try:
        payload = _load_cap_compliance_sheet_payload(sheet_name)
        warning_rows = _filter_non_compliant_rows(payload["headers"], payload["rows"])
        html = render_template(
            "admin.cap_noncompliant_report.html",
            sheet=sheet_name,
            headers=payload["headers"],
            rows=warning_rows,
            hide_navbar=True,
        )

        try:
            from reportlab.lib.pagesizes import letter
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

            buffer = io.BytesIO()
            doc = SimpleDocTemplate(
                buffer,
                pagesize=letter,
                leftMargin=36,
                rightMargin=36,
                topMargin=36,
                bottomMargin=36,
            )
            styles = getSampleStyleSheet()
            story = [
                Paragraph(f"Non-compliant Report - {sheet_name}", styles.get("Title", styles["Normal"])),
                Spacer(1, 12),
            ]

            if not warning_rows:
                story.append(Paragraph("No partial/non-compliant rows found.", styles.get("Normal")))
            else:
                try:
                    id_idx = next(
                        idx
                        for idx, header in enumerate(payload["headers"])
                        if header and "requirement" in str(header).lower() and "id" in str(header).lower()
                    )
                except StopIteration:
                    id_idx = 0

                for row in warning_rows:
                    title_value = row[id_idx] if id_idx < len(row) else ""
                    story.append(Paragraph(str(title_value), styles.get("Heading2", styles["Normal"])))
                    story.append(Spacer(1, 6))
                    for idx, value in enumerate(row):
                        header_label = (
                            payload["headers"][idx]
                            if idx < len(payload["headers"]) and payload["headers"][idx]
                            else f"Column {idx + 1}"
                        )
                        story.append(Paragraph(f"<b>{header_label}:</b> {'' if value is None else str(value)}", styles.get("Normal")))
                    story.append(Spacer(1, 12))

            doc.build(story)
            buffer.seek(0)
            response = make_response(buffer.getvalue())
            response.headers["Content-Type"] = "application/pdf"
            response.headers["Content-Disposition"] = f'attachment; filename="noncompliant-report-{sheet_name}.pdf"'
            return response
        except Exception as e:
            logger.warning(f"ReportLab PDF generation failed: {e}")
            response = make_response(html)
            response.headers["Content-Type"] = "text/html"
            response.headers["Content-Disposition"] = f'attachment; filename="noncompliant-report-{sheet_name}.html"'
            return response
    except Exception as e:
        logger.exception(f"Error generating non-compliant PDF report: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@ai_engine.route("/cap-compliance-sheet-update", methods=["POST"])
@login_required
def cap_compliance_sheet_update():
    try:
        data = request.get_json() or {}
        sheet_name = data.get("sheet") or CAP_SHEET_NAME
        header_row_index = int(data.get("header_row_index", 0))
        updates = data.get("updates", [])

        sheet_id = os.getenv("CAP_COMPLIANCE_SHEET_ID") or CAP_SHEET_ID
        if not sheet_id:
            return jsonify({"success": False, "error": "CAP_COMPLIANCE_SHEET_ID not configured"}), 400
        if not updates:
            return jsonify({"success": True, "updated": 0})

        body_updates = []
        for update in updates:
            try:
                row_idx = int(update.get("row", 0))
                col_idx = int(update.get("col", 0))
                value = update.get("value", "")
                sheet_row = header_row_index + 2 + row_idx
                cell_range = f"{sheet_name}!{get_column_letter(col_idx + 1)}{sheet_row}"
                body_updates.append({"range": cell_range, "values": [[value]]})
            except Exception:
                continue

        if not body_updates:
            return jsonify({"success": False, "error": "No valid updates provided"}), 400

        response = Google_API.batchupdate_values_sheets(
            sheet_id,
            {"valueInputOption": "RAW", "data": body_updates},
            spoof=False,
        )
        return jsonify({"success": True, "updated": len(body_updates), "response": response})
    except Exception as e:
        logger.exception(f"Error updating CAP compliance sheet: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@ai_engine.route("/cap-compliance-update-comment", methods=["POST"])
@login_required
def cap_compliance_update_comment():
    try:
        data = request.get_json() or {}
        row_index = int(data.get("row_index"))
        comment = data.get("comment", "")
        sheet_name = data.get("sheet") or request.args.get("sheet") or CAP_SHEET_NAME

        sheet_id = os.getenv("CAP_COMPLIANCE_SHEET_ID")
        if not sheet_id:
            return jsonify({"success": False, "error": "CAP_COMPLIANCE_SHEET_ID not configured"}), 400

        payload = _load_cap_compliance_sheet_payload(sheet_name)
        headers = list(payload["headers"])
        header_row_index = payload["header_row_index"]

        if "Comments" in headers:
            comments_col_index = headers.index("Comments")
        else:
            comments_col_index = len(headers)
            headers.append("Comments")
            header_range = f"{sheet_name}!A{header_row_index + 1}"
            Google_API.batchupdate_values_sheets(
                sheet_id,
                {
                    "valueInputOption": "RAW",
                    "data": [{"range": header_range, "values": [headers]}],
                },
                spoof=False,
            )

        cell_range = f"{sheet_name}!{get_column_letter(comments_col_index)}{row_index}"
        Google_API.batchupdate_values_sheets(
            sheet_id,
            {
                "valueInputOption": "RAW",
                "data": [{"range": cell_range, "values": [[comment]]}],
            },
            spoof=False,
        )

        logger.info(f"Successfully updated CAP compliance comment at {cell_range}")
        return jsonify({"success": True, "message": "Comment updated successfully"})
    except Exception as e:
        logger.error(f"Error updating CAP compliance comment: {e}", exc_info=True)
        return jsonify({"success": False, "error": f"Error updating comment: {str(e)}"}), 500


@ai_engine.route("/export-report", methods=["POST"])
@login_required
def export_report():
    try:
        report_data = _resolve_report_for_export(request.get_json() or {})
        return jsonify({"html": _generate_ai_engine_compliance_report_html(report_data)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.exception(f"Error exporting report: {e}")
        return jsonify({"error": str(e)}), 500


@ai_engine.route("/export-all-reports-pdf", methods=["POST"])
@login_required
def export_all_reports_pdf():
    try:
        data = request.get_json() or {}
        if data.get("use_server_reports"):
            reports = _filter_reports_for_current_user(_load_ai_reports()) or []
            local_reports = data.get("local_reports") or []
            reports = list(reports) + list(local_reports)
        else:
            reports = data.get("reports") or _filter_reports_for_current_user(_load_ai_reports())

        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            rightMargin=40,
            leftMargin=40,
            topMargin=60,
            bottomMargin=40,
        )
        styles = getSampleStyleSheet()
        normal = styles["Normal"]
        h1 = styles["Heading1"]
        h2 = styles.get("Heading2", styles["Heading1"])

        def _safe_text(value):
            try:
                return str(value)
            except Exception:
                return ""

        def _escape_for_paragraph(value):
            return _safe_text(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        def _status_color(status_text):
            status_text = (status_text or "").lower()
            if "non" in status_text or "non-compliant" in status_text or "non_compliant" in status_text:
                return "#dc3545"
            if "compliant" in status_text and "non" not in status_text:
                return "#198754"
            if "warn" in status_text or "warning" in status_text:
                return "#d39e00"
            return "#6c757d"

        elements = [
            Paragraph("Exported Saved Reports", h1),
            Paragraph(f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}", normal),
            Spacer(1, 12),
        ]

        for idx, report_entry in enumerate(reports):
            title = _safe_text(report_entry.get("name") or report_entry.get("title") or f"Report {idx + 1}")
            elements.append(Paragraph(title, h2))
            elements.append(
                Paragraph(
                    f"Saved by: {_safe_text(report_entry.get('user') or 'unknown')} | Date: {_safe_text(report_entry.get('date') or '')}",
                    normal,
                )
            )
            elements.append(Spacer(1, 6))

            payload = _trim_report_for_export(
                _unwrap_saved_report_payload(report_entry.get("report") or report_entry)
            )
            try:
                elements.append(Paragraph("<strong>Summary</strong>", normal))
                elements.append(Paragraph(f"Total Checks: {_safe_text(payload.get('total_checks') or payload.get('totalChecks') or 0)}", normal))
                elements.append(Paragraph(f"Compliant: {_safe_text(payload.get('compliant') or 0)}", normal))
                elements.append(Paragraph(f"Non-Compliant: {_safe_text(payload.get('non_compliant') or 0)}", normal))
                elements.append(Paragraph(f"Partial: {_safe_text(payload.get('partial') or payload.get('warnings') or 0)}", normal))
                elements.append(Paragraph(f"Compliance Rate: {_safe_text(payload.get('compliance_rate') or payload.get('complianceRate') or 0)}%", normal))
            except Exception:
                pass

            elements.append(Spacer(1, 8))

            for check in payload.get("checks") or []:
                try:
                    checklist_item = check.get("checklist_item") or check.get("check") or check.get("name") or ""
                    requirement = check.get("requirement") or ""
                    status = check.get("status") or check.get("status_value") or ""
                    elements.append(
                        Paragraph(
                            f"<font color='#0d6efd'><strong>{_escape_for_paragraph(checklist_item)}</strong></font> - {_escape_for_paragraph(requirement)}",
                            normal,
                        )
                    )
                    elements.append(
                        Paragraph(
                            f"Status: <font color='{_status_color(status)}'><strong>{_escape_for_paragraph(status)}</strong></font>",
                            normal,
                        )
                    )
                    if check.get("details"):
                        elements.append(Paragraph(f"Details: {_safe_text(check.get('details'))}", normal))
                    if check.get("findings"):
                        elements.append(Paragraph("Findings:", normal))
                        for finding in check.get("findings"):
                            elements.append(Paragraph(f"- {_safe_text(finding)}", normal))
                    if check.get("evidence"):
                        elements.append(Paragraph("Evidence:", normal))
                        for evidence in check.get("evidence"):
                            elements.append(Paragraph(f"- {_safe_text(evidence)}", normal))
                    if check.get("ai_evidence_summary"):
                        elements.append(Paragraph("AI Search Summary:", normal))
                        elements.append(Paragraph(_escape_for_paragraph(check.get("ai_evidence_summary")), normal))
                    if check.get("ai_recommendations"):
                        elements.append(Paragraph("AI Recommendations:", normal))
                        for recommendation in check.get("ai_recommendations"):
                            elements.append(Paragraph(f"- {_safe_text(recommendation)}", normal))
                    elements.append(Spacer(1, 6))
                except Exception:
                    continue

            if idx < len(reports) - 1:
                elements.append(PageBreak())

        try:
            doc.build(elements)
            buffer.seek(0)
            return send_file(
                buffer,
                as_attachment=True,
                download_name="saved_reports_export.pdf",
                mimetype="application/pdf",
            )
        except Exception as build_error:
            logger.exception("Platypus PDF build failed, attempting canvas fallback: %s", build_error)
            from reportlab.pdfgen import canvas
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont

            fallback_buffer = io.BytesIO()
            pdf_canvas = canvas.Canvas(fallback_buffer, pagesize=letter)
            dejavu_paths = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/local/share/fonts/DejaVuSans.ttf",
            ]
            registered = False
            for font_path in dejavu_paths:
                try:
                    if os.path.exists(font_path):
                        pdfmetrics.registerFont(TTFont("DejaVuSans", font_path))
                        pdf_canvas.setFont("DejaVuSans", 10)
                        registered = True
                        break
                except Exception:
                    continue
            if not registered:
                pdf_canvas.setFont("Helvetica", 10)

            y = 750
            left = 40
            line_height = 12
            pdf_canvas.drawString(left, y, "Exported Saved Reports")
            y -= line_height * 2
            pdf_canvas.drawString(left, y, f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
            y -= line_height * 2

            for idx, report_entry in enumerate(reports):
                if y < 80:
                    pdf_canvas.showPage()
                    y = 750
                    pdf_canvas.setFont("DejaVuSans" if registered else "Helvetica", 10)

                title = _safe_text(report_entry.get("name") or report_entry.get("title") or f"Report {idx + 1}")
                pdf_canvas.drawString(left, y, title)
                y -= line_height
                meta = f"Saved by: {_safe_text(report_entry.get('user') or 'unknown')} | Date: {_safe_text(report_entry.get('date') or '')}"
                pdf_canvas.drawString(left, y, meta)
                y -= line_height

                payload = report_entry.get("report") or report_entry
                try:
                    pdf_canvas.drawString(left, y, f"Total Checks: {_safe_text(payload.get('total_checks') or payload.get('totalChecks') or 0)}")
                    y -= line_height
                    pdf_canvas.drawString(left, y, f"Compliant: {_safe_text(payload.get('compliant') or 0)}")
                    y -= line_height
                    pdf_canvas.drawString(left, y, f"Non-Compliant: {_safe_text(payload.get('non_compliant') or 0)}")
                    y -= line_height
                    pdf_canvas.drawString(left, y, f"Partial: {_safe_text(payload.get('partial') or payload.get('warnings') or 0)}")
                    y -= line_height * 1.5
                except Exception:
                    y -= line_height

                for check in payload.get("checks") or []:
                    try:
                        line = f"{_safe_text(check.get('checklist_item') or check.get('check') or check.get('name') or '')} - {_safe_text(check.get('requirement') or '')} (Status: {_safe_text(check.get('status') or check.get('status_value') or '')})"
                        if len(line) > 200:
                            line = line[:197] + "..."
                        pdf_canvas.drawString(left, y, line)
                        y -= line_height
                        if y < 80:
                            pdf_canvas.showPage()
                            y = 750
                            pdf_canvas.setFont("DejaVuSans" if registered else "Helvetica", 10)
                    except Exception:
                        continue

                y -= line_height
                if idx < len(reports) - 1:
                    pdf_canvas.showPage()
                    y = 750

            pdf_canvas.save()
            fallback_buffer.seek(0)
            return send_file(
                fallback_buffer,
                as_attachment=True,
                download_name="saved_reports_export.pdf",
                mimetype="application/pdf",
            )
    except Exception as e:
        logger.exception("Failed to export reports to PDF: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500
