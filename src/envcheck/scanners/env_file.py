"""Scanner for .env-format files (``.env``, ``.env.*``, ``.env.example``).

Parses standard ``KEY=VALUE`` lines, respecting:

- ``#`` comments (full-line and inline, outside quotes)
- ``export`` prefix
- Single/double-quoted values (with escape-sequence handling)
- Whitespace around keys and values
- Empty lines and malformed lines (skipped)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class EnvVarEntry(BaseModel):
    """A single environment variable parsed from a file."""

    key: str = Field(description="Variable name")
    value: str = Field(description="Parsed value (unquoted, comment-stripped)")
    source_file: str = Field(description="Path to the source file")
    line_number: int = Field(description="1-indexed line number in the source file")


class EnvFileScanResult(BaseModel):
    """Result of scanning a single ``.env`` file."""

    source: str = Field(description="Path to the scanned file")
    variables: Dict[str, EnvVarEntry] = Field(
        default_factory=dict,
        description="Map of variable name → entry (last value wins for duplicates)",
    )
    total_lines: int = Field(default=0, description="Total lines in the file")
    parsed_lines: int = Field(default=0, description="Lines that yielded a valid variable")


# ---------------------------------------------------------------------------
# Parser helpers
# ---------------------------------------------------------------------------

#: Match a ``KEY=VALUE`` line with optional ``export`` prefix, optional
#: whitespace, and a value capturing the remainder of the input line.
_ENV_LINE_RE = re.compile(
    r"^(?:\s*export\s+)?\s*"  # optional export prefix + whitespace
    r"(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)"  # variable name
    r"\s*=\s*"  # equals with optional whitespace
    r"(?P<value>.*)",  # value (rest of line)
)


def _strip_inline_comment(raw: str) -> str:
    """Remove an inline ``#`` comment from *raw*, respecting quotes.

    A ``#`` is only treated as a comment marker when preceded by whitespace
    (space or tab) so that values like ``PASSWORD=abc#123`` are preserved.
    """
    in_sq = False
    in_dq = False
    for i, ch in enumerate(raw):
        if ch == "'" and not in_dq:
            in_sq = not in_sq
        elif ch == '"' and not in_sq:
            in_dq = not in_dq
        elif ch == "#" and not in_sq and not in_dq:
            # Only treat as comment when preceded by whitespace or start
            if i == 0 or raw[i - 1] in (" ", "\t"):
                return raw[:i].rstrip()
    return raw


def _unquote(value: str) -> str:
    """Strip surrounding quotes and interpret escape sequences.

    - Double-quoted: ``\\n``, ``\\r``, ``\\t``, ``\\\"``, ``\\\\``
    - Single-quoted: literal, no escapes processed.
    """
    if len(value) < 2:
        return value

    if value.startswith('"') and value.endswith('"'):
        inner = value[1:-1]
        inner = inner.replace("\\n", "\n")
        inner = inner.replace("\\r", "\r")
        inner = inner.replace("\\t", "\t")
        inner = inner.replace('\\"', '"')
        inner = inner.replace("\\\\", "\\")
        return inner

    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]

    return value


def _parse_env_line(line: str, line_number: int, source_file: str) -> Optional[EnvVarEntry]:
    """Parse a single line of a ``.env`` file into an ``EnvVarEntry``, or
    return ``None`` if the line is blank, a comment, or malformed."""
    stripped = line.strip()

    if not stripped or stripped.startswith("#"):
        return None

    m = _ENV_LINE_RE.match(stripped)
    if not m:
        return None

    key = m.group("key")
    raw_value = m.group("value").strip()

    # Strip inline comments (before unquoting, because the comment marker
    # is outside any quotes).
    cleaned = _strip_inline_comment(raw_value)
    value = _unquote(cleaned)

    return EnvVarEntry(key=key, value=value, source_file=source_file, line_number=line_number)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scan_env_file(path: Path) -> EnvFileScanResult:
    """Scan a single ``.env``-format file and return parsed variables.

    Parameters
    ----------
    path:
        Absolute or relative path to the file.

    Returns
    -------
    EnvFileScanResult
        The parsed result.

    Raises
    ------
    FileNotFoundError
        The file does not exist.
    """
    resolved = path.resolve() if not path.is_absolute() else path
    if not resolved.is_file():
        raise FileNotFoundError(f"Env file not found: {resolved}")

    text = resolved.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    source = str(resolved)
    variables: Dict[str, EnvVarEntry] = {}
    parsed_count = 0

    for i, line in enumerate(lines):
        entry = _parse_env_line(line, i + 1, source)
        if entry is not None:
            variables[entry.key] = entry  # last wins
            parsed_count += 1

    return EnvFileScanResult(
        source=source,
        variables=variables,
        total_lines=len(lines),
        parsed_lines=parsed_count,
    )


def scan_env_files(paths: List[Path]) -> List[EnvFileScanResult]:
    """Scan multiple ``.env``-format files.

    Non-existent files are silently skipped so callers can pass glob
    results that may not exist.

    Parameters
    ----------
    paths:
        List of file paths to attempt scanning.

    Returns
    -------
    list[EnvFileScanResult]
        Results for every file that existed and was successfully parsed.
    """
    results: List[EnvFileScanResult] = []
    for path in paths:
        try:
            results.append(scan_env_file(path))
        except FileNotFoundError:
            pass
    return results
