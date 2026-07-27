"""envcheck — Environment Parity Checker."""

from envcheck.config import EnvcheckConfig, load_config

# Re-export scanner types so dependent modules (profile builder, etc.)
# can import directly from ``envcheck``.
from envcheck.scanners.env_file import EnvFileScanResult, EnvVarEntry, scan_env_file, scan_env_files

__version__ = "0.1.0"
__all__ = [
    "EnvcheckConfig",
    "load_config",
    "EnvFileScanResult",
    "EnvVarEntry",
    "scan_env_file",
    "scan_env_files",
]
