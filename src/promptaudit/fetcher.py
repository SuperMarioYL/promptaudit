"""Per-package text fetcher.

For each resolved package, pulls the corpus the scanner cares about — README,
description/summary, error strings — into a local filesystem cache at
``~/.promptaudit/cache/<ecosystem>/<name>/<version>/``.

Cache layout (per package version)::

    meta.json     # {"name", "version", "ecosystem", "fetched_at", "sources": {...}}
    readme.md     # primary README text (npm/PyPI)
    summary.txt   # short description / summary
    errors.txt    # extracted string-literal candidates from tarball/sdist (best-effort)

Build notes from mvp_plan.md that drive this module:

* npm's ``readme`` field is empty for many popular packages (e.g. express);
  if so, download ``dist.tarball`` and pull README from the tarball root.
* PyPI ``requires_dist`` is handled in resolver.py; here we just consume the
  same JSON endpoint for ``info.description`` / ``info.summary``.
"""

from __future__ import annotations

import io
import json
import re
import sys
import tarfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests

from .resolver import (
    HTTP_TIMEOUT,
    NPM_REGISTRY,
    PYPI_REGISTRY,
    USER_AGENT,
    ResolvedPackage,
)

DEFAULT_CACHE_ROOT = Path.home() / ".promptaudit" / "cache"
MAX_TARBALL_BYTES = 8 * 1024 * 1024  # don't pull >8MB tarballs just to grep a README
MAX_TEXT_FILE_BYTES = 512 * 1024
# Decompression-bomb guard. MAX_TARBALL_BYTES bounds the COMPRESSED download and
# MAX_TEXT_FILE_BYTES bounds a SINGLE member, but neither bounds the TOTAL
# uncompressed bytes across all members: a crafted high-ratio sdist of thousands
# of just-under-512KB highly-compressible members fits under the 8MB compressed
# cap yet expands to ~1GB once each member is fh.read() into memory, OOMing/hanging
# the scanner on a routine `promptaudit scan .` (an attacker-controlled PyPI sdist
# is exactly the supply-chain surface this tool audits). We therefore cap the
# running total of uncompressed bytes read across all members, the number of
# members inspected, and read each member through a bounded reader so a single
# lying-size member can't blow the budget in one read.
MAX_SDIST_UNCOMPRESSED_BYTES = 24 * 1024 * 1024  # total expanded bytes budget
MAX_SDIST_MEMBERS = 2000  # members inspected before we stop walking the archive
README_CANDIDATE_NAMES = (
    "README.md",
    "README.MD",
    "readme.md",
    "README",
    "README.markdown",
    "README.rst",
    "README.txt",
)
# Match printable string literals long enough to plausibly carry an imperative
# sentence. Used as a best-effort error-string sweep over sdists.
STRING_LITERAL_RE = re.compile(rb'["\']([\x20-\x7e]{40,400})["\']')


class _VersionNotFound(Exception):
    """The registry returned 404 for a package/version.

    A yanked, unpublished, or wrongly-resolved version — itself a supply-chain
    red flag. Kept distinct from ``requests.RequestException`` (a transient
    transport failure) so ``_fetch_one`` can attribute the coverage gap to a
    missing version rather than a network blip, and so the 404 path is not
    silently promoted to a scanned-clean cache dir.
    """


@dataclass(frozen=True)
class FetchResult:
    """Outcome of fetching a single package."""

    package: ResolvedPackage
    cache_dir: Path
    sources_written: tuple[str, ...]
    status: str  # "ok" | "skipped" | "error"
    message: str = ""


def fetch_all(
    packages: Iterable[ResolvedPackage],
    cache_root: Path | None = None,
    *,
    force: bool = False,
) -> list[FetchResult]:
    """Fetch corpus for every package, returning per-package outcomes."""
    cache_root = (cache_root or DEFAULT_CACHE_ROOT).expanduser()
    cache_root.mkdir(parents=True, exist_ok=True)
    session = _http_session()
    results: list[FetchResult] = []
    for pkg in packages:
        results.append(_fetch_one(pkg, cache_root, session, force=force))
    return results


def cache_dir_for(pkg: ResolvedPackage, cache_root: Path | None = None) -> Path:
    """Resolve where a given package's cache lives (no I/O)."""
    root = (cache_root or DEFAULT_CACHE_ROOT).expanduser()
    return root / pkg.cache_key


