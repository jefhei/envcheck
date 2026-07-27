"""Tests for the ``.env`` file scanner."""

from pathlib import Path

import pytest

from envcheck.scanners.env_file import (
    EnvFileScanResult,
    EnvVarEntry,
    scan_env_file,
    scan_env_files,
)


# ===================================================================
# Unit tests — internal parser helpers
# ===================================================================


class TestParseEnvLine:
    """Exercised indirectly through ``scan_env_file`` in the integration
    tests below.  A few targeted checks for edge cases."""

    # Imported / tested via integration tests below


# ===================================================================
# Integration tests — scan_env_file
# ===================================================================


class TestScanEnvFile:
    def test_simple_key_value(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text("DATABASE_URL=localhost:5432\nDEBUG=true\n")
        result = scan_env_file(f)

        assert result.source == str(f.resolve())
        assert result.total_lines == 2
        assert result.parsed_lines == 2
        assert result.variables["DATABASE_URL"].value == "localhost:5432"
        assert result.variables["DEBUG"].value == "true"

    def test_export_prefix(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text("export MY_VAR=hello\n")
        result = scan_env_file(f)
        assert result.variables["MY_VAR"].value == "hello"
        assert result.parsed_lines == 1

    def test_export_prefix_with_extra_spaces(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text("  export   SPACED_VAR = spaced-value\n")
        result = scan_env_file(f)
        assert result.variables["SPACED_VAR"].value == "spaced-value"

    def test_whitespace_around_equals(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text("  KEY  =  padded-value  \n")
        result = scan_env_file(f)
        assert result.variables["KEY"].value == "padded-value"

    def test_full_line_comment(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text("# This is a comment\nKEY=val\n  # another comment\n")
        result = scan_env_file(f)
        assert result.parsed_lines == 1
        assert result.variables["KEY"].value == "val"
        assert result.total_lines == 3

    def test_inline_comment(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text("KEY=value  # this is inline\n")
        result = scan_env_file(f)
        assert result.variables["KEY"].value == "value"

    def test_inline_comment_no_space_before_hash(self, tmp_path: Path):
        """A hash without a leading space is not treated as a comment."""
        f = tmp_path / ".env"
        f.write_text('PASSWORD=abc#123\n')
        result = scan_env_file(f)
        # Most .env parsers treat # as part of the value when there's
        # no space before it. Our parser requires a space before # to
        # treat it as a comment.
        assert result.variables["PASSWORD"].value == "abc#123"

    def test_empty_value(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text("EMPTY=\n")
        result = scan_env_file(f)
        assert result.variables["EMPTY"].value == ""

    def test_double_quoted_value(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text('GREETING="Hello World"\n')
        result = scan_env_file(f)
        assert result.variables["GREETING"].value == "Hello World"

    def test_single_quoted_value(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text("GREETING='Hello World'\n")
        result = scan_env_file(f)
        assert result.variables["GREETING"].value == "Hello World"

    def test_double_quote_escape_sequences(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text('MULTILINE="line1\\nline2"\nESCAPED_QUOTE="he said \\"hi\\""\nBACKSLASH="a\\\\b"\n')
        result = scan_env_file(f)
        assert result.variables["MULTILINE"].value == "line1\nline2"
        assert result.variables["ESCAPED_QUOTE"].value == 'he said "hi"'
        assert result.variables["BACKSLASH"].value == "a\\b"

    def test_single_quote_no_escape(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text("RAW='no \\n escape'\n")
        result = scan_env_file(f)
        assert result.variables["RAW"].value == "no \\n escape"

    def test_duplicate_key_last_wins(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text("KEY=first\nKEY=second\n")
        result = scan_env_file(f)
        assert result.variables["KEY"].value == "second"
        assert result.parsed_lines == 2

    def test_empty_lines_skipped(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text("\n\nKEY=val\n\n\n")
        result = scan_env_file(f)
        assert result.parsed_lines == 1
        assert result.total_lines == 5

    def test_malformed_line_skipped(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text("NOT_A_VALID_LINE\nKEY=val\n=no_key\n123_NUM_START=bad\n")
        result = scan_env_file(f)
        assert result.parsed_lines == 1
        assert result.variables["KEY"].value == "val"

    def test_file_not_found(self, tmp_path: Path):
        missing = tmp_path / "does_not_exist.env"
        with pytest.raises(FileNotFoundError, match="Env file not found"):
            scan_env_file(missing)

    def test_value_with_equals_sign(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text("CONN=host=localhost port=5432 db=test\n")
        result = scan_env_file(f)
        assert result.variables["CONN"].value == "host=localhost port=5432 db=test"

    def test_trailing_whitespace_in_value(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text("KEY=value   \n")
        result = scan_env_file(f)
        assert result.variables["KEY"].value == "value"

    def test_leading_whitespace_stripped(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text("   KEY=value\n")
        result = scan_env_file(f)
        assert result.variables["KEY"].value == "value"

    def test_inline_comment_inside_quotes_preserved(self, tmp_path: Path):
        f = tmp_path / ".env"
        f.write_text('URL="http://example.com#fragment"\n')
        result = scan_env_file(f)
        assert result.variables["URL"].value == "http://example.com#fragment"

    def test_env_example_file(self, tmp_path: Path):
        f = tmp_path / ".env.example"
        f.write_text("DB_HOST=localhost\nDB_PORT=5432\n")
        result = scan_env_file(f)
        assert "DB_HOST" in result.variables
        assert "DB_PORT" in result.variables

    def test_complex_env_file(self, tmp_path: Path):
        """A realistic multi-line .env file."""
        content = """# Database configuration
DB_HOST=localhost
DB_PORT=5432
DB_NAME=myapp

# Redis
REDIS_URL=redis://localhost:6379/0

# Secrets (example only)
SECRET_KEY=change-me-in-production
JWT_SECRET=change-me-too

# Feature flags
export FEATURE_X_ENABLED=true
export FEATURE_Y_ENABLED=false

# Quoted values
APP_NAME="My Cool App"
APP_DESC='A description with a # hash inside'
"""
        f = tmp_path / ".env"
        f.write_text(content)
        result = scan_env_file(f)
        assert result.total_lines == 19
        assert result.parsed_lines == 10
        assert result.variables["DB_HOST"].value == "localhost"
        assert result.variables["DB_PORT"].value == "5432"
        assert result.variables["REDIS_URL"].value == "redis://localhost:6379/0"
        assert result.variables["FEATURE_X_ENABLED"].value == "true"
        assert result.variables["APP_NAME"].value == "My Cool App"
        assert result.variables["APP_DESC"].value == "A description with a # hash inside"


# ===================================================================
# Integration tests — scan_env_files (batch)
# ===================================================================


class TestScanEnvFiles:
    def test_multiple_files(self, tmp_path: Path):
        a = tmp_path / ".env"
        a.write_text("A=1\n")
        b = tmp_path / ".env.dev"
        b.write_text("B=2\n")
        results = scan_env_files([a, b])
        assert len(results) == 2
        assert results[0].variables["A"].value == "1"
        assert results[1].variables["B"].value == "2"

    def test_missing_file_skipped_silently(self, tmp_path: Path):
        existing = tmp_path / ".env"
        existing.write_text("EXISTING=yes\n")
        missing = tmp_path / "missing.env"
        results = scan_env_files([missing, existing])
        assert len(results) == 1
        assert results[0].variables["EXISTING"].value == "yes"

    def test_all_missing_returns_empty_list(self, tmp_path: Path):
        results = scan_env_files([tmp_path / "nope1.env", tmp_path / "nope2.env"])
        assert results == []

    def test_empty_paths_list(self, tmp_path: Path):
        results = scan_env_files([])
        assert results == []
