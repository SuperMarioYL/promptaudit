[简体中文](./README.zh-CN.md) · [Website](https://promptaudit.lei6393.com) · [GitHub](https://github.com/SuperMarioYL/promptaudit)

<picture>
  <source media="(max-width: 600px) and (prefers-color-scheme: dark)" srcset="./assets/presentation/hero-mobile-dark.svg">
  <source media="(max-width: 600px)" srcset="./assets/presentation/hero-mobile-light.svg">
  <source media="(prefers-color-scheme: dark)" srcset="./assets/presentation/hero-dark.svg">
  <img src="./assets/presentation/hero-light.svg" width="960" alt="Hero diagram">
</picture>

# promptaudit

**Inspect dependency text before an agent treats it as instruction.**

PromptAudit resolves supported dependency inputs, gathers package text, and applies a curated rule corpus to identify suspicious instruction patterns.

## Why use it

Dependency README and error text can be read by coding agents during normal work. A local text scan makes recognizable instruction payloads and unscanned packages visible before you rely on that material.

- **Find recognizable payloads** — Rules target instructions aimed at coding agents.
- **Keep package context** — Findings carry package and source locations.
- **Expose incomplete coverage** — Missing fetches remain visible to the operator.

## Architecture

<picture>
  <source media="(max-width: 600px) and (prefers-color-scheme: dark)" srcset="./assets/presentation/architecture-mobile-dark.svg">
  <source media="(max-width: 600px)" srcset="./assets/presentation/architecture-mobile-light.svg">
  <source media="(prefers-color-scheme: dark)" srcset="./assets/presentation/architecture-dark.svg">
  <img src="./assets/presentation/architecture-light.svg" width="960" alt="Architecture diagram">
</picture>

The resolver enumerates packages, the fetcher writes bounded cached text, and scanner runs ecosystem-filtered patterns over README, summary and error strings. Findings retain rule, package, version and source location. The CLI also surfaces incomplete coverage.

| Component | Responsibility |
| --- | --- |
| `Dependency resolver` | resolver.py |
| `Text cache` | fetcher.py |
| `Rule scanner` | scanner.py; rules.py |
| `Findings / coverage` | report and CLI |

## Install and quickstart

Build with the version declared in the repository manifest. Run the example from the repository root.

```bash
git clone https://github.com/SuperMarioYL/promptaudit.git
cd promptaudit
uv venv .venv
uv pip install --python .venv/bin/python -e .
source .venv/bin/activate
```

Scan tests/fixtures/jqwik_payload.txt as inert text using the production rule corpus, then print rule IDs, severity and line numbers.

```bash
.venv/bin/python examples/presentation-demo.py
```

## Recorded demo

<picture>
  <source media="(max-width: 600px) and (prefers-color-scheme: dark)" srcset="./assets/presentation/process-mobile-dark.svg">
  <source media="(max-width: 600px)" srcset="./assets/presentation/process-mobile-light.svg">
  <source media="(prefers-color-scheme: dark)" srcset="./assets/presentation/process-dark.svg">
  <img src="./assets/presentation/process-light.svg" width="960" alt="Process diagram">
</picture>

The bundled payload text produces rule findings without executing any of its instructions.

```text
input: tests/fixtures/jqwik_payload.txt; text is scanned, never executed
{"rule_id": "PI-001-imperative-to-agent-delete", "severity": "critical", "line": 22}
{"rule_id": "PI-003-ignore-previous-instructions", "severity": "critical", "line": 31}
{"rule_id": "PI-101-second-person-to-ai", "severity": "high", "line": 20}
{"rule_id": "PI-101-second-person-to-ai", "severity": "high", "line": 22}
{"rule_id": "PI-102-role-override", "severity": "high", "line": 22}
{"rule_id": "PI-105-conditional-on-agent-context", "severity": "high", "line": 22}
{"rule_id": "PI-102-role-override", "severity": "high", "line": 27}
{"rule_id": "PI-102-role-override", "severity": "high", "line": 30}
{"rule_id": "PI-105-conditional-on-agent-context", "severity": "high", "line": 30}
{"rule_id": "PI-202-do-not-tell-user", "severity": "medium", "line": 26}
{"rule_id": "PI-201-jailbreak-preamble", "severity": "medium", "line": 27}
{"rule_id": "PI-202-do-not-tell-user", "severity": "medium", "line": 27}
```

The complete command and output are recorded in [docs/demo-results.json](./docs/demo-results.json). Inputs and reproduction code are included in the repository.

![Existing terminal recording](./assets/demo.gif)

The existing recording is retained for context; the text example above documents the reproducible scenario.

## Usage

The CLI exposes the following operations. Commands after the example use your own paths or identifiers.

```bash
promptaudit scan .
promptaudit scan . --json
promptaudit rules
```

## Configuration

Supported project inputs include package-lock.json, poetry.lock and requirements.txt. Inspect CLI coverage output when dependency resolution or fetch fails; target Python/platform settings are available for cross-environment resolution. Customize the corpus only with reproducible examples.

## Integrations and responsibilities

<picture>
  <source media="(max-width: 600px) and (prefers-color-scheme: dark)" srcset="./assets/presentation/integrations-mobile-dark.svg">
  <source media="(max-width: 600px)" srcset="./assets/presentation/integrations-mobile-light.svg">
  <source media="(prefers-color-scheme: dark)" srcset="./assets/presentation/integrations-dark.svg">
  <img src="./assets/presentation/integrations-light.svg" width="960" alt="Integrations diagram">
</picture>

The following routes are implemented in the source. Choose the input that matches your task and keep the resulting artifact with your project.

| Route | Implemented role |
| --- | --- |
| Lockfiles / requirements | Supported dependency inputs |
| Package registries | Metadata and text fetch |
| Rule corpus YAML | Pattern definitions |
| JSON findings | Severity and source location |

## Limits and next steps

- Pattern scanning can miss unfamiliar attacks and can flag benign text. A clean scan is not proof that a package is safe.
- Normal scan may contact package registries. The recorded demo scans an included text fixture without fetching or executing a package.
- Unresolved, skipped or unscanned packages are coverage gaps, not clean results.

Rule improvements and dependency-format support require real false-positive and false-negative reports. Runtime agent isolation is a separate responsibility.

## License and contributions

See [LICENSE](./LICENSE). When reporting an issue, include a minimal input, the command, and the observed output.