def _fetch_one(
    pkg: ResolvedPackage,
    cache_root: Path,
    session: requests.Session,
    *,
    force: bool,
) -> FetchResult:
    target = cache_dir_for(pkg, cache_root)
    meta_path = target / "meta.json"
    if meta_path.exists() and not force:
        return FetchResult(pkg, target, (), "skipped", "cached")

    if pkg.ecosystem not in ("npm", "pypi"):
        return FetchResult(pkg, target, (), "error", f"unknown ecosystem {pkg.ecosystem}")

    # Fetch into a temp dir first; only promote to the real cache path on
    # success. A failed fetch must NOT leave an empty cache dir behind — the
    # scanner would then treat the package as "scanned, zero findings" and an
    # undownloadable dependency would silently pass the CI gate (false negative
    # on exactly the surface this tool exists to cover). Leaving no dir means a
    # later run retries instead of trusting an empty directory.
    staging = target.parent / f".{target.name}.tmp"
    if staging.exists():
        _rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        if pkg.ecosystem == "npm":
            sources = _fetch_npm(pkg, staging, session)
        else:
            sources = _fetch_pypi(pkg, staging, session)
    except _VersionNotFound as exc:
        # A 404 (yanked / unpublished / wrongly-resolved version) is a coverage
        # failure, not a clean result — and a yanked version is itself a
        # supply-chain red flag. Do NOT promote an empty cache dir: the scanner
        # would otherwise see "dir exists, zero source files" and emit zero
        # findings, silently passing the CI gate (the exact false-negative shape
        # v0.2.0's RequestException fix targeted, but the 404 path slipped past
        # it). Leaving no dir means a later run retries.
        _rmtree(staging)
        return FetchResult(pkg, target, (), "error", f"version_not_found: {exc}")
    except requests.RequestException as exc:
        _rmtree(staging)
        return FetchResult(pkg, target, (), "error", f"network: {exc}")
    except Exception as exc:  # pragma: no cover — last-ditch isolation
        _rmtree(staging)
        return FetchResult(pkg, target, (), "error", f"{type(exc).__name__}: {exc}")

    # A fetch that returned zero source files (e.g. a manifest with no readme,
    # description, or extractable error strings) is likewise a coverage failure:
    # the scanner would emit zero findings for an empty cache dir and the dep
    # would silently pass the gate. Don't promote an empty corpus to "ok".
    if not sources:
        _rmtree(staging)
        return FetchResult(
            pkg, target, (), "error", "empty_corpus: no readme/summary/error text"
        )

    meta = {
        "name": pkg.name,
        "version": pkg.version,
        "ecosystem": pkg.ecosystem,
        "via_path": list(pkg.via_path),
        "fetched_at": int(time.time()),
        "sources": sources,
    }
    (staging / "meta.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8"
    )
    # Promote staging → final atomically-ish. Remove a stale target first
    # (e.g. a force-refetch over an existing entry).
    if target.exists():
        _rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging.replace(target)
    return FetchResult(pkg, target, tuple(sources.keys()), "ok")


def _rmtree(path: Path) -> None:
    """Best-effort recursive delete (used to clean up staging / stale cache)."""
    import shutil

    shutil.rmtree(path, ignore_errors=True)


# ---------- npm ----------------------------------------------------------------


def _fetch_npm(
    pkg: ResolvedPackage, target: Path, session: requests.Session
) -> dict[str, str]:
    """Pull README + description for an npm package; fall back to tarball if empty."""
    sources: dict[str, str] = {}
    url = f"{NPM_REGISTRY}/{pkg.name}/{pkg.version}"
    resp = session.get(url, timeout=HTTP_TIMEOUT)
    if resp.status_code == 404:
        raise _VersionNotFound(f"{pkg.name}@{pkg.version} not on npm registry")
    resp.raise_for_status()
    manifest = resp.json()

    readme = (manifest.get("readme") or "").strip()
    description = (manifest.get("description") or "").strip()

    if description:
        (target / "summary.txt").write_text(description, encoding="utf-8")
        sources["summary"] = "registry.description"

    if readme and not _looks_like_no_readme(readme):
        (target / "readme.md").write_text(readme, encoding="utf-8")
        sources["readme"] = "registry.readme"
    else:
        # Build note: many popular npm packages (e.g. express) have an empty
        # `readme` field on the version document — fall back to the tarball.
        tarball_url = (manifest.get("dist") or {}).get("tarball")
        if tarball_url:
            extracted = _extract_readme_from_tarball(session, tarball_url)
            if extracted:
                (target / "readme.md").write_text(extracted, encoding="utf-8")
                sources["readme"] = "dist.tarball"

    return sources


