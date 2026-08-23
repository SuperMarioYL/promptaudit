# Changelog

All notable changes to PromptAudit are recorded here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.0] — 2026-08-23

Correctness-fix release continuing the v0.2.0–v0.8.0 silent-false-negative
hardening cadence. Three resolver regressions on the tool's canonical
supply-chain surfaces (a yanked PyPI pinned version, a crafted npm packument,
and a pip-tools `-r` include) — each silently dropped a dep, crashed the CLI,
or under-scanned a partial tree. Each fix ships with an adversarial regression
test that is red on the v0.8.0 baseline.

### Fixed

- **A 404 / unreachable PyPI release-doc fetch in the transitive walk no longer
  silently drops the dep.** `_walk_pypi` did `info = _fetch_pypi_release(...)`
  then `if info is None: continue` BEFORE the `yield ResolvedPackage`, so a
  directly-pinned (`foo==1.0.0`) or transitively-required PyPI dep whose
  version was yanked / unpublished / hit a transient registry blip — itself a
  supply-chain red flag — vanished from BOTH `packages` and `marker_skipped`
  and never reached the fetcher (whose v0.3.0 `fix-404-empty-corpus-silent-clean`
  would otherwise surface it as `version_not_found` unscanned): zero findings,
  zero unscanned signal, exit 0. This is the PyPI-side analog of v0.7.0's
  `fix-npm-no-lockfile-404-silent-drop` (which closed the npm no-lockfile
  resolve-time 404 path but missed this sibling — the npm walk yields the
  parent BEFORE its dependency fetch so a 404 is caught downstream, whereas
  the PyPI walk 404-checks and `continue`s BEFORE yielding). A 404 / over-cap
  / unparseable release doc (and a reachable but malformed doc with no
  `version` field) is now surfaced as `pypi_release_not_found` via the existing
  `marker_skipped` / `skipped_seen` plumbing so the CLI reports an
  `UnscannedPackage` instead of exiting 0 clean.
- **A crafted npm packument with a non-dict `versions` field no longer crashes
  the CLI.** `_resolve_npm_max_satisfying` did `versions =
  list((doc.get("versions") or {}).keys())` whose `or {}` only guards a
  falsy / absent `versions`; a crafted / malformed packument (the tool's
  canonical supply-chain surface) with a truthy non-dict `versions` (`["a","b"]`
  or `42`) made `(non_dict or {})` return the non-dict unchanged, and
  `non_dict.keys()` raised `AttributeError` — crashing the CLI on a routine
  `scan .` of a no-lockfile npm project. The v0.8.0 `fix-npm-range-empty-
  versions-falls-to-latest` added `if not versions:` (False for a non-empty
  list, so the non-dict sibling slipped past). A non-dict `versions` is now
  guarded with `isinstance(..., dict)` and treated as the empty-versions
  coverage gap (the same `_NpmRangeUnsatisfiable` sentinel the empty-versions
  path returns) — never a crash. The same `isinstance` guard is applied to
  `dist-tags` in `_resolve_npm_dist_tag` and `releases` in
  `_resolve_pypi_max_satisfying` for parity.
- **`-r` / `--requirements` include directives in `requirements.txt` are now
  resolved so the included deps are scanned.** `_resolve_requirements_txt`
  skipped every line starting with `-`, so a `-r base.txt` /
  `--requirements base.txt` include (the common pip-tools / multi-env pattern)
  silently dropped the included file's deps from `packages`,
  `packages_with_coverage`'s `missing`, and `marker_skipped` — a silent
  partial-tree under-scan that exited 0 clean if the top-level deps happened to
  be clean, on the exact requirements-parsing surface v0.7.0's
  `fix-requirements-hashes-silent-drop` hardened (the `--hash` strip and the
  `-r` skip lived in the same loop). `-r` / `--requirements` targets are now
  resolved relative to the current file's directory and recursed (with a
  `visited` set bounding include cycles), so the included deps ARE scanned.
  `-c` / `--constraints` includes (pip does not install constraint files as
  deps — recursing them would scan non-dependencies) and any missing `-r`
  target are surfaced as a coverage marker (`<kind>_include_not_resolved:<file>`)
  rather than dropped silently.

