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
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import requests
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

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


@dataclass(frozen=True)
class MarkerSkipped:
    """A transitive dep dropped because its PEP 508 marker did not apply.

    Surfaced so a dep that IS installed on the project's real runtime target —
    but not on the scanner host — does not vanish silently. Without this a Windows
    dep gated by ``sys_platform=="win32"`` (or an old-Python / extras marker)
    evaluates False on a macOS/Linux CI runner and is never fetched or scanned: a
    silent false-negative on the tool's core surface. The CLI turns these into
    ``UnscannedPackage`` (reason ``marker_skipped:<marker>``) so the coverage gap
    is visible in ``--json``.
    """

    name: str
    ecosystem: str
    marker: str
    via_path: tuple[str, ...] = field(default_factory=tuple)


class ResolverError(RuntimeError):
    """Raised when no supported manifest is found or a manifest is malformed."""


def resolve(
    project_root: Path,
    *,
    target_python: str | None = None,
    target_platform: str | None = None,
    marker_skipped: list[MarkerSkipped] | None = None,
) -> list[ResolvedPackage]:
    """Resolve a project's transitive dependency tree.

    Probes ``project_root`` for known manifests in priority order and returns
    a de-duplicated, stably ordered list across all detected ecosystems.
    Multiple ecosystems may be present (a polyglot repo); we resolve each.

    ``target_python`` / ``target_platform`` override the environment PEP 508
    markers are evaluated against, so a project whose runtime target differs from
    the scanner host (e.g. auditing a Windows install from a Linux CI runner) can
    be resolved correctly instead of dropping host-inapplicable deps. They default
    to the host interpreter/OS. ``marker_skipped``, if supplied, is appended with a
    ``MarkerSkipped`` for every transitive dep dropped by a non-applicable marker,
    so the CLI can surface the coverage gap as an ``UnscannedPackage`` rather than
    letting the dep vanish silently.
    """
    root = project_root.resolve()
    if not root.exists():
        raise ResolverError(f"project root does not exist: {root}")

    target_env = _build_target_environment(target_python, target_platform)
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
        for pkg in _resolve_requirements_txt(
            requirements, target_env=target_env, marker_skipped=marker_skipped
        ):
            seen.setdefault((pkg.ecosystem, pkg.name, pkg.version), pkg)
    elif pyproject.exists() and _pyproject_has_project_deps(pyproject):
        found_manifest = True
        for pkg in _resolve_pyproject_project_deps(
            pyproject, target_env=target_env, marker_skipped=marker_skipped
        ):
            seen.setdefault((pkg.ecosystem, pkg.name, pkg.version), pkg)

    if not found_manifest:
        raise ResolverError(
            f"no supported manifest found in {root}: expected one of "
            "package-lock.json, package.json, poetry.lock, requirements.txt, pyproject.toml"
        )

    return sorted(seen.values(), key=lambda p: (p.ecosystem, p.name, p.version))


def _build_target_environment(
    target_python: str | None, target_platform: str | None
) -> dict[str, str] | None:
    """Build a PEP 508 marker environment overriding the host where requested.

    Returns ``None`` when no override is asked for (markers evaluate against the
    host, the historical behaviour). Otherwise starts from the host's default
    environment and overrides the keys implied by ``target_platform`` (one of
    ``windows`` / ``linux`` / ``darwin`` / ``win32`` …) and ``target_python``
    (e.g. ``3.8``), so a cross-platform install can be audited.
    """
    if not target_python and not target_platform:
        return None

    from packaging.markers import default_environment

    env = dict(default_environment())

    if target_platform:
        plat = target_platform.strip().lower()
        sys_platform, platform_system = _platform_aliases(plat)
        if sys_platform:
            env["sys_platform"] = sys_platform
        if platform_system:
            env["platform_system"] = platform_system
        env["os_name"] = "nt" if platform_system == "Windows" else "posix"

    if target_python:
        py = target_python.strip()
        env["python_version"] = ".".join(py.split(".")[:2]) if py else env.get(
            "python_version", ""
        )
        env["python_full_version"] = py

    return env