def _looks_like_no_readme(text: str) -> bool:
    """npm sometimes stores a sentinel like 'ERROR: No README data found!' verbatim."""
    return text.strip().lower().startswith("error: no readme")


def _extract_readme_from_tarball(
    session: requests.Session, tarball_url: str
) -> str | None:
    try:
        resp = session.get(tarball_url, timeout=HTTP_TIMEOUT, stream=True)
        resp.raise_for_status()
        payload = _read_bounded(resp, MAX_TARBALL_BYTES)
    except requests.RequestException:
        return None
    if payload is None:
        return None

    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                # npm tarballs root everything at "package/<file>".
                base = member.name.split("/", 1)[-1]
                if base in README_CANDIDATE_NAMES and member.size <= MAX_TEXT_FILE_BYTES:
                    fh = tar.extractfile(member)
                    if fh is None:
                        continue
                    raw = fh.read()
                    return raw.decode("utf-8", errors="replace")
    except (tarfile.TarError, OSError):
        return None
    return None


# ---------- PyPI ---------------------------------------------------------------


def _fetch_pypi(
    pkg: ResolvedPackage, target: Path, session: requests.Session
) -> dict[str, str]:
    sources: dict[str, str] = {}
    url = f"{PYPI_REGISTRY}/{pkg.name}/{pkg.version}/json"
    resp = session.get(url, timeout=HTTP_TIMEOUT)
    if resp.status_code == 404:
        raise _VersionNotFound(f"{pkg.name}@{pkg.version} not on PyPI")
    resp.raise_for_status()
    payload = resp.json()
    info = payload.get("info") or {}

    summary = (info.get("summary") or "").strip()
    if summary:
        (target / "summary.txt").write_text(summary, encoding="utf-8")
        sources["summary"] = "info.summary"

    description = (info.get("description") or "").strip()
    if description:
        (target / "readme.md").write_text(description, encoding="utf-8")
        sources["readme"] = "info.description"

    # Best-effort: peek at the sdist for in-code string literals. Errors here
    # are non-fatal — the scanner can still run on README + summary.
    sdist_url = _pick_sdist_url(payload.get("urls") or [])
    if sdist_url:
        candidates = _extract_strings_from_sdist(session, sdist_url)
        if candidates:
            (target / "errors.txt").write_text("\n".join(candidates), encoding="utf-8")
            sources["errors"] = "sdist.strings"

    return sources


def _pick_sdist_url(urls: list[dict]) -> str | None:
    for entry in urls:
        if entry.get("packagetype") == "sdist":
            return entry.get("url")
    return None


class _BudgetExceeded(Exception):
    """Raised to abort sdist extraction once a hard budget is hit.

    Either the total uncompressed bytes read, the member count inspected, or the
    500-string early-exit. Caught so the caller returns what it collected so far
    rather than the whole (possibly malicious) archive.
    """


def _read_member_bounded(fh, remaining: int) -> bytes:
    """Read at most ``remaining`` bytes from a member, ignoring its declared size.

    A decompression bomb can declare a small ``member.size`` yet stream far more
    on read; reading in capped chunks means even a lying member can never push us
    past the running uncompressed budget in a single ``fh.read()`` call.
    """
    cap = max(0, min(remaining, MAX_TEXT_FILE_BYTES))
    if cap == 0:
        raise _BudgetExceeded("uncompressed byte budget exhausted")
    # +1 so we can detect a member that exceeds the per-member cap and skip it
    # without materialising the whole thing.
    data = fh.read(cap + 1)
    if len(data) > cap:
        # Member is larger than we allow — drop it (don't scan a partial,
        # potentially misleading slice) but charge the cap against the budget so
        # a flood of oversized members still terminates the walk.
        return b""
    return data


