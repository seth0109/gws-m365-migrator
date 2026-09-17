from __future__ import annotations

import re

# SharePoint/OneDrive limits and forbidden characters
_MAX_PATH_LEN = 400
_ILLEGAL_CHARS = re.compile(r'[~#%&*{}\\:<>?/|"]+')
_LEADING_TRAILING_SPACES = re.compile(r"^ +| +$", re.MULTILINE)
_CONSECUTIVE_DOTS = re.compile(r"\.{2,}")

# Windows reserved names (SharePoint rejects COM0/LPT0 too, unlike Windows)
_RESERVED_NAMES = frozenset([
    "CON", "PRN", "AUX", "NUL",
    *[f"COM{i}" for i in range(10)],
    *[f"LPT{i}" for i in range(10)],
])

# Whole names OneDrive/SharePoint rejects outright (case-insensitive).
_FORBIDDEN_NAMES = frozenset({"desktop.ini"})

_SEGMENT_MAX = 128  # safe max per path segment


def sanitize_segment(name: str) -> str:
    """Clean a single path segment (file or folder name)."""
    name = _ILLEGAL_CHARS.sub("_", name)
    name = _LEADING_TRAILING_SPACES.sub("", name)
    name = _CONSECUTIVE_DOTS.sub(".", name)
    name = name.strip(".")
    # "_vti_" is rejected anywhere in a OneDrive/SharePoint name.
    name = name.replace("_vti_", "_vti-")
    if not name:
        name = "_unnamed"
    # rpartition on a dotless name puts everything in `ext`; split explicitly so
    # bare reserved names ("CON", "NUL") are caught too.
    if "." in name:
        stem, _, ext = name.rpartition(".")
    else:
        stem, ext = name, ""
    if stem.upper() in _RESERVED_NAMES or name.lower() in _FORBIDDEN_NAMES:
        stem = f"{stem}_"
        name = f"{stem}.{ext}" if ext else stem
    if len(name) > _SEGMENT_MAX:
        if ext:
            name = name[:_SEGMENT_MAX - len(ext) - 1] + "." + ext
        else:
            name = name[:_SEGMENT_MAX]
    return name


def sanitize_path(path: str) -> str:
    """Sanitize a full path, trimming total length if needed."""
    parts = path.replace("\\", "/").split("/")
    sanitized = [sanitize_segment(p) for p in parts if p]
    result = "/".join(sanitized)
    if len(result) > _MAX_PATH_LEN:
        # Truncate from the middle of the path (preserve root and filename)
        while len(result) > _MAX_PATH_LEN and len(sanitized) > 1:
            sanitized.pop(-2)  # drop deepest parent
            result = "/".join(sanitized)
    return result
