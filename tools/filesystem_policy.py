"""Filesystem boundary and basic sensitive-file policy."""

from collections.abc import Iterable
from pathlib import Path

SENSITIVE_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "known_hosts",
}
SENSITIVE_SUFFIXES = {".key", ".p12", ".pfx", ".pem"}
SENSITIVE_TERMS = {"credential", "private_key", "secret", "token"}


class PathOutsideApprovedRoots(ValueError):
    pass


def canonical_roots(roots: Iterable[str | Path]) -> tuple[Path, ...]:
    return tuple(Path(root).expanduser().resolve() for root in roots)


def resolve_approved_path(
    path: str | Path,
    roots: Iterable[str | Path],
    *,
    must_exist: bool = True,
) -> Path:
    candidate = Path(path).expanduser().resolve(strict=must_exist)
    approved = canonical_roots(roots)
    if not any(
        candidate == root or candidate.is_relative_to(root) for root in approved
    ):
        raise PathOutsideApprovedRoots("Path is outside Cato's approved roots.")
    return candidate


def is_sensitive_path(path: str | Path) -> bool:
    candidate = Path(path)
    name = candidate.name.lower()
    if name in SENSITIVE_NAMES or candidate.suffix.lower() in SENSITIVE_SUFFIXES:
        return True
    return any(term in name for term in SENSITIVE_TERMS)
