"""Version-lockstep guard.

The released version is declared in several surfaces; a bump that updates only
some of them ships a repo whose `--version`, wheel metadata and website disagree.
This test fails mechanically on drift so the next version bump cannot miss one.

Surfaces checked (all relative to the repo root):

* ``pyproject.toml`` — the wheel/sdist version, i.e. what PyPI and ``pip`` see.
* ``src/promptaudit/__init__.py`` — ``__version__``, the runtime source of truth
  used by the CLI ``--version`` flag and the registry User-Agent.
* ``web/site.json`` — ``content_version`` (carries a leading ``v``).
* ``CHANGELOG.md`` — a heading for the version.

``docs/demo-results.json`` is deliberately NOT checked: it records the run that
produced it (``source_revision``), so it keeps the version of that run until the
demo is re-captured for real.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _pyproject_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def _package_version() -> str:
    from promptaudit import __version__

    return __version__


def test_pyproject_and_package_version_agree():
    assert _pyproject_version() == _package_version(), (
        "pyproject.toml version and promptaudit.__version__ drifted; bump both"
    )


def test_cli_version_matches_package_version():
    from promptaudit.cli import main

    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0, result.output
    assert _package_version() in result.output, (
        f"`promptaudit --version` must report {_package_version()}; "
        f"got {result.output!r}"
    )


def test_site_json_content_version_matches():
    site = json.loads((ROOT / "web" / "site.json").read_text(encoding="utf-8"))
    assert site["content_version"] == f"v{_package_version()}", (
        f"web/site.json content_version={site['content_version']!r} must track "
        f"the package version v{_package_version()}"
    )


def test_changelog_has_an_entry_for_the_version():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    version = _package_version()
    assert re.search(rf"^##\s*\[{re.escape(version)}\]", changelog, re.MULTILINE), (
        f"CHANGELOG.md has no `## [{version}]` heading"
    )


@pytest.mark.parametrize("surface", ["pyproject.toml", "src/promptaudit/__init__.py"])
def test_no_stale_previous_version_string(surface):
    """A leftover previous-version literal means a half-finished bump."""
    text = (ROOT / surface).read_text(encoding="utf-8")
    major, minor, _patch = _package_version().split(".")
    if int(minor) == 0:
        pytest.skip("no previous minor version to check")
    stale = f"{major}.{int(minor) - 1}."
    assert stale not in text, (
        f"{surface} still contains a previous-minor version literal ({stale})"
    )
