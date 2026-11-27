"""v0.13.0 regression tests — five defect classes reproduced against shipped v0.12.0.

1. fix-pypi-wildcard-pin-fetches-literal-version — ``_pin_from_specifier``
   returned the literal version of a PEP 440 prefix pin (``==1.2.*``,
   resolver.py:1202-1203), so the walk GET ``/pypi/<name>/1.2.*/json`` 404'd and a
   legal pin became a ``pypi_release_not_found`` coverage gap.
2. fix-crafted-registry-doc-nondict-shape — ``_get_registry_json`` returned
   whatever the document root was (resolver.py:1502), and every caller called
   ``.get(...)`` on it, so a 200 whose root is a JSON array raised AttributeError
   out of ``resolve()`` (raw traceback, exit 1 = scan's critical-finding code).
   Sibling site: an npm ``dist`` field that is not a dict (fetcher.py:248) leaked
   ``AttributeError: ...`` as the operator-facing fetch reason.
3. fix-corpus-rule-error-uncaught-traceback — ``scan``/``rules`` called
   ``load_rules`` with no ``RuleError`` handling (cli.py:198, :319), so a
   malformed ``--corpus`` crashed with a traceback instead of the clean
   ``promptaudit: <message>`` + exit 2 contract.
4. fix-requirements-attached-include-silent-drop — ``_requirements_include_path``
   knew only the space and equals forms (resolver.py:838-870), so pip's attached
   short-option form (``-rbase.txt``) was skipped as a ``-`` directive and every
   dep in the included file vanished silently.
5. fix-npm-caret-zero-wildcard-band — the caret branch applied the
   explicit-patch rule ``<0.0.patch+1`` whenever major and minor were 0
   (resolver.py:667-676), so ``^0.0`` / ``^0.0.x`` / ``^0.x`` produced the
   unsatisfiable band ``<0.0.1`` instead of npm's ``<0.1.0`` / ``<1.0.0``.

Each test is RED on the v0.12.0 baseline and GREEN after the fix.
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
    """Streaming stand-in whose iter_content serialises the payload back to JSON."""

    def __init__(self, *, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def iter_content(self, chunk_size=65536):
        if self._payload is None:
            return iter(())
        data = json.dumps(self._payload).encode("utf-8")
        return iter(data[i : i + chunk_size] for i in range(0, len(data), chunk_size))

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}")


def _install_session(monkeypatch, by_url, *, default=None):
    """Route every GET through a URL→payload map; record the URLs seen."""
    import requests

    calls: list[str] = []

    def _fake_get(self, url, *a, **kw):
        calls.append(url)
        payload = by_url.get(url, default)
        if payload is _NOT_FOUND:
            return _FakeResponse(status_code=404, payload={})
        return _FakeResponse(status_code=200, payload=payload)

    monkeypatch.setattr(requests.Session, "get", _fake_get)
    return calls


_NOT_FOUND = object()


def _assert_no_traceback(result) -> None:
    """CliRunner records the command's own SystemExit; anything else is a crash."""
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(
        result.exception
    )


# --------------------------------------------------------------------------- #
# 1 — fix-pypi-wildcard-pin-fetches-literal-version
# --------------------------------------------------------------------------- #


def test_pypi_wildcard_pin_resolves_max_satisfying(tmp_path, monkeypatch):
    """``foo==1.2.*`` is a PEP 440 prefix match: the dep must resolve to the
    highest 1.2.x release (1.2.5 here) and no GET may target the literal
    ``1.2.*`` (which 404s and turned the pin into a coverage gap)."""
    from promptaudit.resolver import resolve

    (tmp_path / "requirements.txt").write_text("foo==1.2.*\n", encoding="utf-8")
    calls = _install_session(
        monkeypatch,
        {
            "https://pypi.org/pypi/foo/json": {
                "info": {"name": "foo", "version": "1.3.0", "requires_dist": None},
                "releases": {"1.2.3": [], "1.2.5": [], "1.3.0": []},
            },
            "https://pypi.org/pypi/foo/1.2.5/json": {
                "info": {"name": "foo", "version": "1.2.5", "requires_dist": None}
            },
        },
        default=_NOT_FOUND,
    )

    marker_skipped: list = []
    pkgs = list(resolve(tmp_path, marker_skipped=marker_skipped))

    assert [(p.name, p.version) for p in pkgs] == [("foo", "1.2.5")], pkgs
    assert marker_skipped == [], marker_skipped
    assert not any("1.2.*" in url for url in calls), calls

    # 1.2.5 is inside the prefix range; 1.3.0 is not.
    assert not any("1.3.0/json" in url for url in calls), calls


def test_pypi_exact_pin_needs_no_releases_lookup(tmp_path, monkeypatch):
    """An exact ``==1.2.3`` pin still resolves with a single release-doc GET:
    the prefix-match change must not turn exact pins into range lookups."""
    from promptaudit.resolver import resolve

    (tmp_path / "requirements.txt").write_text("foo==1.2.3\n", encoding="utf-8")
    calls = _install_session(
        monkeypatch,
        {
            "https://pypi.org/pypi/foo/1.2.3/json": {
                "info": {"name": "foo", "version": "1.2.3", "requires_dist": None}
            }
        },
        default=_NOT_FOUND,
    )

    pkgs = list(resolve(tmp_path))

    assert [(p.name, p.version) for p in pkgs] == [("foo", "1.2.3")], pkgs
    assert calls == ["https://pypi.org/pypi/foo/1.2.3/json"], calls