### Tests

- `tests/test_v090_fixes.py` (new): six adversarial regression tests, each red
  on the v0.8.0 baseline. (1) a 404 PyPI release-doc fetch asserts the dep
  surfaces as `pypi_release_not_found` in `marker_skipped` (red: `marker_skipped`
  empty). (2) a packument with `"versions": ["a","b"]` asserts no `AttributeError`
  and a `range_unsatisfiable` coverage gap (red: crashes). (3,4) the `dist-tags`
  and `releases` sibling sites assert no crash on a non-dict field (red:
  `AttributeError` / `TypeError`). (5) a `-r base.txt` include asserts the
  included file's dep IS scanned (red: absent). (6) an `a -> b -> a` include
  cycle asserts both deps are scanned once with no unbounded recursion (red:
  both absent).

## [0.6.0] — 2026-07-25

Correctness-fix release continuing the v0.2.0–v0.5.0 cadence. Three
adversarial-input / transient-failure regressions on the tool's canonical
supply-chain surfaces (a crafted high-member-count sdist, a crafted multi-MB
registry-JSON doc, and a `~=` pin hit by a transient registry outage) — each
audited the WRONG version, audited NOTHING silently, or OOMed/hung the
scanner. Each fix ships with an adversarial regression test that is red on
the v0.5.0 baseline.

### Fixed