def _extract_strings_from_sdist(
    session: requests.Session, sdist_url: str
) -> list[str]:
    try:
        resp = session.get(sdist_url, timeout=HTTP_TIMEOUT, stream=True)
        resp.raise_for_status()
        payload = _read_bounded(resp, MAX_TARBALL_BYTES)
    except requests.RequestException:
        return []
    if payload is None:
        return []

    collected: list[str] = []
    seen: set[str] = set()
    # Mutable accounting shared across members. ``budget`` is the remaining
    # uncompressed-byte allowance; ``members`` counts archive entries inspected.
    budget = {"bytes": MAX_SDIST_UNCOMPRESSED_BYTES, "members": 0}

    def _scan_bytes(blob: bytes) -> None:
        for match in STRING_LITERAL_RE.finditer(blob):
            candidate = match.group(1).decode("ascii", errors="replace").strip()
            if len(candidate) < 40 or candidate in seen:
                continue
            seen.add(candidate)
            collected.append(candidate)
            if len(collected) >= 500:
                raise _BudgetExceeded("string cap reached")

    def _charge_member() -> None:
        """Count one inspected member; abort once the member-count cap is hit."""
        budget["members"] += 1
        if budget["members"] > MAX_SDIST_MEMBERS:
            raise _BudgetExceeded("member count cap reached")

    def _read_and_account(fh) -> bytes:
        """Read a member bounded by the running budget and debit what we read."""
        if budget["bytes"] <= 0:
            raise _BudgetExceeded("uncompressed byte budget exhausted")
        blob = _read_member_bounded(fh, budget["bytes"])
        # Always charge the per-member cap (not just len(blob)) so a flood of
        # oversized-and-skipped members still drains the budget and terminates.
        budget["bytes"] -= max(len(blob), MAX_TEXT_FILE_BYTES)
        return blob

    try:
        buf = io.BytesIO(payload)
        if sdist_url.endswith(".zip"):
            try:
                with zipfile.ZipFile(buf) as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        if not info.filename.endswith((".py", ".txt", ".md", ".rst")):
                            continue
                        _charge_member()
                        # Honour the declared size as a cheap pre-filter, but the
                        # bounded read below is the real guard against a lying size.
                        if info.file_size > MAX_TEXT_FILE_BYTES:
                            continue
                        with zf.open(info) as fh:
                            _scan_bytes(_read_and_account(fh))
            except zipfile.BadZipFile:
                return collected
        else:
            try:
                with tarfile.open(fileobj=buf, mode="r:*") as tar:
                    for member in tar:
                        if not member.isfile():
                            continue
                        if not member.name.endswith((".py", ".txt", ".md", ".rst")):
                            continue
                        _charge_member()
                        if member.size > MAX_TEXT_FILE_BYTES:
                            continue
                        fh = tar.extractfile(member)
                        if fh is None:
                            continue
                        _scan_bytes(_read_and_account(fh))
            except tarfile.TarError:
                return collected
    except _BudgetExceeded:
        pass

    return collected


# ---------- shared -------------------------------------------------------------


def _read_bounded(resp: requests.Response, max_bytes: int) -> bytes | None:
    """Read at most ``max_bytes`` from a streaming response, else give up."""
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def _cli_preview(root: str = ".") -> int:
    """Tiny preview for ``python -m promptaudit.fetcher <path>``."""
    from .resolver import ResolverError, resolve

    try:
        pkgs = resolve(Path(root))
    except ResolverError as exc:
        print(f"resolver error: {exc}", file=sys.stderr)
        return 2

    results = fetch_all(pkgs)
    ok = sum(1 for r in results if r.status == "ok")
    skipped = sum(1 for r in results if r.status == "skipped")
    errored = sum(1 for r in results if r.status == "error")
    for r in results:
        if r.status == "error":
            print(f"[ERR ] {r.package.ecosystem} {r.package.name}@{r.package.version}: {r.message}", file=sys.stderr)
    print(
        f"fetched: ok={ok} skipped={skipped} errors={errored} (cache={DEFAULT_CACHE_ROOT})",
        file=sys.stderr,
    )
    return 0 if errored == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_cli_preview(*sys.argv[1:2]))
