"""JSON output mode — machine-readable drift reports (``--json``).

Serializes :class:`~envcheck.diff.EnvVarDiffResult` and
:class:`~envcheck.services.ServiceDiffResult` into a single JSON
document designed for CI pipelines and machine parsing (PRD F7).

The output is intentionally plain data — no Rich markup, no emoji, no
ANSI codes — so it can be piped directly into ``jq``, a GitHub Actions
step, or a monitoring script.  Every field is always present (``null``
when there is no value) so consumers never have to guess.

Schema
------

.. code-block:: json

    {
      "base_env": "dev",
      "target_env": "staging",
      "env_vars": {
        "total_compared": 5,
        "drift_count": 4,
        "diffs": [
          {
            "key": "DEBUG",
            "kind": "type_changed",
            "base_value": "1",
            "target_value": "true",
            "base_type": "int",
            "target_type": "bool",
            "base_source": "/p/.env.dev",
            "target_source": null
          }
        ]
      },
      "services": {
        "total_compared": 2,
        "drift_count": 1,
        "diffs": [
          {
            "service": "postgres",
            "kind": "version_changed",
            "base_image": "postgres:16",
            "target_image": "postgres:15",
            "base_version": "16",
            "target_version": "15",
            "base_source": null,
            "target_source": null
          }
        ]
      },
      "drift_count": 5,
      "verdict": "drift detected"
    }

Field reference
---------------

- ``base_env`` / ``target_env`` — names of the two environments compared.
- ``env_vars`` — always present.  ``diffs`` holds one object per
  variable in the union of both profiles; ``kind`` is one of
  ``match``, ``missing``, ``extra``, ``changed``, ``type_changed``.
- ``services`` — always present.  ``diffs`` holds one object per
  compared Docker service; ``kind`` is one of ``match``,
  ``version_changed``, ``image_changed``, ``missing``, ``extra``.
  When no Docker comparison was performed every field is ``0``/``[]``.
- ``drift_count`` — total number of non-matching variables + services.
- ``verdict`` — ``"in sync"`` when ``drift_count == 0``, otherwise
  ``"drift detected"``.

The main entrypoint is :func:`print_json_report`, which writes the
rendered document to stdout (or an explicit file object).
"""

from __future__ import annotations

import json
import sys
from typing import Dict, List, Optional, TextIO, TypedDict

from envcheck.diff import EnvVarDiffResult
from envcheck.services import ServiceDiffResult


# ---------------------------------------------------------------------------
# Report schema (TypedDicts document the machine-facing JSON contract)
# ---------------------------------------------------------------------------


class VarDiffRow(TypedDict):
    """JSON object for a single environment-variable diff."""

    key: str
    kind: str
    base_value: Optional[str]
    target_value: Optional[str]
    base_type: Optional[str]
    target_type: Optional[str]
    base_source: Optional[str]
    target_source: Optional[str]


class ServiceDiffRow(TypedDict):
    """JSON object for a single Docker-service diff."""

    service: str
    kind: str
    base_image: Optional[str]
    target_image: Optional[str]
    base_version: Optional[str]
    target_version: Optional[str]
    base_source: Optional[str]
    target_source: Optional[str]


class DiffSection(TypedDict):
    """JSON object for one comparison section (env vars or services)."""

    total_compared: int
    drift_count: int
    diffs: List[VarDiffRow] | List[ServiceDiffRow]


class JsonReport(TypedDict):
    """Top-level JSON report document emitted by ``--json``."""

    base_env: str
    target_env: str
    env_vars: DiffSection
    services: DiffSection
    drift_count: int
    verdict: str


def _env_var_diffs(result: EnvVarDiffResult) -> List[VarDiffRow]:
    """Convert each VarDiff into its JSON object (stable key order)."""
    rows: List[VarDiffRow] = []
    for d in result.diffs:
        rows.append(
            {
                "key": d.key,
                "kind": d.kind.value,
                "base_value": d.base_value,
                "target_value": d.target_value,
                "base_type": d.base_type,
                "target_type": d.target_type,
                "base_source": d.base_source,
                "target_source": d.target_source,
            }
        )
    return rows


