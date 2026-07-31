"""Environment-variable diff engine.

Compares the merged environment-variable sets of two
:class:`~envcheck.profile.EnvironmentProfile` instances (e.g. ``dev`` vs
``staging``) and classifies every variable as one of:

- **match** — present in both profiles with an identical value
- **missing** — present in the base profile but absent from the target
- **extra** — present in the target profile but absent from the base
- **changed** — present in both, values differ, inferred type unchanged
- **type_changed** — present in both, values differ, inferred type changed
  (e.g. ``DEBUG=1`` vs ``DEBUG=true``)

The engine operates on :attr:`EnvironmentProfile.env_vars`, the flat
key → value view aggregated from every scanner source.  Optional
``ignore`` patterns (exact names or globs such as ``BUILD_ID`` or
``TIMESTAMP_*``) are honored, matching the ``ignore:`` list in
``.envcheck.yaml``.

The main entrypoint is :func:`diff_env_vars`.
"""

from __future__ import annotations

import fnmatch
import re
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from envcheck.profile import EnvironmentProfile

# ---------------------------------------------------------------------------
# Value type inference
# ---------------------------------------------------------------------------

#: Regex matching a plain integer literal (optional sign, digits only).
#: Deliberately stricter than ``int()`` so values like ``1_000`` or
#: ``0x10`` are treated as strings, not numbers.
_INT_RE = re.compile(r"^[+-]?\d+$")

#: Regex matching a float literal, including scientific notation
#: (``3.14``, ``.5``, ``1.``, ``1e5``, ``-2.5e-3``).
_FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")

_BOOL_LITERALS = frozenset({"true", "false"})


def infer_value_type(value: str) -> str:
    """Infer the runtime type of an environment variable value.

    Values are stored as strings by every scanner, but a string can still
    represent a boolean, an integer, or a float.  Type inference is what
    lets the diff engine flag a ``DEBUG=1`` → ``DEBUG=true`` drift as a
    *type change* rather than a plain value change.

    Returns one of ``"bool"``, ``"int"``, ``"float"``, ``"string"``, or
    ``"empty"`` (for the empty string).

    Parameters
    ----------
    value:
        The raw string value of an environment variable.

    Returns
    -------
    str
        The inferred type name.

    Examples
    --------
    >>> infer_value_type("true")
    'bool'
    >>> infer_value_type("8080")
    'int'
    >>> infer_value_type("3.14")
    'float'
    >>> infer_value_type("postgres://localhost")
    'string'
    >>> infer_value_type("")
    'empty'
    """
    if value == "":
        return "empty"
    if value.lower() in _BOOL_LITERALS:
        return "bool"
    if _INT_RE.match(value):
        return "int"
    if _FLOAT_RE.match(value):
        return "float"
    return "string"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class DiffKind(str, Enum):
    """Classification of a single environment variable across two profiles."""

    MATCH = "match"
    MISSING = "missing"
    EXTRA = "extra"
    CHANGED = "changed"
    TYPE_CHANGED = "type_changed"


class VarDiff(BaseModel):
    """The diff state of a single environment variable between two profiles."""

    key: str = Field(description="Variable name")
    kind: DiffKind = Field(description="How the variable differs between profiles")
    base_value: Optional[str] = Field(
        default=None, description="Value in the base environment (None if extra)"
    )
    target_value: Optional[str] = Field(
        default=None, description="Value in the target environment (None if missing)"
    )
    base_type: Optional[str] = Field(
        default=None, description="Inferred type in the base environment"
    )
    target_type: Optional[str] = Field(
        default=None, description="Inferred type in the target environment"
    )
    base_source: Optional[str] = Field(
        default=None, description="Source file that defined the variable in the base environment"
    )
    target_source: Optional[str] = Field(
        default=None, description="Source file that defined the variable in the target environment"
    )


