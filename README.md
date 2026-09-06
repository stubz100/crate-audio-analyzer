# Crate

A local similarity-search and mapping tool for a large personal sample library, built for a Bitwig Studio 6.1 workflow.

Not a sampler, drum machine, or plugin — a standalone search engine over `D:\_soundPacks` (~110,000 files / 340GB): automatic classification, a similarity map, transient-level "one-shot inside a longer sample" detection, and one-click drag-out into Bitwig.

**Full specification:** [.docs/samples_final.md](.docs/samples_final.md) — the living spec, updated as the design evolves. [.docs/samples001.md](.docs/samples001.md) and [.docs/samples002.md](.docs/samples002.md) are the historical proposal/discussion trail behind it.

**Development journal:** [.docs/crate_journal.md](.docs/crate_journal.md) — session-by-session progress against the phase plan.

## Status

Phases 1–3 (ingestion & metadata, heuristic analysis, transient segmentation) — complete, validated against the real library. Phase 4 (CLAP embeddings & classification) and Phase 4.5 ("listen and grab": sortable list, preview, drag into Bitwig) — complete. Next: Phase 7 (list, search, filter) for the first daily-drivable milestone.

```bash
uv sync --extra ml   # Phase 4 onwards: torch + transformers (CLAP downloads on first use, ~600 MB)
crate-scan      # index a library root (incremental; moves keep their rows)
crate-analyze   # descriptors, tempo/loop-ness, structural type
crate-segment   # find one-shot hits buried inside longer samples
crate-embed     # CLAP vectors, zero-shot tag chips, content class
crate           # the window: filter, sort, preview, drag a sample or a buried hit into Bitwig
```

## Development

Requires Python 3.11+ and [uv](https://github.com/astral-sh/uv).

```bash
uv sync
uv run crate
```