def _service_diffs(result: ServiceDiffResult) -> List[ServiceDiffRow]:
    """Convert each ServiceDiff into its JSON object (stable key order)."""
    rows: List[ServiceDiffRow] = []
    for d in result.diffs:
        rows.append(
            {
                "service": d.service,
                "kind": d.kind.value,
                "base_image": d.base_image,
                "target_image": d.target_image,
                "base_version": d.base_version,
                "target_version": d.target_version,
                "base_source": d.base_source,
                "target_source": d.target_source,
            }
        )
    return rows


def _empty_section() -> DiffSection:
    """A comparison section with nothing compared (schema-stable)."""
    return {"total_compared": 0, "drift_count": 0, "diffs": []}


def build_json_report(
    env_diff: Optional[EnvVarDiffResult],
    service_diff: Optional[ServiceDiffResult],
) -> JsonReport:
    """Build the JSON-serializable drift report for two diff results.

    Parameters
    ----------
    env_diff:
        Result of the environment-variable comparison.  May be ``None``
        when no env-var comparison was performed (rare — the CLI always
        compares variables).
    service_diff:
        Result of the Docker-service comparison, or ``None`` when no
        Docker comparison was performed.

    Returns
    -------
    JsonReport
        The report document matching the schema above.  Every key is
        always present; ``env_vars`` and ``services`` are always
        objects (with empty ``diffs`` when nothing was compared).

    Examples
    --------
    >>> build_json_report(None, None)
    {'base_env': '', 'target_env': '', 'env_vars': {'total_compared': 0, 'drift_count': 0, 'diffs': []}, 'services': {'total_compared': 0, 'drift_count': 0, 'diffs': []}, 'drift_count': 0, 'verdict': 'in sync'}
    """
    if env_diff is not None:
        env_section: DiffSection = {
            "total_compared": env_diff.total_compared,
            "drift_count": env_diff.drift_count,
            "diffs": _env_var_diffs(env_diff),
        }
    else:
        env_section = _empty_section()

    if service_diff is not None:
        svc_section: DiffSection = {
            "total_compared": service_diff.total_compared,
            "drift_count": service_diff.drift_count,
            "diffs": _service_diffs(service_diff),
        }
    else:
        svc_section = _empty_section()

    drift = env_section["drift_count"] + svc_section["drift_count"]

    return {
        "base_env": env_diff.base_env if env_diff is not None else "",
        "target_env": env_diff.target_env if env_diff is not None else "",
        "env_vars": env_section,
        "services": svc_section,
        "drift_count": drift,
        "verdict": "in sync" if drift == 0 else "drift detected",
    }


def render_json_report(
    env_diff: Optional[EnvVarDiffResult],
    service_diff: Optional[ServiceDiffResult],
    indent: int = 2,
) -> str:
    """Render the drift report as a JSON string.

    The document is serialized with ``ensure_ascii=False`` (UTF-8
    output, no ``\\uXXXX`` escapes) and a trailing newline so it is
    immediately consumable on the command line.

    Parameters
    ----------
    env_diff, service_diff:
        See :func:`build_json_report`.
    indent:
        Pretty-print indentation (``2`` by default).

    Returns
    -------
    str
        The JSON document, terminated by a single newline.
    """
    report = build_json_report(env_diff, service_diff)
    return json.dumps(report, indent=indent, ensure_ascii=False) + "\n"


def print_json_report(
    env_diff: Optional[EnvVarDiffResult],
    service_diff: Optional[ServiceDiffResult],
    file: Optional[TextIO] = None,
) -> None:
    """Write the JSON drift report to *file* (stdout by default).

    This is the CI-facing entrypoint: it emits *only* the JSON document
    (plus a trailing newline) and nothing else, so ``envcheck diff
    dev staging --json | jq .drift_count`` works without filtering.

    Parameters
    ----------
    env_diff, service_diff:
        See :func:`build_json_report`.
    file:
        Output stream; defaults to :data:`sys.stdout`.
    """
    file = file if file is not None else sys.stdout
    file.write(render_json_report(env_diff, service_diff))
