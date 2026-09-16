"""v0.11.0 regression tests — four defect classes reproduced against shipped
v0.10.0 by the inline bug-hunt:

1. fix-malformed-manifest-shape-crash — nine local-manifest shape sites
   (``package-lock.json`` / ``package.json`` / ``poetry.lock``) consumed fields
   via ``.get(...)`` / ``.items()`` / ``>=`` without validating the parsed
   shape, so a corrupt or hand-malformed manifest crashed the CLI with a raw
   ``AttributeError`` / ``TypeError`` traceback instead of the established
   ``promptaudit: <message>`` + exit-2 contract (``resolve()``/``scan_cmd``
   only catch ``ResolverError``).
2. fix-manifest-read-decode-parse-crash — unreadable / undecodable /
   unparseable manifests raised raw ``UnicodeDecodeError`` /
   ``TOMLDecodeError`` / ``PermissionError`` (only syntactically-broken JSON
   got the clean-error treatment).
3. fix-nonlist-dep-collection-per-char-explosion — a string-valued
   ``[project] dependencies`` or PyPI ``requires_dist`` was iterated
   CHARACTER BY CHARACTER; every letter parses as a valid PEP 508 bare name,
   so each character became a bogus dep that was resolved, fetched, and
   scanned.
4. fix-fetcher-nonstring-registry-fields — non-string ``readme`` /
   ``description`` or non-dict ``info`` / non-list ``urls`` in a registry doc
   collapsed into the fetcher's last-ditch ``except Exception`` with the
   misattributed reason ``AttributeError: ...`` instead of an honest coverage
   gap.

Each test is RED on the v0.10.0 baseline (crash / bogus deps / misattributed
message) and GREEN after the fix (clean ``ResolverError`` or graceful
degradation, never a crash, never silent wrong output).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make src/ importable when running pytest from the repo root without an
# editable install (mirrors tests/test_scanner.py / test_v0100_fixes.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest


# --------------------------------------------------------------------------- #
# Test helpers (self-contained mirrors of the stand-ins in the older test
# modules — kept local so this module has no cross-test-module import coupling).
# --------------------------------------------------------------------------- #


class _FakeResponse:
    """Streaming stand-in whose iter_content serialises the payload back to
    JSON bytes so the bounded read round-trips to the same dict."""

    def __init__(self, *, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def iter_content(self, chunk_size=65536):
        data = json.dumps(self._payload).encode("utf-8") if self._payload else b""
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}")


class _FakeSession:
    """Stand-in session: every GET returns the given payload."""

    def __init__(self, payload):
        self._payload = payload
        self.urls: list[str] = []

    def get(self, url, *a, **kw):
        self.urls.append(url)
        return _FakeResponse(status_code=200, payload=self._payload)


# --------------------------------------------------------------------------- #
# 1 — fix-malformed-manifest-shape-crash
# --------------------------------------------------------------------------- #


def test_lockfile_root_non_dict_raises_resolver_error(tmp_path):
    """A package-lock.json whose JSON root is a list (corrupt file) must raise
    the clean ``ResolverError`` the CLI renders as ``promptaudit: ...`` + exit
    2 — not ``AttributeError: 'list' object has no attribute 'get'`` at
    ``data.get("lockfileVersion", 1)`` (resolver.py:271 on v0.10.0)."""
    from promptaudit.resolver import ResolverError, resolve

    (tmp_path / "package-lock.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ResolverError, match="root must be a JSON object"):
        list(resolve(tmp_path))


def test_lockfile_string_lockfile_version_raises_resolver_error(tmp_path):
    """A string ``lockfileVersion`` (``"3"``) must raise a clean
    ``ResolverError`` — not ``TypeError: '>=' not supported between instances
    of 'str' and 'int'`` at ``lockfile_version >= 2`` (resolver.py:274 on
    v0.10.0)."""
    from promptaudit.resolver import ResolverError, resolve

    (tmp_path / "package-lock.json").write_text(
        json.dumps({"name": "x", "lockfileVersion": "3", "packages": {}}),
        encoding="utf-8",
    )
    with pytest.raises(ResolverError, match="lockfileVersion"):
        list(resolve(tmp_path))


def test_lockfile_packages_non_dict_raises_resolver_error(tmp_path):
    """A v2/v3 lockfile whose ``packages`` map is a list must raise a clean
    ``ResolverError`` — not ``AttributeError: ... has no attribute 'items'``
    at ``packages.items()`` (resolver.py:282 on v0.10.0)."""
    from promptaudit.resolver import ResolverError, resolve

    (tmp_path / "package-lock.json").write_text(
        json.dumps({"name": "x", "lockfileVersion": 3, "packages": []}),
        encoding="utf-8",
    )
    with pytest.raises(ResolverError, match="'packages' must be an object"):
        list(resolve(tmp_path))


def test_lockfile_v2_entry_non_dict_raises_resolver_error(tmp_path):
    """A v2/v3 lockfile whose ``packages`` ENTRY value is a string (e.g.
    ``{"node_modules/foo": "1.0.0"}``) must raise a clean ``ResolverError``
    naming the entry — not ``AttributeError: 'str' object has no attribute
    'get'`` at ``meta.get("dev")`` (resolver.py:302 on v0.10.0)."""
    from promptaudit.resolver import ResolverError, resolve

    (tmp_path / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "x",
                "lockfileVersion": 3,
                "packages": {"node_modules/foo": "1.0.0"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ResolverError, match="node_modules/foo"):
        list(resolve(tmp_path))


def test_lockfile_v1_dependencies_non_dict_raises_resolver_error(tmp_path):
    """A v1 lockfile whose ``dependencies`` map is a list must raise a clean
    ``ResolverError`` — not ``AttributeError: ... has no attribute 'items'``
    at ``deps.items()`` (resolver.py:318 on v0.10.0)."""
    from promptaudit.resolver import ResolverError, resolve

    (tmp_path / "package-lock.json").write_text(
        json.dumps({"name": "x", "lockfileVersion": 1, "dependencies": []}),
        encoding="utf-8",
    )
    with pytest.raises(ResolverError, match="'dependencies' must be an object"):
        list(resolve(tmp_path))


def test_lockfile_v1_entry_non_dict_raises_resolver_error(tmp_path):
    """A v1 lockfile whose ``dependencies`` ENTRY value is a string (e.g.
    ``{"foo": "1.0.0"}``) must raise a clean ``ResolverError`` naming the
    entry — not ``AttributeError: 'str' object has no attribute 'get'`` at
    ``meta.get("dev")`` (resolver.py:320 on v0.10.0)."""
    from promptaudit.resolver import ResolverError, resolve

    (tmp_path / "package-lock.json").write_text(
        json.dumps({"name": "x", "lockfileVersion": 1, "dependencies": {"foo": "1.0.0"}}),
        encoding="utf-8",
    )
    with pytest.raises(ResolverError, match="foo"):
        list(resolve(tmp_path))


def test_package_json_dependencies_non_dict_raises_resolver_error(tmp_path):
    """A package.json whose ``dependencies`` is a list must raise a clean
    ``ResolverError`` — not ``AttributeError: ... has no attribute 'items'``
    at ``direct.items()`` (resolver.py:372 on v0.10.0)."""
    from promptaudit.resolver import ResolverError, resolve

    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "dependencies": ["a", "b"]}),
        encoding="utf-8",
    )
    with pytest.raises(ResolverError, match="'dependencies' must be an object"):
        list(resolve(tmp_path))


def test_package_json_non_string_spec_skipped(tmp_path):
    """A package.json entry whose spec VALUE is a non-string (e.g.
    ``{"a": 42}``) must be skipped as a malformed entry — not crash with
    ``AttributeError: 'int' object has no attribute 'strip'`` at
    ``(spec or "").strip()`` in ``_is_non_registry_spec`` (resolver.py:450 on
    v0.10.0). GREEN: no exception, no packages resolved (the only dep was
    malformed)."""
    from promptaudit.resolver import resolve

    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "dependencies": {"a": 42}}),
        encoding="utf-8",
    )
    pkgs = list(resolve(tmp_path))
    assert pkgs == [], f"a malformed non-string spec must be skipped; got {pkgs}"


def test_poetry_lock_package_non_list_raises_resolver_error(tmp_path):
    """A poetry.lock whose ``package`` key is a string (corrupt TOML value)
    must raise a clean ``ResolverError`` — not ``AttributeError: 'str' object
    has no attribute 'get'`` while iterating the characters (resolver.py:704
    on v0.10.0)."""
    from promptaudit.resolver import ResolverError, resolve

    (tmp_path / "poetry.lock").write_text('package = "not-a-list"\n', encoding="utf-8")
    with pytest.raises(ResolverError, match="'package' must be a list"):
        list(resolve(tmp_path))


def test_poetry_lock_entry_non_dict_raises_resolver_error(tmp_path):
    """A poetry.lock whose ``package`` array holds a non-dict entry (e.g.
    ``["not-a-table"]``) must raise a clean ``ResolverError`` — not
    ``AttributeError: 'str' object has no attribute 'get'`` at
    ``entry.get("category")`` (resolver.py:704 on v0.10.0)."""
    from promptaudit.resolver import ResolverError, resolve

    (tmp_path / "poetry.lock").write_text(
        'package = ["not-a-table"]\n', encoding="utf-8"
    )
    with pytest.raises(ResolverError, match="must be tables"):
        list(resolve(tmp_path))


# --------------------------------------------------------------------------- #
# 2 — fix-manifest-read-decode-parse-crash
# --------------------------------------------------------------------------- #


def test_lockfile_invalid_utf8_raises_resolver_error(tmp_path):
    """A package-lock.json containing invalid UTF-8 bytes must raise a clean
    ``ResolverError`` — not ``UnicodeDecodeError`` leaking out of
    ``path.read_text(encoding="utf-8")`` (resolver.py:1245 on v0.10.0, which
    only caught ``json.JSONDecodeError``)."""
    from promptaudit.resolver import ResolverError, resolve

    (tmp_path / "package-lock.json").write_bytes(b'\xff\xfe{"name":"x"}')
    with pytest.raises(ResolverError, match="package-lock.json"):
        list(resolve(tmp_path))


def test_poetry_lock_invalid_toml_raises_resolver_error(tmp_path):
    """A poetry.lock that is invalid TOML must raise a clean ``ResolverError``
    — not ``tomllib.TOMLDecodeError`` leaking out of ``tomllib.load``
    (resolver.py:700-701 on v0.10.0)."""
    from promptaudit.resolver import ResolverError, resolve

    (tmp_path / "poetry.lock").write_text("package = [broken\n", encoding="utf-8")
    with pytest.raises(ResolverError, match="poetry.lock"):
        list(resolve(tmp_path))


def test_requirements_unreadable_raises_resolver_error(tmp_path):
    """A requirements.txt with no read permission must raise a clean
    ``ResolverError`` — not ``PermissionError: [Errno 13]`` leaking out of
    ``req_file.read_text()`` (resolver.py:798 on v0.10.0)."""
    from promptaudit.resolver import ResolverError, resolve

    req = tmp_path / "requirements.txt"
    req.write_text("requests==2.0.0\n", encoding="utf-8")
    req.chmod(0o000)
    try:
        with pytest.raises(ResolverError, match="requirements.txt"):
            list(resolve(tmp_path))
    finally:
        req.chmod(0o644)


def test_requirements_invalid_utf8_raises_resolver_error(tmp_path):
    """A requirements.txt containing invalid UTF-8 bytes must raise a clean
    ``ResolverError`` — not ``UnicodeDecodeError`` leaking out of
    ``req_file.read_text()`` (resolver.py:798 on v0.10.0)."""
    from promptaudit.resolver import ResolverError, resolve

    (tmp_path / "requirements.txt").write_bytes(b"\xff\xferquests==1.0\n")
    with pytest.raises(ResolverError, match="requirements.txt"):
        list(resolve(tmp_path))


# --------------------------------------------------------------------------- #
# 3 — fix-nonlist-dep-collection-per-char-explosion
# --------------------------------------------------------------------------- #


def test_pyproject_string_dependencies_raises_resolver_error(tmp_path):
    """A pyproject.toml with ``[project] dependencies = "requests"`` (string,
    not list) must raise a clean ``ResolverError`` — on v0.10.0 the string was
    iterated CHARACTER BY CHARACTER and every letter became a bogus dep that
    was resolved, fetched, and scanned (silent wrong output + a registry GET
    per character)."""
    from promptaudit.resolver import ResolverError, resolve

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = "requests"\n', encoding="utf-8"
    )
    with pytest.raises(ResolverError, match="dependencies must be a list"):
        list(resolve(tmp_path))


def test_pypi_requires_dist_string_yields_no_perchar_deps(tmp_path, monkeypatch):
    """A crafted PyPI release doc whose ``requires_dist`` is the STRING
    "requests" must not explode into one bogus dep per character. On v0.10.0,
    ``requires_dist = info.get("requires_dist") or []`` kept the string and
    ``for raw in requires_dist`` iterated its characters — every letter parsed
    as a valid PEP 508 bare-name requirement, so ``resolve()`` yielded the
    bogus packages e/q/r/s/t/u@1.0.0 alongside the real foo@1.0.0 (each then
    fetched and scanned). GREEN: only foo@1.0.0 is resolved; a non-list
    ``requires_dist`` degrades to no transitive children (mirroring the
    v0.10.0 treatment of a non-dict npm ``dependencies``)."""
    import requests

    from promptaudit.resolver import resolve

    (tmp_path / "requirements.txt").write_text("foo==1.0.0\n", encoding="utf-8")

    def _fake_get(self, url, *a, **kw):
        assert url == "https://pypi.org/pypi/foo/1.0.0/json", url
        return _FakeResponse(
            status_code=200,
            payload={
                "info": {"version": "1.0.0", "requires_dist": "requests"},
            },
        )

    monkeypatch.setattr(requests.Session, "get", _fake_get)

    pkgs = list(resolve(tmp_path))

    names = sorted(p.name for p in pkgs)
    assert names == ["foo"], (
        f"a string requires_dist must yield NO per-character deps; got {names}"
    )
    assert pkgs[0].version == "1.0.0", pkgs[0].version


# --------------------------------------------------------------------------- #
# 4 — fix-fetcher-nonstring-registry-fields
# --------------------------------------------------------------------------- #


def test_fetch_npm_nonstring_readme_honest_error(tmp_path):
    """An npm registry doc whose ``readme`` is an integer and ``description``
    a list must degrade to the honest ``empty_corpus`` coverage gap — on
    v0.10.0 ``(manifest.get("readme") or "").strip()`` raised AttributeError
    and the last-ditch handler surfaced the operator-facing fetch reason as
    ``AttributeError: 'int' object has no attribute 'strip'``."""
    from promptaudit.fetcher import _fetch_one
    from promptaudit.resolver import ResolvedPackage

    pkg = ResolvedPackage(name="x", version="1.0.0", ecosystem="npm")
    session = _FakeSession({"readme": 42, "description": ["a"]})

    result = _fetch_one(pkg, tmp_path, session, force=True)

    assert result.status == "error"
    assert "AttributeError" not in result.message, (
        f"the operator-facing reason must never be an internal exception "
        f"string; got {result.message!r}"
    )
    assert "empty_corpus" in result.message, result.message


def test_fetch_pypi_non_dict_info_honest_error(tmp_path):
    """A PyPI release doc whose ``info`` is a list and ``urls`` a string must
    degrade to the honest ``empty_corpus`` coverage gap — on v0.10.0
    ``payload.get("info") or {}`` kept the truthy list and
    ``info.get("summary")`` raised AttributeError, surfacing the
    misattributed last-ditch reason."""
    from promptaudit.fetcher import _fetch_one
    from promptaudit.resolver import ResolvedPackage

    pkg = ResolvedPackage(name="y", version="2.0.0", ecosystem="pypi")
    session = _FakeSession({"info": ["a", "b"], "urls": "not-a-list"})

    result = _fetch_one(pkg, tmp_path, session, force=True)

    assert result.status == "error"
    assert "AttributeError" not in result.message, result.message
    assert "empty_corpus" in result.message, result.message
