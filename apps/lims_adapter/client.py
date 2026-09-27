"""HTTP client for external LIMS evidence APIs."""

from __future__ import annotations

import logging
import os
from typing import List, Optional
from urllib.parse import urljoin

import requests

from apps.lims_adapter.normalize import normalize_lims_payload

logger = logging.getLogger(__name__)


def external_lims_configured() -> bool:
    return bool((os.environ.get("LIMS_API_BASE_URL") or "").strip())


def _auth_headers() -> dict:
    headers = {"Accept": "application/json"}
    token = (os.environ.get("LIMS_API_TOKEN") or "").strip()
    api_key = (os.environ.get("LIMS_API_KEY") or "").strip()
    header_name = (os.environ.get("LIMS_API_AUTH_HEADER") or "").strip()

    if header_name and (token or api_key):
        headers[header_name] = token or api_key
    elif token:
        headers["Authorization"] = f"Bearer {token}"
    elif api_key:
        headers["X-API-Key"] = api_key

    extra = (os.environ.get("LIMS_API_EXTRA_HEADERS") or "").strip()
    # Optional: "Header-Name: value; Other: value2"
    if extra:
        for part in extra.split(";"):
            if ":" not in part:
                continue
            name, value = part.split(":", 1)
            name, value = name.strip(), value.strip()
            if name and value:
                headers[name] = value
    return headers


def fetch_lims_evidence_via_api(
    query_text: str,
    *,
    keywords: Optional[List[str]] = None,
    req_code: Optional[str] = None,
    max_records: int = 3,
    lookback_days: int = 365,
) -> List[dict]:
    """
    Call the configured external LIMS evidence endpoint.

    Contract (default):
      GET|POST {LIMS_API_BASE_URL}{LIMS_API_EVIDENCE_PATH}
      Query/body: q, keywords, req_code, limit, days
      Response JSON: { "records": [ ... ] }  (also accepts results/data/items)

    Each record may be either:
      - already-normalized evidence fields (snippet, document_name, lims_type, ...), or
      - raw test_result / audit_action fields (result, notes, action_type, ...).
    """
    base = (os.environ.get("LIMS_API_BASE_URL") or "").strip().rstrip("/") + "/"
    if not base.strip("/"):
        return []

    path = (os.environ.get("LIMS_API_EVIDENCE_PATH") or "/v1/compliance/evidence").strip()
    if not path.startswith("/"):
        path = "/" + path
    url = urljoin(base, path.lstrip("/"))

    method = (os.environ.get("LIMS_API_METHOD") or "GET").strip().upper()
    timeout = float(os.environ.get("LIMS_API_TIMEOUT", "15") or "15")
    keywords = list(keywords or [])

    params = {
        "q": str(query_text or ""),
        "keywords": ",".join(keywords),
        "req_code": req_code or "",
        "limit": int(max_records),
        "days": int(lookback_days),
    }

    headers = _auth_headers()
    try:
        if method == "POST":
            resp = requests.post(url, json=params, headers=headers, timeout=timeout)
        else:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.warning(
            "External LIMS API request failed for req %s: %s",
            req_code,
            exc,
        )
        return []

    records = normalize_lims_payload(
        payload,
        req_code=req_code,
        keywords=keywords,
        max_records=max_records,
    )
    if records:
        logger.info(
            "External LIMS API returned %s evidence row(s) for req %s",
            len(records),
            req_code,
        )
    return records
