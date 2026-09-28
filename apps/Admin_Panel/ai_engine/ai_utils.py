"""
AI Engine Utilities Module

AI-specific configuration, file storage, and document processing functions.
This module is INDEPENDENT from compliance_review module.
All AI engine functions are imported here from ai_engine.py.

Dependencies:
- apps.shared_utils: Generic utility functions
- Google_API: For sheet/drive integration (external)
- ai_service: For AI model access (external)
"""

import os
import json
import logging
from apps.Admin_Panel.shared_utils import get_module_root, get_val_fuzzy, ensure_directory_exists

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# === AI ENGINE CONFIGURATION ===
# CAP Checklist configuration - using Google Sheets document ID from environment
CAP_SHEET_ID = os.getenv("CAP_SHEET_ID", "")
if not CAP_SHEET_ID:
    logger.warning("CAP_SHEET_ID environment variable not set")

CAP_FILE_NAME = "CUS_GEN_XYZ"
CAP_SHEET_NAME = os.getenv("CAP_SHEET_NAME", "GEN")
MODULE_ROOT = get_module_root()

# AI-specific file storage paths
AI_SEARCH_REPORTS_FILE = os.getenv(
    "AI_SEARCH_REPORTS_FILE", os.path.join(MODULE_ROOT, "data", "ai_search_reports.json")
)
AI_GAP_ANALYSIS_REPORTS_FILE = os.getenv(
    "AI_GAP_ANALYSIS_REPORTS_FILE",
    os.path.join(MODULE_ROOT, "data", "ai_gap_analysis.json"),
)

# GCS path to inspection sheet link mapping (persistent across requests)
GCS_PATH_MAPPING_FILE = os.path.join(MODULE_ROOT, "data", "gcs_path_link_mapping.json")


# === AI ENGINE FILE STORAGE FUNCTIONS ===
# Demo sessions must never read or write the real report files.

def _demo_active():
    from apps.demo import is_demo_session

    return is_demo_session()


def _ensure_ai_reports_store():
    """Ensure the AI reports storage directory exists."""
    ensure_directory_exists(AI_SEARCH_REPORTS_FILE)


def _load_ai_reports():
    """Load AI search reports from persistent storage.
    
    Returns:
        list: List of AI search reports, or empty list if file doesn't exist or on error
    """
    if _demo_active():
        from apps.demo.sample_data import search_reports

        return search_reports()
    _ensure_ai_reports_store()
    try:
        if not os.path.exists(AI_SEARCH_REPORTS_FILE):
            return []
        with open(AI_SEARCH_REPORTS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh) or []
    except Exception as e:
        logger.debug(f"Failed to load AI search reports: {e}")
        return []


def _save_ai_reports(reports):
    """Save AI search reports to persistent storage.
    
    Args:
        reports: List of AI search reports to save
    
    Returns:
        bool: True if successful, False otherwise
    """
    if _demo_active():
        return False
    _ensure_ai_reports_store()
    try:
        with open(AI_SEARCH_REPORTS_FILE, "w", encoding="utf-8") as fh:
            json.dump(reports, fh, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"Failed to save AI search reports: {e}", exc_info=True)
        return False

def _ensure_gap_analysis_reports_store():
    """Ensure the gap analysis reports storage directory exists."""
    ensure_directory_exists(AI_GAP_ANALYSIS_REPORTS_FILE)


def _load_gap_analysis_reports():
    """Load AI gap analysis reports from persistent storage."""
    if _demo_active():
        from apps.demo.sample_data import gap_analysis_reports

        return gap_analysis_reports()
    _ensure_gap_analysis_reports_store()
    try:
        if not os.path.exists(AI_GAP_ANALYSIS_REPORTS_FILE):
            return []
        with open(AI_GAP_ANALYSIS_REPORTS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh) or []
    except Exception as e:
        logger.debug(f"Failed to load AI gap analysis reports: {e}")
        return []


def _save_gap_analysis_reports(reports):
    """Save AI gap analysis reports to persistent storage."""
    if _demo_active():
        return False
    _ensure_gap_analysis_reports_store()
    try:
        with open(AI_GAP_ANALYSIS_REPORTS_FILE, "w", encoding="utf-8") as fh:
            json.dump(reports, fh, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"Failed to save AI gap analysis reports: {e}", exc_info=True)
        return False


