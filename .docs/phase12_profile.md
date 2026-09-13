# Phase 12 — the profiling pass

*2026-09-13. `scripts/phase12_profile.py`, run by hand against the user's real index: 84,372 samples, 310,207 sections (4,834 of them CLAP windows), 394,219 feature-table items, 458 MB on disk, all of `D:\_soundPacks` in scope. Read-only; timings on this CPU. This is the pass spec §10 and §12 asked for before any native code could be considered.*

## What a session does, and what it costs

Warm figures (the index in the OS page cache; a cold first open added ~3 s to the samples query and ~5 s to the feature table — disk, not code).

| step | before | after | notes |
|---|---:|---:|---|
| `open_db` | 0.00 s | 0.00 s | |
| `load_samples` (the list's rows) | 0.64 s | 0.64 s | 84,372 rows; a pre-aggregated rewrite measured 0.50 s — not worth its complexity |
| `load_sections` (every child row) | 0.59 s | 0.59 s | 310,207 rows under 42,989 samples |
| `index_summary` (status bar) | 0.10 s | 0.09 s | |
| `SampleTreeModel.set_rows` | 0.09 s | 0.08 s | the tree built and sorted |
| column filter, File contains "kick" | 0.46 s | 0.45 s | 3,203 rows kept; Python per row, once per keystroke |
| model sort (Length / File) | 0.04 s | 0.03 s | |
| **`FeatureTable.load`** | **7.93 s** (12.6 s cold) | **4.18 s** | 394,219 items, 30,263 with vectors |
| feature-table arrays | **943 MB** | **130 MB** | before: float64 features 136 MB + a full (394,219 × 512) float32 vector matrix 807 MB, 92 % zeros; after: float32 features 68 MB + a (30,263 × 512) compact matrix 62 MB. The process working set grows 230 MB across the load, the rest being the 394k-entry row index and transient tuples |
| `distances` from an anchor | 0.15 s | 0.09 s | (n, 5) |
| `rank` (blend + fold) | 0.18–0.26 s | 0.17–0.24 s | 84,012 samples scored, 40,818 hits; the fold is a Python loop over scored rows |
| `set_similarity` into the tree | 0.15 s | 0.18 s | |
| scanner walk of `D:\_soundPacks` | 0.54 s | — | 108,932 files, 365 GB, 0 errors (warm directory cache) |

## What bound, and what was done

- **The feature table was the one hot spot.** `cProfile` put 8.8 of 12.6 s in a per-row Python parser, 5.7 s of that in 1.17 million separate `json.loads` calls on the three JSON-list descriptor columns. The loader now works column-wise: rows come out of SQLite as tuples, are transposed once, the JSON columns are parsed as *one* array each, and every transform runs over a whole column. The vectors are held compactly — one row per item that has one, an index of −1 for the rest — and the feature blocks are float32. `distances`, `search` and the layout's `weighted_matrix` read through the index. Behaviour is unchanged (the similarity, layout and GUI tests pass as they were).
- **The list is fine at this scale.** The tree model builds in 0.08 s and filters in 0.45 s over 84k rows with 310k children; the "lazier per-sample section fetch" the journal had pencilled in for Phase 12 is not needed and is not built.
- **The scanner's incremental re-scan is fine.** The walk over the whole library is half a second; the work of a rescan is hashing the files whose size or mtime changed, which is the design. Nothing to harden.
- **No native code.** Spec §10 reserved a Rust extension for a hot path profiling named. The only one was Python-level parsing, and vectorising it was the fix. The remaining 4 s of the load is SQLite fetch, the transpose and the standardisation, all in C already; a cached on-disk table (rebuilt after a recompute) would be the next step if the anchor-time wait ever matters, and it is not scheduled.

## The other two Phase 12 items, informed by the index

- **Corrupt and exotic files.** 360 rows had no analysis: all `.wav`, all with a header the scanner could not read. 189 were macOS AppleDouble resource forks (`._name.wav`, a few KB of metadata under the audio extension); of the rest, 39 of a 40-file probe had no RIFF marker at all — raw bytes with a `.wav` name — and one an unrecognised format. The stages already skip a sample without a readable header (`WHERE s.duration_s IS NOT NULL`), so nothing was being retried; the rows were simply blank in the list. Now: the scanner skips `._` files outright and counts them (an existing row goes at the next rescan), and the list's Type cell reads `unreadable` for a header-less row, with a tooltip saying what that means, so the Type filter can find or drop them. RX2 stays the accepted gap (§3, §13 risk #5), and a WAV without a RIFF header is the same kind of gap.
- **Cache eviction.** The render cache had 35 files, 90 MB, and the index pointed at none of them (orphans of earlier recomputes). `render.evict_cache` now drops orphans first, then the least recently *used* renders (a cache hit touches the file's mtime) until the cache is under the Library panel's limit (default 1,024 MB); `render_segment` runs it after writing a new file, tied to a preview or a drag, never a timer. The panel reads the cache out and has a *Clear* button.

## The recompute pass (second round, 2026-09-13)

*After the user's first full run: ten silent minutes of scanning, over an hour of analysis and segmentation at ~0.08 s per sample or segment, then CLAP at 0.48 s a clip — 16 hours projected. Measured on real files from the library; scripts in the session's scratchpad, numbers here.*

**Where one file's time went** (single-threaded, before the change):

| kind | audio | decode | HPSS | descriptors (incl. its HPSS) | detection (incl. its HPSS) | per segment |
|---|---:|---:|---:|---:|---:|---:|
| long (12 files) | 80 s mean | 0.83 s | 3.7 s | 5.0 s | 3.9 s | 57 ms × 5 = 0.29 s |
| short (12 files) | 1.4 s mean | 35 ms | 36 ms | 44 ms | ~0 (one-shots) | 14 ms × 3.7 = 51 ms |

HPSS ran twice per file — once for the descriptors, once for the transients — and was 7.4 of a long file's ~10 s. The second decode was another 0.8 s. The per-segment descriptor passes were 3 % on long files and up to a third on short multi-hits.

**Worker processes do not go further.** Analysis over 1,616 real files (instrument samples): 8 workers 63 ms/file, 16 workers 72, 32 workers 82; segmentation 39 / 36 / 45 ms per sample. The saved setting was already 8, the default cap. More processes contend for memory bandwidth on the median filters; the saving had to be algorithmic.

**The fused pass** (`describe.py`): one decode, one set of frame features per file, the descriptors, the structural type, the transients and every segment's descriptors read off them. On the same 1,616 files with 8 workers: the two stages in sequence 194.6 s, the fused pass **106.0 s — 1.84×**, identical segments. A window's descriptors are now *the parent's frames inside it* on the parent's grid, by one definition everywhere; a manual save without the parent's frames at hand computes them over a grid-aligned excerpt with 32 frames of context, which reproduces the parent's frames exactly except for the MFCC energy coefficient's 80 dB floor (relative to the loudest frame in view: a few percent on that coefficient of near-silent frames).

**Feedback.** The scanner walks first and then reads headers on up to eight threads with a progress line every 5,000 files; a stop in either phase writes nothing. Every stage's progress line carries an ETA from its running rate, and the embedding stage opens by saying how many whole-file clips and how many segments it is about to run through CLAP — a segment costs the same fixed 10-second pass as a file, which is the number that decides whether a run is an hour or a night.

**What CLAP costs, and what would change it.** The feature extractor pads every clip to 10 s, so the model's cost per clip is fixed at ~0.5 s on this CPU whatever the clip's length; nothing in the index or the storage is involved. Not done, in order of payoff: run the samples first with *Embed segments* off (the toggle exists; 310k segments would be the bulk of the night); a quantisation spike — int8 dynamic quantisation or an ONNX Runtime export of the audio encoder, typically 1.5–3× on CPU, to be measured before adoption like Phase 5; and the one order-of-magnitude change, a GPU, which is outside the project's stated constraint and the user's call. A vector database would change none of this: the vectors are cheap to store and to search (90 ms over 394k with numpy), the ranking blends five axes an approximate index cannot serve, and the cost is producing them.

## Left as noted, not built

- The window's text search and the Recompute tab hold one CLAP model each when both are used in a session: memory, not correctness. Sharing one instance across two threads is not worth the locking today.
- `SampleTreeModel.update_row` (Phase 11) does not re-sort; a re-typed row stays put until the next sort or reload — cheaper than losing the selection on every click.
