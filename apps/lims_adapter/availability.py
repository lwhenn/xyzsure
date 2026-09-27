"""Probe external LIMS API and local ORM availability (cached at startup)."""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cached: Optional[Dict[str, Any]] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_local_orm() -> Dict[str, Any]:
    """Return whether local LIMS ORM models/tables are usable."""
    result: Dict[str, Any] = {
        "available": False,
        "detail": "not checked",
    }
    try:
        from database import db_session, engine
        from models.sample_control import Test_Result  # noqa: F401
        from sqlalchemy import text
    except Exception as exc:
        result["detail"] = f"models unavailable: {exc}"
        logger.info("Local LIMS ORM unavailable: %s", exc)
        return result

    action_ok = False
    try:
        from models.general import Action  # noqa: F401

        action_ok = True
    except Exception:
        action_ok = False

    try:
        # Confirm the primary evidence table is reachable (empty result is fine).
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM sample_control.test_result LIMIT 0"))
        result["available"] = True
        result["detail"] = "sample_control.test_result reachable" + (
            "; Action model present" if action_ok else "; Action model missing"
        )
        logger.info("Local LIMS ORM available: %s", result["detail"])
    except Exception as exc:
        result["detail"] = f"table check failed: {exc}"
        logger.info("Local LIMS ORM unavailable: %s", exc)
        try:
            db_session.rollback()
        except Exception:
            pass
    return result


def check_external_lims_api() -> Dict[str, Any]:
    """Return whether the configured external LIMS evidence API responds."""
    result: Dict[str, Any] = {
        "available": False,
        "configured": False,
        "detail": "LIMS_API_BASE_URL not set",
    }
    base = (os.environ.get("LIMS_API_BASE_URL") or "").strip().rstrip("/") + "/"
    if not base.strip("/"):
        return result

    result["configured"] = True
    health_path = (os.environ.get("LIMS_API_HEALTH_PATH") or "").strip()
    if not health_path:
        health_path = (
            os.environ.get("LIMS_API_EVIDENCE_PATH") or "/v1/compliance/evidence"
        ).strip()
    if not health_path.startswith("/"):
        health_path = "/" + health_path

    url = urljoin(base, health_path.lstrip("/"))
    method = (os.environ.get("LIMS_API_METHOD") or "GET").strip().upper()
    timeout = float(os.environ.get("LIMS_API_HEALTH_TIMEOUT", "5") or "5")

    from apps.lims_adapter.client import _auth_headers

    headers = _auth_headers()
    params = {
        "q": "__health__",
        "keywords": "",
        "req_code": "",
        "limit": 0,
        "days": 1,
    }

    try:
        if method == "POST":
            resp = requests.post(url, json=params, headers=headers, timeout=timeout)
        else:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)

        if 200 <= resp.status_code < 300:
            result["available"] = True
            result["detail"] = f"HTTP {resp.status_code} from {url}"
            logger.info("External LIMS API available: %s", result["detail"])
        else:
            result["detail"] = f"HTTP {resp.status_code} from {url}"
            logger.info("External LIMS API unavailable: %s", result["detail"])
    except Exception as exc:
        result["detail"] = str(exc)
        logger.info("External LIMS API unavailable: %s", exc)

    return result


def probe_lims_sources() -> Dict[str, Any]:
    """Check both sources and return a summary used by UI + fetch paths."""
    api = check_external_lims_api()
    orm = check_local_orm()
    available = bool(api.get("available") or orm.get("available"))

    parts = []
    if api.get("available"):
        parts.append("external API")
    elif api.get("configured"):
        parts.append("external API configured but unreachable")
    if orm.get("available"):
        parts.append("local ORM")
    else:
        parts.append("local ORM unavailable")

    if available:
        sources = []
        if api.get("available"):
            sources.append("external LIMS API")
        if orm.get("available"):
            sources.append("local LIMS database")
        hint = "Uses " + " and ".join(sources) + "."
    else:
        hint = "No LIMS source available - checkbox disabled. " + "; ".join(parts) + "."

    summary = {
        "available": available,
        "api": api,
        "orm": orm,
        "hint": hint,
        "checked_at": _now_iso(),
    }
    logger.info(
        "LIMS availability: available=%s api=%s orm=%s",
        available,
        api.get("available"),
        orm.get("available"),
    )
    return summary


def refresh_lims_availability() -> Dict[str, Any]:
    """Run probes and update the process-wide cache (call at app startup)."""
    global _cached
    summary = probe_lims_sources()
    with _lock:
        _cached = summary
    return summary


def get_lims_availability(*, refresh: bool = False) -> Dict[str, Any]:
    """Return cached availability; probe once on first use if startup skipped."""
    global _cached
    if refresh or _cached is None:
        return refresh_lims_availability()
    with _lock:
        return dict(_cached)


def lims_data_available() -> bool:
    return bool(get_lims_availability().get("available"))