def _load_gcs_path_mapping():
    """Load GCS path to inspection sheet link mapping.
    
    Returns dict with structure:
    {
        "CODE": {
            "gcs_top_folder": link_index,
            ...
        }
    }
    
    Returns:
        dict: Mapping of GCS paths to sheet links
    """
    try:
        if not os.path.exists(GCS_PATH_MAPPING_FILE):
            return {}
        with open(GCS_PATH_MAPPING_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception as e:
        logger.debug(f"Failed to load GCS path mapping: {e}")
        return {}


def _save_gcs_path_mapping(mapping):
    """Save GCS path to inspection sheet link mapping.
    
    Args:
        mapping: Dictionary of GCS path mappings to save
    
    Returns:
        bool: True if successful, False otherwise
    """
    _ensure_ai_reports_store()
    try:
        with open(GCS_PATH_MAPPING_FILE, "w", encoding="utf-8") as fh:
            json.dump(mapping, fh, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"Failed to save GCS path mapping: {e}", exc_info=True)
        return False


# === AI ENGINE DOCUMENT PROCESSING FUNCTIONS ===

def _extract_gcs_top_level_folder(gcs_path):
    """Extract the top-level folder from a GCS path.
    
    E.g., from "gs://bucket/Inspection Documents/Lab Policies/K-Quality/..."
    returns "lab policies"
    
    Args:
        gcs_path: GCS path string
    
    Returns:
        str: Top-level folder name in lowercase, or None if not found
    """
    if not gcs_path or "/Inspection Documents/" not in gcs_path:
        return None
    
    try:
        # Split at Inspection Documents and take next folder component
        after_insp = gcs_path.split("/Inspection Documents/", 1)[1]
        # Get first folder (top-level)
        folders = after_insp.split("/")
        if folders:
            top_folder = folders[0].strip()
            return top_folder.lower() if top_folder else None
    except Exception as e:
        logger.debug(f"Failed to extract GCS top-level folder: {e}")
    
    return None


def _doc_filename(d):
    """Return a filename for a document-like object.

    Prefer explicit `filename` key; otherwise derive from a URL-like link.
    Do NOT fall back to `document_name` or `title` to avoid storing full names.
    Returns empty string when no clear filename is available.
    
    Args:
        d: Document object (dict or string)
    
    Returns:
        str: Filename extracted from document, or empty string
    """
    try:
        if not d:
            return ""
        # If a dict, try filename or link/url fields
        if isinstance(d, dict):
            fn = d.get("filename") or d.get("file_name")
            if fn and str(fn).strip():
                return str(fn).strip()
            link = d.get("link") or d.get("url") or d.get("document_link") or d.get("document_uri") or d.get("href")
            if link and str(link).strip():
                s = str(link).split("?")[0].split("#")[0]
                s = s.rstrip("/")
                parts = s.split("/")
                last = parts[-1] if parts else s
                return last
            return ""
        # If a plain string, try to treat it as a URL/path and take last segment
        s = str(d).strip()
        if not s:
            return ""
        if s.startswith("http") or "/" in s:
            s2 = s.split("?")[0].split("#")[0]
            s2 = s2.rstrip("/")
            return s2.split("/")[-1]
        return ""
    except Exception:
        return ""


def _normalize_doc_name(name):
    """Normalize document name for matching (same logic as frontend).
    
    Args:
        name: Document name to normalize
    
    Returns:
        str: Normalized document name
    """
    if not name:
        return ""
    # Extract filename from path
    normalized = str(name).split('/')[-1].split('\\')[-1]
    # Keep extension - user requirement for display
    # Lowercase and trim
    normalized = normalized.lower().strip()
    return normalized


def _extract_doc_code(name):
    """Extract a document code (e.g., K1003.8) from a document name.
    
    Looks for pattern like "LETTER_OR_MARK + DIGITS + optional (. + DIGITS)"
    where the code starts the document name.
    Requires at least one digit to avoid matching non-code names like "MP.jpg".
    
    Args:
        name: Document name (e.g., "K1003.8_Annual Laboratory...")
    
    Returns:
        str: Extracted code (e.g., "K1003.8"), or None if not found
    """
    if not name:
        return None
    
    try:
        name_str = str(name).strip()
        # Pattern: LetterOrMark + Digits (required) + optional . + Digits
        # Must have at least one digit to prevent matching non-code names
        import re
        # Match pattern at start: e.g., K1003.8 or K1003 or QSA.05216
        match = re.match(r'^([A-Z]+\d+(?:\.\d+)?)', name_str)
        if match:
            return match.group(1)
    except Exception as e:
        logger.debug(f"Failed to extract doc code from '{name}': {e}")
    
    return None


# Export public API - functions used by ai_engine module
__all__ = [
    'CAP_SHEET_ID',
    'CAP_FILE_NAME',
    'CAP_SHEET_NAME',
    'MODULE_ROOT',
    'AI_SEARCH_REPORTS_FILE',
    'AI_GAP_ANALYSIS_REPORTS_FILE',
    'GCS_PATH_MAPPING_FILE',
    '_load_ai_reports',
    '_save_ai_reports',
    '_load_gap_analysis_reports',
    '_save_gap_analysis_reports',
    '_load_gcs_path_mapping',
    '_save_gcs_path_mapping',
    '_extract_gcs_top_level_folder',
    '_doc_filename',
    '_normalize_doc_name',
    '_extract_doc_code',
]
