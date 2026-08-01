"""Service version diff engine.

Compares the Docker service images of two
:class:`~envcheck.profile.EnvironmentProfile` instances (e.g. ``dev`` vs
``staging``) and classifies every service as one of:

- **match** — present in both profiles with the same image name and tag
- **version_changed** — same image, different version tag
  (e.g. ``postgres:16`` vs ``postgres:15``)
- **image_changed** — a completely different image
  (e.g. ``postgres`` vs ``mysql``)
- **missing** — present in the base profile but absent from the target
- **extra** — present in the target profile but absent from the base

The engine operates on :attr:`EnvironmentProfile.docker_services`, the
service-name → image-string view aggregated from every scanned
docker-compose file.  An optional ``tracked`` mapping (from the
``services:`` section of ``.envcheck.yaml``) restricts the comparison to
a declared set of services and honors per-service options such as
``version_field`` and ``path``.

The main entrypoint is :func:`diff_services`.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from envcheck.config import ServiceConfig
from envcheck.profile import EnvironmentProfile

# ---------------------------------------------------------------------------
# Image parsing
# ---------------------------------------------------------------------------


class ParsedImage(BaseModel):
    """A Docker image reference split into its components.

    Supports the common forms:

    - ``postgres:16`` — name + tag
    - ``nginx:1.25-alpine`` — name + tag (any tag string)
    - ``postgres`` — name only (implicitly ``latest``)
    - ``ghcr.io/org/app:2.1`` — registry-qualified name + tag
    - ``localhost:5000/postgres:16`` — registry with port + name + tag
    - ``postgres@sha256:abc…`` — name + digest (no tag)
    """

    name: str = Field(description="Repository/image name (e.g. postgres)")
    tag: Optional[str] = Field(
        default=None, description="Version tag (None if not present)"
    )
    digest: Optional[str] = Field(
        default=None, description="Digest reference (None if not present)"
    )

    @property
    def version(self) -> str:
        """The effective version: tag, else digest, else ``latest``."""
        if self.tag is not None:
            return self.tag
        if self.digest is not None:
            return self.digest
        return "latest"


def parse_image(image: str) -> ParsedImage:
    """Parse a Docker image reference into :class:`ParsedImage`.

    Parameters
    ----------
    image:
        Raw image string from a docker-compose ``image:`` field.

    Returns
    -------
    ParsedImage
        The parsed components.

    Raises
    ------
    ValueError
        The image reference is empty or whitespace-only.
    """
    raw = image.strip()
    if not raw:
        raise ValueError("Empty Docker image reference")

    # --- digest: everything after the first '@' ---
    digest: Optional[str] = None
    if "@" in raw:
        raw, digest = raw.split("@", 1)

    # --- tag: the last ':' that appears after the final '/' ---
    # This keeps registry ports (localhost:5000/img) out of the tag slot.
    tag: Optional[str] = None
    last_slash = raw.rfind("/")
    last_colon = raw.rfind(":")
    if last_colon > last_slash:
        tag = raw[last_colon + 1 :]
        raw = raw[:last_colon]

    return ParsedImage(name=raw, tag=tag, digest=digest)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class ServiceDiffKind(str, Enum):
    """Classification of a single Docker service across two profiles."""

    MATCH = "match"
    VERSION_CHANGED = "version_changed"
    IMAGE_CHANGED = "image_changed"
    MISSING = "missing"
    EXTRA = "extra"


class ServiceDiff(BaseModel):
    """The diff state of a single Docker service between two profiles."""

    service: str = Field(description="Compose service name")
    kind: ServiceDiffKind = Field(description="How the service differs between profiles")
    base_image: Optional[str] = Field(
        default=None, description="Image in the base environment (None if extra)"
    )
    target_image: Optional[str] = Field(
        default=None, description="Image in the target environment (None if missing)"
    )
    base_version: Optional[str] = Field(
        default=None, description="Effective version in the base environment"
    )
    target_version: Optional[str] = Field(
        default=None, description="Effective version in the target environment"
    )
    base_source: Optional[str] = Field(
        default=None, description="Compose file that defined the service in the base environment"
    )
    target_source: Optional[str] = Field(
        default=None, description="Compose file that defined the service in the target environment"
    )


class ServiceDiffResult(BaseModel):
    """Result of comparing the Docker services of two profiles.

    ``diffs`` contains one :class:`ServiceDiff` per service in the union
    of both profiles (sorted by service name), including matching
    services so a reporter can render green "ok" rows alongside drift.
    """

    base_env: str = Field(description="Name of the base environment (e.g. dev)")
    target_env: str = Field(description="Name of the target environment (e.g. staging)")
    diffs: List[ServiceDiff] = Field(
        default_factory=list,
        description="One ServiceDiff per service in the union of both profiles",
    )

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def matches(self) -> List[ServiceDiff]:
        """Services present in both profiles with identical image + version."""
        return [d for d in self.diffs if d.kind == ServiceDiffKind.MATCH]

    @property
    def version_changed(self) -> List[ServiceDiff]:
        """Services whose image tag differs (e.g. postgres:16 → postgres:15)."""
        return [d for d in self.diffs if d.kind == ServiceDiffKind.VERSION_CHANGED]

    @property
    def image_changed(self) -> List[ServiceDiff]:
        """Services using a completely different image (e.g. postgres → mysql)."""
        return [d for d in self.diffs if d.kind == ServiceDiffKind.IMAGE_CHANGED]

    @property
    def missing(self) -> List[ServiceDiff]:
        """Services present in the base profile but absent from the target."""
        return [d for d in self.diffs if d.kind == ServiceDiffKind.MISSING]

    @property
    def extra(self) -> List[ServiceDiff]:
        """Services present in the target profile but absent from the base."""
        return [d for d in self.diffs if d.kind == ServiceDiffKind.EXTRA]

    @property
    def has_drift(self) -> bool:
        """True if any service is not an exact match."""
        return any(d.kind != ServiceDiffKind.MATCH for d in self.diffs)

    @property
    def drift_count(self) -> int:
        """Number of services that are not exact matches."""
        return len(self.diffs) - len(self.matches)

    @property
    def total_compared(self) -> int:
        """Total number of services compared (union of both profiles)."""
        return len(self.diffs)


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


def _service_source(profile: EnvironmentProfile, service: str) -> Optional[str]:
    """Find the compose file that defined *service* in *profile*.

    Returns ``None`` when the profile has no Docker results or the
    service is not present.
    """
    if profile.docker_result is None:
        return None
    for cr in profile.docker_result.compose_results:
        if service in cr.services:
            return cr.source
    return None


def _matches_path(source: Optional[str], expected: str) -> bool:
    """True if *source* (an absolute compose path) matches *expected*.

    *expected* may be absolute or relative; relative paths are resolved
    against the current working directory.
    """
    if source is None:
        return False
    return Path(source) == Path(expected).resolve()


def diff_services(
    base: EnvironmentProfile,
    target: EnvironmentProfile,
    tracked: Optional[Dict[str, ServiceConfig]] = None,
) -> ServiceDiffResult:
    """Compare the Docker service images of two profiles.

    Parameters
    ----------
    base:
        The reference profile (e.g. ``dev``).
    target:
        The profile being compared against the base (e.g. ``staging``).
    tracked:
        Optional map of service name → :class:`~envcheck.config.ServiceConfig`
        (from the ``services:`` section of ``.envcheck.yaml``).  When
        provided, only these services are compared, and per-service
        options are honored:

        - ``version_field`` — must be ``"image"`` (the only field the
          Docker scanner extracts); anything else raises ``ValueError``.
        - ``path`` — when set, the service is only considered if it is
          defined in that compose file.

        When omitted, every service found in either profile's Docker
        results is compared.

    Returns
    -------
    ServiceDiffResult
        One :class:`ServiceDiff` per compared service, sorted by name.

    Raises
    ------
    ValueError
        A tracked service declares an unsupported ``version_field``.
    """
    if tracked:
        for svc, cfg in tracked.items():
            if cfg.version_field != "image":
                raise ValueError(
                    f"Service {svc!r}: version_field {cfg.version_field!r} is not "
                    f"supported by the Docker scanner — only 'image' is available"
                )

    base_services = base.docker_services
    target_services = target.docker_services

    if tracked:
        service_names = sorted(tracked.keys())
    else:
        service_names = sorted(set(base_services) | set(target_services))

    diffs: List[ServiceDiff] = []

    for svc in service_names:
        cfg = tracked.get(svc) if tracked else None
        base_src = _service_source(base, svc)
        target_src = _service_source(target, svc)

        # Apply the optional per-service path restriction
        if cfg is not None and cfg.path is not None:
            in_base = svc in base_services and _matches_path(base_src, cfg.path)
            in_target = svc in target_services and _matches_path(target_src, cfg.path)
        else:
            in_base = svc in base_services
            in_target = svc in target_services

        if in_base and in_target:
            base_image = base_services[svc]
            target_image = target_services[svc]
            base_parsed = parse_image(base_image)
            target_parsed = parse_image(target_image)

            if base_parsed.name != target_parsed.name:
                kind = ServiceDiffKind.IMAGE_CHANGED
            elif base_parsed.version != target_parsed.version:
                kind = ServiceDiffKind.VERSION_CHANGED
            else:
                kind = ServiceDiffKind.MATCH

            diffs.append(
                ServiceDiff(
                    service=svc,
                    kind=kind,
                    base_image=base_image,
                    target_image=target_image,
                    base_version=base_parsed.version,
                    target_version=target_parsed.version,
                    base_source=base_src,
                    target_source=target_src,
                )
            )
        elif in_base:
            base_image = base_services[svc]
            diffs.append(
                ServiceDiff(
                    service=svc,
                    kind=ServiceDiffKind.MISSING,
                    base_image=base_image,
                    base_version=parse_image(base_image).version,
                    base_source=base_src,
                )
            )
        elif in_target:
            target_image = target_services[svc]
            diffs.append(
                ServiceDiff(
                    service=svc,
                    kind=ServiceDiffKind.EXTRA,
                    target_image=target_image,
                    target_version=parse_image(target_image).version,
                    target_source=target_src,
                )
            )
        # else: restricted out by a path filter → not compared

    return ServiceDiffResult(base_env=base.name, target_env=target.name, diffs=diffs)
