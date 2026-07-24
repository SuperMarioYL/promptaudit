"""v0.6.0 regression tests — three correctness fixes.

Each test is written to be RED on the v0.5.0 baseline (the bug class it
targets) and GREEN after the fix:

* ``test_sdist_60_members_all_scanned`` — fix-sdist-budget-per-member-charge:
  the v0.5.0 per-member charge of ``max(len(blob), MAX_TEXT_FILE_BYTES)``
  floored every member at the 512KB cap, so the 24MB budget exhausted at 48
  members and truncated legit >48-member sdists. RED: 48 of 60. GREEN: all 60.
* ``test_pypi_registry_json_fetch_is_bounded`` — fix-registry-json-fetch-unbounded:
  the v0.5.0 npm/PyPI registry-JSON GETs used non-streaming ``.json()`` with
  no byte cap, so a crafted multi-MB registry doc OOMed the scanner. RED:
  ``.json()`` parses the whole doc. GREEN: abandoned at the byte cap.
* ``test_tilde_pin_lookup_failure_surfaces_as_unscanned_not_latest`` —
  fix-tilde-pin-lookup-failure-resolves-latest: a ``~=1.4.2`` pin whose
  max-satisfying lookup failed (network blip) returned ``None`` and fell
  through to the version-less ``/{name}/json`` latest path, auditing registry
  LATEST ``2.0.0`` outside the ``~=1.4.2`` range. RED: audits 2.0.0. GREEN:
  surfaces as UnscannedPackage.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make src/ importable when running pytest from the repo root without an
# editable install (mirrors tests/test_scanner.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


# --------------------------------------------------------------------------- #
# Test helpers (self-contained mirrors of the _StreamResponse / _FakeResponse
# stands-in in tests/test_scanner.py — kept local so this module has no
# cross-test-module import coupling).
# --------------------------------------------------------------------------- #


class _StreamResponse:
    """Streaming-capable stand-in for requests.Response (iter_content + json)."""

    def __init__(self, *, status_code=200, content=b"", payload=None):
        self.status_code = status_code
        self._content = content
        self._payload = payload or {}

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(str(self.status_code))


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
# Fix A — fix-sdist-budget-per-member-charge
# --------------------------------------------------------------------------- #


def test_sdist_60_members_all_scanned(monkeypatch):
    """A 60-member sdist of small ``.py`` members must have ALL 60 string
    literals collected — not just 48. The v0.5.0 ``_read_and_account`` charged
    ``max(len(blob), MAX_TEXT_FILE_BYTES)`` per member, flooring every member at
    the 512KB cap; the 24MB budget exhausted at 48 members (24MiB / 512KiB),
    truncating legit >48-member sdists. The fix debits ACTUAL bytes consumed
    per member (so a 80-byte member debits 80 bytes, not 512KB) while keeping
    the oversized-member flood guard (cap+1 charge) intact."""
    import io
    import tarfile

    import requests

    from promptaudit.fetcher import _extract_strings_from_sdist

    member_count = 60
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for i in range(member_count):
            # A unique ≥40-char double-quoted string literal per member so the
            # collected set lets us count exactly which members were scanned.
            # No internal quotes so STRING_LITERAL_RE captures cleanly.
            payload = (
                b'msg = "member_' + str(i).zfill(3).encode()
                + b'_marker_ignore_previous_instructions_value"\n'
            )
            info = tarfile.TarInfo(name=f"pkg/mod_{i}.py")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    blob = raw.getvalue()

    monkeypatch.setattr(
        requests.Session,
        "get",
        lambda self, url, *a, **kw: _StreamResponse(status_code=200, content=blob),
    )

    session = requests.Session()
    strings = _extract_strings_from_sdist(
        session, "https://example.test/pkg-1.0.0.tar.gz"
    )

    found = [
        s
        for s in strings
        if s.startswith("member_")
        and s.endswith("_marker_ignore_previous_instructions_value")
    ]
    assert len(found) == member_count, (
        f"expected all {member_count} members scanned; got {len(found)} "
        f"(the v0.5.0 per-member-cap floor truncated at 48). Collected: {found}"
    )
    # Sanity: each member index 0..59 is present exactly once.
    indexes = sorted(int(s[len("member_") : len("member_") + 3]) for s in found)
    assert indexes == list(range(member_count)), indexes


# --------------------------------------------------------------------------- #
# Fix B — fix-registry-json-fetch-unbounded
# --------------------------------------------------------------------------- #


def test_pypi_registry_json_fetch_is_bounded(tmp_path, monkeypatch):
    """A >5MB PyPI registry-JSON response must be bounded — the scanner must
    NOT parse the whole doc into memory. The v0.5.0 ``_fetch_pypi`` used
    non-streaming ``session.get()`` + ``.json()`` with no byte cap, so a
    crafted multi-MB registry doc OOMed the scanner on a routine ``promptaudit
    scan .`` (a PyPI registry GET happens on every resolved package). The fix
    routes the GET through ``_read_bounded`` (cap a few MB) so the oversized
    doc is abandoned → empty corpus (coverage gap), and ``resp.json()`` is
    never called."""
    import requests

    from promptaudit.fetcher import MAX_REGISTRY_JSON_BYTES, _fetch_pypi
    from promptaudit.resolver import ResolvedPackage

    # A registry doc larger than the cap (and not valid JSON — the bounded
    # path must abort before parsing; the unbounded path would hand the whole
    # multi-MB body to .json()).
    big = b"x" * (MAX_REGISTRY_JSON_BYTES + 1024 * 1024)
    json_called = {"n": 0}

    class _OversizedResponse(_StreamResponse):
        def json(self):  # pragma: no cover — must NOT be called on the green path
            json_called["n"] += 1
            return {}  # if the bug is present, .json() is called on the whole doc

    monkeypatch.setattr(
        requests.Session,
        "get",
        lambda self, url, *a, **kw: _OversizedResponse(status_code=200, content=big),
    )

    pkg = ResolvedPackage(name="bigpkg", version="1.0.0", ecosystem="pypi")
    target = tmp_path / "cache"
    target.mkdir()
    sources = _fetch_pypi(pkg, target, requests.Session())

    assert sources == {}, "oversized registry doc must yield no sources (bounded)"
    assert json_called["n"] == 0, (
        "the oversized registry doc must be abandoned at the byte cap, not "
        "parsed whole via .json() (the unbounded OOM path); .json() was "
        f"called {json_called['n']} time(s)"
    )
    # And the oversized cache dir must NOT have been promoted (coverage gap,
    # not a silent clean scan) — mirrors the v0.2.0 fetch-error contract.
    assert not any(target.iterdir()), "no source files written for the bounded doc"


# --------------------------------------------------------------------------- #
# Fix C — fix-tilde-pin-lookup-failure-resolves-latest
# --------------------------------------------------------------------------- #


def test_tilde_pin_lookup_failure_surfaces_as_unscanned_not_latest(
    tmp_path, monkeypatch
):
    """A ``foo~=1.4.2`` pin whose ``_resolve_pypi_max_satisfying`` returns the
    ``_PinFailed`` sentinel (a network blip on the releases-list GET, an
    over-cap body, or no satisfying version) must surface as an
    ``UnscannedPackage`` coverage gap — NOT fall through to the version-less
    ``/{name}/json`` latest path and audit registry LATEST ``2.0.0``, which is
    outside the ``~=1.4.2`` range. Previously a failed lookup returned ``None``,
    indistinguishable from a loose spec's ``None``, so a transient registry
    outage silently demoted the pin to "audit whatever 2.0.0 the registry
    happens to advertise" — exactly the wrong version."""
    import requests
    from packaging.specifiers import SpecifierSet

    from promptaudit.resolver import MarkerSkipped, _PinFailed, resolve

    (tmp_path / "requirements.txt").write_text("foo~=1.4.2\n", encoding="utf-8")

    # Stub the max-satisfying lookup to return the _PinFailed sentinel — the
    # exact shape a network blip / over-cap / no-satisfying-version produces.
    monkeypatch.setattr(
        "promptaudit.resolver._resolve_pypi_max_satisfying",
        lambda session, name, specifier: _PinFailed(),
    )

    # Bait: if the fix is absent, _walk_pypi proceeds to _fetch_pypi_release
    # with version=_PinFailed() (truthy) → a version-specific (bogus) URL
    # /pypi/foo/<repr>/json, OR with version=None (the v0.5.0 blip shape) →
    # the version-less /foo/json latest path. Either way the registry would
    # advertise 2.0.0; catch BOTH shapes so the "NOT audited as 2.0.0" signal
    # is meaningful (the 2.0.0 is available but the fix must not pick it).
    def _fake_get(self, url, *a, **kw):
        if "/foo/" in url and url.endswith("/json"):
            return _FakeResponse(
                status_code=200,
                payload={
                    "info": {"version": "2.0.0", "requires_dist": None},
                    "releases": {},
                },
            )
        return _FakeResponse(status_code=404)

    monkeypatch.setattr(requests.Session, "get", _fake_get)

    skipped: list[MarkerSkipped] = []
    pkgs = list(resolve(tmp_path, marker_skipped=skipped))

    foo = [p for p in pkgs if p.name == "foo"]
    # Hard signal: foo must NOT be resolved to a (wrong) version. The 2.0.0
    # bait was available on every /foo/.../json path, but the fix must short-
    # circuit at the _PinFailed sentinel and never fetch it.
    assert not foo, (
        "foo~=1.4.2 with a failed pin lookup must NOT be resolved to a version "
        f"(would be the out-of-range 2.0.0 the registry advertises); got {foo}"
    )
    foo_versions = {p.version for p in pkgs if p.name == "foo"}
    assert "2.0.0" not in foo_versions, (
        f"2.0.0 must NOT be audited for a ~=1.4.2 pin whose lookup failed; "
        f"got versions {foo_versions}"
    )
    # And it must be surfaced as a coverage gap, not dropped silently.
    foo_skipped = [ms for ms in skipped if ms.name == "foo"]
    assert foo_skipped, (
        "foo pin-failure must surface as a coverage gap (marker_skipped); "
        f"skipped was {skipped}"
    )
    assert foo_skipped[0].marker == "pin_failed", foo_skipped[0].marker
    assert foo_skipped[0].ecosystem == "pypi", foo_skipped[0].ecosystem
    # Sanity: 2.0.0 is genuinely outside the ~=1.4.2 range, so auditing it
    # would be a false-clean on the "audit the version you install" promise.
    assert "2.0.0" not in SpecifierSet("~=1.4.2")
