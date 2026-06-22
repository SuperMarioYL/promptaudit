"""Transitive dependency resolver for npm and PyPI projects.

Reads the manifest/lockfile under a project root and returns a flat list of
``ResolvedPackage`` entries representing the full transitive dependency tree.
Preference order per ecosystem:

* npm: ``package-lock.json`` (authoritative, fully resolved tree). Falls back
  to ``package.json`` + live npm registry walk.
* PyPI: ``poetry.lock`` (fully pinned). Falls back to ``requirements.txt``
  pinned entries; for any unpinned tail, walks the PyPI JSON API recursively
  honouring PEP 508 markers via ``packaging``.

A null ``requires_dist`` on PyPI means "no runtime deps" (per the build note in
mvp_plan.md) and is treated as an empty list, not as an error.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import requests
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

NPM_REGISTRY = "https://registry.npmjs.org"
PYPI_REGISTRY = "https://pypi.org/pypi"
HTTP_TIMEOUT = 15
USER_AGENT = "promptaudit/0.1 (+https://github.com/supermario-leo/promptaudit)"


@dataclass(frozen=True)
class ResolvedPackage:
    """A single concrete dependency to scan."""

    name: str
    version: str
    ecosystem: str  # "npm" | "pypi"
    via_path: tuple[str, ...] = field(default_factory=tuple)

    @property
    def cache_key(self) -> str:
        # Filesystem-safe, ecosystem-namespaced. PyPI canonicalises name.
        norm = self.name.replace("/", "__")
        return f"{self.ecosystem}/{norm}/{self.version}"


class ResolverError(RuntimeError):
    """Raised when no supported manifest is found or a manifest is malformed."""


def resolve(project_root: Path) -> list[ResolvedPackage]:
    """Resolve a project's transitive dependency tree.

    Probes ``project_root`` for known manifests in priority order and returns
    a de-duplicated, stably ordered list across all detected ecosystems.
    Multiple ecosystems may be present (a polyglot repo); we resolve each.
    """
    root = project_root.resolve()
    if not root.exists():
        raise ResolverError(f"project root does not exist: {root}")

    seen: dict[tuple[str, str, str], ResolvedPackage] = {}
    found_manifest = False

    npm_lock = root / "package-lock.json"
    npm_pkg = root / "package.json"
    poetry_lock = root / "poetry.lock"
    requirements = root / "requirements.txt"
    pyproject = root / "pyproject.toml"

    if npm_lock.exists():
        found_manifest = True
        for pkg in _resolve_npm_lockfile(npm_lock):
            seen.setdefault((pkg.ecosystem, pkg.name, pkg.version), pkg)
    elif npm_pkg.exists():
        found_manifest = True
        for pkg in _resolve_npm_package_json(npm_pkg):
            seen.setdefault((pkg.ecosystem, pkg.name, pkg.version), pkg)

    if poetry_lock.exists():
        found_manifest = True
        for pkg in _resolve_poetry_lock(poetry_lock):
            seen.setdefault((pkg.ecosystem, pkg.name, pkg.version), pkg)
    elif requirements.exists():
        found_manifest = True
        for pkg in _resolve_requirements_txt(requirements):
            seen.setdefault((pkg.ecosystem, pkg.name, pkg.version), pkg)
    elif pyproject.exists() and _pyproject_has_project_deps(pyproject):
        found_manifest = True
        for pkg in _resolve_pyproject_project_deps(pyproject):
            seen.setdefault((pkg.ecosystem, pkg.name, pkg.version), pkg)

    if not found_manifest:
        raise ResolverError(
            f"no supported manifest found in {root}: expected one of "
            "package-lock.json, package.json, poetry.lock, requirements.txt, pyproject.toml"
        )

    return sorted(seen.values(), key=lambda p: (p.ecosystem, p.name, p.version))


# ---------- npm ----------------------------------------------------------------


def _resolve_npm_lockfile(lockfile: Path) -> Iterable[ResolvedPackage]:
    """Walk an npm v1/v2/v3 package-lock.json.

    Lockfile v2/v3 use the top-level ``packages`` map keyed by node_modules
    path; lockfile v1 uses nested ``dependencies``. We handle both.
    """
    data = _load_json(lockfile)
    lockfile_version = data.get("lockfileVersion", 1)
    root_name = data.get("name", "<root>")

    if "packages" in data and lockfile_version >= 2:
        yield from _walk_lockfile_v2(data["packages"], root_name)
    elif "dependencies" in data:
        yield from _walk_lockfile_v1(data["dependencies"], parent_path=(root_name,))
    # else: empty lockfile, nothing to yield


def _walk_lockfile_v2(packages: dict, root_name: str) -> Iterable[ResolvedPackage]:
    for path_key, meta in packages.items():
        if path_key == "":
            # The root project itself; not a dependency.
            continue
        if meta.get("dev") or meta.get("peer") or meta.get("optional"):
            # v0.1 scope: runtime deps only. devDeps are not pulled at install.
            continue
        version = meta.get("version")
        if not version:
            continue
        # path_key is e.g. "node_modules/foo" or "node_modules/foo/node_modules/bar"
        segments = [seg for seg in path_key.split("node_modules/") if seg]
        name = segments[-1].rstrip("/")
        via_path = (root_name, *(seg.rstrip("/") for seg in segments))
        yield ResolvedPackage(name=name, version=version, ecosystem="npm", via_path=via_path)


def _walk_lockfile_v1(
    deps: dict, parent_path: tuple[str, ...]
) -> Iterable[ResolvedPackage]:
    for name, meta in deps.items():
        if meta.get("dev") or meta.get("peer") or meta.get("optional"):
            # v0.1 scope: runtime deps only — mirror _walk_lockfile_v2 so v1
            # and v2 lockfiles share runtime-only semantics (don't resolve,
            # fetch, or scan dev/peer/optional deps that aren't installed at
            # runtime, which could otherwise trip a spurious critical exit-1).
            continue
        version = meta.get("version")
        if version:
            yield ResolvedPackage(
                name=name,
                version=version,
                ecosystem="npm",
                via_path=(*parent_path, name),
            )
        nested = meta.get("dependencies")
        if isinstance(nested, dict):
            yield from _walk_lockfile_v1(nested, (*parent_path, name))


def _resolve_npm_package_json(manifest: Path) -> Iterable[ResolvedPackage]:
    """No lockfile path: take direct deps from package.json and walk npm registry.

    This is the slow path. Recursion depth is capped to keep the network walk
    bounded; users without a lockfile get a best-effort tree.
    """
    data = _load_json(manifest)
    root_name = data.get("name", "<root>")
    direct = data.get("dependencies", {}) or {}
    if not direct:
        return

    visited: set[tuple[str, str]] = set()
    session = _http_session()
    # BFS so via_path stays shallow.
    queue: list[tuple[str, str, tuple[str, ...]]] = [
        (name, _strip_npm_range(spec), (root_name,)) for name, spec in direct.items()
    ]
    max_depth = 6  # bounded to avoid runaway network walks without a lockfile
    while queue:
        name, spec, via = queue.pop(0)
        if len(via) > max_depth:
            continue
        version = _resolve_npm_dist_tag(session, name, spec) or spec
        if (name, version) in visited:
            continue
        visited.add((name, version))
        yield ResolvedPackage(
            name=name, version=version, ecosystem="npm", via_path=(*via, name)
        )
        sub = _fetch_npm_dependencies(session, name, version)
        for sub_name, sub_spec in sub.items():
            queue.append((sub_name, _strip_npm_range(sub_spec), (*via, name)))


def _strip_npm_range(spec: str) -> str:
    # Handle "^1.2.3", "~1.2", ">=1.0 <2.0", "1.2.3" — we just want a hint.
    spec = (spec or "").strip()
    for prefix in ("^", "~", ">=", "<=", ">", "<", "="):
        if spec.startswith(prefix):
            spec = spec[len(prefix) :].lstrip()
            break
    return spec.split()[0].split(",")[0].strip() if spec else ""


def _resolve_npm_dist_tag(session: requests.Session, name: str, spec: str) -> str | None:
    """Resolve a tag like 'latest' or an empty spec to a concrete version."""
    if not spec or not spec[0].isdigit():
        try:
            resp = session.get(f"{NPM_REGISTRY}/{name}", timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            tags = resp.json().get("dist-tags", {})
            return tags.get(spec or "latest")
        except requests.RequestException:
            return None
    return spec


def _fetch_npm_dependencies(
    session: requests.Session, name: str, version: str
) -> dict[str, str]:
    try:
        resp = session.get(
            f"{NPM_REGISTRY}/{name}/{version}", timeout=HTTP_TIMEOUT
        )
        resp.raise_for_status()
        return resp.json().get("dependencies", {}) or {}
    except requests.RequestException:
        return {}


# ---------- PyPI ---------------------------------------------------------------


def _resolve_poetry_lock(lockfile: Path) -> Iterable[ResolvedPackage]:
    """Parse poetry.lock (TOML, stdlib tomllib in 3.11+)."""
    import tomllib  # stdlib in 3.11+

    with lockfile.open("rb") as fh:
        data = tomllib.load(fh)
    packages = data.get("package", [])
    for entry in packages:
        if entry.get("category") == "dev":
            # poetry < 1.5 marked dev deps with category; newer uses groups.
            continue
        if "dev" in (entry.get("groups") or []):
            continue
        name = entry.get("name")
        version = entry.get("version")
        if name and version:
            yield ResolvedPackage(
                name=canonicalize_name(name),
                version=str(version),
                ecosystem="pypi",
                via_path=("<root>", canonicalize_name(name)),
            )


def _resolve_requirements_txt(req_file: Path) -> Iterable[ResolvedPackage]:
    """Parse pinned ``requirements.txt`` entries and walk PyPI transitively.

    Only ``name==version`` and ``name~=version`` lines are treated as resolved.
    Anything looser is sent through the PyPI walk to pick the latest.
    """
    direct: list[tuple[str, str | None]] = []
    for raw in req_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # Strip inline comments and environment markers handled by Requirement.
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            req = Requirement(line)
        except Exception:
            continue
        version = _pin_from_specifier(req)
        direct.append((canonicalize_name(req.name), version))

    yield from _walk_pypi(direct, root_name="<root>")


def _pyproject_has_project_deps(pyproject: Path) -> bool:
    import tomllib

    try:
        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        return False
    return bool(data.get("project", {}).get("dependencies"))


def _resolve_pyproject_project_deps(pyproject: Path) -> Iterable[ResolvedPackage]:
    import tomllib

    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    deps = data.get("project", {}).get("dependencies", []) or []
    direct: list[tuple[str, str | None]] = []
    for raw in deps:
        try:
            req = Requirement(raw)
        except Exception:
            continue
        direct.append((canonicalize_name(req.name), _pin_from_specifier(req)))
    yield from _walk_pypi(direct, root_name="<root>")


def _pin_from_specifier(req: Requirement) -> str | None:
    """Return a concrete version iff the specifier is an exact pin (``==`` / ``===``)."""
    for spec in req.specifier:
        if spec.operator in ("==", "==="):
            return spec.version
    return None


def _walk_pypi(
    direct: list[tuple[str, str | None]], root_name: str
) -> Iterable[ResolvedPackage]:
    session = _http_session()
    visited: set[tuple[str, str]] = set()
    queue: list[tuple[str, str | None, tuple[str, ...]]] = [
        (name, version, (root_name,)) for name, version in direct
    ]
    max_depth = 8
    while queue:
        name, version, via = queue.pop(0)
        if len(via) > max_depth:
            continue
        info = _fetch_pypi_release(session, name, version)
        if info is None:
            continue
        resolved_version = info.get("version") or version
        if not resolved_version:
            continue
        key = (name, resolved_version)
        if key in visited:
            continue
        visited.add(key)
        yield ResolvedPackage(
            name=name,
            version=resolved_version,
            ecosystem="pypi",
            via_path=(*via, name),
        )
        # PEP 508: requires_dist is null when the package declares no deps —
        # this is "no deps", not an error (build_notes).
        requires_dist = info.get("requires_dist") or []
        for raw in requires_dist:
            try:
                req = Requirement(raw)
            except Exception:
                continue
            if req.marker and not _marker_applies(req.marker):
                continue
            sub_name = canonicalize_name(req.name)
            sub_version = _pin_from_specifier(req)
            queue.append((sub_name, sub_version, (*via, name)))


def _fetch_pypi_release(
    session: requests.Session, name: str, version: str | None
) -> dict | None:
    url = (
        f"{PYPI_REGISTRY}/{name}/{version}/json"
        if version
        else f"{PYPI_REGISTRY}/{name}/json"
    )
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return None
        return resp.json().get("info", {})
    except requests.RequestException:
        return None


def _marker_applies(marker: Marker) -> bool:
    """Evaluate a PEP 508 marker against the current interpreter (extras stripped)."""
    try:
        return bool(marker.evaluate())
    except Exception:
        # If extras are present we can't evaluate without context — drop the
        # branch rather than guess. Scanner can still catch payloads via other
        # transitive paths.
        return False


# ---------- shared -------------------------------------------------------------


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ResolverError(f"malformed JSON in {path}: {exc}") from exc


def _http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


def _cli_preview(root: str = ".") -> int:
    """Tiny preview for ``python -m promptaudit.resolver <path>``."""
    try:
        pkgs = resolve(Path(root))
    except ResolverError as exc:
        print(f"resolver error: {exc}", file=sys.stderr)
        return 2
    for pkg in pkgs:
        via = " -> ".join(pkg.via_path)
        print(f"{pkg.ecosystem}\t{pkg.name}@{pkg.version}\t{via}")
    print(f"\nresolved {len(pkgs)} packages", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_cli_preview(*sys.argv[1:2]))
