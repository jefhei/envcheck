"""envcheck — Environment Parity Checker."""

from envcheck.config import EnvcheckConfig, load_config

# Re-export scanner types so dependent modules (profile builder, etc.)
# can import directly from ``envcheck``.
from envcheck.profile import EnvironmentProfile, build_profile
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
    "EnvironmentProfile",
    "build_profile",
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