def _platform_aliases(plat: str) -> tuple[str | None, str | None]:
    """Map a friendly platform name to (sys_platform, platform_system) values."""
    table = {
        "windows": ("win32", "Windows"),
        "win32": ("win32", "Windows"),
        "win": ("win32", "Windows"),
        "linux": ("linux", "Linux"),
        "darwin": ("darwin", "Darwin"),
        "macos": ("darwin", "Darwin"),
        "mac": ("darwin", "Darwin"),
        "osx": ("darwin", "Darwin"),
    }
    return table.get(plat, (plat, plat.capitalize() if plat else None))


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
    # BFS so via_path stays shallow. We carry the RAW spec (not a pre-stripped
    # hint) so _resolve_npm_dist_tag can tell an exact version from a range/tag
    # and resolve a range to a real published version instead of fetching a
    # non-version like "1.2" or "1.x" (which 404s and leaves the dep unscanned).
    queue: list[tuple[str, str, tuple[str, ...]]] = [
        (name, spec, (root_name,)) for name, spec in direct.items()
    ]
    max_depth = 6  # bounded to avoid runaway network walks without a lockfile
    while queue:
        name, spec, via = queue.pop(0)
        if len(via) > max_depth:
            continue
        if _is_non_registry_spec(spec):
            # workspace:/npm:/file:/git+/http(s) deps aren't fetchable from the
            # registry by version — skip rather than 404 on a bogus "version".
            continue
        version = _resolve_npm_dist_tag(session, name, spec)
        if not version:
            continue
        if (name, version) in visited:
            continue
        visited.add((name, version))
        yield ResolvedPackage(
            name=name, version=version, ecosystem="npm", via_path=(*via, name)
        )
        sub = _fetch_npm_dependencies(session, name, version)
        for sub_name, sub_spec in sub.items():
            queue.append((sub_name, sub_spec, (*via, name)))


_EXACT_NPM_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+].*)?$")
_NON_REGISTRY_PREFIXES = (
    "workspace:",
    "npm:",
    "file:",
    "link:",
    "git+",
    "git:",
    "github:",
    "http://",
    "https://",
    "portal:",
)


def _is_non_registry_spec(spec: str) -> bool:
    """True for specs that name a non-registry source (no version to fetch)."""
    s = (spec or "").strip().lower()
    if not s:
        return False
    if s.startswith(_NON_REGISTRY_PREFIXES):
        return True
    # "user/repo" or "user/repo#ref" GitHub shorthand (no @, has a slash, not scoped).
    if "/" in s and not s.startswith("@") and not s[0].isdigit() and "://" not in s:
        head = s.split("/", 1)[0]
        if head and not head[0] == "^" and not head[0] == "~":
            return True
    return False