class EnvVarDiffResult(BaseModel):
    """Result of comparing the environment variables of two profiles.

    ``diffs`` contains one :class:`VarDiff` per variable in the union of
    both profiles (sorted by key), including matching variables so a
    reporter can render green "ok" rows alongside the drift.
    """

    base_env: str = Field(description="Name of the base environment (e.g. dev)")
    target_env: str = Field(description="Name of the target environment (e.g. staging)")
    diffs: List[VarDiff] = Field(
        default_factory=list,
        description="One VarDiff per variable in the union of both profiles",
    )

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def matches(self) -> List[VarDiff]:
        """Variables present in both profiles with identical values."""
        return [d for d in self.diffs if d.kind == DiffKind.MATCH]

    @property
    def missing(self) -> List[VarDiff]:
        """Variables present in the base profile but absent from the target."""
        return [d for d in self.diffs if d.kind == DiffKind.MISSING]

    @property
    def extra(self) -> List[VarDiff]:
        """Variables present in the target profile but absent from the base."""
        return [d for d in self.diffs if d.kind == DiffKind.EXTRA]

    @property
    def changed(self) -> List[VarDiff]:
        """Variables whose value changed without a type change."""
        return [d for d in self.diffs if d.kind == DiffKind.CHANGED]

    @property
    def type_changed(self) -> List[VarDiff]:
        """Variables whose value changed together with its inferred type."""
        return [d for d in self.diffs if d.kind == DiffKind.TYPE_CHANGED]

    @property
    def has_drift(self) -> bool:
        """True if any variable is not an exact match (missing/extra/changed/type_changed)."""
        return any(d.kind != DiffKind.MATCH for d in self.diffs)

    @property
    def drift_count(self) -> int:
        """Number of variables that are not exact matches."""
        return len(self.diffs) - len(self.matches)

    @property
    def total_compared(self) -> int:
        """Total number of variables compared (union of both profiles)."""
        return len(self.diffs)


# ---------------------------------------------------------------------------
# Ignore-pattern matching
# ---------------------------------------------------------------------------


def _matches_ignore(key: str, patterns: List[str]) -> bool:
    """Return True if *key* matches any exact or glob pattern in *patterns*."""
    for pattern in patterns:
        if fnmatch.fnmatchcase(key, pattern):
            return True
    return False


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def _source_of(details: Dict[str, object], key: str) -> Optional[str]:
    """Extract the ``source_file`` attribute from an entry, if present."""
    entry = details.get(key)
    if entry is None:
        return None
    return getattr(entry, "source_file", None)


def diff_env_vars(
    base: EnvironmentProfile,
    target: EnvironmentProfile,
    ignore: Optional[List[str]] = None,
) -> EnvVarDiffResult:
    """Compare the environment variables of two profiles.

    Parameters
    ----------
    base:
        The reference profile (e.g. ``dev``).
    target:
        The profile being compared against the base (e.g. ``staging``).
    ignore:
        Optional list of variable-name patterns (exact names or globs
        such as ``BUILD_ID`` or ``TIMESTAMP_*``) to exclude from the
        comparison.  Pass ``config.ignore`` from a loaded
        :class:`~envcheck.config.EnvcheckConfig` to honor the
        ``.envcheck.yaml`` ignore list.

    Returns
    -------
    EnvVarDiffResult
        One :class:`VarDiff` per variable in the union of both profiles.

    Notes
    -----
    Classification rules for variables present in both profiles:

    - identical values → ``match``
    - different values, same inferred type → ``changed``
    - different values, different inferred type → ``type_changed``
    """
    patterns = ignore or []

    base_vars = base.env_vars
    target_vars = target.env_vars
    base_details = base.env_var_details
    target_details = target.env_var_details

    diffs: List[VarDiff] = []

    all_keys = sorted(set(base_vars) | set(target_vars))
    for key in all_keys:
        if _matches_ignore(key, patterns):
            continue

        in_base = key in base_vars
        in_target = key in target_vars

        if in_base and in_target:
            base_value = base_vars[key]
            target_value = target_vars[key]
            base_type = infer_value_type(base_value)
            target_type = infer_value_type(target_value)

            if base_value == target_value:
                kind = DiffKind.MATCH
            elif base_type != target_type:
                kind = DiffKind.TYPE_CHANGED
            else:
                kind = DiffKind.CHANGED

            diffs.append(
                VarDiff(
                    key=key,
                    kind=kind,
                    base_value=base_value,
                    target_value=target_value,
                    base_type=base_type,
                    target_type=target_type,
                    base_source=_source_of(base_details, key),
                    target_source=_source_of(target_details, key),
                )
            )
        elif in_base:
            base_value = base_vars[key]
            diffs.append(
                VarDiff(
                    key=key,
                    kind=DiffKind.MISSING,
                    base_value=base_value,
                    base_type=infer_value_type(base_value),
                    base_source=_source_of(base_details, key),
                )
            )
        else:
            target_value = target_vars[key]
            diffs.append(
                VarDiff(
                    key=key,
                    kind=DiffKind.EXTRA,
                    target_value=target_value,
                    target_type=infer_value_type(target_value),
                    target_source=_source_of(target_details, key),
                )
            )

    return EnvVarDiffResult(base_env=base.name, target_env=target.name, diffs=diffs)
