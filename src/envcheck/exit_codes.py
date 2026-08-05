"""Exit codes and drift classification for the CLI (M2.5).

The CLI exit-code contract, per the PRD data flow:

- ``0`` — clean: the two environments are in sync (or only informational
  drift exists in default mode)
- ``1`` — drift detected: the comparison found environment drift
- ``2`` — error: the command could not complete (missing config, unknown
  environment, malformed YAML, unsupported service configuration, ...)

Default (non-strict) mode is designed for interactive use: it fails only
on *parity-critical* drift — variables the target environment is missing,
value/type changes, and any Docker-service drift.  Extra variables that
exist only in the target are reported but do **not** fail the run,
because they are often intentional (staging-only secrets, local
overrides, ...).

``--strict`` mode is designed for CI gates: **any** drift — extras
included — produces exit code 1, so a pipeline fails on every last
difference between two environments.

The main entrypoint is :func:`compute_exit_code`.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Optional

from envcheck.diff import EnvVarDiffResult
from envcheck.services import ServiceDiffResult


class ExitCode(IntEnum):
    """Process exit codes used by the envcheck CLI."""

    OK = 0
    DRIFT = 1
    ERROR = 2


def _env_drift_is_critical(env_diff: EnvVarDiffResult) -> bool:
    """True when env-var drift is parity-critical in default mode.

    Missing variables (present in the base, absent from the target),
    value changes, and type changes are critical.  Extra variables
    (present only in the target) are informational.
    """
    return bool(env_diff.missing or env_diff.changed or env_diff.type_changed)


def compute_exit_code(
    env_diff: Optional[EnvVarDiffResult],
    service_diff: Optional[ServiceDiffResult],
    strict: bool = False,
) -> ExitCode:
    """Determine the process exit code for a completed diff run.

    Parameters
    ----------
    env_diff:
        Result of the environment-variable comparison, or ``None`` when
        no comparison was performed.
    service_diff:
        Result of the Docker-service comparison, or ``None`` when no
        comparison was performed.
    strict:
        When True, any drift — including extra variables/services that
        exist only in the target — produces :attr:`ExitCode.DRIFT`.
        When False (default), extra variables are reported but do not
        fail the run; only parity-critical drift (missing, changed, or
        type-changed variables, plus any Docker-service drift) produces
        :attr:`ExitCode.DRIFT`.

    Returns
    -------
    ExitCode
        :attr:`ExitCode.OK` when the environments are in sync,
        :attr:`ExitCode.DRIFT` otherwise.

    Examples
    --------
    >>> compute_exit_code(None, None)
    <ExitCode.OK: 0>

    With an extra-only diff result (base ``A``, target ``A`` + ``B``):

    >>> from envcheck.diff import DiffKind, EnvVarDiffResult, VarDiff
    >>> extra = EnvVarDiffResult(base_env="dev", target_env="staging", diffs=[
    ...     VarDiff(key="A", kind=DiffKind.MATCH, base_value="1", target_value="1"),
    ...     VarDiff(key="B", kind=DiffKind.EXTRA, target_value="2"),
    ... ])
    >>> compute_exit_code(extra, None)
    <ExitCode.OK: 0>
    >>> compute_exit_code(extra, None, strict=True)
    <ExitCode.DRIFT: 1>
    """
    if strict:
        drift = 0
        if env_diff is not None:
            drift += env_diff.drift_count
        if service_diff is not None:
            drift += service_diff.drift_count
        return ExitCode.DRIFT if drift > 0 else ExitCode.OK

    if env_diff is not None and _env_drift_is_critical(env_diff):
        return ExitCode.DRIFT
    if service_diff is not None and service_diff.drift_count > 0:
        return ExitCode.DRIFT
    return ExitCode.OK
