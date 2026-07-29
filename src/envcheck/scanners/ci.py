"""Scanner for CI/CD configuration files.

Parses:

- GitHub Actions workflow YAML files (``.github/workflows/*.yml``)
    - ``env:`` blocks at the workflow level
    - ``env:`` blocks at the job level
    - ``env:`` blocks at the step level
    - ``${{ secrets.X }}`` and ``${{ vars.X }}`` references in values
    - ``secrets:`` blocks in reusable workflow calls
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class CiVarEntry(BaseModel):
    """A single environment variable or secret reference parsed from a CI
    workflow file."""

    key: str = Field(description="Variable name")
    value: str = Field(description="Value (or expression string such as ${{ secrets.X }})")
    source_file: str = Field(description="Path to the source file")
    scope: str = Field(
        description="Scope where the variable was defined (workflow, job, step)"
    )
    source_name: str = Field(
        description="Name context: workflow name, job id, or step name/index"
    )
    has_secret_ref: bool = Field(
        default=False,
        description="Whether the value references a GitHub secret (${{ secrets.X }})",
    )
    has_var_ref: bool = Field(
        default=False,
        description="Whether the value references a GitHub variable (${{ vars.X }})",
    )


class CiSecretEntry(BaseModel):
    """A secrets reference found in a CI workflow file.

    Tracks ``secrets:`` blocks (in reusable workflow ``uses:`` steps) and
    ``${{ secrets.X }}`` references in values.
    """

    name: str = Field(description="Secret name (e.g. API_KEY, DEPLOY_TOKEN)")
    source_file: str = Field(description="Path to the source file")
    scope: str = Field(
        description="Scope where the reference was found (workflow, job, step, uses)"
    )
    source_name: str = Field(
        description="Name context: workflow name, job id, or step name/index"
    )
    reference_type: str = Field(
        description="How the secret was referenced (expression or secrets_block)"
    )


class CiWorkflowScanResult(BaseModel):
    """Result of scanning a single CI workflow file."""

    source: str = Field(description="Path to the scanned file")
    workflow_name: Optional[str] = Field(
        default=None, description="Workflow name from the `name:` field"
    )
    variables: Dict[str, CiVarEntry] = Field(
        default_factory=dict,
        description="Map of variable name → entry (last value wins for duplicates)",
    )
    secrets: Dict[str, CiSecretEntry] = Field(
        default_factory=dict,
        description="Map of secret name → entry (deduplicated)",
    )
    jobs: Dict[str, str] = Field(
        default_factory=dict,
        description="Map of job id → job name (or id if no name given)",
    )
    triggers: List[str] = Field(
        default_factory=list,
        description="List of trigger events (e.g. push, pull_request, workflow_dispatch)",
    )
    total_secrets: int = Field(default=0, description="Total unique secrets referenced")
    total_variables: int = Field(default=0, description="Total unique variables defined")


class CiScanResult(BaseModel):
    """Combined result of scanning all CI workflow files in a project."""

    workflow_results: List[CiWorkflowScanResult] = Field(default_factory=list)

    @property
    def all_variables(self) -> Dict[str, CiVarEntry]:
        """Aggregate all variables from all scanned workflow files (last source wins)."""
        merged: Dict[str, CiVarEntry] = {}
        for wr in self.workflow_results:
            merged.update(wr.variables)
        return merged

    @property
    def all_secrets(self) -> Dict[str, CiSecretEntry]:
        """Aggregate all secrets from all scanned workflow files (last source wins)."""
        merged: Dict[str, CiSecretEntry] = {}
        for wr in self.workflow_results:
            merged.update(wr.secrets)
        return merged


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Regex to find ``${{ secrets.X }}`` or ``${{ secrets.X.Y }}`` references.
_SECRETS_EXPR_RE = re.compile(r"\$\{\{\s*secrets\.([a-zA-Z_][a-zA-Z0-9_.]*)\s*\}\}")

#: Regex to find ``${{ vars.X }}`` or ``${{ vars.X.Y }}`` references.
_VARS_EXPR_RE = re.compile(r"\$\{\{\s*vars\.([a-zA-Z_][a-zA-Z0-9_.]*)\s*\}\}")

#: Standard GitHub Actions trigger event names.
_STANDARD_TRIGGERS = frozenset({
    "push", "pull_request", "pull_request_target", "pull_request_review",
    "workflow_dispatch", "workflow_call", "schedule", "release",
    "issues", "issue_comment", "discussion", "discussion_comment",
    "fork", "watch", "star", "create", "delete", "deployment",
    "deployment_status", "merge_group", "milestone", "page_build",
    "public", "registry_package", "status", "check_run", "check_suite",
    "commit_comment", "gollum", "label", "member", "project",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_secret_refs(
    value: str,
) -> List[str]:
    """Extract all ``${{ secrets.X }}`` reference names from *value*."""
    return [m.group(1) for m in _SECRETS_EXPR_RE.finditer(value)]


def _extract_var_refs(
    value: str,
) -> List[str]:
    """Extract all ``${{ vars.X }}`` reference names from *value*."""
    return [m.group(1) for m in _VARS_EXPR_RE.finditer(value)]


def _ensure_str(val: object) -> str:
    """Convert a YAML-scalar value to its string representation."""
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    return str(val)


def _get_event_names(on_block: object) -> List[str]:
    """Extract event/trigger names from the ``on:`` block of a workflow.

    Handles:
    - Single string: ``on: push``
    - List: ``on: [push, pull_request]``
    - Mapping: ``on: {push: {branches: [main]}}``
    """
    if isinstance(on_block, str):
        return [on_block]
    if isinstance(on_block, list):
        return [str(e) for e in on_block]
    if isinstance(on_block, dict):
        return list(on_block.keys())
    return []


# ---------------------------------------------------------------------------
# Main scanner
# ---------------------------------------------------------------------------


def scan_ci_workflow(path: Path) -> CiWorkflowScanResult:
    """Scan a single GitHub Actions workflow YAML file for environment
    variables, secrets references, and trigger events.

    Parameters
    ----------
    path:
        Path to the workflow YAML file (typically
        ``.github/workflows/<name>.yml``).

    Returns
    -------
    CiWorkflowScanResult
        The parsed result.

    Raises
    ------
    FileNotFoundError
        The file does not exist.
    ValueError
        The file is not valid YAML or is not a mapping.
    """
    resolved = path.resolve() if not path.is_absolute() else path
    if not resolved.is_file():
        raise FileNotFoundError(f"CI workflow file not found: {resolved}")

    import yaml

    text = resolved.read_text(encoding="utf-8", errors="replace")
    try:
        raw: object = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in CI workflow file: {e}") from e

    if raw is None:
        raise ValueError(
            f"CI workflow file is empty: {resolved}"
        )
    if not isinstance(raw, dict):
        raise ValueError(
            f"CI workflow file must be a YAML mapping, got {type(raw).__name__}"
        )

    source = str(resolved)
    workflow_name = _ensure_str(raw.get("name")) or None
    variables: Dict[str, CiVarEntry] = {}
    secrets: Dict[str, CiSecretEntry] = {}

    # --- triggers ---
    on_block = raw.get("on")
    triggers = _get_event_names(on_block) if on_block is not None else []

    # --- workflow-level env ---
    workflow_env = raw.get("env")
    if isinstance(workflow_env, dict):
        for key, val in workflow_env.items():
            val_str = _ensure_str(val)
            secret_refs = _extract_secret_refs(val_str)
            var_refs = _extract_var_refs(val_str)
            has_secret = len(secret_refs) > 0
            has_var = len(var_refs) > 0

            variables[key] = CiVarEntry(
                key=key,
                value=val_str,
                source_file=source,
                scope="workflow",
                source_name=workflow_name or "(unnamed)",
                has_secret_ref=has_secret,
                has_var_ref=has_var,
            )

            # Also track secrets found in expressions
            for ref_name in secret_refs:
                secrets[ref_name] = CiSecretEntry(
                    name=ref_name,
                    source_file=source,
                    scope="workflow",
                    source_name=workflow_name or "(unnamed)",
                    reference_type="expression",
                )

    # --- jobs ---
    jobs_data = raw.get("jobs")
    if isinstance(jobs_data, dict):
        for job_id, job_config in jobs_data.items():
            if not isinstance(job_config, dict):
                continue

            job_name = str(job_config.get("name", job_id))

            # --- job-level env ---
            job_env = job_config.get("env")
            if isinstance(job_env, dict):
                for key, val in job_env.items():
                    val_str = _ensure_str(val)
                    secret_refs = _extract_secret_refs(val_str)
                    var_refs = _extract_var_refs(val_str)
                    has_secret = len(secret_refs) > 0
                    has_var = len(var_refs) > 0

                    variables[key] = CiVarEntry(
                        key=key,
                        value=val_str,
                        source_file=source,
                        scope="job",
                        source_name=job_id,
                        has_secret_ref=has_secret,
                        has_var_ref=has_var,
                    )

                    for ref_name in secret_refs:
                        secrets[ref_name] = CiSecretEntry(
                            name=ref_name,
                            source_file=source,
                            scope="job",
                            source_name=job_id,
                            reference_type="expression",
                        )

            # --- secrets block in reusable workflow calls ---
            uses = job_config.get("uses")
            if isinstance(uses, str):
                secrets_config = job_config.get("secrets")
                if isinstance(secrets_config, dict):
                    if secrets_config.get("inherit") is True:
                        # Secrets: inherit — record as a marker
                        secrets["__inherit__"] = CiSecretEntry(
                            name="__inherit__",
                            source_file=source,
                            scope="job",
                            source_name=job_id,
                            reference_type="secrets_block",
                        )
                    else:
                        for secret_key, secret_val in secrets_config.items():
                            if secret_key == "inherit":
                                continue
                            val_str = _ensure_str(secret_val)
                            secrets[secret_key] = CiSecretEntry(
                                name=secret_key,
                                source_file=source,
                                scope="job",
                                source_name=job_id,
                                reference_type="secrets_block",
                            )
                            # Also check for expressions in the value mapping;
                            # only track references to *different* secret names
                            # (the key itself was already recorded as secrets_block).
                            ref_names = _extract_secret_refs(val_str)
                            for ref_name in ref_names:
                                if ref_name != secret_key:
                                    secrets[ref_name] = CiSecretEntry(
                                        name=ref_name,
                                        source_file=source,
                                        scope="job",
                                        source_name=job_id,
                                        reference_type="expression",
                                    )

            # --- steps ---
            steps = job_config.get("steps")
            if isinstance(steps, list):
                for step_idx, step_config in enumerate(steps):
                    if not isinstance(step_config, dict):
                        continue

                    step_name = str(step_config.get("name", f"step_{step_idx}"))

                    # --- step-level env ---
                    step_env = step_config.get("env")
                    if isinstance(step_env, dict):
                        for key, val in step_env.items():
                            val_str = _ensure_str(val)
                            secret_refs = _extract_secret_refs(val_str)
                            var_refs = _extract_var_refs(val_str)
                            has_secret = len(secret_refs) > 0
                            has_var = len(var_refs) > 0

                            variables[key] = CiVarEntry(
                                key=key,
                                value=val_str,
                                source_file=source,
                                scope="step",
                                source_name=f"{job_id}/{step_name}",
                                has_secret_ref=has_secret,
                                has_var_ref=has_var,
                            )

                            for ref_name in secret_refs:
                                secrets[ref_name] = CiSecretEntry(
                                    name=ref_name,
                                    source_file=source,
                                    scope="step",
                                    source_name=f"{job_id}/{step_name}",
                                    reference_type="expression",
                                )

                    # Also scan the ``run:`` and other string fields for secret refs
                    for field in ("run", "shell", "working-directory"):
                        field_val = step_config.get(field)
                        if isinstance(field_val, str):
                            ref_names = _extract_secret_refs(field_val)
                            for ref_name in ref_names:
                                secrets[ref_name] = CiSecretEntry(
                                    name=ref_name,
                                    source_file=source,
                                    scope="step",
                                    source_name=f"{job_id}/{step_name}",
                                    reference_type="expression",
                                )

    # --- Build jobs map ---
    jobs_map: Dict[str, str] = {}
    if isinstance(jobs_data, dict):
        for job_id, job_config in jobs_data.items():
            if isinstance(job_config, dict):
                jobs_map[job_id] = str(job_config.get("name", job_id))

    return CiWorkflowScanResult(
        source=source,
        workflow_name=workflow_name,
        variables=variables,
        secrets=secrets,
        jobs=jobs_map,
        triggers=triggers,
        total_secrets=len(secrets),
        total_variables=len(variables),
    )


def scan_ci_workflows(paths: List[Path]) -> List[CiWorkflowScanResult]:
    """Scan multiple CI workflow files.

    Non-existent files are silently skipped so callers can pass glob
    results that may not exist.

    Parameters
    ----------
    paths:
        List of file paths to attempt scanning.

    Returns
    -------
    list[CiWorkflowScanResult]
        Results for every file that existed and was successfully parsed.
    """
    results: List[CiWorkflowScanResult] = []
    for path in paths:
        try:
            results.append(scan_ci_workflow(path))
        except (FileNotFoundError, ValueError) as exc:
            # Skip missing files; re-raise genuine ValueErrors from
            # empty/invalid content so the user knows something is wrong.
            if isinstance(exc, FileNotFoundError):
                pass
            else:
                raise
    return results