# --------------------------------------------------------------------------- #
# 2 — fix-crafted-registry-doc-nondict-shape
# --------------------------------------------------------------------------- #


def test_registry_json_array_root_is_a_coverage_gap(tmp_path, monkeypatch):
    """A 200 whose JSON ROOT is an array must degrade to the documented
    ``npm_doc_not_found`` gap instead of raising AttributeError out of
    ``resolve()`` (resolver.py:535/576/747/1237/1404 on v0.12.0)."""
    from promptaudit.resolver import resolve

    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "dependencies": {"left-pad": "^1.0.0"}}),
        encoding="utf-8",
    )
    _install_session(monkeypatch, {}, default=[  # a JSON array root
        "not",
        "an",
        "object",
    ])

    marker_skipped: list = []
    pkgs = list(resolve(tmp_path, marker_skipped=marker_skipped))

    assert pkgs == [], pkgs
    assert [(m.name, m.marker) for m in marker_skipped] == [
        ("left-pad", "npm_doc_not_found")
    ], marker_skipped


def test_registry_json_array_root_scan_exits_three_without_traceback(
    tmp_path, monkeypatch
):
    """The CLI must report that gap (exit 3) rather than dying with an uncaught
    AttributeError — whose exit 1 is scan's "critical finding" code."""
    from promptaudit.cli import main

    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "dependencies": {"left-pad": "^1.0.0"}}),
        encoding="utf-8",
    )
    _install_session(monkeypatch, {}, default=[])

    result = CliRunner().invoke(
        main,
        [
            "scan",
            str(tmp_path),
            "--no-fetch",
            "--json",
            "--cache-root",
            str(tmp_path / "cache"),
        ],
    )

    _assert_no_traceback(result)
    assert result.exit_code == 3, (result.exit_code, result.output)
    payload = json.loads(result.stdout)
    assert [u["package"] for u in payload["unscanned"]] == ["left-pad"], payload


def test_npm_non_dict_dist_is_an_honest_coverage_gap(tmp_path, monkeypatch):
    """A truthy non-dict npm ``dist`` (fetcher.py:248) must yield the honest
    ``empty_corpus`` gap, not the internal ``AttributeError: ...`` string."""
    from promptaudit.fetcher import fetch_all
    from promptaudit.resolver import ResolvedPackage

    _install_session(
        monkeypatch,
        {
            "https://registry.npmjs.org/evil/1.0.0": {
                "name": "evil",
                "version": "1.0.0",
                "readme": "",
                "description": "",
                "dist": ["x"],  # not a mapping
            }
        },
        default=_NOT_FOUND,
    )

    pkg = ResolvedPackage(name="evil", version="1.0.0", ecosystem="npm")
    result = fetch_all([pkg], cache_root=tmp_path / "cache")[0]

    assert result.status == "error", result
    assert "empty_corpus" in result.message, result.message
    assert "AttributeError" not in result.message, result.message


# --------------------------------------------------------------------------- #
# 3 — fix-corpus-rule-error-uncaught-traceback
# --------------------------------------------------------------------------- #


def _bad_corpus(tmp_path: Path) -> Path:
    corpus = tmp_path / "bad_corpus.yaml"
    corpus.write_text("rules: 42\n", encoding="utf-8")
    return corpus


