"""v0.8.0 regression tests — two correctness fixes.

Each test is written to be RED on the v0.7.0 baseline (the bug class it
targets) and GREEN after the fix:

* ``test_npm_lockfile_v2_workspace_not_yielded_but_registry_dep_is`` —
  fix-npm-lockfile-v2-workspace-leak: ``_walk_lockfile_v2`` only skipped
  ``path_key == ""`` (the project root), so npm v2/v3 workspace packages
  keyed by their RELATIVE directory path (e.g. ``"apps/web"``,
  ``"packages/foo"``) — which carry a ``version`` (from the workspace's
  package.json) and no ``dev``/``peer``/``optional`` flag — passed every
  guard and were yielded as ``ResolvedPackage(name="apps/web", ...)``. The
  fetcher then GETs ``registry.npmjs.org/apps/web/<ver>`` which 404s,
  surfacing the workspace as a bogus ``version_not_found`` UnscannedPackage
  with a nonsense name; under ``--fail-on-fetch-error`` this false-fails CI
  on every workspace monorepo (the dominant modern npm layout). RED: the
  ``apps/web`` workspace is yielded with name ``"apps/web"``. GREEN: skipped,
  while a sibling ``node_modules/lodash`` IS yielded.
* ``test_npm_range_empty_versions_does_not_resolve_to_latest`` —
  fix-npm-range-empty-versions-falls-to-latest: ``_resolve_npm_max_satisfying``
  returned ``(doc.get("dist-tags") or {}).get("latest")`` when the ``versions``
  block was empty — a silent fall-back to the registry latest, necessarily
  OUTSIDE the requested range (else it would itself be a candidate). This is
  the exact wrong-version/false-clean class v0.7.0's
  fix-npm-range-no-match-resolves-latest closed for the empty-``candidates``
  path, but the empty-``versions`` sibling path was missed. RED: audits
  ``9.9.9``. GREEN: surfaces as ``range_unsatisfiable:<spec>`` coverage gap.
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
# tests/test_scanner.py / test_v070_fixes.py — kept local so this module has
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
# Fix 1 — fix-npm-lockfile-v2-workspace-leak
# --------------------------------------------------------------------------- #


def test_npm_lockfile_v2_workspace_not_yielded_but_registry_dep_is(tmp_path):
    """fix-npm-lockfile-v2-workspace-leak: in an npm v2/v3 lockfile the
    ``packages`` map keys workspace / local packages by their RELATIVE
    directory path (e.g. ``"apps/web"``, ``"packages/foo"``), NOT by a
    ``node_modules/...`` path. Such entries carry a ``version`` (from the
    workspace's package.json) and no ``dev``/``peer``/``optional`` flag, so
    the v0.7.0 ``_walk_lockfile_v2`` — which only skipped ``path_key == ""``
    (the project root) — yielded them as ``ResolvedPackage(name="apps/web",
    ...)``. The fetcher then GETs ``registry.npmjs.org/apps/web/<ver>`` which
    404s, surfacing the workspace as a bogus ``version_not_found``
    UnscannedPackage with a nonsense name; under ``--fail-on-fetch-error``
    this false-fails the CI gate on every workspace monorepo (the dominant
    modern npm layout) and the bogus name corrupts the terminal/JSON report.
    The fix skips any ``path_key`` that does not start with ``node_modules/``
    (workspace/local packages are not registry-fetchable) right after the
    ``path_key == ""`` guard. The lockfile walk is offline (no registry
    fetch), so this is a pure ``resolve()`` fixture test."""
    from promptaudit.resolver import resolve

    # A realistic npm v2 lockfile for a workspace monorepo: the project root
    # (""), an "apps/web" workspace package (keyed by relative path, carries a
    # version from its package.json, no dev/peer/optional flag), and a real
    # registry dep under "node_modules/lodash".
    (tmp_path / "package-lock.json").write_text(
        json.dumps(
            {
                "name": "monorepo",
                "version": "1.0.0",
                "lockfileVersion": 2,
                "requires": True,
                "packages": {
                    "": {
                        "name": "monorepo",
                        "version": "1.0.0",
                        "workspaces": ["apps/*", "packages/*"],
                    },
                    "apps/web": {
                        "version": "1.2.0",
                        "dependencies": {"lodash": "^4.17.21"},
                    },
                    "node_modules/lodash": {
                        "version": "4.17.21",
                        "resolved": "https://registry.npmjs.org/lodash/-/lodash-4.17.21.tgz",
                        "integrity": "sha512-v2vDk1m+1==",
                        "license": "MIT",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    pkgs = list(resolve(tmp_path))

    names = {p.name for p in pkgs}
    # Hard signal: the "apps/web" workspace must NOT be yielded as a bogus
    # registry dep. On the v0.7.0 baseline the walker split "apps/web" on
    # "node_modules/" (no split → one segment "apps/web") and yielded
    # ResolvedPackage(name="apps/web", version="1.2.0").
    assert "apps/web" not in names, (
        "the 'apps/web' workspace package (keyed by relative path, not "
        "node_modules/) must NOT be yielded as a registry dep — the fetcher "
        "would 404 on registry.npmjs.org/apps/web/1.2.0 and false-fail CI "
        f"under --fail-on-fetch-error; got names {sorted(names)}"
    )
    # And no workspace-ish relative-path name should leak through at all.
    leaked = [n for n in names if "/" in n and not n.startswith("@")]
    assert not leaked, (
        f"no relative-path workspace keys should leak as package names; "
        f"got {leaked}"
    )
    # The sibling registry dep under node_modules/ MUST still be yielded —
    # the workspace guard must not over-skip real registry entries.
    lodash = [p for p in pkgs if p.name == "lodash"]
    assert lodash, (
        f"the node_modules/lodash registry dep must still be yielded; "
        f"got names {sorted(names)}"
    )
    assert lodash[0].version == "4.17.21", lodash[0].version
    assert lodash[0].ecosystem == "npm", lodash[0].ecosystem
    # Sanity: only the real registry dep (plus no workspace) is resolved.
    assert names == {"lodash"}, (
        f"expected only lodash resolved; got {sorted(names)}"
    )


# --------------------------------------------------------------------------- #
# Fix 2 — fix-npm-range-empty-versions-falls-to-latest
# --------------------------------------------------------------------------- #


def test_npm_range_empty_versions_does_not_resolve_to_latest(
    tmp_path, monkeypatch
):
    """fix-npm-range-empty-versions-falls-to-latest: an npm range spec whose
    registry document has an EMPTY ``versions`` block (but a
    ``dist-tags.latest`` present) must NOT fall back to that latest — it is
    necessarily OUTSIDE the requested range (else it would itself be a
    candidate), so auditing it scans a version the project cannot even
    install and false-passes the CI gate. The v0.7.0
    ``_resolve_npm_max_satisfying`` returned ``(doc.get("dist-tags") or
    {}).get("latest")`` on this path (``"9.9.9"`` for a ``^1.2.0`` spec) —
    the exact wrong-version/false-clean class v0.7.0's
    fix-npm-range-no-match-resolves-latest closed for the empty-``candidates``
    path, but the empty-``versions`` sibling was missed. The fix returns the
    ``_NpmRangeUnsatisfiable`` sentinel (mirroring the empty-candidates path)
    so the no-lockfile walk surfaces ``range_unsatisfiable:<spec>`` as a
    coverage gap. Mirrors ``test_npm_range_no_match_does_not_resolve_to_latest``
    but with ``"versions": {}`` and a ``dist-tags.latest`` present."""
    import requests
    from packaging.version import Version

    from promptaudit.resolver import MarkerSkipped, _npm_range_matcher, resolve

    (tmp_path / "package.json").write_text(
        json.dumps({"name": "app", "dependencies": {"leftpad": "^1.2.0"}}),
        encoding="utf-8",
    )

    def _fake_get(self, url, *a, **kw):
        # Full registry document — the versions block is EMPTY (a yanked /
        # unpublished-everything / malformed registry doc), but dist-tags.latest
        # is set to 9.9.9: the out-of-range bait the v0.7.0 fallback would
        # audit for a ^1.2.0 spec.
        if url.endswith("/leftpad"):
            return _FakeResponse(
                status_code=200,
                payload={
                    "dist-tags": {"latest": "9.9.9"},
                    "versions": {},
                },
            )
        # Bait version doc — if the bug is present, the resolver fetches
        # /leftpad/9.9.9 (the out-of-range latest) and yields leftpad@9.9.9.
        if url.endswith("/leftpad/9.9.9"):
            return _FakeResponse(status_code=200, payload={"dependencies": {}})
        return _FakeResponse(status_code=404)

    monkeypatch.setattr(requests.Session, "get", _fake_get)

    skipped: list[MarkerSkipped] = []
    pkgs = list(resolve(tmp_path, marker_skipped=skipped))

    leftpad = [p for p in pkgs if p.name == "leftpad"]
    # Hard signal: leftpad must NOT be resolved to the out-of-range 9.9.9.
    assert not leftpad, (
        "leftpad ^1.2.0 whose registry doc has an EMPTY versions block must "
        "NOT fall back to dist-tags.latest 9.9.9 (necessarily outside the "
        "range); the v0.7.0 fallback would audit an un-installable version. "
        f"resolved: {leftpad}"
    )
    leftpad_versions = {p.version for p in pkgs if p.name == "leftpad"}
    assert "9.9.9" not in leftpad_versions, (
        f"9.9.9 must NOT be audited for a ^1.2.0 range whose versions block "
        f"is empty; got versions {leftpad_versions}"
    )
    # And it must be surfaced as a coverage gap, not dropped silently — the
    # empty-versions path must behave identically to the empty-candidates
    # path (both return the _NpmRangeUnsatisfiable sentinel).
    leftpad_skipped = [ms for ms in skipped if ms.name == "leftpad"]
    assert leftpad_skipped, (
        "leftpad with an empty versions block must surface as a coverage gap "
        f"(marker_skipped/range_unsatisfiable); skipped was {skipped}"
    )
    assert leftpad_skipped[0].marker == "range_unsatisfiable:^1.2.0", (
        leftpad_skipped[0].marker
    )
    assert leftpad_skipped[0].ecosystem == "npm", leftpad_skipped[0].ecosystem
    # Sanity: 9.9.9 is genuinely outside ^1.2.0 (and 1.9.9 would satisfy but
    # is not in the empty versions block), so auditing 9.9.9 would be a
    # false-clean on the "audit the version you actually install" promise.
    matcher = _npm_range_matcher("^1.2.0")
    assert not matcher(Version("9.9.9"))
    assert matcher(Version("1.9.9"))
