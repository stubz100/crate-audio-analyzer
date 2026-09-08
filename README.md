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
crate-embed     # CLAP vectors, zero-shot tag chips, content class; --export FILE.npz dumps every vector
crate           # the window: filter, sort, preview, drag a sample or a buried hit into Bitwig;
                # Recompute tab = Library (folders, Root / In scope, Add / Remove, Rescan) + Recompute (§9.6)
```

First run, in the window: on the **Recompute** tab press **Add folder…** and pick a sample folder — it is scanned in and ticked *In scope* — then tick **Attributes** and press **Run**. Every folder the index knows is listed with two ticks: *Root* (the library's home) and *In scope* (shown in the list and map, walked by **Rescan**, covered by Run; unticked folders stay in the index, dormant). **Remove folder** deletes a folder's samples from the index. The steps under Run — Attributes (the files in scope, new/changed or all again; or **anchor only**: the anchored sample alone, every stage again under the settings below — the quick way to see what a changed setting does), Map layout, Captions — run in that order when ticked. Nothing runs until you press a button; **Stop** ends the current step after its current file and keeps everything committed so far.

Then, on the **Search** tab: type what you are after ("footsteps on gravel", "sword clash") and press Search — a segment that matches better than its parent shows as an indented *hit* row you can preview and drag, and a long file whose best part is one of its 10-s CLAP windows shows an indented *window* row the same way, so a search can land at minute seven of an ambience. Press the **⚓** at the start of any row (a hit row too): that sample becomes the anchor and the whole list is ranked against it at once, anchor on top, with a Similarity column; the distance ranges under Filters then cut the list down per axis. Move the weight bars and the list re-ranks as you release them; **✕** clears the anchor and leaves the order alone. **Recompute map layout** (same tab, needs `uv sync --extra map`) fits the map; the **Map** button switches the list for it — click a point to select and preview, wheel to zoom, drag to pan, right-click to fit.

The bottom panel shows the selected sample's waveform with its segments as begin/end markers, the measured attack/decay envelope and the playhead; the Attributes tab starts with the per-axis difference between the selected sample and the anchor. Analysis and segmentation fan out to worker processes (Recompute tab → Worker processes, or `--workers N`). The Rhythmic / Melodic / Vocal / Other columns are CLAP's own numbers: how well the sample matches each of four prompt sets, as percentages that sum to 100 (the prompts are in the tooltips on the Attributes tab). The map is coloured by the current ranking or search score, one colour otherwise. The window uses a dark theme (`theme.py`). **Captions** (a step under Run on the Recompute tab, off by default, a batch of files per Run so a folder gets done in sittings; **Caption this sample** on the Attributes tab for one file; `crate-caption`) write one Qwen2-Audio sentence per sample and show it on the Attributes tab — about 10 s per file; the Phase 5 spike (`.docs/phase5_spike.md`) found the model's encoder latent no better than CLAP for telling voices apart, so there is no vocal axis.

## Development

Requires Python 3.11+ and [uv](https://github.com/astral-sh/uv).

```bash
uv sync
uv run crate
```
