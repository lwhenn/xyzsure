"""
Shared Utilities Module

Generic, independent utility functions used across multiple modules.
This module has NO dependencies on ai_engine, compliance_review, or other app-specific modules.
It only imports standard libraries and Flask basics.
"""

import os
import logging

logger = logging.getLogger(__name__)


def get_module_root():
    """
    Get the project root directory.
    Works from apps/Admin_Panel/* location (3 levels up).
    
    Returns:
        str: Absolute path to project root directory
    """
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def get_val_fuzzy(row, keys):
    """Retrieve value from row dict looking up keys case-insensitively.
    
    Attempts exact match first for performance, then falls back to case-insensitive search.
    This is a generic utility used across multiple modules for CSV/sheet data access.
    
    Args:
        row: Dictionary (e.g. from csv.DictReader or Google Sheets API)
        keys: List of string keys to look for
    
    Returns:
        str: The value if found, empty string otherwise
    """
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
            if val:
                return val
    return ""


def get_column_letter(col_index):
    """Convert zero-based column index to Excel-style letter.
    
    Examples:
        0 -> "A"
        1 -> "B"
        25 -> "Z"
        26 -> "AA"
        27 -> "AB"
    
    Args:
        col_index: Zero-based column index (integer)
    
    Returns:
        str: Excel-style column letter(s)
    """
    result = ""
    while col_index >= 0:
        result = chr(65 + (col_index % 26)) + result
        col_index = col_index // 26 - 1
    return result


def find_header_index(candidates, headers):
    """Find the index of a header matching any of the candidate names.
    
    Performs case-insensitive matching of header names to find the appropriate column.
    Used when parsing CSV/sheet data with variable header names.
    
    Args:
        candidates: List of potential header names to look for
        headers: List of actual headers in the data
    
    Returns:
        int: Zero-based index of the header, or -1 if not found
    """
    if not headers:
        return -1
    
    # Normalize candidates for comparison
    norm_candidates = [str(c).lower().strip() for c in candidates]
    
    # Normalize and compare headers
    for i, h in enumerate(headers):
        norm_h = str(h).lower().strip()
        if norm_h in norm_candidates:
            return i
    
    return -1


def ensure_directory_exists(filepath):
    """Create directory for a filepath if it doesn't exist.
    
    Safe to call even if the directory already exists.
    
    Args:
        filepath: Path to file whose parent directory should be created
    
    Returns:
        bool: True if successful, False if error occurred
    """
    try:
        d = os.path.dirname(filepath)
        if d:
            os.makedirs(d, exist_ok=True)
        return True
    except Exception as e:
        logger.debug(f"Failed to ensure directory exists for {filepath}: {e}")
        return False
