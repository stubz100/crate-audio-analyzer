# Crate

A local similarity-search and mapping tool for a large personal sample library, built for a Bitwig Studio 6.1 workflow.

Not a sampler, drum machine, or plugin — a standalone search engine over `D:\_soundPacks` (~110,000 files / 340GB): automatic classification, a similarity map, transient-level "one-shot inside a longer sample" detection, and one-click drag-out into Bitwig.

**Full specification:** [.docs/samples_final.md](.docs/samples_final.md) — the living spec, updated as the design evolves. [.docs/samples001.md](.docs/samples001.md) and [.docs/samples002.md](.docs/samples002.md) are the historical proposal/discussion trail behind it.

**Development journal:** [.docs/crate_journal.md](.docs/crate_journal.md) — session-by-session progress against the phase plan.

## Status

Phase 0 (foundations) — complete. Next: Phase 1 (ingestion & metadata).

## Development

Requires Python 3.11+ and [uv](https://github.com/astral-sh/uv).

```bash
uv sync
uv run crate
```
