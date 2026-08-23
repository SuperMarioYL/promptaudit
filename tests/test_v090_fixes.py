"""v0.9.0 regression tests — three correctness fixes.

Each test is written to be RED on the v0.8.0 baseline (the bug class it
targets) and GREEN after the fix:

* ``test_pypi_walk_404_release_doc_surfaces_as_unscanned`` —
  fix-pypi-walk-404-silent-drop: ``_walk_pypi`` did ``info =
  _fetch_pypi_release(...)`` then ``if info is None: continue`` BEFORE the
  ``yield ResolvedPackage``, so a PyPI dep whose release-doc fetch 404s /
  over-caps / is unparseable (a yanked / unpublished pinned version — a
  supply-chain red flag) vanished from BOTH ``packages`` and
  ``marker_skipped`` and never reached the fetcher — zero findings, zero
  unscanned signal, exit 0. This is the PyPI-side analog of v0.7.0's
  ``fix-npm-no-lockfile-404-silent-drop`` (which closed the npm no-lockfile
  resolve-time 404 path but missed this sibling). RED: the 404'd dep appears
  in neither ``packages`` nor ``marker_skipped``. GREEN: surfaced as
  ``pypi_release_not_found`` in ``marker_skipped``.
* ``test_npm_registry_non_dict_versions_does_not_crash`` ( +
  ``test_npm_dist_tags_non_dict_does_not_crash`` / +
  ``test_pypi_releases_non_dict_does_not_crash``) —
  fix-npm-registry-versions-non-dict-crash: ``_resolve_npm_max_satisfying``
  did ``versions = list((doc.get("versions") or {}).keys())`` whose ``or {}``
  only guards a falsy / absent ``versions``; a crafted packument (the tool's
  canonical supply-chain surface) with a truthy non-dict ``versions`` (list /
  int) raised ``AttributeError`` and crashed the CLI on a routine ``scan .``.
  The v0.8.0 ``if not versions`` guard is False for a non-empty list, so the
  non-dict sibling slipped past. RED: raises ``AttributeError`` (and the
  ``dist-tags`` / ``releases`` sibling sites crash too). GREEN: ``isinstance``
  guard returns the coverage-gap sentinel (no crash).
* ``test_requirements_r_include_deps_are_scanned`` ( +
  ``test_requirements_r_include_cycle_is_bounded``) —
  fix-requirements-r-include-silent-drop: ``_resolve_requirements_txt`` skipped
  every line starting with ``-``, so a ``-r base.txt`` / ``--requirements
  base.txt`` include (the common pip-tools / multi-env pattern) silently
  dropped the included file's deps from ``packages``, ``missing``, and
  ``marker_skipped`` — a silent partial-tree false-clean. RED: the included
  deps are absent (and an include cycle would recurse unbounded). GREEN: the
  included file's deps ARE scanned, and an include cycle is bounded by
  ``visited``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make src/ importable when running pytest from the repo root without an
# editable install (mirrors tests/test_scanner.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


# --------------------------------------------------------------------------- #
# Test helpers (self-contained mirrors of the _FakeResponse stand-in in
# tests/test_scanner.py / test_v080_fixes.py — kept local so this module has
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
# Fix 1 — fix-pypi-walk-404-silent-drop
# --------------------------------------------------------------------------- #


def test_pypi_walk_404_release_doc_surfaces_as_unscanned(tmp_path, monkeypatch):
    """fix-pypi-walk-404-silent-drop: a PyPI dep whose release-doc fetch
    404s / over-caps / is unparseable (a yanked or unpublished pinned
    version, or a transient registry blip — itself a supply-chain red flag)
    must NOT vanish from BOTH ``packages`` and ``marker_skipped`` before the
    ``yield ResolvedPackage``. The v0.8.0 ``_walk_pypi`` did ``info =
    _fetch_pypi_release(...)`` then ``if info is None: continue`` BEFORE
    yielding, so the dep was in NEITHER ``packages`` NOR ``marker_skipped``
    and never reached the fetcher (whose v0.3.0
    ``fix-404-empty-corpus-silent-clean`` would otherwise surface it as
    ``version_not_found`` unscanned) — zero findings, zero unscanned signal,
    exit 0. This is the PyPI-side analog of v0.7.0's
    ``fix-npm-no-lockfile-404-silent-drop`` (closed the npm path, missed this
    sibling). The fix surfaces it as a coverage gap (``pypi_release_not_found``
    via the existing ``marker_skipped`` / ``skipped_seen`` plumbing) so the
    CLI reports an ``UnscannedPackage`` instead of exiting 0 clean. The
    ``not resolved_version`` branch (a malformed doc with no ``version``
    field) is covered by the same marker. RED: ``skipped`` is empty. GREEN:
    ``marker_skipped`` carries ``pypi_release_not_found``."""
    import requests

    from promptaudit.resolver import MarkerSkipped, resolve

    # A pinned PyPI dep whose version was yanked / unpublished — the release
    # doc at /pypi/<name>/<version>/json 404s.
    (tmp_path / "requirements.txt").write_text(
        "yanked-pkg==1.0.0\n", encoding="utf-8"
    )

    def _fake_get(self, url, *a, **kw):
        # Every PyPI release-doc fetch 404s (the dep is yanked / unpublished).
        return _FakeResponse(status_code=404)

    monkeypatch.setattr(requests.Session, "get", _fake_get)

    skipped: list[MarkerSkipped] = []
    pkgs = list(resolve(tmp_path, marker_skipped=skipped))

    # Hard signal: the yanked dep must NOT be yielded as a scanned package.
    yanked = [p for p in pkgs if p.name == "yanked-pkg"]
    assert not yanked, (
        "a yanked / 404'd PyPI dep must NOT be scanned at a version whose "
        f"release doc 404s; got {yanked}"
    )
    # And it must be surfaced as a coverage gap, not dropped silently — the
    # v0.8.0 ``if info is None: continue`` left it in neither ``packages``
    # nor ``marker_skipped`` (exit 0 clean, zero unscanned signal).
    yanked_skipped = [ms for ms in skipped if ms.name == "yanked-pkg"]
    assert yanked_skipped, (
        "a 404'd PyPI release-doc fetch must surface as a coverage gap "
        f"(marker_skipped/pypi_release_not_found); skipped was {skipped}"
    )
    assert yanked_skipped[0].marker == "pypi_release_not_found", (
        yanked_skipped[0].marker
    )
    assert yanked_skipped[0].ecosystem == "pypi", yanked_skipped[0].ecosystem
    # via_path threads the resolve path so the CLI can report where the dep
    # entered the tree.
    assert yanked_skipped[0].via_path == ("<root>", "yanked-pkg"), (
        yanked_skipped[0].via_path
    )


# --------------------------------------------------------------------------- #
# Fix 2 — fix-npm-registry-versions-non-dict-crash
# --------------------------------------------------------------------------- #


def test_npm_registry_non_dict_versions_does_not_crash(tmp_path, monkeypatch):
    """fix-npm-registry-versions-non-dict-crash: a crafted npm packument (the
    tool's canonical supply-chain surface) whose ``versions`` field is a
    truthy non-dict (``"versions": ["a","b"]``) must NOT crash the resolver.
    The v0.8.0 ``_resolve_npm_max_satisfying`` did ``versions =
    list((doc.get("versions") or {}).keys())`` whose ``or {}`` only guards a
    falsy / absent ``versions``; a non-empty list slipped past (``if not
    versions`` is False for a list) and ``["a","b"].keys()`` raised
    ``AttributeError``, crashing the CLI on a routine ``scan .``. The fix
    guards ``isinstance(versions_raw, dict)`` and returns the
    ``_NpmRangeUnsatisfiable`` sentinel (the same coverage gap the
    empty-versions path returns) — never a crash. RED: raises
    ``AttributeError`` (the call to ``resolve`` crashes). GREEN: surfaces as
    ``range_unsatisfiable:^1.2.0`` unscanned."""
    import requests

    from promptaudit.resolver import MarkerSkipped, resolve

    (tmp_path / "package.json").write_text(
        json.dumps({"name": "app", "dependencies": {"leftpad": "^1.2.0"}}),
        encoding="utf-8",
    )

    def _fake_get(self, url, *a, **kw):
        # Crafted packument: a truthy non-dict `versions` field (a list).
        # The v0.8.0 `or {}` guard returns the list unchanged, so `.keys()`
        # below would raise AttributeError.
        if url.endswith("/leftpad"):
            return _FakeResponse(
                status_code=200,
                payload={
                    "dist-tags": {"latest": "9.9.9"},
                    "versions": ["a", "b"],
                },
            )
        return _FakeResponse(status_code=404)

    monkeypatch.setattr(requests.Session, "get", _fake_get)

    # Must not raise. On the v0.8.0 baseline this raises AttributeError from
    # `["a","b"].keys()` and the test errors out here.
    skipped: list[MarkerSkipped] = []
    pkgs = list(resolve(tmp_path, marker_skipped=skipped))

    leftpad = [p for p in pkgs if p.name == "leftpad"]
    assert not leftpad, (
        "leftpad ^1.2.0 with a non-dict versions field must NOT be resolved "
        f"(a non-dict versions is a coverage gap, not a fetchable version); "
        f"got {leftpad}"
    )
    leftpad_skipped = [ms for ms in skipped if ms.name == "leftpad"]
    assert leftpad_skipped, (
        "a non-dict versions field must surface as a coverage gap "
        f"(range_unsatisfiable); skipped was {skipped}"
    )
    assert leftpad_skipped[0].marker == "range_unsatisfiable:^1.2.0", (
        leftpad_skipped[0].marker
    )
    assert leftpad_skipped[0].ecosystem == "npm", leftpad_skipped[0].ecosystem


def test_npm_dist_tags_non_dict_does_not_crash(monkeypatch):
    """fix-npm-registry-versions-non-dict-crash (sibling site — dist-tags):
    a crafted packument whose ``dist-tags`` field is a non-dict (list) would
    crash on ``tags.get(...)`` in ``_resolve_npm_dist_tag``. The fix guards
    ``isinstance(tags, dict)`` and returns ``None`` (surfaced as
    ``npm_doc_not_found``, mirroring the ``doc is None`` path). RED: raises
    ``AttributeError`` (list has no ``.get``). GREEN: returns ``None``."""
    import requests

    from promptaudit.resolver import _http_session, _resolve_npm_dist_tag

    def _fake_get(self, url, *a, **kw):
        return _FakeResponse(
            status_code=200, payload={"dist-tags": ["a", "b"]}
        )

    monkeypatch.setattr(requests.Session, "get", _fake_get)

    # Must not raise. On the v0.8.0 baseline `["a","b"].get("latest")` raises
    # AttributeError here.
    session = _http_session()
    result = _resolve_npm_dist_tag(session, "leftpad", "latest")
    assert result is None, (
        "a non-dict dist-tags field must be treated as an unusable doc "
        f"(return None -> npm_doc_not_found); got {result!r}"
    )


def test_pypi_releases_non_dict_does_not_crash(monkeypatch):
    """fix-npm-registry-versions-non-dict-crash (sibling site — releases): a
    crafted PyPI doc whose ``releases`` field is a non-iterable non-dict
    (``"releases": 42``) would crash on ``for ver_str in releases`` (TypeError:
    'int' object is not iterable) in ``_resolve_pypi_max_satisfying``. The
    fix guards ``isinstance(releases, dict)`` and returns ``_PinFailed``
    (mirroring the ``doc is None`` path). RED: raises ``TypeError``. GREEN:
    returns ``_PinFailed``."""
    import requests
    from packaging.specifiers import SpecifierSet

    from promptaudit.resolver import _PinFailed, _http_session, _resolve_pypi_max_satisfying

    def _fake_get(self, url, *a, **kw):
        return _FakeResponse(status_code=200, payload={"releases": 42})

    monkeypatch.setattr(requests.Session, "get", _fake_get)

    # Must not raise. On the v0.8.0 baseline `for ver_str in 42` raises
    # TypeError: 'int' object is not iterable here.
    session = _http_session()
    result = _resolve_pypi_max_satisfying(
        session, "foo", SpecifierSet("~=1.4.2")
    )
    assert isinstance(result, _PinFailed), (
        "a non-dict releases field must be treated as a pin-lookup failure "
        f"(return _PinFailed -> coverage gap); got {result!r}"
    )


# --------------------------------------------------------------------------- #
# Fix 3 — fix-requirements-r-include-silent-drop
# --------------------------------------------------------------------------- #


def test_requirements_r_include_deps_are_scanned(tmp_path, monkeypatch):
    """fix-requirements-r-include-silent-drop: a ``requirements.txt`` that
    does ``-r base.txt`` (the common pip-tools / multi-env pattern) must have
    the INCLUDED file's deps scanned, not silently dropped. The v0.8.0
    ``_resolve_requirements_txt`` skipped every line starting with ``-``, so
    a ``-r base.txt`` include dropped every dep listed in ``base.txt`` from
    ``packages``, ``packages_with_coverage``'s ``missing``, and
    ``marker_skipped`` — a silent partial-tree false-clean (exit 0 if the
    top-level deps happen to be clean). The fix resolves ``-r`` /
    ``--requirements`` includes recursively (path relative to the current
    file's dir, the way pip resolves includes). RED: ``included-dep`` is
    absent from the resolved set. GREEN: ``included-dep`` IS scanned."""
    import requests

    from promptaudit.resolver import resolve

    # Top-level requirements includes base.txt, then lists a top-level dep.
    (tmp_path / "requirements.txt").write_text(
        "-r base.txt\ntop-dep==1.0.0\n", encoding="utf-8"
    )
    # The included file lists a dep that must be scanned, not dropped.
    (tmp_path / "base.txt").write_text(
        "included-dep==2.0.0\n", encoding="utf-8"
    )

    def _fake_get(self, url, *a, **kw):
        # Minimal PyPI release docs so each pinned dep resolves to a real
        # ResolvedPackage (no transitive deps -> no further fetches).
        if url == "https://pypi.org/pypi/included-dep/2.0.0/json":
            return _FakeResponse(
                status_code=200,
                payload={"info": {"version": "2.0.0", "requires_dist": []}},
            )
        if url == "https://pypi.org/pypi/top-dep/1.0.0/json":
            return _FakeResponse(
                status_code=200,
                payload={"info": {"version": "1.0.0", "requires_dist": []}},
            )
        return _FakeResponse(status_code=404)

    monkeypatch.setattr(requests.Session, "get", _fake_get)

    pkgs = list(resolve(tmp_path))
    names = {p.name for p in pkgs}

    # Hard signal: the dep listed in the INCLUDED base.txt must be scanned.
    # On the v0.8.0 baseline the `-r base.txt` line was skipped, so
    # included-dep never entered `direct` and never resolved.
    assert "included-dep" in names, (
        "a dep listed in an `-r base.txt`-included requirements file must be "
        f"scanned, not silently dropped; resolved {sorted(names)}"
    )
    included = [p for p in pkgs if p.name == "included-dep"]
    assert included[0].version == "2.0.0", included[0].version
    assert included[0].ecosystem == "pypi", included[0].ecosystem
    # The top-level dep is still scanned too (the include fix must not
    # over-skip the rest of the file).
    assert "top-dep" in names, (
        f"the top-level dep must still be scanned; resolved {sorted(names)}"
    )


def test_requirements_r_include_cycle_is_bounded(tmp_path, monkeypatch):
    """fix-requirements-r-include-silent-drop (cycle guard): an include cycle
    (``a.txt`` includes ``b.txt`` which includes ``a.txt``) must be bounded
    by the ``visited`` set so the recursion terminates, and every dep listed
    in the cycle is still scanned once. RED (v0.8.0): every ``-r`` line was
    skipped, so neither ``a.txt`` nor ``b.txt`` was read and both deps were
    absent (and without a cycle guard the recursion would be unbounded).
    GREEN: both deps resolved once, no crash."""
    import requests

    from promptaudit.resolver import resolve

    # requirements.txt -> a.txt -> b.txt -> a.txt (cycle). Each file also
    # lists a distinct dep so we can assert each is scanned exactly once.
    (tmp_path / "requirements.txt").write_text("-r a.txt\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("-r b.txt\ndep-a==1.0.0\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("-r a.txt\ndep-b==2.0.0\n", encoding="utf-8")

    def _fake_get(self, url, *a, **kw):
        if url == "https://pypi.org/pypi/dep-a/1.0.0/json":
            return _FakeResponse(
                status_code=200,
                payload={"info": {"version": "1.0.0", "requires_dist": []}},
            )
        if url == "https://pypi.org/pypi/dep-b/2.0.0/json":
            return _FakeResponse(
                status_code=200,
                payload={"info": {"version": "2.0.0", "requires_dist": []}},
            )
        return _FakeResponse(status_code=404)

    monkeypatch.setattr(requests.Session, "get", _fake_get)

    # Must terminate (visited bounds the a->b->a cycle) and not raise.
    pkgs = list(resolve(tmp_path))
    names = {p.name for p in pkgs}

    assert "dep-a" in names, (
        f"dep-a (listed in a.txt) must be scanned; resolved {sorted(names)}"
    )
    assert "dep-b" in names, (
        f"dep-b (listed in b.txt) must be scanned; resolved {sorted(names)}"
    )
    # Each dep resolved exactly once despite the cycle (the second a.txt
    # entry is a visited no-op).
    dep_a = [p for p in pkgs if p.name == "dep-a"]
    dep_b = [p for p in pkgs if p.name == "dep-b"]
    assert len(dep_a) == 1, f"dep-a resolved once, got {dep_a}"
    assert len(dep_b) == 1, f"dep-b resolved once, got {dep_b}"