- **A crafted high-member-count sdist can no longer truncate legit
  >48-member sdists via the per-member byte budget.** The v0.5.0
  `_read_and_account` charged `max(len(blob), MAX_TEXT_FILE_BYTES)` per
  member, flooring every member at the 512KB per-member cap; the 24MB
  uncompressed-byte budget therefore exhausted at 48 members (24MiB / 512KiB)
  and `_BudgetExceeded` truncated the walk mid-archive — so a benign 60-member
  sdist was scanned as if it had 48. `_read_member_bounded` now returns
  `(blob, bytes_consumed)` and `_read_and_account` debits the ACTUAL bytes
  consumed per member (a 1KB file debits 1KB, not 512KB), so a 60-member sdist
  is scanned in full. The oversized-member flood guard is preserved: a member
  that exceeds the per-member cap still charges `cap + 1`, so a flood of
  oversized-and-skipped members still drains the budget and terminates the
  walk (but a SINGLE huge file can't exhaust the whole budget on its own).
- **npm/PyPI registry-JSON GETs are now bounded; a crafted multi-MB registry
  doc can no longer OOM the scanner.** The v0.5.0 fetcher/resolver made
  non-streaming `session.get()` + `.json()` calls against the npm and PyPI
  registries with no byte cap (npm version docs, npm dependency docs, npm
  dist-tags, PyPI release docs, PyPI `~=` max-satisfying) — a registry GET
  that happens on every resolved package of every `promptaudit scan .`. A
  crafted multi-MB registry doc (the tool's canonical supply-chain surface)
  was parsed whole into memory, OOMing/hanging the scanner on a routine scan.
  The registry-JSON GETs are now routed through the existing `_read_bounded`
  streaming helper with a few-MB cap (`MAX_REGISTRY_JSON_BYTES`): an oversized
  doc is abandoned → empty corpus / `_PinFailed` (coverage gap), not a silent
  OOM. A shared `_get_registry_json` helper in `resolver.py` uses a
  function-local import of `_read_bounded` to avoid the circular import
  (`fetcher` imports `resolver` at module top).
- **A `~=` pin hit by a transient registry outage no longer silently audits
  the registry LATEST outside the pin range.** A `foo~=1.4.2` pin (means
  `>=1.4.2, ==1.4.*`) whose `_resolve_pypi_max_satisfying` returned `None`
  (a network blip / non-200 / unparseable body / no satisfying version) was
  indistinguishable from the `None` a LOOSE spec (`>=`, `<`, wildcards)
  returns — so `_pin_from_specifier` returned `None`, `_walk_pypi` took the
  version-less `/{name}/json` latest path, and the scanner audited the
  registry LATEST `2.0.0`, a version OUTSIDE the `~=1.4.2` range — a
  false-clean on the "audit the version you actually install" promise, on
  the exact transient-failure surface (a registry blip). The lookup now
  returns a `_PinFailed` sentinel on failure (distinct from genuine `None`
  for loose specs, whose latest path is preserved); `_walk_pypi` surfaces a
  `_PinFailed` as an `UnscannedPackage` coverage gap (via the existing
  `marker_skipped` plumbing) instead of falling through to latest.

### Tests

- `tests/test_v060_fixes.py` (new): three adversarial regression tests, each
  red on the v0.5.0 baseline. (1) a 60-member sdist asserts all 60 members
  scanned (red: 48). (2) a >5MB registry-JSON stub asserts the fetch is
  bounded and `.json()` is never called on the whole doc (red: parses whole).
  (3) a `~=1.4.2` pin with `_resolve_pypi_max_satisfying` monkeypatched to
  return `_PinFailed` asserts the dep surfaces as `UnscannedPackage` and is
  NOT audited as the out-of-range 2.0.0 (red: audits 2.0.0).

## [0.5.0] — 2026-07-21

Bug-hardening release. External demand stayed zero (0 open issues / 0 PRs / 0
forks, stars flat at 1) and the 30-day kill clock (§8) closes ~2026-07-22, so
v0.5.0 is the last scheduled fix-train iteration before that falsifier is
evaluated. It continues the 0.2.0–0.4.0 correctness-fix cadence with no new
feature/growth scope.

### Fixed

- **A crafted high-member-count npm tarball can no longer hang / OOM the
  scanner during README extraction.** The npm tarball README extractor called
  eager `tar.getmembers()`, which decompresses the whole `r:gz` archive and
  materialises a `TarInfo` object for every member before the find-loop, then
  read the matching README member with an unbounded `fh.read()`. A crafted npm
  tarball (the tool's canonical supply-chain surface — the jqwik hero fixture
  is npm) of hundreds of thousands of tiny non-README members forced the
  extractor to decompress the whole archive and hold every member's metadata
  before it could find the README and exit. The 0.4.0 decompression-bomb fix
  bounded the SDIST string sweep, but this separate extractor was never given
  the same lazy/bounded treatment, so the identical DoS class survived on the
  README channel. The extractor now iterates the archive lazily (`for member
  in tar`), stops at the first README candidate, gives up after the same
  `MAX_SDIST_MEMBERS` cap as the sdist path, and routes the README read through
  the 0.4.0 bounded reader (`_read_member_bounded`) so a lying-size README
  member can never blow memory in a single read.
- **The registry User-Agent and the hosted-CI waitlist URL now point at the
  real repo and carry the live version.** The User-Agent sent on every npm and
  PyPI registry HTTP request was frozen at `promptaudit/0.1` (stale since
  v0.1.0 — the package was at `0.4.0`) and advertised a wrong
  `supermario-leo/promptaudit` slug, and the hosted-CI waitlist URL printed in
  the footer of every terminal scan report used the same dead slug plus a
  `#hosted-ci-waitlist` anchor that never existed in the README — breaking the
  commercial monetization funnel (§1) on the exact surface a lead clicks
  through after a scan. The User-Agent is now derived from `__version__` so it
  never freezes again, and both URLs point at `github.com/SuperMarioYL/promptaudit`
  (matching `pyproject.toml`), with the footer anchored at the real
  `#pricing--hosted-ci` section.
- **`promptaudit fetch` no longer exits 1 on a fetch error.** The `fetch`
  subcommand exited with `EXIT_CRITICAL_FOUND` (1) when any package failed to
  fetch — but exit 1 is the `scan` subcommand's contract for "a critical
  prompt-injection finding landed", while `scan` deliberately uses a distinct
  `EXIT_FETCH_ERROR` (3) for coverage gaps. So `promptaudit fetch` returning 1
  on a fetch error collided with `promptaudit scan` returning 1 on a critical
  payload, false-positiving any CI gate that branches on `$? -eq 1` to mean
  "critical finding". `fetch` produces no findings, so exit 1 was unambiguously
  a fetch error; it now exits 3 (`EXIT_FETCH_ERROR`) on fetch errors, aligning
  a single exit-code contract across both subcommands.

## [0.4.0] — 2026-07-01

Bug-hardening release. External demand stayed zero (0 issues / 0 PRs / 0 forks,
stars flat at 1 over the 14 days post-0.3.0) and the install→star decoupling
persists post-CTA, but the 30-day kill clock has not closed — so v0.4.0
deliberately holds new growth scope and spends the version closing three
post-ship bug-hunt findings, matching the 0.2.0 (4 fixes) / 0.3.0 (3 fixes)
cadence. Both items the 0.3.0 changelog deferred ("re-hunt next pass") land here.

### Fixed

- **A crafted high-ratio sdist can no longer OOM / hang the scanner.** The sdist
  string sweep bounded only the *compressed* download (8 MB) and rejected only
  *individual* members over 512 KB — so a malicious sdist of thousands of
  just-under-512 KB highly-compressible members fit under the compressed cap yet
  expanded to ~1 GB once each member was read into memory (verified: a 1.06 MB
  `.tar.gz` forcing ~1000 MB of reads), and the 500-string early-exit never
  tripped because the members carried no quote-delimited literals. The sweep now
  tracks a running budget of total *uncompressed* bytes (24 MB) and caps the
  member count (2000), reading each member through a bounded reader — applied
  identically to the tar and zip paths — and returns what it collected so far
  once any limit is hit. An attacker-controlled PyPI sdist is exactly the
  supply-chain surface this tool audits, so this closes a denial-of-service on a
  routine `promptaudit scan .`.
- **Deps skipped by a host-only PEP 508 marker are no longer dropped silently.**
  A transitive dep gated by a marker that evaluates `False` on the scanner host
  (`sys_platform == "win32"`, `platform_system == "Windows"`,
  `python_version < "3.8"`, or an extras-only `extra == "..."`) was never
  fetched or scanned and produced no machine-readable signal — a silent
  false-negative for a Windows developer (or a CI runner whose platform differs
  from the project's runtime target). Such deps are now surfaced as an
  `unscanned` coverage gap with reason `marker_skipped:<marker>`, and two new
  flags — `--target-python X.Y` and `--target-platform windows|linux|darwin` —
  evaluate markers against a chosen environment so a cross-platform install can
  be audited (e.g. resolve win32-gated deps from a Linux runner). Compound
  markers with an extra are retried with an empty `extra` so their host/OS/python
  half still resolves.
- **No-lockfile npm range specs now resolve to a real published version.** On
  the `package.json` fallback (no `package-lock.json`), range specs were passed
  through verbatim as the "version" whenever the first character was a digit
  (`~1.2` → `"1.2"`, `>=1.0 <2.0` → `"1.0"`, `1.x` → `"1.x"`), so the fetcher
  GET-ed a non-version that 404-ed and — after the 0.3.0 fix — was correctly
  surfaced as unscanned, degrading the tool to "scans nothing" for every
  range-specified direct dep in a lockfile-less npm project. Ranges
  (`^` / `~` / x-range / comparator set / `||`) now resolve to the highest
  published registry version satisfying the range (mirroring the PyPI `~=`
  max-satisfying lookup), and non-registry specs (`workspace:`, `npm:`, `file:`,
  `link:`, `git+`, `github:`, …) are skipped explicitly instead of being
  fetched as a bogus version.

### Added

- `--target-python` / `--target-platform` flags on `promptaudit scan` for
  auditing PEP 508 markers against a non-host environment.
- `marker_skipped:<marker>` entries in the `unscanned` array of the
  `promptaudit.findings/v1` JSON document.

## [0.3.0] — 2026-06-28

Fix + growth release. Post-ship signal stayed thin (0 issues / 0 PRs / 0 forks,
stars 0→1 over 14 days), so v0.3.0 scope comes from the bug-hunt
false-negative findings plus the measured install→star funnel gap, not user
feature requests.

### Fixed

- **A 404 / yanked-version fetch is no longer silently scored clean.** A 404
  (yanked, unpublished, or wrongly-resolved version — itself a supply-chain
  red flag) returned an empty sources dict and was promoted to `status=ok`,
  so the scanner found no source files, emitted zero findings, and the dep
  passed the CI gate at exit 0 with no machine-readable signal. A 404 (or any
  fetch yielding zero source files) is now a coverage failure —
  `status=error` with reason `version_not_found` / `empty_corpus` — and the
  empty cache dir is not promoted, so it flows into the existing `unscanned`
  plumbing and the `--fail-on-fetch-error` gate. Extends v0.2.0's fetch-error
  fix, which only covered `requests.RequestException` (total network failure),
  not the 404-empty-sources path.
- **`~=1.4.2` compatible-release pins now resolve to a satisfying version.**
  `_pin_from_specifier` only returned a concrete version for `==` / `===`;
  `~=` returned `None` and `_walk_pypi` resolved to the registry LATEST —
  possibly outside the compatible range (e.g. `2.0.0` for `~=1.4.2`) — so the
  scanner audited the wrong version. `~=` is now resolved to the highest PyPI
  release satisfying the full specifier via the `packaging` releases-list
  lookup, matching what `pip install` would pick. The
  `_resolve_requirements_txt` docstring (which wrongly claimed `~=` was
  treated as resolved) is corrected.
- **`scan . --no-fetch` on a cold cache no longer exits 0 clean.** `scan()`
  silently skipped any package whose cache dir was absent, and the CLI only
  populated `unscanned` inside the fetch path, so a cold/partial cache under
  `--no-fetch` printed "Scanned N packages, no payloads" (the resolved count)
  and exited 0 having scanned zero. Coverage is now tracked explicitly:
  packages missing from the cache are surfaced as `UnscannedPackage`
  (reason `no_cache`) even under `--no-fetch`, the terminal panel reports the
  actually-scanned count, and a zero-scanned run exits `3` (a distinct yellow
  coverage panel replaces the misleading green "no payloads found").

### Added

- One-line star CTA after a completed scan ("★ star if PromptAudit caught a
  payload you'd have shipped"), reconnecting the install funnel (pipx/PyPI
  installers bypass the GitHub starring page) to the star funnel. Suppressed
  under `--json` and the new `--quiet` (`-q`) flag so machine output stays
  clean. The same CTA appears in the README header near the `pipx install`
  line.
- `-q` / `--quiet` flag on `promptaudit scan`.

## [0.2.0] — 2026-06-22

Hardening release. No new feature scope — four correctness fixes from the
post-ship bug-hunt that strengthen the guarantees v0.1 already advertised:
reliable snippet display, machine-independent JSON, visible coverage gaps,
and consistent runtime-only dependency scope.

### Fixed

- **Snippet no longer drops the flagged payload on indented/long lines.**
  `_extract_snippet` centered its truncation window using a raw-text offset
  while indexing into an already-`.strip()`'d snippet; on a deeply-indented
  long line the index-space mismatch scrolled the flagged match out of the
  snippet entirely. The window is now computed in stripped-snippet
  coordinates, so the reported snippet always contains the flagged string.
- **`source_file` is now a stable, machine-independent locator.** Findings
  previously recorded the absolute cache path
  (`/Users/<user>/.promptaudit/cache/...`) in `--json`, leaking the
  operator's home directory / username into committed CI artifacts and making
  output non-deterministic across machines. Findings now report a logical
  locator like `jqwik@1.9.2/README.md` (scoped npm names round-trip back to
  `@scope/pkg@<version>/README.md`). Absolute paths are gone from product
  output.
- **A failed README fetch can no longer silently pass the CI gate.** Fetch
  errors no longer leave an empty cache directory (which the scanner treated
  as "scanned, zero findings"); a failed fetch leaves no directory so a later
  run retries. Unscanned packages are surfaced as a machine-readable
  `unscanned` section in the JSON output and as a warning in the terminal
  report. A new `--fail-on-fetch-error` flag makes `scan` exit `3` so CI can
  gate on scan coverage, not just findings.
- **npm lockfile v1 walker now skips peer/optional deps, not just dev.** The
  v1 walker only filtered `dev`, while the v2/v3 walker skips dev/peer/
  optional. v1 projects therefore resolved, fetched, and scanned optional/peer
  dependencies that aren't installed at runtime, diverging from the stated
  runtime-only scope and risking a spurious critical exit-1. Both walkers now
  share the same filter.

### Added

- `--fail-on-fetch-error` flag on `promptaudit scan` (exit code `3` on any
  unscanned package).
- `unscanned` array in the `promptaudit.findings/v1` JSON document.

## [0.1.0] — 2026-06-03

Initial public release. Implements the m1–m3 milestones from the v0.1 plan:
resolve a project's transitive dep tree, fetch each package's free-text
metadata, scan it against a curated prompt-injection corpus, and emit a
report aimed at gating CI.

### Added

- `promptaudit scan PROJECT_ROOT` — full pipeline (resolve → fetch → scan →
  report). Exits `1` on any `critical` finding, `0` otherwise.
- `promptaudit fetch PROJECT_ROOT` — resolve + fetch only; warms the cache
  ahead of a separate `scan` step in CI.
- `promptaudit rules` — prints the loaded rule corpus grouped by severity.
- Lockfile resolvers for npm (`package-lock.json` v1/v2/v3), Poetry
  (`poetry.lock`), and pip (`requirements.txt`). Treats `requires_dist: null`
  as "no deps" per PyPI's semantics.
- Registry fetchers for npm (with `dist.tarball` README fallback when the
  registry `readme` field is empty — a known gap for popular packages like
  `express`) and PyPI JSON API. Filesystem cache under
  `~/.promptaudit/cache`, `If-Modified-Since` aware.
- Curated seed corpus (`src/promptaudit/corpus/seed_payloads.yaml`) with 30+
  rules derived from the jqwik incident and adjacent public payloads. Three
  severity tiers: `critical` / `high` / `medium`.
- Rich terminal report grouped by severity; `--json` flag for machine
  output; `--corpus` flag to override the bundled rule set.
- jqwik regression fixture (`tests/fixtures/jqwik_payload.txt`) — the
  May 2026 payload triggers at least one `critical` finding under unit test.
- Apache 2.0 license for code; CC0 dual-license for the seed corpus so security
  researchers can reuse it.
- GitHub Actions CI: pytest on Python 3.12.

### Known limitations

- Detection is pure regex + curated corpus. The lazy 80% of payloads
  (jqwik-shape) gets caught; sophisticated obfuscation is not in scope for
  v0.1. LLM-assisted semantic detection is a v0.2 hosted-tier feature.
- npm + PyPI only. crates.io / Maven / Go modules are on the roadmap, one
  ecosystem per minor version.
- No MCP-server scan mode yet — `awesome-mcp-servers` ingestion is m4.

[0.9.0]: https://github.com/SuperMarioYL/promptaudit/releases/tag/v0.9.0
[0.4.0]: https://github.com/SuperMarioYL/promptaudit/releases/tag/v0.4.0
[0.3.0]: https://github.com/SuperMarioYL/promptaudit/releases/tag/v0.3.0
[0.2.0]: https://github.com/SuperMarioYL/promptaudit/releases/tag/v0.2.0
[0.1.0]: https://github.com/SuperMarioYL/promptaudit/releases/tag/v0.1.0
