# Crate

A local similarity-search and mapping tool for a large personal sample library, built for a Bitwig Studio 6.1 workflow.

Not a sampler, drum machine, or plugin — a standalone search engine over `D:\_soundPacks` (~110,000 files / 340GB): automatic classification, a similarity map, transient-level "one-shot inside a longer sample" detection, and one-click drag-out into Bitwig.

**Full specification:** [.docs/samples_final.md](.docs/samples_final.md) — the living spec, updated as the design evolves. [.docs/samples001.md](.docs/samples001.md) and [.docs/samples002.md](.docs/samples002.md) are the historical proposal/discussion trail behind it.

**Development journal:** [.docs/crate_journal.md](.docs/crate_journal.md) — session-by-session progress against the phase plan.

## Status

Phases 1–3 (ingestion & metadata, heuristic analysis, transient segmentation) — complete, validated against the real library. Phase 4 (CLAP embeddings & classification) and Phase 4.5 ("listen and grab": sortable list, preview, drag into Bitwig) — complete. Phase 8's Recompute tab (rescan, folder scope, recompute attributes with its settings, stop, log) is in the window, so an index can be built without the CLI. Phase 7 (list, search, filter) — complete: CLAP text search, filters, an anchor with per-axis distance ranges, Recompute ranking with the weight bars, and sub-hit rows for segments that beat their parent. **The first daily-drivable milestone (Phases 1–4 + 4.5 + 7) is reached.** Phase 6 (map view) — complete: UMAP layout over the weighted feature space (Recompute tab, full re-fit or anchored-only), one point per sample coloured by class and shaped by type, halo for the ranked neighbours, badges for hits inside longer samples. Next: Phase 5's Qwen2-Audio spike, then Phase 9 (header: waveform, markers).

```bash
uv sync --extra ml   # Phase 4 onwards: torch + transformers (CLAP downloads on first use, ~600 MB)
crate-scan      # index a library root (incremental; moves keep their rows)
crate-analyze   # descriptors, tempo/loop-ness, structural type
crate-segment   # find one-shot hits buried inside longer samples
crate-embed     # CLAP vectors, zero-shot tag chips, content class
crate           # the window: filter, sort, preview, drag a sample or a buried hit into Bitwig;
                # Recompute tab = Rescan library, folder scope, Recompute attributes (§9.6)
```

First run, in the window: Browse to the library root → **Rescan library** → add a folder to the **Library scope** (or *Add root*) → **Recompute attributes**. Nothing runs until you press a button; **Stop** ends the current stage after its current file and keeps everything committed so far.

Then, on the **Attributes** tab: type what you are after ("footsteps on gravel", "sword clash") and press Search — a segment that matches better than its parent shows as an indented *hit* row you can preview and drag. Select a sample or a hit, press **⚓ Anchor**, set the weight bars, and press **Recompute ranking** (Recompute tab) for a Similarity column; the distance ranges under Filters then cut the list down per axis. **Recompute map layout** (same tab, needs `uv sync --extra map`) fits the map; the **Map** button switches the list for it — click a point to select and preview, wheel to zoom, drag to pan, right-click to fit.

The bottom panel shows the selected sample's waveform with its segments as begin/end markers, the measured attack/decay envelope and the playhead; the Attributes tab starts with the per-axis difference between the selected sample and the anchor. The Rhythmic / Melodic / Vocal / Other columns are CLAP's own numbers: how well the sample matches each of four prompt sets, as percentages that sum to 100 (the prompts are in the tooltips on the Attributes tab). The map is coloured by the current ranking or search score, one colour otherwise. The window uses a dark theme (`theme.py`).

## Development

Requires Python 3.11+ and [uv](https://github.com/astral-sh/uv).

```bash
uv sync
uv run crate
```
