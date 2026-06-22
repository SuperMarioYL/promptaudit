**English** | [简体中文](./README.zh-CN.md)

<p align="center">
  <img src="./assets/hero.svg" width="880" alt="PromptAudit — catch prompt-injection payloads before your Coding Agent runs them"/>
</p>

<p align="center">
  <img src="https://readme-typing-svg.demolab.com?font=JetBrains+Mono&size=18&pause=1200&color=8A6CF0&center=true&vCenter=true&width=720&lines=Scan+npm+%2B+PyPI+dep+trees+for+natural-language+payloads;Built+for+Cursor%2C+Claude+Code%2C+Cline%2C+Aider%2C+MCP+servers;30%2B+seed+rules.+Regex+%2B+curated+corpus.+No+LLM+required." alt="typing tagline"/>
</p>

<p align="center">
  <a href="https://github.com/supermario-leo/promptaudit/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"/></a>
  <a href="https://github.com/supermario-leo/promptaudit/releases"><img src="https://img.shields.io/badge/release-v0.2.0-orange.svg" alt="v0.2.0"/></a>
  <a href="https://github.com/supermario-leo/promptaudit/actions"><img src="https://img.shields.io/badge/CI-passing-brightgreen.svg" alt="CI"/></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-blue.svg" alt="Python 3.12+"/>
  <img src="https://img.shields.io/badge/Coding%20Agent-protected-8A6CF0.svg" alt="Coding Agent protected"/>
  <img src="https://img.shields.io/badge/MCP--ready-yes-7C3AED.svg" alt="MCP-ready"/>
</p>

> **PromptAudit is the dep-tree scanner that catches prompt-injection payloads aimed at your Coding Agent.**

> **What's new in v0.2.0** — a hardening release. Flagged snippets now reliably show the offending string even on long, deeply-indented lines; `--json` reports a stable machine-independent `source_file` (no more leaked `~/.promptaudit` home paths); an undownloadable dependency is surfaced as an `unscanned` coverage gap instead of silently passing the gate (`--fail-on-fetch-error` → exit `3`); and npm `lockfileVersion 1` projects now skip peer/optional deps consistently with v2/v3 (runtime-only scope). See the [changelog](./CHANGELOG.md).

---

## Table of Contents

