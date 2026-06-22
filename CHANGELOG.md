# Changelog

All notable changes to PromptAudit are recorded here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- MIT license for code; CC0 dual-license for the seed corpus so security
  researchers can reuse it.
- GitHub Actions CI: pytest on Python 3.12.

### Known limitations

- Detection is pure regex + curated corpus. The lazy 80% of payloads
  (jqwik-shape) gets caught; sophisticated obfuscation is not in scope for
  v0.1. LLM-assisted semantic detection is a v0.2 hosted-tier feature.
- npm + PyPI only. crates.io / Maven / Go modules are on the roadmap, one
  ecosystem per minor version.
- No MCP-server scan mode yet — `awesome-mcp-servers` ingestion is m4.

[0.2.0]: https://github.com/supermario-leo/promptaudit/releases/tag/v0.2.0
[0.1.0]: https://github.com/supermario-leo/promptaudit/releases/tag/v0.1.0
