"""envcheck — Environment Parity Checker."""

from envcheck.config import EnvcheckConfig, load_config

# Re-export scanner types so dependent modules (profile builder, etc.)
# can import directly from ``envcheck``.
from envcheck.diff import (
    DiffKind,
    EnvVarDiffResult,
    VarDiff,
    diff_env_vars,
    infer_value_type,
)
from envcheck.init import (
    DEFAULT_ENV,
    apply_env_names,
    bootstrap,
    discover_files,
    group_environments,
    parse_env_names,
    render_config,
    validate_env_name,
)
from envcheck.json_reporter import (
    build_json_report,
    print_json_report,
    render_json_report,
)
from envcheck.profile import EnvironmentProfile, build_profile
from envcheck.reporter import (
    ENV_VAR_STYLES,
    SERVICE_STYLES,
    build_verdict,
    print_env_var_diff,
    print_report,
    print_service_diff,
    render_env_var_diff,
    render_service_diff,
    summarize_env_var_diff,
    summarize_service_diff,
)
from envcheck.services import (
    ParsedImage,
    ServiceDiff,
    ServiceDiffKind,
    ServiceDiffResult,
    diff_services,
    parse_image,
)
from envcheck.scanners.ci import (
    CiScanResult,
    CiSecretEntry,
    CiVarEntry,
    CiWorkflowScanResult,
    scan_ci_workflow,
    scan_ci_workflows,
)
from envcheck.scanners.docker import (
    DockerComposeScanResult,
    DockerScanResult,
    DockerVarEntry,
    DockerfileScanResult,
    scan_docker_compose,
    scan_docker_compose_files,
    scan_dockerfile,
    scan_dockerfiles,
)
from envcheck.scanners.env_file import EnvFileScanResult, EnvVarEntry, scan_env_file, scan_env_files

__version__ = "0.1.0"
__all__ = [
    "EnvcheckConfig",
    "load_config",
    "DiffKind",
    "EnvVarDiffResult",
    "VarDiff",
    "diff_env_vars",
    "infer_value_type",
    "DEFAULT_ENV",
    "apply_env_names",
    "bootstrap",
    "discover_files",
    "group_environments",
    "parse_env_names",
    "render_config",
    "validate_env_name",
    "build_json_report",
    "render_json_report",
    "print_json_report",
    "EnvironmentProfile",
    "build_profile",
    "ENV_VAR_STYLES",
    "SERVICE_STYLES",
    "render_env_var_diff",
    "render_service_diff",
    "print_env_var_diff",
    "print_service_diff",
    "print_report",
    "build_verdict",
    "summarize_env_var_diff",
    "summarize_service_diff",
    "ParsedImage",
    "ServiceDiff",
    "ServiceDiffKind",
    "ServiceDiffResult",
    "diff_services",
    "parse_image",
    "EnvFileScanResult",
    "EnvVarEntry",
    "scan_env_file",
    "scan_env_files",
    "DockerVarEntry",
    "DockerfileScanResult",
    "DockerComposeScanResult",
    "DockerScanResult",
    "scan_dockerfile",
    "scan_dockerfiles",
    "scan_docker_compose",
    "scan_docker_compose_files",
    "CiVarEntry",
    "CiSecretEntry",
    "CiWorkflowScanResult",
    "CiScanResult",
    "scan_ci_workflow",
    "scan_ci_workflows",
]