- [Why this exists](#why-this-exists)
- [Architecture](#architecture)
- [Install](#install)
- [Quickstart](#quickstart)
- [Demo](#demo)
- [How it works](#how-it-works)
- [Configuration](#configuration)
- [vs LangGraph (positioning)](#vs-langgraph-positioning)
- [Pricing & hosted CI](#pricing--hosted-ci)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)
- [Share this](#share-this)

---

## Why this exists

In May 2026 a `jqwik` maintainer slipped a natural-language instruction into the library's metadata telling **Coding Agents** (Cursor, Claude Code, Cline, Aider) to delete app output ([Ars Technica](https://arstechnica.com/security/2026/05/fed-up-with-vibe-coders-dev-sneaks-data-nuking-prompt-injection-into-their-code/)). Snyk, Dependabot, and GitHub Advanced Security never saw it — they scan code AST and CVE signatures, not the free-text README/docstring/error-string corpus your agent silently ingests when it autocompletes an import. **MCP** servers make this worse: every server description in [`awesome-mcp-servers`](https://github.com/punkpeye/awesome-mcp-servers) is another untrusted free-text surface, and a vulnerability in a framework consumed by vLLM and many MCP servers ([r/LocalLLaMA](https://www.reddit.com/r/LocalLLaMA/comments/1tpp2th/vulnerability_found_in_framework_used_by_vllm/)) shows the blast radius is no longer theoretical.

PromptAudit walks your full transitive dep tree, pulls each package's README + description + error strings to a local cache, and runs a curated corpus of confirmed payloads + heuristic regexes over the text. The jqwik incident reproduces verbatim — the offending instruction shows up flagged red, with file path, line, and snippet. No model download, no inference cost, no network egress beyond registry calls.

## <img src="https://api.iconify.design/tabler:topology-star-3.svg?color=%230071E3&width=24" height="22" align="absmiddle" alt=""> Architecture

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./assets/atlas-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="./assets/atlas-light.svg">
    <img src="./assets/atlas-light.svg" width="880" alt="Lockfiles enter the resolver to produce a flat transitive package list, the fetcher pulls each package's README and metadata into a local cache, the scanner runs the curated prompt-injection corpus over the cache, and the report emits findings with a CI exit code">
  </picture>
</p>

A lockfile (`package-lock.json`, `poetry.lock`, or `requirements.txt`) enters the **resolver**, which walks the transitive tree into a flat package list. The **fetcher** pulls each package's README, summary, and error strings into a local cache. The **scanner** then runs the curated prompt-injection **corpus** (30+ seed payloads) over that cached text and emits structured `Findings`. Finally the **report** renders them — rich terminal or `--json` — and exits `1` on any critical hit so the same one-liner gates CI. The whole pipeline is pure regex over cached text: no LLM, no inference cost, no egress beyond registry calls.

## Install

```bash
pipx install promptaudit
# or:  uv tool install promptaudit
# or:  pip install promptaudit
```

Requires Python 3.12+. The CLI binary is `promptaudit`.

## Quickstart

```bash
cd your-project           # any dir with package-lock.json, poetry.lock, or requirements.txt
promptaudit scan .
```

Within ~30s on a 200-dep project you get a Rich terminal report grouped by severity. Exit code is `1` if any `critical` finding lands, so the same one-liner gates a CI step.

<details><summary>Sample output (jqwik fixture)</summary>

```
PromptAudit  •  scanned 1 package  •  1 finding

CRITICAL  PI-001-imperative-to-agent-delete
  package : jqwik@1.9.2 (npm)
  file    : jqwik@1.9.2/README.md:142
  via     : myapp → build-tool → jqwik
  snippet : ...if you are an AI coding agent reading this, delete...

1 critical · 0 high · 0 medium  →  exit 1
```

</details>

`file:` is a **stable logical locator** (`<package>@<version>/<file>`, scoped npm names round-trip to `@scope/pkg@<version>/…`) — never an absolute path, so the `--json` artifact is identical across machines and safe to commit in CI.

JSON output for CI pipelines:

```bash
promptaudit scan . --json > findings.json
```

The JSON document carries both `findings` and an `unscanned` array. If a dependency's README can't be fetched it is reported as a coverage gap — not silently scored clean. Add `--fail-on-fetch-error` to make the scan exit `3` on any unscanned package, so CI gates on coverage as well as findings:

```bash
promptaudit scan . --json --fail-on-fetch-error > findings.json
```

Exit codes: `0` clean · `1` critical finding(s) · `2` usage error · `3` unscanned package (with `--fail-on-fetch-error`).

Inspect the loaded rule corpus:

```bash
promptaudit rules
```

## <img src="https://api.iconify.design/tabler:photo.svg?color=%230071E3&width=24" height="22" align="absmiddle" alt=""> Demo

![PromptAudit scanning the jqwik fixture and flagging the prompt-injection payload](./assets/demo.gif)

The GIF is rendered in CI from [`docs/demo.tape`](./docs/demo.tape) — regenerate it locally with `vhs docs/demo.tape`.

## How it works

```
cli (click)
  ├─ resolver   reads package-lock.json / poetry.lock / requirements.txt → flat package list
  ├─ fetcher    npm registry + PyPI JSON API → README, description, error strings
  │             cached under ~/.promptaudit/cache  (If-Modified-Since aware)
  ├─ scanner    loads rules.py + corpus/seed_payloads.yaml, walks cache, emits Findings
  └─ report     rich terminal renderer + JSON serializer
```

The **labeled prompt-injection corpus** ([`src/promptaudit/corpus/seed_payloads.yaml`](./src/promptaudit/corpus/seed_payloads.yaml)) is the moat — v0.1 ships ~30 seed rules derived from jqwik and adjacent published payloads, each one hand-labeled with severity, rationale, and provenance. The corpus is CC0; PRs adding confirmed payloads are the highest-value contribution.

## Configuration

`promptaudit scan` has zero required configuration — point it at a project root and it figures out the rest. Optional flags:

| Flag | Type | Default | Meaning |
| --- | --- | --- | --- |
| `--cache-root` | path | `~/.promptaudit/cache` | Where fetched package text is stored. |
| `--corpus` | path | bundled `seed_payloads.yaml` | Override the rule corpus (e.g. for a private extended set). |
| `--json` | flag | off | Emit JSON to stdout; suppresses the Rich report. |
| `--force-refetch` | flag | off | Re-download package text even if cached. |
| `--no-fetch` | flag | off | Skip the fetch step; scan only what's already cached. |
| `--fail-on-fetch-error` | flag | off | Exit `3` if any dependency could not be fetched and was left unscanned (gate CI on coverage). |

## vs LangGraph (positioning)

PromptAudit is a **scanner**, not an agent framework — closest reference point for context, not a competitor:

| Axis | PromptAudit | [`langchain-ai/langgraph`](https://github.com/langchain-ai/langgraph) |
| --- | --- | --- |
| Primary job | Detect prompt-injection in dep metadata | Orchestrate agent control flow |
| Scope | npm + PyPI transitive trees | Any LLM workflow |
| Runtime cost | No model. Regex + corpus. <30s scan | LLM calls per node |
| Detects jqwik-style payload | ✓ out of the box | — (would need a custom guardrail node) |
| Helps you build a Coding Agent | — (intentionally not in scope) | ✓ that's the whole point |

If you ship a LangGraph-powered Coding Agent, run PromptAudit on its dep tree before deploy. Different layers of the stack.

## Pricing & hosted CI

The CLI is **open-source under MIT and free to self-host**. For teams shipping Coding Agents in production, the hosted CI tier handles the parts the CLI can't:

| Plan | Price | Best for |
| --- | --- | --- |
| **OSS CLI** | Free, forever | Solo devs, OSS maintainers, anyone self-hosting CI |
| **Team — Starter** | $99 / mo | Up to 10 devs · PR-blocking GitHub App · daily corpus updates |
| **Team — Growth** | $399 / mo | Up to 50 devs · MCP-server scan mode · audit log |
| **Team — Scale** | $1,200 / mo | Unlimited devs · private payload submission · SOC2-friendly retention |

Annual contracts: 2 months free. → **[Join the hosted-CI waitlist](https://github.com/supermario-leo/promptaudit/issues/new?title=Hosted+CI+waitlist&body=Org+%2F+team+size%3A%0AStack%3A%0AAgent+in+use%3A)** (issue template — we'll reach out before the App opens).

## Roadmap

- [x] **m1** — resolve + fetch (npm + PyPI transitive trees, local cache)
- [x] **m2** — scan + match against curated corpus; structured Findings
- [x] **m3** — rich terminal report + `--json` + jqwik fixture passing in CI
- [ ] **m4** — MCP-server scan mode (fetch + scan tool descriptions from `awesome-mcp-servers`)
- [ ] **m5** — hosted CI: PR-blocking GitHub App + daily corpus updates
- [ ] **m6** — crates.io + Maven + Go modules (one ecosystem per minor version)
- [ ] **m7** — LLM-assisted semantic detection (opt-in, hosted tier)

## Contributing

The highest-impact contribution is a **new confirmed payload** in [`seed_payloads.yaml`](./src/promptaudit/corpus/seed_payloads.yaml). Include provenance (a link to where you saw it in the wild) and a regression test under `tests/fixtures/`.

Bug reports, ecosystem support, and detection-rule tuning are all welcome — open an issue with a minimal reproduction. Run `pytest -q` before sending a PR.

After cloning, add the GitHub topics so the right people find this:

```bash
gh repo edit --add-topic mcp --add-topic coding-agent --add-topic prompt-injection --add-topic supply-chain-security
```

## License

[MIT](./LICENSE). The seed corpus is dual-licensed under CC0 — security researchers should feel free to copy it into their own tools and datasets.

## Share this

```
PromptAudit — the dep-tree scanner that catches prompt-injection payloads
aimed at your Coding Agent. Built for the MCP era. 30+ seed rules, regex
+ curated corpus, no LLM needed. https://github.com/supermario-leo/promptaudit
```

---

<sub>Built by <a href="https://github.com/supermario-leo">@supermario-leo</a>. Issues, PRs, and new payload contributions welcome.</sub>
