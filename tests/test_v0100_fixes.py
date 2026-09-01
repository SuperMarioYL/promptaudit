"""v0.10.0 regression tests — two sibling non-dict crash sites the v0.9.0
fix-npm-registry-versions-non-dict-crash explicitly aimed at but missed.

v0.9.0 added ``isinstance(..., dict)`` guards at three sites — ``versions``
(in ``_resolve_npm_max_satisfying``), ``dist-tags`` (in ``_resolve_npm_dist_tag``),
and ``releases`` (in ``_resolve_pypi_max_satisfying``) — but two structurally
identical sibling sites that consume a registry-doc field via ``.get(...)`` /
``.items()`` on a truthy non-dict were missed. Both reproduce against shipped
v0.9.0 as an unhandled ``AttributeError`` (``resolve()``/``scan_cmd`` only catch
``ResolverError``), crashing the CLI on a routine ``scan .`` of a project whose
resolved dep has a crafted/malformed registry doc (the tool's canonical
supply-chain surface — a registry GET happens on every scan).

Each test is RED on the v0.9.0 baseline (AttributeError) and GREEN after the
fix (the non-dict field degrades to the existing coverage path, never a crash):

* ``test_npm_dependencies_non_dict_does_not_crash`` —
  ``_fetch_npm_dependencies`` did ``return doc.get("dependencies", {}) or {}``
  whose ``or {}`` only guards a falsy/absent ``dependencies``; a crafted npm
  packument with a truthy non-dict ``dependencies`` (``["a","b"]``) made
  ``.items()`` in the caller raise ``AttributeError`` on the no-lockfile
  transitive walk. GREEN: a non-dict ``dependencies`` returns ``{}`` (no
  transitive children), the direct dep still resolves, no crash.
* ``test_pypi_info_non_dict_surfaces_as_unscanned`` —
  ``_fetch_pypi_release`` did ``return doc.get("info", {})`` whose ``{}``
  default only applies when the key is ABSENT; a crafted PyPI release doc with a
  truthy non-dict ``info`` (``["a","b"]``) made ``info.get("version")`` in
  ``_walk_pypi`` raise ``AttributeError`` (the ``info is not None`` check is
  True for a truthy non-dict, so it does NOT take the safe ``else None``
  branch). GREEN: a non-dict ``info`` returns ``None``, which ``_walk_pypi``
  surfaces as a ``pypi_release_not_found`` coverage gap (via the v0.9.0
  fix-pypi-walk-404-silent-drop plumbing) instead of crashing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make src/ importable when running pytest from the repo root without an
# editable install (mirrors tests/test_scanner.py / test_v090_fixes.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


# --------------------------------------------------------------------------- #
# Test helpers (self-contained mirrors of the _FakeResponse stand-in in
# tests/test_scanner.py / test_v090_fixes.py — kept local so this module has
# no cross-test-module import coupling).
# --------------------------------------------------------------------------- #


class _FakeResponse:
    """Non-payload stand-in; iter_content serialises payload back to JSON bytes
    so the streaming + bounded read round-trips to the same dict the v0.5.0
    ``.json()`` path returned."""

    def __init__(self, *, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def iter_content(self, chunk_size=65536):
        data = json.dumps(self._payload).encode("utf-8") if self._payload else b""
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}")


# --------------------------------------------------------------------------- #
# Fix — fix-registry-nondict-dependencies-and-info (npm `dependencies` site)
# --------------------------------------------------------------------------- #


def test_npm_dependencies_non_dict_does_not_crash(tmp_path, monkeypatch):
    """fix-registry-nondict-dependencies-and-info (npm `dependencies` site):
    a crafted npm packument (the tool's canonical supply-chain surface) whose
    per-version doc's ``dependencies`` field is a truthy non-dict (``["a","b"]``)
    must NOT crash the resolver. The v0.9.0 ``_fetch_npm_dependencies`` did
    ``return doc.get("dependencies", {}) or {}`` whose ``or {}`` only guards a
    falsy/absent ``dependencies``; a non-empty list slipped past (``["a","b"]
    or {}`` returns the list) and ``["a","b"].items()`` in the caller
    ``_resolve_npm_package_json`` raised ``AttributeError``, crashing the CLI
    on a routine ``scan .`` of a no-lockfile npm project. The v0.9.0
    fix-npm-registry-versions-non-dict-crash guarded ``versions``/``dist-tags``/
    ``releases`` but missed this sibling site. The fix guards
    ``isinstance(deps, dict)`` and returns ``{}`` (no transitive children) —
    the same empty path the falsy guard already returns — never a crash. RED:
    raises ``AttributeError`` (the call to ``resolve`` crashes). GREEN: the
    direct dep still resolves and no exception is raised."""
    import requests

    from promptaudit.resolver import resolve

    # A no-lockfile npm project: package.json with one direct dep.
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "app", "dependencies": {"leftpad": "^1.2.0"}}),
        encoding="utf-8",
    )

    def _fake_get(self, url, *a, **kw):
        # The direct dep's full registry doc: a real `versions` map so the
        # range resolves to 1.2.0 (the exact-match / max-satisfying path).
        if url.endswith("/leftpad"):
            return _FakeResponse(
                status_code=200,
                payload={
                    "dist-tags": {"latest": "1.2.0"},
                    "versions": {"1.2.0": {}},
                },
            )
        # The per-version doc GETs /leftpad/1.2.0 — craft a non-dict
        # `dependencies` (a list) that would crash .items() on v0.9.0.
        if url.endswith("/leftpad/1.2.0"):
            return _FakeResponse(
                status_code=200,
                payload={"dependencies": ["a", "b"]},  # non-dict!
            )
        return _FakeResponse(status_code=404)

    monkeypatch.setattr(requests.Session, "get", _fake_get)

    # Must not raise. On the v0.9.0 baseline `["a","b"].items()` raises
    # AttributeError here and the test errors out before the assertions.
    pkgs = list(resolve(tmp_path))

    # The direct dep is still resolved (its children are simply absent — a
    # non-dict `dependencies` yields no transitive deps, the same as {}).
    leftpad = [p for p in pkgs if p.name == "leftpad"]
    assert leftpad, (
        "leftpad ^1.2.0 with a real versions map must still resolve; the "
        f"non-dict `dependencies` only suppresses its children. got {pkgs}"
    )
    assert leftpad[0].version == "1.2.0", leftpad[0].version
    assert leftpad[0].ecosystem == "npm", leftpad[0].ecosystem


# --------------------------------------------------------------------------- #
# Fix — fix-registry-nondict-dependencies-and-info (pypi `info` site)
# --------------------------------------------------------------------------- #


def test_pypi_info_non_dict_surfaces_as_unscanned(tmp_path, monkeypatch):
    """fix-registry-nondict-dependencies-and-info (pypi `info` site): a crafted
    PyPI release doc whose ``info`` field is a truthy non-dict (``["a","b"]``)
    must NOT crash the resolver. The v0.9.0 ``_fetch_pypi_release`` did
    ``return doc.get("info", {})`` whose ``{}`` default only applies when the
    key is ABSENT; a truthy non-dict ``info`` made ``info.get("version")`` in
    ``_walk_pypi`` raise ``AttributeError`` (the ``info is not None`` check is
    True for a truthy non-dict, so it does NOT take the safe ``else None``
    branch). The v0.9.0 fix-npm-registry-versions-non-dict-crash guarded
    ``versions``/``dist-tags``/``releases`` but missed this sibling site. The
    fix guards ``isinstance(info, dict)`` and returns ``None``, which
    ``_walk_pypi`` surfaces as a ``pypi_release_not_found`` coverage gap (via
    the v0.9.0 fix-pypi-walk-404-silent-drop plumbing) instead of crashing.
    RED: raises ``AttributeError``. GREEN: the dep is surfaced in
    ``marker_skipped`` (not silently dropped, not crashed)."""
    import requests

    from promptaudit.resolver import MarkerSkipped, resolve

    # A pinned PyPI dep whose release doc carries a crafted non-dict `info`.
    (tmp_path / "requirements.txt").write_text("foo==1.0.0\n", encoding="utf-8")

    def _fake_get(self, url, *a, **kw):
        # The pinned version's release doc: `info` is a crafted non-dict list.
        if url == "https://pypi.org/pypi/foo/1.0.0/json":
            return _FakeResponse(
                status_code=200,
                payload={"info": ["a", "b"]},  # non-dict!
            )
        return _FakeResponse(status_code=404)

    monkeypatch.setattr(requests.Session, "get", _fake_get)

    # Must not raise. On the v0.9.0 baseline `["a","b"].get("version")` raises
    # AttributeError here and the test errors out before the assertions.
    skipped: list[MarkerSkipped] = []
    pkgs = list(resolve(tmp_path, marker_skipped=skipped))

    # The dep must NOT be yielded as a scanned package — a non-dict `info`
    # means we could not read its version, so it is a coverage gap.
    foo = [p for p in pkgs if p.name == "foo"]
    assert not foo, (
        "a dep whose release-doc `info` is a non-dict must NOT be scanned at "
        f"a version we couldn't read; got {foo}"
    )
    # And it must be surfaced as a coverage gap, not crashed or silently
    # dropped — the v0.9.0 baseline raised AttributeError here.
    foo_skipped = [ms for ms in skipped if ms.name == "foo"]
    assert foo_skipped, (
        "a non-dict `info` must surface as a coverage gap "
        f"(marker_skipped/pypi_release_not_found); skipped was {skipped}"
    )
    assert foo_skipped[0].marker == "pypi_release_not_found", (
        foo_skipped[0].marker
    )
    assert foo_skipped[0].ecosystem == "pypi", foo_skipped[0].ecosystem
    assert foo_skipped[0].via_path == ("<root>", "foo"), (
        foo_skipped[0].via_path
    )