def _resolve_npm_dist_tag(session: requests.Session, name: str, spec: str) -> str | None:
    """Resolve an npm spec to a concrete published version.

    * An exact ``x.y.z`` (optionally with prerelease/build) is returned as-is.
    * A dist-tag (``latest``, ``next``, …) or empty spec resolves via the
      registry ``dist-tags`` map.
    * A range (``^1.2.3``, ``~1.2``, ``1.x``, ``>=1.0 <2.0``, …) resolves to the
      highest PUBLISHED version satisfying the range — mirroring the PyPI ``~=``
      max-satisfying lookup — instead of being passed through verbatim as a
      bogus "version" that 404s and leaves the dep unscanned.
    """
    spec = (spec or "").strip()
    if _EXACT_NPM_VERSION_RE.match(spec):
        return spec
    if not spec or spec[0].isalpha():
        # Empty spec or a bare dist-tag word (latest/next/...).
        try:
            resp = session.get(f"{NPM_REGISTRY}/{name}", timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            tags = resp.json().get("dist-tags", {})
            return tags.get(spec or "latest")
        except requests.RequestException:
            return None
    # Anything else is a range/version-ish spec — resolve max-satisfying.
    return _resolve_npm_max_satisfying(session, name, spec)


def _resolve_npm_max_satisfying(
    session: requests.Session, name: str, spec: str
) -> str | None:
    """Highest published npm version satisfying ``spec`` (``None`` if none/unreachable)."""
    try:
        resp = session.get(f"{NPM_REGISTRY}/{name}", timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        doc = resp.json()
    except requests.RequestException:
        return None

    versions = list((doc.get("versions") or {}).keys())
    if not versions:
        # Fall back to the dist-tag map if the doc has no versions block.
        return (doc.get("dist-tags") or {}).get("latest")

    matcher = _npm_range_matcher(spec)
    candidates: list[Version] = []
    for ver_str in versions:
        try:
            ver = Version(ver_str)
        except Exception:
            continue
        if ver.is_prerelease:
            # npm excludes prereleases from range resolution unless the range
            # itself pins one; we keep the common (non-prerelease) behaviour.
            continue
        if matcher(ver):
            candidates.append(ver)
    if candidates:
        return str(max(candidates))
    # No published version matched the range — fall back to the dist-tag latest
    # so the dep is still scanned at *some* real version rather than dropped.
    return (doc.get("dist-tags") or {}).get("latest")


def _npm_range_matcher(spec: str):
    """Compile an npm range spec into a predicate over ``packaging.Version``.

    Supports the common npm range syntax: caret (``^``), tilde (``~``), x-ranges
    (``1.x`` / ``1.2.*``), comparator sets (``>=1.0.0 <2.0.0``), the ``||`` OR of
    several ranges, ``*``/``""`` (any), and exact versions. Translates each into a
    ``packaging.SpecifierSet`` and ORs the alternatives. Falls back to "match
    anything" on an unparseable spec so a weird range never silently drops the dep.
    """
    spec = (spec or "").strip()
    if spec in ("", "*", "x", "X", "latest"):
        return lambda v: True

    alternatives = [alt.strip() for alt in spec.split("||")]
    specifier_sets: list[SpecifierSet] = []
    matched_any = False
    for alt in alternatives:
        try:
            specifier_sets.append(_npm_alt_to_specifierset(alt))
            matched_any = True
        except Exception:
            continue
    if not matched_any:
        return lambda v: True

    def _match(v: Version) -> bool:
        return any(v in ss for ss in specifier_sets)

    return _match


def _npm_alt_to_specifierset(alt: str) -> SpecifierSet:
    """Translate ONE npm range alternative into a packaging SpecifierSet."""
    alt = alt.strip()
    if alt in ("", "*", "x", "X"):
        return SpecifierSet("")

    # Caret: ^1.2.3 -> >=1.2.3,<2.0.0 ; ^0.2.3 -> >=0.2.3,<0.3.0 ; ^0.0.3 -> >=0.0.3,<0.0.4
    if alt.startswith("^"):
        major, minor, patch = _npm_version_parts(alt[1:])
        lower = f"{major}.{minor}.{patch}"
        if major > 0:
            upper = f"{major + 1}.0.0"
        elif minor > 0:
            upper = f"0.{minor + 1}.0"
        else:
            upper = f"0.0.{patch + 1}"
        return SpecifierSet(f">={lower},<{upper}")

    # Tilde: ~1.2.3 -> >=1.2.3,<1.3.0 ; ~1.2 -> >=1.2.0,<1.3.0 ; ~1 -> >=1.0.0,<2.0.0
    if alt.startswith("~"):
        body = alt[1:]
        major, minor, patch = _npm_version_parts(body)
        explicit = [p for p in body.replace("-", ".").split(".") if p != ""]
        lower = f"{major}.{minor}.{patch}"
        if len(explicit) >= 2:
            upper = f"{major}.{minor + 1}.0"
        else:
            upper = f"{major + 1}.0.0"
        return SpecifierSet(f">={lower},<{upper}")

    # Comparator set: ">=1.0.0 <2.0.0" (space-separated AND).
    if any(op in alt for op in (">", "<", "=")):
        parts = [p for p in alt.split() if p]
        translated = []
        for p in parts:
            p = p.strip()
            if p.startswith(("<=", ">=", "<", ">", "==", "=")):
                if p.startswith("="):
                    p = "==" + p.lstrip("=")
                translated.append(p)
        if translated:
            return SpecifierSet(",".join(translated))

    # X-range: 1.x / 1.2.x / 1.* / 1.2.* — translate to a >=,< band.
    if "x" in alt.lower() or "*" in alt:
        norm = alt.lower().replace("*", "x")
        segs = norm.split(".")
        if segs and segs[0] not in ("x", ""):
            major = int(segs[0])
            if len(segs) >= 2 and segs[1] not in ("x", ""):
                minor = int(segs[1])
                return SpecifierSet(f">={major}.{minor}.0,<{major}.{minor + 1}.0")
            return SpecifierSet(f">={major}.0.0,<{major + 1}.0.0")
        return SpecifierSet("")  # leading x -> any

    # Bare partial version: "1" -> >=1.0.0,<2.0.0 ; "1.2" -> >=1.2.0,<1.3.0.
    segs = [s for s in alt.replace("-", ".").split(".") if s != ""]
    if segs and all(s.isdigit() for s in segs[:3]):
        major, minor, patch = _npm_version_parts(alt)
        if len(segs) == 1:
            return SpecifierSet(f">={major}.0.0,<{major + 1}.0.0")
        if len(segs) == 2:
            return SpecifierSet(f">={major}.{minor}.0,<{major}.{minor + 1}.0")
        return SpecifierSet(f"=={major}.{minor}.{patch}")

    # Unknown shape — let packaging try, else raise to the OR-fallback.
    return SpecifierSet(f"=={alt}")


def _npm_version_parts(body: str) -> tuple[int, int, int]:
    """Parse the leading major.minor.patch ints from a (partial) version body."""
    core = body.split("+", 1)[0].split("-", 1)[0]
    nums = core.split(".")
    major = int(nums[0]) if len(nums) >= 1 and nums[0].isdigit() else 0
    minor = int(nums[1]) if len(nums) >= 2 and nums[1].isdigit() else 0
    patch = int(nums[2]) if len(nums) >= 3 and nums[2].isdigit() else 0
    return major, minor, patch


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


def _resolve_requirements_txt(
    req_file: Path,
    *,
    target_env: dict[str, str] | None = None,
    marker_skipped: list[MarkerSkipped] | None = None,
) -> Iterable[ResolvedPackage]:
    """Parse pinned ``requirements.txt`` entries and walk PyPI transitively.

    ``name==version`` and ``name===version`` are exact pins (returned as-is).
    ``name~=version`` is a compatible-release pin resolved to the highest PyPI
    release satisfying the full specifier (so we audit the version a real
    install picks, not the registry latest). Anything looser (``>=``, ``<``,
    ranges, wildcards, bare names) is left unpinned and sent through the PyPI
    walk to resolve to the latest release.
    """
    session = _http_session()
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
        version = _pin_from_specifier(req, session)
        direct.append((canonicalize_name(req.name), version))

    yield from _walk_pypi(
        direct,
        root_name="<root>",
        session=session,
        target_env=target_env,
        marker_skipped=marker_skipped,
    )


def _pyproject_has_project_deps(pyproject: Path) -> bool:
    import tomllib

    try:
        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        return False
    return bool(data.get("project", {}).get("dependencies"))


def _resolve_pyproject_project_deps(
    pyproject: Path,
    *,
    target_env: dict[str, str] | None = None,
    marker_skipped: list[MarkerSkipped] | None = None,
) -> Iterable[ResolvedPackage]:
    import tomllib

    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    deps = data.get("project", {}).get("dependencies", []) or []
    session = _http_session()
    direct: list[tuple[str, str | None]] = []
    for raw in deps:
        try:
            req = Requirement(raw)
        except Exception:
            continue
        direct.append((canonicalize_name(req.name), _pin_from_specifier(req, session)))
    yield from _walk_pypi(
        direct,
        root_name="<root>",
        session=session,
        target_env=target_env,
        marker_skipped=marker_skipped,
    )


def _pin_from_specifier(
    req: Requirement, session: requests.Session | None = None
) -> str | None:
    """Return a concrete version for an exact (``==``/``===``) or compatible-
    release (``~=``) pin; ``None`` for anything looser.

    For ``==``/``===`` the specifier already names the version. For ``~=`` (e.g.
    ``~=1.4.2`` means ``>=1.4.2, ==1.4.*``) we resolve the highest version from
    the PyPI releases list that satisfies the FULL specifier — so we audit the
    version a real ``pip install`` would resolve to, not the registry latest,
    which may fall outside the compatible range (e.g. ``2.0.0`` for ``~=1.4.2``).
    Looser specifiers (``>=``, ``<``, ranges, wildcards, bare names) return
    ``None`` and fall through to the PyPI walk's latest-resolution path.
    """
    has_tilde = False
    for spec in req.specifier:
        if spec.operator in ("==", "==="):
            return spec.version
        if spec.operator == "~=":
            has_tilde = True
    if has_tilde and session is not None:
        return _resolve_pypi_max_satisfying(session, req.name, req.specifier)
    return None


def _resolve_pypi_max_satisfying(
    session: requests.Session, name: str, specifier: SpecifierSet
) -> str | None:
    """Highest PyPI release version satisfying ``specifier`` (``None`` if none / unreachable).

    Mirrors what ``pip install`` picks for a ``~=`` / range specifier: the newest
    non-prerelease release inside the constraint. Used so the scanner audits the
    version a real install resolves to, instead of the registry latest (which
    may violate the specifier — the core "audit the version you actually
    install" promise).
    """
    try:
        resp = session.get(f"{PYPI_REGISTRY}/{name}/json", timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return None
        releases = (resp.json().get("releases") or {})
    except requests.RequestException:
        return None

    candidates: list[Version] = []
    for ver_str in releases:
        try:
            ver = Version(ver_str)
        except Exception:  # invalid / non-pep440 release tag
            continue
        # SpecifierSet.__contains__ excludes prereleases unless the specifier
        # itself references one — matching pip's default resolution.
        if ver in specifier:
            candidates.append(ver)
    if not candidates:
        return None
    return str(max(candidates))


def _walk_pypi(
    direct: list[tuple[str, str | None]],
    root_name: str,
    *,
    session: requests.Session | None = None,
    target_env: dict[str, str] | None = None,
    marker_skipped: list[MarkerSkipped] | None = None,
) -> Iterable[ResolvedPackage]:
    if session is None:
        session = _http_session()
    visited: set[tuple[str, str]] = set()
    skipped_seen: set[tuple[str, str]] = set()
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
            sub_name = canonicalize_name(req.name)
            if req.marker and not _marker_applies(req.marker, target_env):
                # The dep is skipped on this (host or target) environment, but it
                # IS installed on some real environment. Record it as a coverage
                # gap so the CLI can surface it instead of dropping it silently.
                if marker_skipped is not None:
                    skip_key = (sub_name, str(req.marker))
                    if skip_key not in skipped_seen:
                        skipped_seen.add(skip_key)
                        marker_skipped.append(
                            MarkerSkipped(
                                name=sub_name,
                                ecosystem="pypi",
                                marker=str(req.marker),
                                via_path=(*via, name, sub_name),
                            )
                        )
                continue
            sub_version = _pin_from_specifier(req, session)
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


def _marker_applies(
    marker: Marker, target_env: dict[str, str] | None = None
) -> bool:
    """Evaluate a PEP 508 marker against the target (or host) environment.

    ``target_env`` overrides the host environment so a cross-platform install can
    be audited (e.g. resolve win32-gated deps from a Linux runner). When a marker
    references an extra we cannot evaluate without an extras context; we retry once
    supplying ``extra=""`` so a pure host/OS/python marker still resolves, and only
    then fall back to ``False`` (the caller records these as a coverage gap).
    """
    try:
        return bool(marker.evaluate(target_env) if target_env else marker.evaluate())
    except Exception:
        # Likely an ``extra == "..."`` marker with no extras context. Retry with
        # an empty extra so the host/OS/python half of a compound marker can still
        # be evaluated; if that also fails, drop the branch (the caller records it
        # as marker_skipped so the gap is visible, not silent).
        try:
            env = dict(target_env or {})
            env.setdefault("extra", "")
            return bool(marker.evaluate(env))
        except Exception:
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