def test_malformed_corpus_is_clean_error_not_traceback(tmp_path, monkeypatch):
    """A malformed ``--corpus`` on ``rules`` and ``scan`` must print the clean
    ``promptaudit:`` error and exit 2 (not a RuleError traceback with exit 1)."""
    from promptaudit.cli import main

    corpus = _bad_corpus(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    (project / "requirements.txt").write_text("foo==1.0.0\n", encoding="utf-8")

    listed = CliRunner().invoke(main, ["rules", "--corpus", str(corpus)])
    _assert_no_traceback(listed)
    assert listed.exit_code == 2, (listed.exit_code, listed.output)
    assert listed.output.startswith("promptaudit: "), listed.output
    assert "rules" in listed.output, listed.output

    scanned = CliRunner().invoke(
        main,
        ["scan", str(project), "--no-fetch", "--corpus", str(corpus)],
    )
    _assert_no_traceback(scanned)
    assert scanned.exit_code == 2, (scanned.exit_code, scanned.output)
    assert scanned.output.startswith("promptaudit: "), scanned.output


def test_malformed_corpus_fails_before_resolving(tmp_path, monkeypatch):
    """The catalog is validated before the resolver runs, so a bad corpus is not
    reported after (or masked by) a manifest problem, and no registry walk
    happens. Here the manifest is ALSO broken; the corpus error must win."""
    from promptaudit.cli import main

    corpus = _bad_corpus(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    (project / "package-lock.json").write_text("[]", encoding="utf-8")
    calls = _install_session(monkeypatch, {}, default={})

    result = CliRunner().invoke(
        main, ["scan", str(project), "--no-fetch", "--corpus", str(corpus)]
    )

    assert result.exit_code == 2, (result.exit_code, result.output)
    assert "corpus" in result.output, result.output
    assert "package-lock" not in result.output, result.output
    assert calls == [], calls


# --------------------------------------------------------------------------- #
# 4 — fix-requirements-attached-include-silent-drop
# --------------------------------------------------------------------------- #


def test_attached_short_option_include_is_recursed(tmp_path, monkeypatch):
    """``-rbase.txt`` (pip's attached short-option form) must recurse into
    base.txt; on v0.12.0 the include was skipped and the project resolved to
    nothing ("no dependencies resolved — nothing to scan", exit 0)."""
    from promptaudit.resolver import resolve

    (tmp_path / "requirements.txt").write_text("-rbase.txt\n", encoding="utf-8")
    (tmp_path / "base.txt").write_text("foo==1.0.0\n", encoding="utf-8")
    _install_session(
        monkeypatch,
        {
            "https://pypi.org/pypi/foo/1.0.0/json": {
                "info": {"name": "foo", "version": "1.0.0", "requires_dist": None}
            }
        },
        default=_NOT_FOUND,
    )

    marker_skipped: list = []
    pkgs = list(resolve(tmp_path, marker_skipped=marker_skipped))

    assert [(p.name, p.version) for p in pkgs] == [("foo", "1.0.0")], pkgs
    assert marker_skipped == [], marker_skipped


def test_attached_constraints_include_is_a_coverage_gap(tmp_path):
    """``-cconstraints.txt`` must keep the same not-a-dep treatment as the space
    form: surfaced as a ``constraints_include_not_resolved`` marker."""
    from promptaudit.resolver import resolve

    (tmp_path / "requirements.txt").write_text(
        "-cconstraints.txt\n", encoding="utf-8"
    )

    marker_skipped: list = []
    list(resolve(tmp_path, marker_skipped=marker_skipped))

    assert [m.marker for m in marker_skipped] == [
        "constraints_include_not_resolved:constraints.txt"
    ], marker_skipped


# --------------------------------------------------------------------------- #
# 5 — fix-npm-caret-zero-wildcard-band
# --------------------------------------------------------------------------- #


def test_npm_caret_zero_bands_match_npm_semver():
    """node-semver's caret bound depends on the left-most SPECIFIED component:
    ``^0.0`` / ``^0.0.x`` -> ``<0.1.0``, ``^0.x`` -> ``<1.0.0``. On v0.12.0 all
    three produced the unsatisfiable ``>=0.0.0,<0.0.1``."""
    from packaging.version import Version

    from promptaudit.resolver import _npm_alt_to_specifierset

    for spec in ("^0.0", "^0.0.x"):
        band = _npm_alt_to_specifierset(spec)
        assert Version("0.0.5") in band, (spec, str(band))
        assert Version("0.1.0") not in band, (spec, str(band))

    zero_x = _npm_alt_to_specifierset("^0.x")
    assert Version("0.9.9") in zero_x, str(zero_x)
    assert Version("1.0.0") not in zero_x, str(zero_x)

    # Explicit-patch and normal carets keep npm's documented bounds.
    explicit = _npm_alt_to_specifierset("^0.0.3")
    assert Version("0.0.3") in explicit and Version("0.0.4") not in explicit
    minor = _npm_alt_to_specifierset("^0.2")
    assert Version("0.2.9") in minor and Version("0.3.0") not in minor
    major = _npm_alt_to_specifierset("^1.2")
    assert Version("1.9.9") in major and Version("2.0.0") not in major


def test_npm_caret_zero_minor_resolves_published_version(tmp_path, monkeypatch):
    """``"foo": "^0.0"`` against a registry publishing 0.0.5 and 0.1.0 must
    resolve foo@0.0.5 — on v0.12.0 no version matched the band, so the dep was
    reported as ``range_unsatisfiable:^0.0`` and never scanned."""
    from promptaudit.resolver import resolve

    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "dependencies": {"foo": "^0.0"}}),
        encoding="utf-8",
    )
    _install_session(
        monkeypatch,
        {
            "https://registry.npmjs.org/foo": {
                "name": "foo",
                "dist-tags": {"latest": "0.1.0"},
                "versions": {
                    "0.0.5": {"name": "foo", "version": "0.0.5"},
                    "0.1.0": {"name": "foo", "version": "0.1.0"},
                },
            },
            "https://registry.npmjs.org/foo/0.0.5": {
                "name": "foo",
                "version": "0.0.5",
                "dependencies": {},
            },
        },
        default=_NOT_FOUND,
    )

    marker_skipped: list = []
    pkgs = list(resolve(tmp_path, marker_skipped=marker_skipped))

    assert [(p.name, p.version) for p in pkgs] == [("foo", "0.0.5")], (
        pkgs,
        marker_skipped,
    )
    assert marker_skipped == [], marker_skipped
