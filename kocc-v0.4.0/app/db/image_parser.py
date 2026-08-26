from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ParsedImage:
    registry: str
    repository: str
    tag: str | None
    digest: str | None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


def parse_image_reference(image: str) -> ParsedImage:
    """Parse OCI/Docker references without resolving or contacting a registry."""
    reference = image.strip()
    name, separator, digest = reference.partition("@")
    last_slash = name.rfind("/")
    last_colon = name.rfind(":")
    tag = name[last_colon + 1:] if last_colon > last_slash else None
    if tag is not None:
        name = name[:last_colon]
    parts = name.split("/") if name else []
    first = parts[0] if parts else ""
    has_registry = len(parts) > 1 and (
        "." in first or ":" in first or first == "localhost"
    )
    registry = first if has_registry else "docker.io"
    repository_parts = parts[1:] if has_registry else parts
    if not has_registry and len(repository_parts) == 1:
        repository_parts.insert(0, "library")
    return ParsedImage(
        registry=registry,
        repository="/".join(repository_parts),
        tag=tag,
        digest=digest if separator else None,
    )
