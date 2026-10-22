"""v0.12.0 regression tests — four defect classes reproduced against shipped v0.11.0
by the inline bug-hunt:

1. fix-empty-resolve-drops-coverage-gaps — ``scan_cmd`` short-circuits on
   ``if not packages`` with exit 0 *before* the coverage pass renders, so every
   coverage gap ``resolve()`` recorded in ``marker_skipped`` is discarded when
   nothing resolved (cli.py:147-149). ``fetch_cmd`` did not even pass
   ``marker_skipped`` down.
2. fix-poetry-lock-entry-shape-crash — a poetry.lock ``[[package]]`` entry with a
   non-list ``groups`` (resolver.py:778) or a non-string ``name``
   (resolver.py:784) crashed the CLI with a raw TypeError / AttributeError
   traceback instead of the clean ``ResolverError`` + exit 2 contract.
3. fix-npm-v-prefix-spec-unresolved — a leading-``v`` exact version spec
   (``v1.3.0``, which npm itself accepts) was routed to the dist-tag branch
   (resolver.py:526-528), resolved to nothing, and the dep was never scanned.
4. fix-requirements-direct-marker-ignored — PEP 508 markers on DIRECT
   requirements.txt / pyproject deps were never evaluated
   (resolver.py:935-939, :1076-1081), so a dep the target platform does not
   install was still resolved, fetched and scanned.

Each test is RED on the v0.11.0 baseline and GREEN after the fix.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make src/ importable when running pytest from the repo root without an
# editable install (mirrors the other test modules).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest
from click.testing import CliRunner


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
    """Stand-in session: every GET returns the given payload (or a per-URL map)."""

    def __init__(self, payload):
        self._payload = payload

    def get(self, url, *a, **kw):
        if isinstance(self._payload, dict) and "by_url" in self._payload:
            payload = self._payload["by_url"].get(url, {})
        else:
            payload = self._payload
        return _FakeResponse(status_code=200, payload=payload)


# --------------------------------------------------------------------------- #
# 1 — fix-empty-resolve-drops-coverage-gaps
# --------------------------------------------------------------------------- #


def _all_gap_project(tmp_path: Path) -> Path:
    """A project whose only dependency cannot be resolved (no version satisfies
    ``^99.0.0``) — so ``packages`` is empty and ``marker_skipped`` is not."""
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "dependencies": {"left-pad": "^99.0.0"}}),
        encoding="utf-8",
    )
    return tmp_path


def _packument_with_only_1_0_0():
    return {
        "name": "left-pad",
        "dist-tags": {"latest": "1.0.0"},
        "versions": {"1.0.0": {"name": "left-pad", "version": "1.0.0"}},
    }


def test_scan_all_gap_project_reports_gap_and_exits_nonzero(tmp_path, monkeypatch):
    """When every resolved dependency landed in a coverage gap, the CLI must
    surface that gap and exit 3 — not print "no dependencies resolved — nothing
    to scan" and exit 0 while ``marker_skipped`` already holds
    ``range_unsatisfiable:^99.0.0`` (cli.py:147-149 on v0.11.0)."""
    import requests

    from promptaudit.cli import main

    project = _all_gap_project(tmp_path)
    monkeypatch.setattr(
        requests.Session, "get", lambda self, url, *a, **kw: _FakeResponse(
            status_code=200, payload=_packument_with_only_1_0_0()
        )
    )

    result = CliRunner().invoke(
        main, ["scan", str(project), "--no-fetch", "--json"]
    )

    assert result.exit_code == 3, (
        f"an all-coverage-gap project must exit 3 (coverage gap), got "
        f"{result.exit_code}; output={result.output!r}"
    )
    payload = json.loads(result.stdout)
    reasons = {u["package"]: u["reason"] for u in payload["unscanned"]}
    assert "left-pad" in reasons, payload
    assert "range_unsatisfiable" in reasons["left-pad"], reasons


def test_scan_genuinely_empty_project_still_exits_zero(tmp_path):
    """A project that genuinely declares no dependencies must keep the exit-0
    "nothing to scan" path — the coverage-gap change must not turn it into a
    failure."""
    from promptaudit.cli import main

    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x"}), encoding="utf-8"
    )
    result = CliRunner().invoke(main, ["scan", str(tmp_path), "--no-fetch"])
    assert result.exit_code == 0, result.output
    assert "nothing to scan" in result.output


def test_fetch_reports_coverage_gap_and_exits_nonzero(tmp_path, monkeypatch):
    """``promptaudit fetch`` must thread the coverage gaps through and exit 3 —
    on v0.11.0 it called ``resolve()`` with no ``marker_skipped`` and printed
    "fetched: ok=0 skipped=0 errors=0" + exit 0 for this project."""
    import requests

    from promptaudit.cli import main

    project = _all_gap_project(tmp_path)
    monkeypatch.setattr(
        requests.Session, "get", lambda self, url, *a, **kw: _FakeResponse(
            status_code=200, payload=_packument_with_only_1_0_0()
        )
    )

    result = CliRunner().invoke(
        main, ["fetch", str(project), "--cache-root", str(tmp_path / "cache")]
    )

    assert result.exit_code == 3, (
        f"fetch must exit 3 when the only dep is an unresolved coverage gap; got "
        f"{result.exit_code}; output={result.output!r}"
    )
    assert "left-pad" in result.output, result.output


# --------------------------------------------------------------------------- #
# 2 — fix-poetry-lock-entry-shape-crash
# --------------------------------------------------------------------------- #


def test_poetry_lock_non_list_groups_raises_resolver_error(tmp_path):
    """A poetry.lock entry with a truthy non-list ``groups`` (``groups = 42``)
    must raise the clean ``ResolverError`` — not ``TypeError: argument of type
    'int' is not iterable`` at resolver.py:778."""
    from promptaudit.resolver import ResolverError, resolve

    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "requests"\nversion = "2.31.0"\ngroups = 42\n',
        encoding="utf-8",
    )
    with pytest.raises(ResolverError, match="groups"):
        list(resolve(tmp_path))


def test_poetry_lock_non_string_name_raises_resolver_error(tmp_path):
    """A poetry.lock entry with a non-string ``name`` (``name = ["requests"]``)
    must raise the clean ``ResolverError`` — not ``AttributeError: 'list' object
    has no attribute 'lower'`` at resolver.py:784."""
    from promptaudit.resolver import ResolverError, resolve

    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = ["requests"]\nversion = "2.31.0"\n',
        encoding="utf-8",
    )
    with pytest.raises(ResolverError, match="name"):
        list(resolve(tmp_path))


def test_poetry_lock_valid_entry_still_resolves(tmp_path):
    """The guards must not change valid lockfiles: a well-formed entry with a
    ``groups`` list still resolves, and a dev-group entry is still skipped."""
    from promptaudit.resolver import resolve

    (tmp_path / "poetry.lock").write_text(
        '[[package]]\nname = "Requests"\nversion = "2.31.0"\ngroups = ["main"]\n'
        "\n"
        '[[package]]\nname = "pytest"\nversion = "8.0.0"\ngroups = ["dev"]\n',
        encoding="utf-8",
    )
    pkgs = list(resolve(tmp_path))
    assert [(p.name, p.version) for p in pkgs] == [("requests", "2.31.0")], pkgs


# --------------------------------------------------------------------------- #
# 3 — fix-npm-v-prefix-spec-unresolved
# --------------------------------------------------------------------------- #


def test_npm_v_prefixed_exact_spec_resolves(tmp_path, monkeypatch):
    """``v1.3.0`` is a valid npm version spec (``npm view left-pad@v1.3.0
    version`` -> 1.3.0) and must resolve to 1.3.0 — on v0.11.0 it took the
    dist-tag branch, resolved to nothing, and the dep was never scanned."""
    import requests

    from promptaudit.resolver import resolve

    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "dependencies": {"left-pad": "v1.3.0"}}),
        encoding="utf-8",
    )
    payload = {
        "by_url": {
            "https://registry.npmjs.org/left-pad/1.3.0": {
                "name": "left-pad",
                "version": "1.3.0",
                "dependencies": {},
            },
        }
    }
    monkeypatch.setattr(
        requests.Session, "get", lambda self, url, *a, **kw: _FakeResponse(
            status_code=200, payload=payload["by_url"].get(url, {})
        )
    )

    pkgs = list(resolve(tmp_path))
    assert [(p.name, p.version) for p in pkgs] == [("left-pad", "1.3.0")], pkgs


# --------------------------------------------------------------------------- #
# 4 — fix-requirements-direct-marker-ignored
# --------------------------------------------------------------------------- #


def _pypi_release_payload(name, version):
    return {"info": {"name": name, "version": version, "requires_dist": None}}


def test_direct_requirement_marker_not_applicable_is_skipped(tmp_path, monkeypatch):
    """A direct requirements.txt line gated by a non-applicable marker
    (``; sys_platform == "win32"`` on a POSIX host) must NOT be resolved,
    fetched or scanned — pip does not install it. On v0.11.0 it was resolved
    (resolver.py:935-939 never consulted ``req.marker``)."""
    import requests

    from promptaudit.resolver import resolve

    (tmp_path / "requirements.txt").write_text(
        'foo==1.0.0 ; sys_platform == "win32"\n', encoding="utf-8"
    )
    calls: list[str] = []

    def _fake_get(self, url, *a, **kw):
        calls.append(url)
        return _FakeResponse(status_code=200, payload=_pypi_release_payload("foo", "1.0.0"))

    monkeypatch.setattr(requests.Session, "get", _fake_get)
    if sys.platform == "win32":  # pragma: no cover - test host is POSIX
        pytest.skip("host is Windows; the marker would apply")

    marker_skipped: list = []
    pkgs = list(resolve(tmp_path, marker_skipped=marker_skipped))

    assert pkgs == [], (
        f"a marker-excluded direct dep must not be resolved on this host; got {pkgs}"
    )
    assert calls == [], f"a marker-excluded dep must not be fetched; got {calls}"
    assert [m.name for m in marker_skipped] == ["foo"], marker_skipped
    assert "sys_platform" in marker_skipped[0].marker, marker_skipped[0].marker


def test_direct_requirement_marker_applicable_still_resolves(tmp_path, monkeypatch):
    """The same marker under ``--target-platform windows`` must resolve the dep
    (the flag is documented to make a win32-gated dep auditable from CI)."""
    import requests

    from promptaudit.resolver import resolve

    (tmp_path / "requirements.txt").write_text(
        'foo==1.0.0 ; sys_platform == "win32"\n', encoding="utf-8"
    )
    monkeypatch.setattr(
        requests.Session, "get", lambda self, url, *a, **kw: _FakeResponse(
            status_code=200, payload=_pypi_release_payload("foo", "1.0.0")
        )
    )

    pkgs = list(resolve(tmp_path, target_platform="windows"))
    assert [(p.name, p.version) for p in pkgs] == [("foo", "1.0.0")], pkgs
