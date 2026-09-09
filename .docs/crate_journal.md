# Crate Development Journal

Session-by-session record of what was actually built, decided, and verified — read in light of the implementation plan (roadmap in [.docs/samples_final.md](samples_final.md) §12). The spec is the source of truth for *design*; this journal is the source of truth for *progress*.

## Maintenance contract — do not let this lapse

- **Every work session ends with an entry here, appended before the work is committed** — even for small sessions. One entry per session (or per logical unit of work).
- Entry format: `## YYYY-MM-DD — <short title>` with a phase tag, then **Done / Decided / Verified / Next** (omit empty sections).
- **Update the roadmap status table below whenever a phase's status changes.**
- Cite commit hashes once work is committed.
- [CLAUDE.md](../CLAUDE.md) enforces this: sessions read the journal at start and append at end. If the journal and the git log ever disagree, reconcile the journal first.

## Roadmap status (mirrors spec §12)

| Phase | Name | Status |
|---|---|---|
| 0 | Foundations | ✅ done — `5bef7d8` |
| 1 | Ingestion & Metadata | ✅ done — `58da501` (build) + `46f131e`, `9d5acea` (review fixes) |
| 2 | Heuristic Analysis | ✅ done — `f650889` (build + review fixes); closed out against 943 real samples, `5e055b2` |
| 3 | Transient Segmentation | ✅ done — `5e055b2` (nodes S/T, both profiles, settings, segment tables, manual path; validated on 472 real samples). UI for manual markers is Phase 9; lazy render is Phase 4.5/9 |
| 4 | Embeddings & Classification | ✅ done — `1e1b585` (nodes D/C2/X/E on transformers' CLAP; Facet A 68% on 335 labeled files; full index embedded at 0.21 s/sample) |
| 4.5 | "Listen and grab" (pull-forward) | ✅ done — `f715d03` (sortable/filterable list, segments drill-down, Qt Multimedia preview, lazy segment render, file-URL drag-out) |
| 5 | Qwen2-Audio + Latent-Similarity Spike | ✅ done — `32ef47b` (`.docs/phase5_spike.md`: 102 files, 16 groups; captioning 6–20 s/file on this CPU, built as the opt-in Recompute stage + `crate-caption`; the latent axis a no-go across 13 poolings — no vocal-semantic axis) |
| 6 | Map View | ✅ done — `72ad400` (UMAP layout over the weighted feature space: full re-fit + anchored transform; painted map with class colours / type shapes, halo, badges; schema v7) |
| 7 | List, Search, Filter | ✅ done — `4756a35` (tree list with sub-hit rows; Attributes tab: weights, CLAP search, filters, anchor ranges, tag chips; Recompute ranking and a minimal anchor came with it; review fixes `7e3e6fb`) |
| 8 | Recompute Tab | ✅ done — Rescan, folder-scope list, Recompute attributes + settings, Stop, log `7bfd001`, review fixes `0c2d33f`; ranking with Phase 7 (`4756a35`), map layout (both scopes) with Phase 6 (`72ad400`); reshaped into a Library panel + one Recompute panel `b04ab9c`; ⚓ anchored-only Recompute *attributes*, the last piece, `b1c037f` (2026-09-08; schema v10 with it) |
| 9 | Header Interactions | ✅ done — preview + drag-out since 4.5, the ⚓ anchor since Phase 7 (`4756a35`; on every row since 2026-09-08), the waveform panel with segment markers, envelope and playhead `5acfcd4`; marker editing — drag, draw, Save / Discard / Delete segment — `06a7ec5` (2026-09-08) |
| 10 | Bitwig Integration | ⬜ not started |
| 11 | Correction Workflow | ⬜ not started |
| 12 | Scale & Polish Hardening | ⬜ not started |
| 13 | Stretch | ⬜ not started |

**Target: first daily-drivable milestone = Phases 1–4 + 4.5 + 7** (scan → analyze → embed → list/search/filter → audition → drag into Bitwig), per spec §12. **Reached 2026-09-07.**

---

## 2026-09-05 — Spec review and revisions

**Phase:** pre-Phase 0 (documentation) · committed in `5bef7d8`

**Done**

- External review of `samples_final.md` against the (then uncommitted) Phase 0 scaffold. All findings resolved into the spec:
  - **aubio dropped entirely.** Strategic reason: §10 justified it on per-segment throughput, but per-segment descriptor extraction is milliseconds — the real multiplier is per-segment CLAP embedding, which aubio does nothing for. Plus source-only distribution since 2019 (hidden MSVC-toolchain dependency) and GPLv3. Stack is librosa-only now. Note for accuracy: aubio 0.4.9 *did* build on this machine (~11 s, MSVC present) — the drop is strategic + hidden-dependency, not installability. Guarded in three places: spec §10, CLAUDE.md ("Don't reintroduce"), `pyproject.toml` comment.
  - **Qwen2-Audio cost framing corrected** (§5.2): ~8.4B params / ~17 GB bf16; CPU captioning is tens-of-seconds-to-minutes per file → weeks at library scale; quantization reframed as a prerequisite, not a fallback. Key separation: the Phase 5 latent-axis spike needs only the ~600M Whisper-style encoder (CPU-viable); captioning needs the full model.
  - **§3 arithmetic revised ~15 h → ~25–40 h** for the embedding pass once the segment multiplier is included; two new §9.6 settings added: *Embed segments* toggle and *Min length for segment embedding*.
  - **Data model**: `map_layout` + `map_position` tables (coordinates are a function of weights + scope + fit, not of a model); `map_x/map_y` removed from embedding tables. `file_size`/`file_mtime` as the change key, `file_hash` nullable (computed only when size/mtime changed). `text_tags` (machine-generated layer) vs `tags`/`sample_tags` (curated user-owned layer) defined with an explicit promotion flow.
  - **§5.1 pitch**: gate is HPSS-derived `harmonic_ratio` (NOT circular — pyin's voicing confidence is a different quantity, recorded only for gate-passing samples); `librosa.yin` is now the default f0 estimator, `pyin` demoted to an optional quality pass.
  - **Roadmap**: Phase 4.5 "Listen and grab" pull-forward (bare preview + drag-out right after the index is populated); first daily-drivable milestone named (Phases 1–4 + 4.5 + 7).
  - Smaller items: classifier mechanism (`E`: rule-based Facet B + CLAP zero-shot Facet A), smpl/ACID chunk reader scoped to Phase 2, S-gate wording aligned with the diagram, UMAP drift guidance, crate-export hardlink staleness check, `Last updated:` line.

**Next:** commit Phase 0.

## 2026-09-05 — Phase 0 validated and committed

**Phase:** 0 — Foundations ✅ · `5bef7d8`

**Done**

- Status lines flipped to "complete" in `README.md` and spec §15; everything committed as the root commit `5bef7d8` (10 files, 4,114 insertions): `.docs/` (living spec + two historical proposals), `CLAUDE.md`, `README.md`, `pyproject.toml`, `uv.lock`, `.gitignore`, `src/crate/{__init__,main}.py`.

**Verified**

- Phase 0 deliverable runs: `MainWindow` instantiated under `QT_QPA_PLATFORM=offscreen` — title "Crate", library root defaults to `D:\_soundPacks`, `QSettings` persistence round-trips.
- `uv lock --check` clean (108 packages resolved). Python 3.12.10 / uv 0.11.21.
- `.venv/` and `__pycache__/` confirmed ignored via `git check-ignore`.

## 2026-09-05 — Phase 1 readiness + journal created

**Phase:** 1 — Ingestion & Metadata (prep only)

**Done**

- `uv sync --extra analysis` — 26 packages installed: numpy 2.5.2, scipy 1.18.1, soundfile 0.14.0, **librosa 1.0.0** (+ numba/llvmlite, scikit-learn, pooch…). `uv.lock` unchanged (extras were already locked).
- `D:\_soundPacks` confirmed reachable; 23 top-level folders listed.
- Created this journal and wired its maintenance into `CLAUDE.md` (session-start read / session-end append) and `README.md`.

**Decided** (recommended defaults for Phase 1 — revisit only if a problem appears)

- DB location: `.crate_cache/crate.db` under the repo root (`.gitignore` already anticipates it); make it `QSettings`-configurable later.
- Files deleted between scans: drop the skeleton row + log a count.
- smpl/ACID chunk parsing: **Phase 2** per spec §10 — Phase 1's "embedded WAV metadata" = `sf.info` fields only.
- Test subset (spec §15 item 2): start with `D:\_soundPacks\ModeAudio - Raw Material [WAV]` (322 WAVs, one-shot-ish — good for later segmentation testing); add a `___DRUMS` subfolder in Phase 3 when loops matter.

**Verified**

- `sf.info` against a real library file — `ModeAudio - Raw Material [WAV]\Ceramic\Ceramic_Hit01.wav` → 44.1 kHz / stereo / 0.302 s / PCM_24. The Phase 1 metadata path is proven against the real library.

**Next** (Phase 1 start)

1. Add a pytest dev dependency group to `pyproject.toml`.
2. `src/crate/db.py` — SQLite schema; `samples` table in its full spec §8 shape.
3. `src/crate/scanner.py` — node `A`: full-root walk, extension filtering (wav/flac in; `.rx2` skip-and-log; containers skip-and-log), skeleton rows, incremental re-scan via size+mtime diff.
4. `tests/` — synthetic fixture tree, per §3's "test on a small subset" principle.

## 2026-09-05 — Phase 1 implemented: scanner, schema, CLI

**Phase:** 1 — Ingestion & Metadata ✅ · `58da501`

**Done**

- `src/crate/db.py` — `open_db()` + the full spec §8 `samples` schema (all 15 columns created up front; Phase 3's `segment_candidates_found`/`segments_capped`/`effective_sensitivity` start NULL). `filepath` UNIQUE; timestamps ISO-8601 UTC.
- `src/crate/scanner.py` — node `A` (spec §7): recursive walk, extension filtering (`.wav`/`.flac` in; `.rx2` skip-and-log; everything else skip-and-log by extension), skeleton rows via header-only `sf.info`, incremental diff on `file_size`+`file_mtime`, blake2b content hash computed **only** when size/mtime changed (spec §8), removal scoped to rows under the scanned root, `ScanSummary` as the node-A worklist.
- `src/crate/cli.py` + `crate-scan` console script — `--root` (default `D:\_soundPacks`), `--db` (default `<cwd>/.crate_cache/crate.db`), `-v`.
- `tests/test_db.py` + `tests/test_scanner.py` — 12 tests over synthetic fixture trees (real WAV/FLAC written with soundfile): first scan, case-insensitive extensions, no-change re-scan, add/remove, change re-hash, mtime-only change, unreadable header, removal scoped to root, missing root.
- `pyproject.toml` — pytest added as a dev dependency group; `.pytest_cache/` added to `.gitignore`.

**Decided**

- `folder` stored POSIX-style relative to the scan root (`""` = root level) — keeps later folder-scope matching simple.
- Unreadable audio header → row kept with NULL metadata, counted as `unreadable` (faithful to "skeleton row per new file" + node B's "failures logged, not fatal"); Phase 12's corrupt-file handling owns these.
- One DB may hold several roots; a scan only removes rows under its own root prefix, so subset DBs and a full-library DB never clobber each other.
- Hash algorithm: blake2b, streamed in 1 MiB chunks.

**Verified**

- `uv run pytest tests -v` → **12 passed** in 2.1 s.
- Real subset: `crate-scan --root "D:\_soundPacks\ModeAudio - Raw Material [WAV]"` → added 322 / 2.8 s, 1 `.pdf` skip-and-logged; second run → unchanged 322 / 0.1 s. Incremental behavior proven against the real library.
- DB spot-check: rows carry correct metadata (e.g. `Kitchen Appliances\Kettle_Boil02.wav` — 37.5 s, 44.1 kHz, stereo), relative folders, NULL hashes for first-inserted files per spec §8.

**Next** (Phase 2 — Heuristic Analysis)

1. `analysis` table (spec §8) + node `B`/`C`: full decode, descriptor set per §5.1 (amplitude/pitch/timbre/spectrum via librosa; HPSS harmonic ratio as the pitch gate; `yin` default f0).
2. Tempo/onset/loop-ness incl. the custom `smpl`/ACID chunk reader (spec §10 — real Phase 2 work).
3. Facet B structural typing inputs (duration/onset count/periodicity) prepared for node `E` in Phase 4.
4. Run against the ModeAudio subset DB built this session.
## 2026-09-06 — Phase 1 code review (15 findings)

**Phase:** 1 — Ingestion & Metadata · review only, **no code changed, nothing committed**

**Done**

- High-effort code review of `58da501` (`scanner.py`, `db.py`, `cli.py`, tests). 15 findings, each re-confirmed against the code rather than accepted on report.

**Fix before Phase 2 starts (4)**

| File:line | Finding |
|---|---|
| `scanner.py:207` | **Data loss.** A file/directory the walk cannot read is treated as "vanished" and its row DELETEd. `os.walk` uses the default `onerror=None` (listing failures silently swallowed) and the `stat()` handler `continue`s *before* `seen.add()` — so a momentarily locked folder, disconnected external/network drive, >260-char path, or a file held open by a DAW drops rows, taking `added_at`, `file_hash`, and (from Phase 2) every FK-linked analysis/embedding row with them. Fix: `onerror` callback that aborts/records failed subtrees + add stat-failed paths to `seen`. |
| `scanner.py:30` | **Phase 1 does not run on a fresh clone.** `import soundfile as sf` is module-level, but `soundfile`/`numpy` sit in the `analysis` extra (labelled Phase 2/3) while README + CLAUDE.md document install as plain `uv sync` → `ModuleNotFoundError` via `cli.py`, and `uv run pytest` fails collection. Works here only because this venv already has the extra. Also contradicts CLAUDE.md's "deps arrive with the phase that needs them" — Phase 1 needs soundfile. |
| `scanner.py:176` | **`file_hash` is write-only.** Written at `:199`, never read back anywhere; the change decision at `:164` is size+mtime only. Backup restore / NAS re-sync / zip re-extract = identical bytes, new mtimes → full blake2b re-read of potentially hundreds of GB **and** files marked `changed`, which from Phase 2 on re-runs the entire analysis+embedding pipeline for content that did not change. Compare against `old["file_hash"]` and demote to `unchanged` when equal — the use spec §8 intended. |
| `scanner.py:115` | **Drive-root scans never detect removals.** `root_prefix = str(root) + os.sep` doubles the separator when the root is a drive root (`Path("D:\\").resolve()` already ends in one), so `vanished` stays permanently empty, deleted files keep stale rows forever, and `removed` always prints 0. Use `os.path.join(root, "")` or `Path.is_relative_to`. |

**Also worth doing now, cheap now / painful later (2)**

| File:line | Finding |
|---|---|
| `db.py:44` | **No schema version.** `CREATE TABLE IF NOT EXISTS` is a no-op against a pre-existing table of any shape, so a Phase 1 DB opened by Phase 2+ appears fine then fails deep in a query (`no column named ...`). `PRAGMA user_version = 1` + a check is one line **now** and is the anchor every later migration needs — Phase 2 adds the `analysis` table, so this is the last cheap moment. |
| `scanner.py:165` | **`folder` goes stale in multi-root DBs.** The unchanged branch updates only `last_scanned_at`, so scanning a parent root into a DB built from a child root leaves `folder = ""` when it should become `"libA"` — violating db.py:22's documented contract and breaking the column's stated purpose ("keeps later folder-scope matching simple"), which spec §9.6's folder-scope list depends on. This is the exact multi-root workflow the module docstring, the 2026-09-05 "one DB may hold several roots" decision, and `test_removal_is_scoped_to_the_scanned_root` all bless. |

**Follow-up (9)**

| File:line | Finding | Category |
|---|---|---|
| `scanner.py:122` | Whole 110k-file walk buffered and committed in one transaction — Ctrl+C / power loss / any `executemany` error discards a multi-hour scan; also holds three ~110k lists + `known` at once. Batch commits. | correctness |
| `cli.py:31` | `--db` default is `Path.cwd() / ...`, so running `crate-scan` from another folder silently creates a second empty index and re-adds all ~110k files, orphaning the real one. Belongs at a stable per-user location (QStandardPaths), as the GUI already does via QSettings. | correctness |
| `cli.py:16` | `DEFAULT_ROOT` re-hardcodes `D:\_soundPacks`, duplicating `main.py:28` and ignoring the QSettings-persisted root that was Phase 0's whole deliverable — two sources of truth that will drift. | correctness |
| `cli.py:44` | `-v/--verbose` is a no-op (only two `log.warning` calls exist, which pass at both levels), and its help text claims per-file warnings are hidden by default — they are not, so a bad folder floods stderr regardless. | correctness |
| `scanner.py:97` | `_read_metadata` catches bare `Exception` and logs only the path, discarding the error — a broken libsndfile install is indistinguishable from 110k individually corrupt files. Log the exception. | correctness |
| `scanner.py:118` | `SELECT *` materializes all 15 columns (incl. hex hashes, wide paths) for 110k rows when the diff reads 3 → >100MB resident for the whole scan. | efficiency |
| `scanner.py:146` | `rel_folder` recomputed per file though constant per directory (~440k redundant `Path` allocations), and `path.stat()` re-issues a syscall `os.scandir`'s `DirEntry.stat()` already has (~110k avoidable) — on this Windows-only tool's hottest path. | efficiency |
| `scanner.py:168` | Insert and update branches carry a byte-identical copy of the read-metadata / count-unreadable / null-fallback block; a one-sided fix would silently diverge first-scan from re-scan behavior, and no test covers a file that becomes unreadable after a good scan. | simplification |
| journal + `README.md:13` | Phase 1 marked ✅ while spec §12's Phase 1 deliverable "embedded WAV metadata" is deferred to Phase 2 — see open decision below. | status accuracy |

**Verified** (independently, not taken from the review report)

- Drive-root prefix bug **reproduced**: root `E:\` → resolved `'E:\'` → prefix `'E:\'` → `'E:\source\x.wav'.startswith(prefix)` is `False`, vs `True` for a non-drive root.
- `grep -rn file_hash src/` → two hits only: the schema comment (`db.py:31`) and the UPDATE (`scanner.py:199`). Nothing reads it. Write-only confirmed.
- `grep '^import' src/crate/scanner.py` → `import soundfile as sf` at line 30 (module level); `pyproject.toml` base `dependencies = ["PySide6>=6.7"]` only. Fresh-clone breakage confirmed.
- `grep 'log\.' src/crate/scanner.py` → exactly two calls, both `log.warning`. `-v` no-op confirmed.
- Deletion-on-error path confirmed by reading: `except OSError: continue` at `:139-142` precedes `seen.add()` at `:145`.

**Open decision (needs a call before Phase 2)**

- Spec §12 defines Phase 1 as "File scanner …, SQLite schema, **embedded WAV metadata**". The scanner reads only `duration_s`/`sample_rate`/`channels` via `sf.info`; the smpl/ACID chunk reader was consciously pushed to Phase 2 on 2026-09-05. Either (a) implement the chunk reader to close Phase 1, or (b) amend spec §12 to record the deliverable as deliberately moved to Phase 2. Until then the roadmap row is flagged ⚠️ rather than ✅, since CLAUDE.md forbids starting a phase on top of an incomplete one.

**Next**

1. Resolve the open decision above (spec amendment or chunk reader).
2. Fix the four "before Phase 2" findings + the two cheap-now ones as a single commit; add regression tests for each (unreadable subtree must not delete rows; drive-root removal; mtime-only-change-with-identical-content stays `unchanged`; re-scan from a parent root refreshes `folder`).
3. Follow-up commit for the nine remaining items.
4. Then Phase 2 — heuristic analysis, as previously planned.

## 2026-09-06 — Phase 1 review resolved

**Phase:** 1 — Ingestion & Metadata ✅ · fixes in `46f131e` + `9d5acea`

**Done**

- All 15 findings from today's review fixed, with regression tests. The six priority items first (`46f131e`):
  1. **Walk error safety** — `os.walk` replaced with an explicit `os.scandir` walk; directory-listing, stat, and hash failures are counted as `walk_errors` and their rows/subtrees PROTECTED from removal (record + protect, never abort).
  2. **Fresh-clone fix** — `soundfile` + `numpy` moved to base deps; bare `uv sync` now satisfies Phase 1.
  3. **`file_hash` read back** — size/mtime changed but stored hash equal ⇒ demoted to `unchanged` (change key still refreshed), so later phases never re-analyze identical bytes.
  4. **Drive-root prefix** — `_under_root_prefix()` via `os.path.join(root, "")`; unit-tested.
  5. **`PRAGMA user_version = 1`** + refuse-newer-builds check in `open_db`.
  6. **`folder` refreshed** in the unchanged branch — parent-root re-scans honor the documented contract.
- The same scanner rewrite absorbed follow-ups **7** (chunked commits, 5k rows), **11** (exceptions logged + sampled into the summary), **12** (diff selects only `filepath, file_size, file_mtime, file_hash`), **13** (`rel_folder` once per directory; `DirEntry.stat()` caching), **14** (shared `_metadata_or_null()` + new became-corrupt test).
- **Spec amendment (finding 15, decision b)**: §12 Phase 1 now reads "audio-header metadata (duration/rate/channels via `sf.info`)"; the `smpl`/ACID chunk reader is explicitly reassigned to Phase 2 (its storage target `analysis.embedded_metadata_json` is a Phase 2 table; §10 always placed it there). `Last updated:` bumped.
- `9d5acea` — follow-ups **8/9/10**: `--db` default → `%LOCALAPPDATA%\Crate\crate.db` (reverses the 2026-09-05 repo-local decision); `config.DEFAULT_LIBRARY_PATH` shared by GUI and CLI; `-v` streams per-file DEBUG detail with honest help text (default level WARNING, summary always shown).

**Decided**

- Error policy: **record + protect, never abort** — a locked folder must not kill a multi-hour scan; the next scan after it becomes readable restores normal removal detection.
- Demotion caveat kept visible: the first change after a fresh scan has NULL stored hash → reported `changed` once (storing the hash); demotion applies from the second change on. Size differences skip the comparison (bytes must differ) but still refresh the stored hash.
- CLI stays Qt-free; reading the QSettings-persisted root is deferred to the GUI phase — the shared constant kills the duplication meanwhile.

**Verified**

- `uv run pytest tests -v` → **20 passed** (12 prior + 8 new regression: unreadable-dir protection, hash-failure protection, demotion, drive-root prefix, parent-root folder refresh, became-corrupt, schema version set/refused) in ~1 s.
- **Fresh-clone proof**: bare `uv sync` uninstalled all 20 extra packages; base imports AND the full test suite still green.
- Real subset: `crate-scan --root "…ModeAudio - Raw Material [WAV]" --db .crate_cache\crate.db` → unchanged 322 / 0.2 s; existing DB auto-stamped user_version 0→1.
- GUI offscreen smoke still OK after the `config.py` refactor.

**Next** — Phase 2 (heuristic analysis), per plan: `analysis` table, node B/C decode + §5.1 descriptor set, tempo/onset/loop-ness incl. the `smpl`/ACID chunk reader (now unambiguously Phase 2), structural-typing inputs for node `E`; run against the ModeAudio subset.

## 2026-09-06 — Phase 2: heuristic analysis (nodes B/C), review of Phase 1 fixes

**Phase:** 2 — Heuristic Analysis 🚧 · **uncommitted**

**Done — review of the Phase 1 fixes (`46f131e`, `9d5acea`)**

- All 15 findings verified as genuinely fixed by reading the current code, not just the diffs. The error-safe walk is the notable one: `_walk_files` now separates *subtree* failures (`failed_dirs`) from *entry* failures (`failed_files`) and the vanished-filter excludes both, so an unreadable folder can no longer be mistaken for a deletion.
- One **residual edge case** of finding 1 spotted, not yet fixed: in `_walk_files`, if `entry.is_dir()` itself raises, the handler records it via `on_error(..., subtree=False)`. Since we could not determine whether it was a directory, its children are *not* protected — only the exact path is. `subtree=True` is the conservative call there (cost of being wrong: one stale row survives a scan; cost of the current behavior: rows deleted).

**Done — Phase 2 implementation**

- `src/crate/analysis.py` (new) — nodes B/C: decode at 22.05 kHz mono, then the full §5.1 descriptor set (amplitude: peak/rms dB, crest, attack/decay ms · pitch: HPSS-gated `yin` · timbre: MFCC mean+var, spectral contrast · spectrum: centroid/bandwidth/rolloff/flatness), onsets, tempo, periodicity, loop-ness, and Facet B structural typing. `analyze_pending()` drives it, committing per file.
- `src/crate/wavmeta.py` — reviewed and corrected (details below), now seeks through RIFF chunks instead of reading whole files.
- `crate-analyze` CLI: `--db`, `--limit` (time a subset before the full library, §3), `--reanalyze`, `-v`.
- `db.py` schema v2: `analysis` + `classification` tables.
- 47 tests passing (20 prior + 27 new across `test_wavmeta.py` / `test_analysis.py`).

**Fixed in the WIP before it was committed**

1. **ACID struct layout was wrong.** `"<IHHfHHf"` (20 bytes) omitted the `uint32` beat count, so every field after it was misaligned: a 120 BPM chunk parsed as `beats=0.0, meter='0/8', tempo=3.7e-40`. Corrected to `"<IHHfIHHf"` (24 bytes).
2. **`read_embedded_metadata` read entire files into memory** (then sliced the whole `data` chunk again — 2x file size peak) to find a 24-byte chunk. Now opens once and seeks past chunk bodies it does not want.
3. **Timbre columns were scalars.** `mfcc_mean`/`mfcc_var`/`spectral_contrast` were `REAL`; MFCC is 13-dimensional and contrast is 7-band, so a scalar mean would have collapsed the entire Timbre similarity axis (§5.1) to one number. Now `TEXT` holding JSON vectors, with a regression test asserting lengths 13/13/7.
4. **Pitch gate sat exactly on the noise midpoint.** Measured HPSS harmonic ratio on this machine: tonal 0.955–0.998, white noise **0.501–0.510**, clicks 0.000. The initial 0.5 threshold meant noise landed either side at random. Now 0.7, with the measurements recorded in the code.
5. `test_schema_version_is_stamped` still asserted v1 after the bump to v2.

**Decided**

- **ACID tempo is derived, never read.** Forensics on 52 real ACIDized files in `D:\_soundPacks`: *every one* stores a nominal `120.0` in the tempo float while `beats` is correct. `beats / duration * 60` reproduces the tempo in the filenames exactly. `_acid_tempo_bpm()` derives it and overrides the acoustic estimate; `wavmeta` still reports the chunk verbatim.
- **A beat count alone marks a loop.** Real ACIDized loops routinely carry `beats`/`meter` with *no* flags set (`flags=0x0`), so keying loop-ness off `acidized`/`stretch` alone would have missed them.
- **`tempo_confidence` stays acoustic** (onset-envelope autocorrelation peak) even when tempo comes from metadata — overloading it would destroy the "is this actually periodic?" signal loop detection needs, since `beat_track` returns *a* tempo for even a single hit.
- **librosa + scipy moved to base dependencies.** Phase 2 has arrived, so the same rule that moved soundfile applies; the `analysis` extra is now empty. librosa is still imported lazily inside functions (import costs seconds; the GUI and `crate-scan` should not pay it).
- Analysis decodes at a fixed 22.05 kHz mono; `samples` keeps the true native rate/channels. Resampling once is much cheaper than analyzing 110k files at native rates.

**Verified**

- `uv run pytest tests -q` → **47 passed**.
- Real subset (`ModeAudio - Raw Material [WAV]`): scan 322 files / 0.8 s, then `crate-analyze --limit 60` → 60 analyzed, 0 failed, **13.7 s ≈ 0.23 s/file**. Extrapolated: **~7 h for a full 110k-file heuristic pass** — a real datapoint for §3's cost model (heuristics only; CLAP is separate).
- Descriptor sanity on real foley: water/boil recordings → many onsets, low harmonic ratio, pitch correctly gated off, no NULLs in any descriptor column across all 60 rows.
- **ACID end-to-end on a real DnB pack**: 12 files, all `facet_b = loop`, all **174.0 BPM derived** — matching the `174` in every filename — across durations 2.76 s → 11.03 s (4 to 32 beats). Before the fixes these would have read 120.0 (or garbage under the original struct).
- Chunk reader across 600 real library WAVs: 52 ACID + 14 smpl parsed, no crashes.

**Next**

1. Fix the residual `is_dir()`-raises edge case above (`subtree=True`).
2. Attack-time definition needs a tuning pass: it is measured to the *global* peak, so a sustained foley take reports a multi-second "attack" (e.g. `Water_Pour01` → 4245 ms). Fine for one-shots, misleading for sustained material.
3. Key estimation (`analysis.key`) is still NULL — spec §5.1 leaves it unscheduled; decide whether Phase 2 closes it or it moves to Phase 4 with the rest of the tonal work.
4. Run the full ModeAudio subset (322) and a loop-heavy pack, then review the Facet B distribution before calling Phase 2 done.

## 2026-09-06 — Phase 2 progress review (uncommitted WIP)

**Phase:** 2 — Heuristic Analysis 🚧 · review only, **no code changed**

**Done**

- Reviewed the uncommitted Phase 2 work (`analysis.py`, `wavmeta.py`, schema v2, `crate-analyze`) against spec §4/§5.1/§7/§8, and re-ran the ModeAudio subset (322 files) into a scratch DB to check the Facet B distribution — journal item 4 of the previous entry.

**Verified** (against the code and real data, not the previous entry)

- `uv run pytest tests -q` → 47 passed. `uv lock --check` clean.
- **Attack/decay are measured on the raw rectified waveform, not an envelope** (`_envelope_times`): `np.abs(y)` dips to zero every half-cycle, so decay hits the −20 dB threshold at the first zero crossing. Synthetic pluck with a true −20 dB point at ~345 ms → 1.1 ms; plain sine → 0.5 ms. Across all 322 subset files: max `decay_ms` 6.9, none above 10 ms. Attack is measured to the *global* peak: `Kettle_Boil02` → 22.8 s; 9 of 17 files >5 s report >1 s. Two of the four §5.1 Amplitude descriptors are unusable as stored.
- **Facet B on a foley one-shot pack: loop 13 / multi-hit 211 / one-shot 98.** 175 of 273 files ≤2 s typed multi-hit; `onset_detect` fires 2–6× on single hits with rattle/ring-out (onset histogram for ≤2 s files: 1×98, 2×65, 3×49, 4×25, 5×14, 6+×28). All 13 "loops" are false positives (`Glass_Hit01` 3.5 s @161 BPM, `Foil_Hit03` 0.93 s @215 BPM) — the 0.30 autocorrelation threshold admits ringing.
- **`tempo_bpm` is populated for 96/98 one-shots and 207/211 multi-hits** (a 0.4 s tone → 198.8 BPM at confidence 0.014); `beat_track` always returns a tempo. Schema comment says "NULL when no detectable tempo".
- **Changed files are never re-analyzed**: the scanner's `changed` branch updates `samples` only; `analyze_pending` selects rows with *no* `analysis` row. Node A's "worklist of new/changed files" (§7) dies with the `ScanSummary`.
- `python -m crate.cli` → `NameError: main` (`cli.py:66`, stale `__main__` guard after the `scan_main` rename).
- Timing: steady state on the subset is **0.076 s/file**, ~**0.037 s per audio-second** (37 s file → 1.4 s); the previous entry's 0.23 s/file included numba JIT warm-up (~8.8 s on the first non-trivial file). Cost scales with total audio duration, not file count.
- The previous entry's "Verified" runs are not reproducible from disk: `.crate_cache/crate.db` is still schema v1 with no `analysis` table and `%LOCALAPPDATA%\Crate` does not exist.

**Next** (before calling Phase 2 done)

1. Envelope: compute a frame envelope (`librosa.feature.rms` or rectified+lowpass), measure attack to the *first onset's* local peak and decay from it; add tests asserting values for a pluck and a sustained tone.
2. Facet B: count *dominant* onsets (strength relative to the strongest, per §4 wording) instead of raw count; require bar alignment for loops (`duration × bpm / 60` ≈ integer — 7.49 for `Glass_Tap01`, exactly 8/16/32 for the real DnB files) and raise `LOOP_MIN_DURATION_S`; validate the acoustic tempo against the 52 ACID files as ground truth.
3. Store `tempo_bpm` only when `is_loop` (or confidence ≥ threshold).
4. Decide the staleness mechanism once (scanner deletes derived rows for `changed` files, or `analyzed_at` vs a content-changed timestamp) — Phase 3/4 tables will hang off the same rule. Add a test.
5. Align `classification` columns with §8's mirror (`content_class`, `structural_type`, `confidence`) while the table is empty.
6. `key`: populate from ACID `root_note` (when `root_set`) / `smpl.midi_unity_note` now (§10 "embedded tempo/key/loop metadata"); Krumhansl-Schmuckler stays Phase 13 stretch.
7. Small: fix the `__main__` guard; progress log visible without `-v`; drop the empty `analysis` extra; `.gitattributes` (`* text=auto`) to stop the CRLF warnings; record the DB path in every Verified entry.

## 2026-09-06 — Phase 2 review fixes: envelope, Facet B tuning, tempo policy, stale flagging

**Phase:** 2 — Heuristic Analysis 🚧 · **uncommitted** (on top of the previous WIP)

Directions from the review discussion: (1) frame envelope, then check the numbers; (2) evaluate a harmonic-aware ("spectral") approach to onset counting; (3) tempo NULL unless explicitly recognisable; (4) flag changed files, recompute on demand only, a scan action on the Recompute tab later; small + cheap-now items as recommended.

**Done**

- **Envelope** (`_envelope_times`): attack/decay measured on a frame RMS envelope (23 ms window, 2.9 ms hop), attack = last sub-threshold point → peak of the *first dominant onset*, decay = that peak → −20 dB (NULL if the sound never drops). Tests assert values for a pluck (−20 dB at 345 ms) and a swell.
- **Facet B, evaluated in three rounds on 500 labeled library files** (220 from seven "loops" folders, 280 from seven "one shots"/"hits" folders, seed 0, ≤30 s), then re-verified end to end with the real code:

  | Round | Change under test | loop-folder → loop | hit-folder → loop (false) | hit-folder → one-shot |
  |---|---|---|---|---|
  | baseline | previous rules | 68.6% | 14.3% | 26.1% |
  | 1 | onsets on HPSS percussive, dominant ≥0.3 strength, superflux | 64–66% | 7.9–12% | 35–40% |
  | 2 | periodicity = genuine local peak on detrended autocorrelation; whole-beat alignment in *beats*; coverage; 16th-grid fit | 40–50% acoustic only | **0.0–0.4%** | 35–40% |
  | 2 | + metadata tiers (ACID beats; filename "<n> BPM" + whole-beat duration) | **96.4–96.8%** | 0.0–0.4% | — |
  | 3 | dominance = strength ≥0.3 **and** loudness ≥0.2 of the file's loudest | 97.3% | 0.0% | 51.4% |
  | final | + sample-zero lead-in fix, symmetric loudness window, beat-tracker octave tie-break | **97.3%** | **0.0%** | **57.1%** |

  The hit folders are not pure one-shot ground truth — "Crunch and Rustle / Single Hits" and vocal shouts are legitimately multi-hit per §4 — so the decisive metrics are loop recall and the false-loop rate. Filename BPM: present in 140 files, all in loop folders, all whole-beat aligned, none in hit folders.
- **Tempo policy**: `tempo_bpm` NULL unless loop (ACID beats exact; filename BPM confirmed by duration; acoustic = whole-beat tempo, octave picked by `beat_track`, which was the more accurate estimator against 74 ACID files — 29/74 within 3% vs 15/74 for the autocorrelation peak).
- **Stale flagging (schema v3)**: `samples.content_changed_at` (scanner stamps on insert and genuine change, never on a hash-identical retouch) + `analysis.analyzed_at`; `crate-scan` reports `stale analysis: N`; `crate-analyze` refreshes new + stale by default, `--reanalyze` forces all. `db.py` gained idempotent migrations (`_MIGRATIONS`) — v0/v1/v2 indexes upgrade in one `open_db`. Timestamps now microsecond ISO-8601.
- **Cheap-now**: `classification` columns → `content_class` / `structural_type` / `confidence` (spec §8 mirror); `analysis.key` = pitch class from ACID `root_note` when `root_set`.
- **Small**: stale `__main__` guard fixed; `crate-analyze` progress at INFO by default with rate; empty `analysis` extra removed (`uv lock` clean); `.gitattributes` (`* text=auto eol=lf`) + working tree normalized to LF (`config.py` shows as modified for that reason only); scanner's undeterminable-entry case now protects the subtree; CLI docstring made raw.
- **Spec**: §4 detection notes, §8 (`content_changed_at`, `analyzed_at`, explicit `classification` columns, tempo-for-loops), §9.6 **Rescan library** row (the user's "scan for changes" button — Phase 8), `Last updated` bumped. README status line updated.

**Decided**

- **`smpl` loop points no longer mark a loop.** Survey of 5,755 files: the 160 files carrying them are spoken word, single vocal notes, reverse breaths, synth chords — sampler sustain regions, not tempo material. ACID beat count remains authoritative.
- **`smpl` unity note is not a key source**: 282 of 290 carry the placeholder 60. Only ACID `root_set` is trusted (392 of 621 ACID files, real varied pitch classes). Key *estimation* stays Phase 13.
- **One-shot keeps spec §4's "short" (≤ 2 s) rule.** Open question for the user: 34 of ModeAudio's 322 files have exactly one dominant onset but exceed 2 s (ringing glass/metal, 3–4 s) and therefore land in multi-hit. Node S would route them to segmentation and find nothing — harmless, but arguably mislabeled.
- Attack on sustained textures is by definition "time since the envelope was last 20 dB down", which for a boiling kettle is 22.6 s. Left as is (3 of 322 files > 1 s); a riser genuinely has a multi-second attack, and the envelope cannot tell the two apart.

**Verified**

- `uv run pytest tests -q` → **58 passed** (was 47): envelope values, tempo NULL on one-shots, quiet echoes not counted, bar-aligned click train = loop @ 120, ACID/smpl/key decisions, stale re-analysis end to end, v2→v3 and unversioned→v3 migrations, scanner stamp semantics.
- Real code end to end on ModeAudio (scratch DB): **258 one-shot / 64 multi-hit / 0 loop** (was 98 / 211 / 13); `tempo_bpm` NULL on all 322; decay 14.5 ms – 24.4 s, median 128 ms (was max 6.9 ms); attack median 11.6 ms. 0.064–0.081 s/file.
- Migration of a **copy** of the real `.crate_cache/crate.db` (v1, 322 samples): → v3, `content_changed_at` backfilled, new tables in final shape.
- Scratch DBs live under the session scratchpad and are disposable; the real subset index is still `.crate_cache/crate.db` (v1 until next opened by the new code, which migrates it).

**Next**

1. Commit Phase 2 WIP + these fixes (this entry precedes the commit per the contract).
2. Decide the long single-onset question above (spec §4 wording vs. a duration-independent one-shot).
3. Run `crate-scan` + `crate-analyze` on the real subset DB and a loop-heavy pack (e.g. `___DRUMS\AUDIOMODERN_SHIFT2_AM036`), review the distribution, then call Phase 2 done.
4. Phase 3 (segmentation) inherits `onset_count` = dominant onsets on the percussive component; node `T` should reuse `_dominant_onsets` rather than re-detect.

**Addendum — same day, after the user reviewed the above**

- **One-shot duration cap is now a setting** (user decision): `structural_type(..., one_shot_max_duration_s)` — a value keeps the cap on (default 2.0 s, spec §4 "short"), `None` makes one-shots duration-independent. `crate-analyze --one-shot-max-duration SECONDS` / `--one-shot-any-duration`; recorded in spec §9.6 ("One-shot max duration", on / 2.0 s) and in §4's detection notes. Two tests.
- The 34 single-dominant-onset ModeAudio files longer than 2 s, from the verification DB. Shortest: `Metal_Lid02` 2.03 s, `GasHob_ClickBurn02` 2.08 s, `Metal_Lid01` 2.16 s, `Coffee_Beans03` 2.22 s. Longest: `Microwave_Hum01` 20.9 s, `Food_Fry03` 30.8 s, `Kettle_Boil01` 36.4 s, `Kettle_Boil02` 37.6 s. By folder: Kitchen Appliances 8, Food 7, Metal 7, Water 5, Kitchen Utensils 4, Plastic 3. The short end are plainly one-shots, the long end are sustained textures — the population straddles any fixed cap, which is why it is a setting and not a rule.
- **Verified**: `uv run pytest tests -q` → **60 passed**; `crate-analyze --help` shows both flags.
- **Committed** (on `master`, like every earlier session): `f650889` — Phase 2 build + every fix in this entry, `.gitattributes` included. A separate whitespace-only renormalization commit turned out to be unnecessary: the index already held LF for every file (the CRLF was only ever in the checkout), so `git add --renormalize` staged nothing. The citation commit `bae4856` wrongly named `672599e` (the previous HEAD) as that non-existent commit — corrected here.

## 2026-09-06 — Published to GitHub

**Phase:** infrastructure (no code change)

**Done**

- Remote `origin` added and the full history pushed to <https://github.com/stubz100/crate-audio-analyzer> (public). The repository was empty beforehand, so this was a clean first push with no reconcile.

**Decided**

- **Pushed `master` as-is rather than renaming to `main`.** The repository's configured default was `main`, but every session in this journal records work on `master`, and GitHub promotes the first branch pushed to an empty repository — so the remote default is now `master`, matching local exactly. Renaming stays a one-command change if it is ever wanted.
- **Commit author identity left unchanged** after the public-exposure tradeoff was put to the user: the personal address in all 10 commits stays visible in public history. Rewriting to a GitHub noreply address was the alternative, rejected because it changes every hash and would invalidate the commit hashes this journal cites (the maintenance contract above depends on them).

**Verified**

- `git ls-remote --heads origin` → `refs/heads/master` at `cc05635`, identical to local `HEAD`; working tree clean.
- 22 tracked files published — source, tests, spec/journal, packaging. No audio and no index: `*.db` and `.crate_cache/` are gitignored, confirmed against the file list before pushing.
- Repository metadata after the push: public, default branch `master`.

**Next** — unchanged: close out Phase 2 (real-DB run + loop-pack review), then Phase 3.

## 2026-09-06 — Phase 2 closed out; Phase 3 built (transient segmentation)

**Phase:** 2 ✅ + 3 ✅ · `5e055b2`

**Done — Phase 2 close-out** (the run the previous entry listed as remaining)

- Scanned two roots into the real index (`.crate_cache/crate.db`, migrated v1 → v3 in place, 322 rows preserved): the ModeAudio foley pack (322) and `AUDIOMODERN_SHIFT2_AM036` (621), then analysed all **943** files — 0 failures, 247.8 s (0.26 s/file).
- Facet B against folder-name ground truth in the drum pack:

  | folder | n | loop | multi-hit | one-shot |
  |---|---|---|---|---|
  | Main Loops | 49 | 49 | 0 | 0 |
  | Loop Elements | 295 | 291 | 4 | 0 |
  | Foley Only Loops | 22 | 22 | 0 | 0 |
  | Percussive One Shot Samples | 205 | 0 | 25 | 180 |
  | Percussive Foley One Shots | 50 | 0 | 17 | 33 |
  | ModeAudio (all foley one-shots) | 322 | 0 | 64 | 258 |

  **98.9% loop recall (362/366), zero false loops in 577 one-shot files.** Tempo policy held exactly: 362 loops all carry a tempo, no non-loop does. Caveat: 200 of the 621 drum-pack files were in the 500-file tuning set, so this is only partly held out; ModeAudio's 322 and 421 of the pack's files were unseen.
- Neither pack carries ACID or `smpl` chunks, so every loop here was found acoustically or by the filename-BPM tier — `key` stayed NULL across all 943, as designed.

**Done — Phase 3** (spec §6, nodes `S` + `T`)

- `db.py` **schema v4**: `segments`, `segment_analysis`, `segment_embedding` (Phase 4), `segment_classification` (Phase 4), plus `samples.segments_detected_at`. `ON DELETE CASCADE` throughout, `UNIQUE (sample_id, start_ms, end_ms, detection_method)`, `CHECK (end_ms > start_ms)`.
- `segmentation.py`: node `S` gate (delegates to Facet B's own one-shot test, so gate and taxonomy cannot drift), both §6.2 profiles, all four controls, the cap with its warning, manual create/edit/delete, and node `C2`'s descriptor half.
- `analysis.py` refactor: descriptor work extracted into `describe_buffer()` / `CoreDescriptors`, so `analysis` and `segment_analysis` are filled by one function and cannot drift. `analyze_file` is now decode + metadata + loop decision on top of it. All 60 Phase 2 tests passed unchanged across the refactor.
- `crate-segment` CLI exposing every §9.6 setting.

**Decided**

- **Backtracking is bounded to ~46 ms and applies to the tight profile only.** Unbounded, it reached **128 ms** back — far enough to pull the previous hit's tail into the segment. The loose profile's windowed-RMS envelope already rises before the attack, so backtracking it as well only dragged starts earlier.
- **`smpl`-style short windows are zero-padded for transform descriptors** (HPSS included), so a 10 ms manual segment cannot blow up MFCC/contrast. Amplitude descriptors still use the true buffer.
- **Per-segment descriptors ship in Phase 3, embeddings do not.** `segment_analysis` is a Phase 3 table (spec §12 "segments+segment_* tables") and filling it needs only Phase 2 machinery; `segment_embedding` and `segment_classification` stay empty for Phase 4, which owns CLAP and Facet A.
- **Editing any segment makes it manual and protected** (§6.3 covers "create or adjust"), and drops its cached render since the bounds it was rendered from no longer hold.
- **The 5-segment cap default is left as spec'd.** It binds on 285 of 362 loops, but that is the material, not the detector: loops average 24 candidate transients and reach 128 (16th notes across 32 beats). §6.6 is explicit that this is findability, not slicing — the 5 strongest hits per loop is the intent, and the parent is flagged so the UI can say so.

**Verified**

- `uv run pytest tests -q` → **85 passed** (60 Phase 2 + 25 new), ~8 s.
- Real run: `crate-segment` over all **472** candidates → **1913 segments**, 0 failures, 191.5 s (0.39 s/sample). Every segment has descriptors; `tempo_bpm` NULL on all 1913 (a segment is a one-shot by construction).
- Integrity: 0 segments out of parent bounds, 0 inverted, 0 strengths outside 0..1, 0 violations of the 50 ms / 2000 ms length rules (min 69 ms, median 348 ms, max exactly 2000).
- Musical sanity: for the 354 loops with a derived tempo, mean 16th-note grid fit **0.86**, and **78%** score ≥ 0.8. (The metric is unreliable below ~4 cuts, which is what the handful of 0.00 scores are.)
- Node `S` verified to skip one-shots, staleness verified to re-detect after a content change, and a second `crate-segment` run over the same index does nothing (0 samples, 0.0 s).

**Next**

1. Phase 4 — Embeddings & Classification: CLAP for samples and segments (`ml` extra: torch, transformers, laion-clap), node `E` for Facet A, filling `segment_embedding`/`segment_classification`. This is the first phase with a real model download and the ~25–40 h full-library cost §3 warns about, so time a subset before committing to a library-wide run.
2. Revisit `--max-segments` and `--sensitivity` once segments are auditionable (Phase 4.5) — that is the first point where the tuning pass §13 risk #1 calls for can be judged by ear rather than by number.

## 2026-09-06 — Code review of the application (Phases 0–3)

**Phase:** review only · **no code changed, nothing committed** (fixes and this entry go in together, as with the Phase 1 review)

**Done**

- Whole-application review at high effort over `src/crate` and `tests` at `f9c095e`. A multi-agent review run was cut off by a session limit with one angle (simplification) reporting; the rest was done by hand, every finding re-checked against the code, and the important ones reproduced with probes rather than argued.

**Fix before user-owned data accumulates (Phase 4.5 / 11) — all reproduced**

| Where | Finding |
|---|---|
| `scanner.py:336` | **A rename or folder move inside the library destroys user-owned data.** The scanner keys on `filepath`, so a moved file is a vanished row plus an added row; the DELETE cascades `classification` (including `is_user_confirmed = 1`), manual `segments`, `analysis`, and from Phase 4 every embedding — 25–40 h of compute per full pass, thrown away by dragging a folder. Probe: scan, confirm a classification, add a manual segment, move the file into a subfolder, rescan → `added=1 removed=1`, confirmed classifications 1→0, manual segments 1→0. Fix: before deleting `vanished`, pair them with `inserts` by `file_size`, hash only those pairs, and on a match UPDATE the existing row's path/folder/name so the id (and every FK) survives. Cheap now; unfixable in retrospect. |
| `segmentation.py:486` | **Stale auto segments survive a type flip.** The worklist's node-S SQL excludes one-shots, so a sample re-typed to one-shot after a content change keeps its old auto segments and stays flagged stale forever. Probe: 8-hit loop → 5 segments; file replaced by a one-shot → typed one-shot, `segment_pending` processes 0 samples, 5 auto segments remain. Fix: worklist = new-or-stale of any type; a one-shot clears its auto segments and stamps `segments_detected_at`. |
| `segmentation.py:437` | **Manual segments are never re-described after the parent's content changes, and their bounds are not re-validated.** Probe: manual 3.0–3.9 s segment, file replaced by a 3.2 s one → descriptors untouched, segment now indexes 700 ms past the end. §6.3 protects bounds from automatic overwrite; descriptors are derived data and should refresh, and an out-of-range manual segment should be flagged, not silently kept. |

**Robustness — cheap now**

| Where | Finding |
|---|---|
| `analysis.py:791`, `segmentation.py:503` | Only decode failures are non-fatal; any other exception in a per-sample call (librosa on an exotic file, or `MemoryError` — HPSS holds several float32 STFT copies, a 10-minute field recording is ~1.3 GB peak) aborts the whole multi-hour run. Not reproduced: five pathological inputs (NaN/inf float WAVs, 1-sample, all-zero, DC) were all caught at decode, and the indexed packs top out at 37.5 s. The full library will not. A `try/except Exception` per sample (count, log, continue) is four lines and belongs before the first full-library run. |
| `db.py:257` | No `journal_mode=WAL` / `busy_timeout`. From Phase 4.5 a GUI reads while a CLI run writes for hours; with the default rollback journal readers see "database is locked" during every commit. Two PRAGMAs in `open_db`, before any UI code assumes concurrency. |
| `scanner.py:177` | Directory junctions are followed (verified with `mklink /J`: `is_dir(follow_symlinks=False)` is True, `is_symlink()` False) with no cycle guard — a junction pointing at an ancestor recurses forever. Rare, but a silent hang on a 110k-file walk. `DirEntry.is_junction()` (3.12) or a visited-inode set. |
| `segmentation.py:519` | `segments_created` counts candidates, not inserted rows; `INSERT OR IGNORE` can drop one (duplicate rounded bounds, or a window that rounds to 0 ms fails the CHECK) and the summary still counts it. Use `cursor.rowcount`. The per-sample `manual_kept` COUNT query (line 504) is ~110k queries on a full run reporting something the run never touches — drop or compute once. |

**Simplification / reuse (the surviving review angle, verified)**

- `analysis.py:79/493` + `segmentation.py:375`: three hand-maintained copies of the descriptor field list plus two CREATE TABLEs. A field added to `CoreDescriptors` but not to `_SHARED_DESCRIPTOR_FIELDS` is computed and silently stored NULL, and `test_descriptors_cover_every_analysis_column` cannot catch it (it checks `Descriptors` against the table, not the copy). Make `Descriptors` extend `CoreDescriptors` and build it with `Descriptors(**asdict(core))`.
- `cli.py:34–248`: three entry points re-declare `--db`, `-v`, logging, open/close, print, and have already drifted (scan defaults to WARNING, the others INFO; three `-v` help strings; line 189 is an f-string with no placeholder). Shared parent parser + one `_run()` helper.
- `segmentation.py:193`: `is_segmentation_candidate` (the Python node S) has no production caller — `segment_pending` gates in SQL. The SQL gate *is* covered by `test_segment_pending_skips_one_shots…`, so this is duplication rather than a coverage hole; keep one.
- `segmentation.py:540–604`: `create_manual_segment`/`update_segment` share an identical load→describe→commit tail; `_parent_row` selects three columns nobody reads.
- `wavmeta.py:129`: unreachable `except struct.error` (every unpack is length-guarded) and split padding logic. Also line 77 rejects a chunk with `tempo == 0.0` outright, discarding valid `beats`/`root_note` — tempo is derived from beats anyway.
- Lint: `segmentation.py` imports `Path` and `ANALYSIS_SR` unused; `test_segmentation.py` carries a leftover `(0.01, 0.5100000000000001) not in bounds` assertion.

**Observations (not defects)**

- The filename-BPM tier types 15 of the drum pack's 362 loops with a single dominant onset (sustained "Loop Elements" — correct). The same rule would type a pack-tempo-labelled one-shot (`…_128bpm_C#_1.wav` in the vocal packs) as a loop whenever its length is a whole number of beats ±0.1, roughly a 20% chance per such file. Watch when the vocal packs are indexed.
- Analyze and segment each decode and HPSS-split every candidate file (0.26 + 0.39 s/file). Spec §7 runs C→S→T on one buffer; a single-pass option would save about a third of the full-library cost.
- Path casing is not an issue: `Path.resolve()` normalises drive letter and folder case on this machine (probed).
- CLAUDE.md constraints all hold: nothing runs automatically, segments never appear in `samples`, RX2 is skip-and-log, dependencies arrive by phase, staleness is a flag.

**Verified** — 85 tests pass at `f9c095e`; every "reproduced" line above is a probe run this session against synthetic fixtures in a temp dir, not against the real index.

**Next**

1. Fix the three data-integrity items (rename reconciliation, type-flip clearing, manual-segment refresh/flag) with regression tests, then the per-sample exception guard and the two PRAGMAs — one commit, this entry with it.
2. The simplification items in a second commit.
3. Then Phase 4.

## 2026-09-06 — Review fixes 1/2: data integrity + robustness

**Phase:** post-review fixes (Phases 1–3) · `8558a7a`

**Done**

- **Moves/renames survive** (`scanner._reconcile_moves`): vanished rows are paired with added files by content before anything is deleted — tier 1 by stored hash, tier 2 (never-hashed rows, the common case) by identical filename + size + duration + rate + channels with exactly one candidate on each side, i.e. a folder move. The row is UPDATEd in place, so its id and every FK'd row (confirmed classification, manual segments, analysis, later embeddings) survive and nothing is re-analysed. Ambiguous twins fall back to delete + add. Summary now reports `moved`.
- **Type flip clears stale auto segments**: `segment_pending` visits every new-or-stale sample regardless of type; a one-shot has its auto segments cleared and is stamped (`one_shots_skipped`). Node `S` now reads `classification.structural_type` — the DB's value, so a manual correction to one-shot (§11) is honoured.
- **Manual segments refreshed after a content change**: descriptors redone, cached render dropped, and `needs_review` (schema **v5**) set when the segment now ends past the file. Bounds never touched (§6.3). Reported as `manual segments needing review`.
- **Per-sample exception isolation** in `analyze_pending` and `segment_pending`: anything past decode is rolled back, logged at WARNING, counted, skipped.
- `open_db`: `journal_mode=WAL` + `busy_timeout=5000`; `.gitignore` covers the WAL sidecars.
- Junction cycle guard in `_walk_files` (visited real paths); junctions are still followed.
- `segments_created` counts rows written; `manual_kept` is one query per run.

**Verified**

- `uv run pytest tests -q` → **97 passed** (85 + 12 regression: folder move keeps id/dependents, rename by hash, ambiguous twins not guessed, junction cycle terminates, WAL + timeout, v2→v5 migration, type flip, manual refresh/flag, in-range manual unflagged, row-count summary, exploding file in each driver).
- Real index: `crate-scan` on the ModeAudio root → `unchanged 322 | moved 0`, schema v4→v5 in place, `journal_mode` = wal.

## 2026-09-06 — Review fixes 2/2: simplification

**Phase:** post-review fixes (Phases 1–3) · `2d59ed0`

**Done**

- `Descriptors` now *extends* `CoreDescriptors` (built with `Descriptors(**asdict(core))`); the loop-decision working values moved to a `LoopEvidence` return; the two hand-written field lists are gone and the segment list is derived from the class. A new lockstep test covers `segment_analysis` the way the Phase 2 one covers `analysis`.
- `cli.py`: shared `_parser` (`--db`, `-v`) and `_run` (logging, open/close, summary); all three commands log at INFO, one `-v` help text, the placeholder-less f-string is gone. New `tests/test_cli.py` runs scan → analyze → segment end to end through the entry points and checks that a bad setting is an argparse error, not a traceback.
- `is_segmentation_candidate` deleted: node `S` is `classification.structural_type`, read from the index, so manual corrections are honoured — a recomputation could not be. Documented in place.
- Manual-segment create/update share `_describe_segment`; the unused-column `_parent_row` is gone; `update_segment` also clears `needs_review`.
- `wavmeta`: unreachable `struct.error` handler removed, one word-alignment seek, and a `tempo == 0.0` ACID chunk no longer discards its valid beat count and root note (test added).
- Unused imports and the leftover assertion removed.

**Verified** — `uv run pytest tests -q` → **100 passed**; `crate-analyze --limit 5 --reanalyze` on the real index writes complete rows (0 NULL descriptor vectors across `analysis` and `segment_analysis`).

## 2026-09-06 — Phase 4: CLAP embeddings, zero-shot tags, Facet A

**Phase:** 4 ✅ · `1e1b585`

**Done**

- `embedding.py` — node **D** (one CLAP vector per sample, unconditional), **C2** (one per segment, gated by *Embed segments* and *Min length for segment embedding* = 200 ms), **X** (top-5 zero-shot chips from a 32-tag vocabulary, cosine scores), **E** (Facet A from four 12-prompt sets, best-of scoring, softmax confidence, flagged below 0.5 with the best guess kept as a `clap-class` tag; Rhythmic-vs-Melodic ties broken by the HPSS harmonic ratio; segments inherit the parent's class with `structural_type = 'one-shot'`). Schema **v6**: `embedding`, `text_tags`. `crate-embed` CLI with `--reclassify` (tags + Facet A from stored vectors, no audio). Manual corrections protected.
- Model access behind a two-method `Encoder` protocol: a deterministic fake drives 16 tests; the real `ClapEncoder` loads transformers' `ClapModel` (`laion/clap-htsat-unfused`) lazily, decodes at 48 kHz, crops to the **first** 10 s deterministically. transformers 5 returns an output object from `get_*_features`; `projected_features` takes `pooler_output` (measured (n, 512)) and still accepts 4.x tensors.
- `ml` extra is now `torch` + `transformers`; `laion-clap` dropped (same weights, one maintained dependency). Segmentation hooks: `auto` profile is tight for Facet A Rhythmic; a segment's vector is dropped when its bounds or its parent's content change. CLI silences the model stack's per-request INFO logging.

**Decided**

- **Prompt counts equalised, best-of scoring kept.** On 335 labeled files the mean and prompt-centroid ensembles collapsed Vocal to ~10%; best-of favours the class with more prompts, so every class got twelve. 60.3% → **68.4%** best-guess accuracy.
- The "Wood Guitar" folder I labeled Melodic is knocks on a guitar body (harmonic ratio ≤ 0.10, tagged percussion/glitch): excluding it, **72.7%**. Per class: Rhythmic 98%, Other 85%, Melodic 50%, Vocal 33% — short vocal chops and breaths read as percussion; §11's manual correction is the intended path, and flagged samples keep their guess visible.
- Threshold stays 0.5 (assigns 90% at 73% accuracy; 0.65 would assign 75% at 77%). `classification.source_model` remains Facet B's; Facet A's provenance is the `clap-class` tag.

**Verified**

- `uv run pytest tests -q` → **116 passed, 1 skipped** (the real-model smoke test; passes with `CRATE_REAL_CLAP=1`).
- Real index (`.crate_cache/crate.db`, v6): **943 samples + 1,429 segments** embedded (484 segments under 200 ms skipped, shortest embedded exactly 200 ms), 0 failed, **195.9 s = 0.21 s/sample with segments** while a second model run shared the CPU; isolated: 8 × 10 s clips in 0.41 s = **0.05 s/clip**; model load 3.1 s from `F:\HF_HOME`. `--reclassify` over all 943: 6.8 s. Facet A on two drum/foley packs: rhythmic 835 / other 45 / melodic 11 / vocal 7 / flagged 45.
- §3 cost model corrected in the spec: ~6.5 h for the full library with segments, not 25–40 h.

**Next** — Phase 4.5 "listen and grab": preview player + native drag-out on a plain sortable list, the first daily-drivable checkpoint. Then tune Facet A by ear against real corrections rather than folder names.

## 2026-09-06 — Quick review after Phase 4

**Phase:** 4 review + fixes · `cc55c68`

**Done** — two hypotheses from re-reading `embedding.py`, both probed before fixing:

- **Segments detected after their parent was embedded never got vectors** (confirmed: 5 of 5 left without, and 3 of 3 after `--resegment`, which makes new rows). The worklist only knew about parents. It now also visits any sample that has a long-enough segment without a vector and, in that case, embeds *only* the segments — the parent's vector, tags and Facet A are untouched and it is not sent to the model.
- **A manual segment lying entirely past the end of its file** (the `needs_review` case) would hand the model an empty clip and fail its whole parent. Windows are now judged on the audio that actually exists and skipped as short.
- Found by the new tests: the progress log divided by zero in the segments-only path (it counted parents; it now counts visits). The junction test unlinks its junction on the way out.

**Verified** — `uv run pytest tests -q` → **118 passed, 1 skipped**; `crate-embed` on the real index visits 0 samples (nothing orphaned).

## 2026-09-06 — Phase 4.5: "listen and grab"

**Phase:** 4.5 ✅ · `f715d03`

**Done**

- `render.py` — node `J`'s file half (§6.5): a segment becomes a WAV in `%LOCALAPPDATA%\Crate\cache\segments` the first time it is previewed or dragged — parent sliced at its native rate/channels/PCM subtype, 2 ms fade-in, 20 ms fade-out, path remembered on the row; a segment past the end of its file is refused (`needs_review`), one that overruns is clamped.
- `catalog.py` — the list's read side, no Qt: one row per sample (never per segment, §6.4) with type, class, BPM, key, top-3 chips, hit count and a flag count; per-sample segments; status counts.
- `listmodel.py` — `SampleTableModel` / `SegmentTableModel`; `mimeData` sets file URLs (→ `CF_HDROP` on Windows), one per row, a segment rendered first; numeric sort role.
- `main.py` — the window: library path (Phase 0), filter across all columns, sortable sample table, the selected sample's segments underneath, Qt Multimedia preview (play/stop, Space, double-click, auto-play on select persisted in QSettings), drag-out from either table, status bar. `crate --db PATH`. Nothing computes here (§9.6).

**Decided**

- Qt Multimedia over `sounddevice`: no new dependency, plays the same file the drag hands out, and a segment preview therefore goes through the same render as its drag (§6.5 "first time you preview *or* drag").
- Segments are shown only in the selected sample's drill-down, never in the main list (§6.4); Phase 7 adds the nested sub-hit rows on match.
- Auto-play on select defaults on — this is an audition tool; the checkbox persists.

**Verified**

- `uv run pytest tests -q` → **130 passed, 1 skipped** (offscreen Qt: model rows/URLs, numeric sort + cross-column filter, segment drag renders first, window loads and drills down; render: native rate/channels/subtype, fades, cache reuse, `force`, edit invalidation, clamping, refusal).
- Real index offscreen: window up with **943 rows in 0.81 s**, preview device present (Focusrite), a loop selected → 5 segments, segment render + select **77 ms**, filter "kick" → 223 rows.
- Not verifiable here: an actual drop into Bitwig. The mime data carries the right file URLs; the first real drag is the user's check.

**Next**

- Use it: `uv run crate`, drag a hit into Bitwig. Then Phase 7 (list/search/filter proper, nested sub-hit rows, Attributes tab) completes the first daily-drivable milestone (spec §12); Phase 5's Qwen2-Audio spike can wait behind that.

## 2026-09-06 — Phase 8 pulled forward: the Recompute tab

**Phase:** 8, library-scope half · `7bfd001`

**Done**

- **Why now.** The window shipped in 4.5 only read an index the CLI had built, so the first real try ("the recompute tab is missing, I couldn't create a list") stalled at step one. The on-demand half of §9.6 — the part that does not need an anchor, a ranking or a map — came forward to right after 4.5.
- `jobs.py` (no Qt) — `RecomputeSettings` (scope, mode, one-shot cap, segmentation + embedding settings) and `recompute_attributes`: analysis → segmentation → embedding over the folder-scope list. An empty scope is refused (nothing is implicit, §9.6); a stop ends the current stage after its file and the later stages do not start; a missing `ml` extra skips embedding with a note instead of failing the run.
- Pipeline plumbing: `scope` and `should_stop` on `analyze_pending` / `segment_pending` / `embed_pending`; `db.scope_clause` builds the `LIKE` filter with `!` as the escape so an `_` in a path is not a wildcard; `scan_library` takes `should_stop` and a stopped scan writes nothing (removals need the whole walk); every summary has `stopped`; each stage logs its worklist size first. `SegmentationSettings` refuses min ≥ max in the same unit.
- `recompute.py` — the tab: library root + Browse (moved out of the header), **Rescan library**, the **folder-scope list** (persisted; Add folder / Add root / Remove), **New/changed only** vs **Force full re-index**, the §9.6 settings table (Qwen shown disabled, Phase 5), **Recompute attributes** + **Stop**, and a log fed by the `crate` logger through a queued signal. `JobThread` runs the job on a connection of its own; the list reloads when the job ends. Waiting for their phases: ranking (7), map layout (6), anchored-only scope (9).
- `main.py` — a right-hand tab widget (Attributes joins in Phase 7); `settings` and `encoder_factory` are injectable, so tests run against an INI file and a fake model.

**Decided**

- One code path: the tab calls `recompute_attributes`, and the Krotos verification below ran that same function headless — not a re-implementation of the buttons.
- Stop means stop: the stage keeps what it committed and nothing later starts; "skip to the next stage" would need its own button and nobody has asked for it.
- The earlier GUI test had written `preview/autoplay = false` into the user's registry (`QSettings("Crate", "Crate")`). That value was removed and tests now get INI-backed settings; the default (auto-play on) is back.
- Layout, from offscreen screenshots: the action row sits above its settings so Recompute/Stop are visible at 800 px height; the scope list is capped at 110 px; long form rows wrap.

**Verified**

- `uv run pytest tests -q` → **139 passed, 1 skipped**. New: the scope gate (incl. the `_` escaping), changed-only vs full, stop, empty scope, missing model stack, stopped scan; GUI: an empty index built to analysed + embedded from the window on the worker thread, settings round trip and validation.
- **Krotos Starter Library / Audio Files** (3,956 WAV: foley, drones, ambiences; median 1.25 s, longest 16.6 min; `.reapeaks` ×4 skip-and-logged): scan 45 s; analysis 815 s (0.21 s/file, the long ambiences dominate); segmentation 187 s; embedding 506 s; **25.1 min all-in, 0 failed**. Result: 2,802 multi-hit / 1,139 one-shot / 15 loops (68–239 BPM); Facet A rhythmic 2,058 / other 858 / vocal 209 / melodic 82 / flagged 749; 8,962 segments (760 samples at the cap of 5), 5,512 embedded, 3,450 skipped short. The 16-minute "…Loop" ambiences typed multi-hit — right: seamless, not rhythmic.
- Window on that index: up in **0.37 s**, reload 0.33 s.
- The user's default index (`%LOCALAPPDATA%\Crate\crate.db`) held the same 3,956 files as skeleton rows only (scanned 19:11 UTC, nothing derived); backed up to the session scratchpad and replaced with the computed index, so `uv run crate` lists Krotos straight away.
- Spec §3: the foley-mix cost added (0.38 s/file all-in, ≈ 12 h extrapolated to the full library — double the drum-pack figure).

**Next**

- Press the buttons for real: Rescan (the registry root is the Krotos parent folder, so the 3,956 rows come back *unchanged* with their `folder` refreshed), then add a second pack to the scope and Recompute — "new/changed only" against a real re-scan is the one path the synthetic tests cover but real files have not. Then Phase 7.

## 2026-09-07 — Quick review after Phase 8

**Phase:** 8 review + fixes · `0c2d33f`

**Done** — a static pass (pyflakes) and one probe, closing the window while a job runs:

- **Close during a job** (the probe confirmed it: `sqlite3.ProgrammingError: Cannot operate on a closed database`, raised from `reload`). The window closed its connection and waited at most 15 s for the thread; the job's queued completion then reloaded a window whose connection was gone, and a job outliving the wait would have had its thread destroyed under it. The window now refuses to close while a job runs, asks it to stop, and closes itself when the job ends; `reload` is disconnected before the connection closes; the panel's last-resort wait is unbounded.
- **A failed model load hid the stages that ran.** An exception from the embedding stage (no checkpoint in the cache and no network — the first-run failure mode) escaped `recompute_attributes`, so the log showed only the failure and not the analysis and segmentation that had already committed. The stage's failure is now a note on the report, with the earlier summaries intact.
- `db.scope_clause` was annotated with an unimported `Iterable` (harmless under `from __future__ import annotations`, wrong all the same); a scope folder outside the library root is now called out in the log when it is added; an unused variable in a scanner test.

**Verified** — `uv run pytest tests -q` → **141 passed, 1 skipped** (new: the deferred close against a job that ignores the stop for a while; a failed model load keeps the earlier stages); pyflakes clean; the close probe re-run: `close()` refused, the window closed itself after the job, no traceback.

**Next** — Phase 7: list, search and filter proper (nested sub-hit rows, the Attributes tab, free-text CLAP search, tag chips), which closes the first daily-drivable milestone.

## 2026-09-07 — Phase 7: list, search and filter — the first daily-drivable milestone

**Phase:** 7 ✅, plus §9.6's Recompute ranking and a minimal anchor · `4756a35`

**Done**

- `similarity.py` (no Qt) — the §5.1 blend. A `FeatureTable` holds every analysed sample *and* segment as robustly standardised per-axis descriptors plus its CLAP vector; `distances(anchor)` gives every item's per-axis distance once per anchor (scaled by the population's 95th percentile to 0..1, NaN where the item lacks the axis); `blend` is the weighted mean over the axes an item has; `rank` = 1 − blend; `search` = cosine against a CLAP text vector; `fold` turns item scores into per-sample scores with **sub-hits** — a segment that beats its own parent (§9.4).
- `catalog.py` — `Criteria` (class / type / length / tempo / per-axis anchor ranges), `load_tags`, `describe_item`.
- `listmodel.py` — `SampleTreeModel`: samples with at most one "↳ hit @ …" child row; Similarity and Match columns; dragging a sub-hit renders the segment. `ListProxy` applies the criteria to samples and lets a sub-hit follow its parent.
- `attributes.py` — the §9.5 tab in the spec's order: weight bars (persisted), the CLAP search box with the selected sample's tag chips under it (click = search), class / type / length / tempo filters, and per-axis distance-from-anchor ranges unlocked by the anchor.
- `main.py` — the list is a tree; **⚓ Anchor** / ✕ in the transport row (persisted, restored on start, re-derived after a Recompute); the query is embedded on a worker thread and the newest query wins while one is in flight; score columns appear only when present and sit next to the file name; column measuring samples 200 rows (0.38 s → 0.07 s on 3,956 rows).
- `recompute.py` — **Recompute ranking** (whole index, or visible rows only), enabled by the anchor.

**Decided**

- Segments are items for ranking and search and fold into their parents; the list never grows a segment row of its own (§6.4). The parent inherits its best hit's score, so it sorts by what is actually inside it.
- Per-axis distances are scaled by the 95th percentile of the population so the weights and the % ranges mean the same on every axis; an item without an axis (unpitched → no pitch) drops out of the blend rather than being penalised.
- A tempo range excludes samples without a tempo; a narrowed anchor range excludes samples without that axis — narrowing is asking for samples that have it.
- Sub-hits follow the search while one is active, else the ranking; two scorings can disagree on which segment wins and only one child row is shown.
- Text embedding runs off the GUI thread: the cold CLAP load measured 20 s inside the window.
- The anchor is Phase 9's, but the Similarity column and the ranges cannot exist without one, so a minimal button ships now; the header proper (waveform, markers) stays Phase 9.
- Qt 6.10 deprecated `invalidateFilter()` and `invalidateRowsFilter()` in favour of begin/endFilterChange; the proxy uses those when present.

**Verified**

- `uv run pytest tests -q` → **153 passed, 1 skipped**; pyflakes clean; no Qt deprecation warnings. New: the feature table over samples + segments; anchor distances (zero at the anchor, same pitch closer than two octaves up, unpitched → NaN); blend NaN rules; ranking order (anchor first, its twin second); scope; text search folding a winning segment into a hit; criteria rules. GUI: search → Match column, sub-hit row, preview and drag of the sub-hit, a query replaced while in flight; anchor → ranges + ranking + persistence across a restart, visible-only scope; filters; weights persisting.
- Krotos index (3,956 samples + 8,962 segments): feature table of **12,918 items in 1.0 s**; distances 5 ms; ranking 5 ms headless, 1.0 s in the window (model reset, sort, expand). Anchored on *Surface Wooden Plank On Gravel-001* with equal weights, the next seven rows are its siblings 002–019. Text search: "sword clash" → five sword hits at 60–62; "kick drum" → body hits, punches, bass drops; "footsteps on gravel" → two ice-crunch *hits inside* spell sounds first, then the gravel footsteps; "wind howling" weak (car interiors) — CLAP sees the first 10 s only and ambiences are where its zero-shot is weakest. Pitch axis covers 0 items here (foley: nothing passes the gate) — expected.
- Window on that index: up in 1.4 s; the first search 8–20 s (the model load, now off the GUI thread), later searches instant.

**Next**

- **Milestone reached (spec §12): Phases 1–4 + 4.5 + 7.** Use it for a while — searches, anchors, ranges, drags into Bitwig — before Phase 9 (the header: waveform, marker editing, the anchor's proper home) or Phase 6 (the map).
- Phase 12 note: the feature table at full scale (110k samples + ~250k segments) holds ~700 MB of float32 vectors; float16, or embedded-only rows, before the whole library.

## 2026-09-07 — Quick review after Phase 7

**Phase:** 7 review + fixes · `7e3e6fb`

**Done** — a re-read of `similarity.py`, `listmodel.py`, `attributes.py` and the window, and two probes against a small index built in-process; both hypotheses held:

- **Selecting a sub-hit left the drill-down and the chips on the previously selected sample.** The hit row only set the preview target; the segments table and the tag chips still belonged to whatever was selected before it (probe: 0 segments and the other sample's 5 chips). A sub-hit is a segment *of* its parent, so the drill-down and the chips are now the parent's for both kinds of row.
- **Ranking with every weight at zero said "ranked 0 samples" and nothing else.** The blend is NaN everywhere when no axis carries weight; the window now says that ranking needs a weight above zero and leaves the Similarity column hidden, instead of an empty result that reads as a bug.
- Looked at and left alone, for the record: the feature table is loaded on the GUI thread (1 s at 12,918 items; at library scale — ~350k items, ~700 MB of vectors — this and the anchor restore at start-up become Phase 12 work, already noted); the search encoder and the Recompute tab's encoder are two model instances if both are used in one session (memory, not correctness); two active scorings can disagree on a parent's best segment and only one child row is shown (documented in the model).

**Verified** — `uv run pytest tests -q` → **153 passed, 1 skipped**, pyflakes clean; the two probes now show the parent's segments and chips after selecting a sub-hit, and the all-zero-weights message. The GUI tests cover both.

**Next** — Use the milestone build. Then Phase 9 (header: waveform, marker editing, the anchor's proper home) or Phase 6 (map).

## 2026-09-07 — Phase 6: the map view

**Phase:** 6 ✅ · `72ad400`

**Done**

- `layout.py` (no Qt) — node G. `fit_layout` projects the §5.1 feature space under the weight bars at fit time (`FeatureTable.weighted_matrix`: each axis's standardised columns × √(weight / dim), the CLAP vector as the conceptual axis, missing values at the median) with UMAP over the samples in the folder-scope list, writes the `map_layout` row + one `map_position` per sample, marks it current, and pickles the fitted reducer next to the segment cache (`…\Crate\cache\layouts\layout_<id>.pkl`). `place_anchor` is the anchored-only path: `transform` one sample into the current layout (a segment anchor places its parent). `load_current_layout` feeds the view. `PcaReducer` stands in without the `map` extra (the summary says so) and is the test double. A stop is honoured only before the fit — UMAP cannot be interrupted — and writes nothing.
- Schema **v7**: §8's `map_layout` / `map_position`, plus `reducer` and `model_path` on the layout row.
- `mapview.py` — a painted widget: one point per sample (§6.4), colour by class, circle / square / diamond by type, legend; the list's filter mirrored; click selects in the list (which previews), double-click plays, wheel zooms about the cursor, drag pans, right-click fits, hover names the file; the halo is the last ranking's 20 nearest (§9.3, last-computed not live), the badge the current search's or ranking's segment hits, the anchor and the selection ringed. The caption names the layout, scope, reducer and time and says when the bars no longer match the weights it was fit under.
- `main.py` — List / Map switch above the quick filter (a stacked widget; the segments drill-down stays under both); `_load_map` on every reload; `_run_layout` puts the fit (or the placement) on the Recompute tab's worker through the panel's new `start_job`, so it shares the log, Stop and the reload. `recompute.py` — **Recompute map layout** with its two radios (anchored-only enabled by the anchor).
- `uv sync --extra map` installs umap-learn 0.5.12 + pynndescent; the extra stays optional per CLAUDE.md.

**Decided**

- The map is fit on the same weighted space the ranking blends, so weights + scope + fit define coordinates exactly as §8 says; the caption flags a mismatch with the bars rather than re-fitting anything on its own (§9.6).
- The layout's scope is the folder-scope list, like Recompute attributes: an empty scope is refused with the same hint.
- One point per sample, never per segment; badges carry the segment information (§6.4, §9.3).
- `MIN_SAMPLES = 3` for a fit; `n_neighbors` is clamped to n − 1 for small scopes.

**Verified**

- `uv run pytest tests -q` → **160 passed, 2 skipped** (the real-CLAP and real-UMAP opt-ins); pyflakes clean. New: fit places every sample in scope and becomes current (same-pitch tones closer than the click), a second fit replaces the current layout and keeps history, anchored placement adds / updates one row and lands where the fit put an in-layout sample, refusals (zero weights, too few samples, no layout, unknown segment) and the pre-fit stop, the weighted matrix's shape and NaN-freedom, PCA pickling; GUI: fit from the window, points, filter mirrored, map click → list → preview, badges from a search, halo + anchor mark from a ranking, anchored-only placement, the view switch, an offscreen paint.
- Krotos index, real UMAP: **3,956 samples placed in 20.4 s** (feature table 1 s, numba already warm; 555 features), model file 10 MB; anchored placement 2.1 s, landing 0.05 from the fitted position of the same sample. Window: up in 0.9 s with the layout, anchor + ranking 1.1 s, the map paints in 20 ms with 3,956 points, 20 halos and 1,050 badges.
- Offscreen screenshot checked: clusters by class, the sword cluster's halo around the anchor, badges on multi-hits.

**Next**

- Phase 5's Qwen2-Audio spike is the last phase below 7 not yet done; then Phase 9 (header: waveform, markers, the anchor's proper home) and the rest of Phase 8 (anchored-only Recompute *attributes*).

## 2026-09-07 — First real use: waveform panel, difference bars, the class taxonomy

**Phase:** feedback on the milestone build (touches 7, 6 and 9) · `5acfcd4`

The user opened the milestone build and raised three things: the bottom panel showed a segments table where a waveform with markers and the envelope belonged (that table belongs on the Attributes tab); the "comparing factors" bars meant nothing to them — the bars should show how the selected sample differs from the anchor; and the five-way "Class" filter and map colours made no sense on their library (a machine-gun burst was *Vocal*), and they thought these were the presets we had agreed to drop.

**Done**

- `waveform.py` — the bottom panel is now the waveform strip §9.2 describes: the selected sample's waveform (per-column min/max read in blocks), every segment as begin/end markers with its strength (manual green, `needs_review` flagged), the measured attack and decay drawn as the envelope — the Amplitude axis's own numbers; a recording has no ADSR beyond attack and decay — and the preview's playhead (a 50 ms timer; a segment preview is offset by its start). Click inside a segment → it is selected and previewed; click elsewhere → seek. Files over 30 s are read on a thread with a "reading…" placeholder and a generation counter so a newer selection wins. Marker editing stays Phase 9.
- `attributes.py` — reshaped top to bottom: **Selected vs anchor — difference per axis** (read-only bars in % of the library's spread, each axis with a tooltip saying what it measures; "n/a" where the item lacks the axis), Search + chips, **Segments of the selected sample** (the table, hosted here), Filters (Type first; the class row is labelled "CLAP class guess" with the four §4 classes and their measured reliability in the tooltip), and last **Weights for the next ranking and map layout** with a tooltip saying they change nothing until Recompute is pressed.
- The list column "Class" → "CLAP guess". The map colours by **folder** by default — the first folder level at which the samples differ (`catalog.folder_groups`), since a library rescanned from a parent root puts everything under one top-level folder — with "Colour by type" and "Colour by CLAP class" as a switch beside the Map button.
- Chips no longer overlap when the selection changes (old buttons hidden and re-parented before `deleteLater`).

**Decided**

- The four classes are not presets: they are spec §4's Facet A, assigned by CLAP's zero-shot guess in Phase 4 (68–73 % on drum packs; clearly worse on foley). The presets dropped earlier were *weight* presets (§9.5). Recorded in §4 as an open question for the user: keep Facet A as a demoted hint with §11's manual correction on top, or replace the taxonomy (folder-derived or user-defined categories). Until then it drives nothing — not the layout, not the ranking.
- The waveform lives in the bottom panel, not the header (§9.1's sketch): the user's steer, and it gives the waveform the full width. Anchor and Drag stay in the transport row.
- The difference readout is per item: selecting a segment (a hit row or a drill-down row) compares that segment's own features with the anchor.

**Verified**

- `uv run pytest tests -q` → **167 passed, 2 skipped**; pyflakes clean. New: the block-wise envelope (shape, duration, peak column, channel mixing, empty file), tick steps, click → segment / seek mapping, header text, threaded read of a long file; folder groups; GUI: waveform loaded on selection, the table hosted in the tab, the selected segment mirrored with its offset, difference bars (0 % against the anchor itself, pitch n/a for clicks, > 0 for another sample), colour modes.
- Krotos index: a 0.6 s file's waveform is inline; the 16-minute "Train Travel" ambience took 4.7 s to read, which is why long files went on a thread. Anchor on *Surfaces Shoes On Gravel Boots-030* vs *-027*: amplitude 64 %, timbre 43 %, spectrum 13 %, conceptual 24 %, pitch n/a — the numbers the user asked to see.

**Next**

- The user's answer on Facet A (keep as a hint / replace). Then Phase 5 (Qwen2-Audio spike), Phase 9 (marker editing on the new waveform panel), the rest of Phase 8.

## 2026-09-07 — Second round of first-use feedback: panel width, map colouring, the look

**Phase:** feedback on the milestone build · `5707f53`

The user: the right panel overshot its edge and was trimmed; the map's folder groups ("Ambiences", "Animals") are the kind of static classification a library of hundreds of sounds cannot use unless it is dynamic; and the whole UI looked like basic HTML.

**Done**

- **Panel width.** Measured, not guessed: the Attributes panel's contents needed 840 px at minimum against a 480 px pane (a single row of five class checkboxes, the long labels, spin boxes sized for "100000.00"), the Recompute panel 726 px. Checkbox rows now wrap three per row, labels wrap, spin boxes are capped, titles shortened; the panels' inner layouts use `SetNoConstraint` so the contents squeeze to the pane and a horizontal scrollbar is the fallback rather than clipping; the tag chips wrap three per row; the right pane's default share is 560 px.
- **Map colouring.** Folder / type / class colouring removed, `catalog.folder_groups` with it. Points are coloured by the current **score**: the last ranking's similarity to the anchor, or the active search's match (stretched between the 5th and 95th percentiles), with a gradient key in the legend; one colour when there is neither. Clearing a search falls back to the ranking's colours; clearing the anchor to uniform unless a search is active.
- **Look.** `theme.py`: Fusion style, a dark palette and a style sheet (group boxes as cards with small-caps titles, rounded fields and buttons, an accent for selection and Play, amber for the anchor, slim scrollbars, styled headers, tabs, sliders and progress bars), applied once in `main()`; the map and waveform painters use the same colour constants (dark ground, accent waveform, amber segments, green manual segments, pink envelope, white playhead and badges). Tests run unthemed.

**Decided**

- Colour on the map is reserved for something the user just did — rank or search — not for any fixed grouping; the CLAP class stays out of the map for good, and the folder grouping is gone.
- One theme, dark, no switch: the tool sits next to a DAW; a light variant is a later nicety.

**Verified**

- `uv run pytest tests -q` → **166 passed, 2 skipped**; pyflakes clean.
- Themed offscreen screenshots on the Krotos index: list + waveform, the map coloured by a gravel-boots ranking (the similar cluster brightens around the anchor), the Recompute tab; the right pane now fits its 560 px share with the chips wrapped.

**Next**

- The user's answer on Facet A (keep as a hint / replace) is still open. Then Phase 5, Phase 9 (marker editing on the waveform), the rest of Phase 8.

## 2026-09-07 — Third round: CLAP's numbers instead of the class label

**Phase:** feedback on the milestone build · `6ef09fb`

The user asked how CLAP classifies and said they would rather see CLAP's output numbers than the class interpretation.

**How it works (for the record).** CLAP embeds the first 10 s of a sample (48 kHz) into a unit vector of 512 numbers; the same model embeds text into the same space. The chips are cosine similarities to 32 prompts of the form "the sound of {tag}", top five kept. The class was four sets of twelve prompts (`CLASS_PROMPTS`): each set scores as its best-matching prompt's cosine, the four scores are scaled by the model's logit scale (18.66) and softmaxed into probabilities summing to 1; the largest was the class, below 0.5 unclassified, with a harmonic-ratio tie-break between Rhythmic and Melodic. Only the winner's probability was stored.

**Done**

- `embedding._write_tags` stores **all four** probabilities (`text_tags`, source `clap-class`, one row per set); `crate-embed --reclassify` regenerates them from stored vectors without audio.
- `catalog.SampleRow.clap_scores`; `Criteria.clap_min` (minimum probability per set) replaces the class filter; the list's "CLAP guess" column became four sortable numeric columns — Rhythmic, Melodic, Vocal, Other — with a header tooltip saying what they are.
- Attributes tab: "CLAP scores of the selected sample" — four bars, the twelve prompts of each set in the tooltip, a caption explaining softmax vs the chips' raw cosines; the filter row is "CLAP score at least" with a box per set (0 = any).
- `classification.content_class` stays in the index (§11 will need it) but is not shown anywhere.

**Verified**

- `uv run pytest tests -q` → **166 passed, 2 skipped**; pyflakes clean (the embedding tests now expect four class rows per sample; the GUI test checks the four values sum to ~100, the column shows a number, and a 100 % minimum empties the list).
- The user's index: `reclassify` over 3,956 samples in 7.4 s → 15,824 class rows, four per sample; the columns and bars are populated on next launch.

**Next**

- The taxonomy question is now moot in the UI: the numbers are what is shown. Phase 5, Phase 9 (marker editing), the rest of Phase 8.

## 2026-09-07 — Fourth round: how CLAP sees a file, the vector itself, worker processes

**Phase:** feedback on the milestone build · `66e9e78`

The user asked what a model that takes 10 s makes of a 1-s one-shot and of a 16-minute ambience; asked to keep and see the 512-number vector; and noticed a folder recompute using 5 % of a 32-core CPU.

**Done**

- **Windows.** Measured on the feature extractor: a short clip is **repeat-padded** — a 1-s hit is tiled to fill 10 s, how CLAP was trained; the vector is dense, never "mostly zeros" — and a long clip used to contribute its first 10 s only (`crop_for_clap`, chosen for determinism over the extractor's random crop). Now `sample_windows`: a file is embedded as the **mean of its 10-s windows** — contiguous up to 24, beyond that 24 spread evenly across the file — re-normalised; a short tail is dropped; segments keep their own window vectors. The user's index: 1,907 files longer than 10 s re-embedded (465 s).
- **The vector.** It was already stored for every sample and segment (`embedding` / `segment_embedding`, float32, unit length). Now it is visible — `vectorstrip.py`: 512 colour stripes, amber positive, blue negative, scaled to the vector's own peak, the anchor's stripes underneath and their cosine — and exportable: `crate-embed --export FILE.npz` (ids, kinds, paths, bounds, vectors, row-aligned).
- **Worker processes.** `parallel.py`: a bounded `ProcessPoolExecutor` map — at most 2 × workers tasks in flight, one BLAS thread per worker, the stop flag polled between submissions, results in completion order. Analysis and segmentation fan their per-file computation out: `analyze_file` as it was; segmentation split into a pure `detect_file` (decode, detect, describe every window including the manual segments handed in) and `apply_segment_work` (the SQLite writes, in the calling process). `workers` on both functions, on `RecomputeSettings`, on the Recompute tab (*Worker processes*) and as `--workers` on `crate-analyze` / `crate-segment`. Embedding stays in-process on torch's threads.

**Decided**

- Mean of windows for the whole-file vector: the standard whole-clip embedding for a fixed-window model. The alternative — per-window vectors as searchable hits inside long files — needs a third kind of segment row and is the user's call.
- Default workers = min(8, cores ÷ 2): measured below, 16 was slower than 8 on short files, and the DAW keeps the rest of the machine.
- A process pool is within CLAUDE.md's "single-process" rule as it was meant — one codebase, one writer of the index, no native component; the wording now says so.
- A Windows lesson, recorded because it cost an hour: a spawned worker re-imports the parent's `__main__`; my first benchmark script had no `if __name__ == "__main__"` guard, re-ran itself in every child and the pool died "abruptly". The app's entry points are guarded. That run also copied the live index file while another process was writing it — a torn copy, "database disk image is malformed" — so benchmarks now take their copy through SQLite's backup API; the real index checked out (`integrity_check` ok).

**Verified**

- `uv run pytest tests -q` → **170 passed, 2 skipped**; pyflakes clean. New: `sample_windows` (1 / 3 / 24 windows, a 2 ms tail dropped) and a 25-s file reaching the model as three clips and coming back as one unit vector; two workers producing the same analysis and segmentation rows as one process; a stop with workers keeping what finished; the bounded map's results, errors and stop; the strip's 512 (32 in tests) dimensions.
- Footsteps folder, 600 short files, on a copy of the user's index (8,524 samples now — more packs scanned since):

  | workers | analysis | segmentation |
  |---|---|---|
  | 1 | 12.9 s | 11.4 s |
  | 8 | 4.2 s | 4.5 s |
  | 16 | 7.3 s | 5.4 s |

  Longer files gain more: the per-file hand-off is fixed, the work is not.

**Next**

- Phase 5, Phase 9, the rest of Phase 8; per-window search hits inside long files if wanted.

## 2026-09-08 — CLAP windows as searchable hits; where the 10k samples came from

**Phase:** feedback on the milestone build · `75fb01e`

The user first asked why the status bar counted over 10k samples when the folder just scanned holds about 1.6k, then picked up the per-window idea left open yesterday: "I like this idea, can you get back to this?"

**Done**

- **The count.** Not stale rows: the index holds the three roots scanned so far (iris2 4,568 · Krotos 3,956 · ___GUITAR_INSTR 1,616, all analysed and embedded). Rescan deliberately removes only under the root it walks (`scanner.py`, so a sub-folder scan can never wipe the rest), and the scope list limits *recompute*, not what the list, map and counts show. Three options put to the user, recommended first: the view follows the scope ("1,616 in scope of 10,140 indexed"); a confirmed "Remove folder from index" action; or leave it and put the root back to `D:\_soundPacks`. Their call is pending.
- **The third kind of segment** (spec §6.4, schema v8). `segments.detection_method` accepts `'window'`: one row per 10-s CLAP window of a file embedded through more than one, with its vector in `segment_embedding`, no strength, no descriptors, no classification. `embedding.py`: `window_spans` (the layout as sample offsets — `sample_windows` slices with it), `_store_windows` (delete-then-insert whenever a parent's windows are computed, nothing for a one-window file), `needs_window_rows` and a `missing_windows` worklist condition so an index from before these rows existed is backfilled — the parent's vector is the mean of the same windows and is left alone. Kept regardless of *Embed segments*; `--export` gains a `methods` array.
- **Searchable.** `similarity.py` loads segments with `LEFT JOIN`s, so a window is a conceptual-axis-only item; `Hit.window` says which kind won; `fold` is unchanged — a window becomes the parent's sub-hit when it beats the parent's mean. `catalog.hit_label` / `clock` name hits everywhere ("window @ 7:10.250 (10 s)", "hit @ 1.234 s (250 ms)"; positions past a minute read as m:ss). The list's *Type* cell says "window"; the transport caption, the anchor label and the vector strip follow; preview and drag render the window like any segment.
- **Not a segment to the user.** `load_segments` excludes windows and `load_windows` returns them; the list's *Hits* count and the status bar's segment count leave them out ("… · 6,036 CLAP windows · …"); `segment_classification` skips them. The waveform draws them as a thin strip along the bottom, the selected one as a band with edge lines.
- **Schema v8.** The allowed values are a CHECK constraint, which SQLite cannot alter, so `_migrate_v8` rebuilds `segments` (create the new shape from SCHEMA's own DDL, copy by column name, drop, rename, recreate the index) with foreign keys **off** for the duration — with them on, the DROP would cascade through every segment_analysis / segment_embedding / segment_classification / map_position row — and refuses to proceed if the pragma did not take; `foreign_key_check` must be clean before COMMIT. `segments_accept_windows` reads the constraint itself (the first version matched the word in a column comment).

**Decided**

- Windows are CLAP-only items. The four DSP axes were designed for one-shots and loops, not a slice of an ambience, and describing every window (pyin, MFCCs on 10 s) would cost several times the model pass; under a ranking a window scores on the conceptual axis alone and has no score when that weight is zero. Additive later if wanted.
- The embedding stage owns them, not segmentation: it is the only stage that knows the window layout, the vectors are computed for the parent anyway, and an existing index needs one backfill pass instead of a re-segment plus re-embed.
- The fold rule stays "beats the parent, strictly": for a two-window loop the parent's normalised mean usually wins by a hair when the halves are alike, and a window surfaces when one part is distinctly the better match — which is the behaviour wanted.

**Verified**

- `uv run pytest tests -q` → **175 passed, 2 skipped**; pyflakes clean. New: the v7→v8 rebuild on a hand-built old index (rows, flags, dependents, the index and the cascade survive; 'window' accepted, 'bogus' rejected; re-open a no-op); a 25-s file → three window rows (0–10, 10–20, 20–25 s) whose normalised mean is the parent's vector, a second run idle, the backfill after deleting them leaving `embedded_at` untouched, `--reembed` replacing rather than doubling, *Embed segments* off still keeping them, the export's `methods`; the feature table holding a window as a conceptual-only item, a text search landing on the last window as a `Hit(window=True)`, a ranking without conceptual weight leaving it unscored; the window's "↳ window @ 20.000 s (5 s)" row in the list, its caption, render, offset, the waveform strip and header, the segments table without it.
- The real index: the v8 rebuild on a backup copy in 0.14 s, every dependent row intact, `foreign_key_check` empty, `integrity_check` ok; then the backfill on the index itself — 2,028 files visited, **6,036 window rows** with vectors in 549 s (0.27 s/file), 0 failed, `integrity_check` ok, 168 → 194 MB. A real search, “a phone ringing”, over 10,140 samples: 5,383 hits inside longer samples, 846 of them on windows — the top one at 2:20 of a 2:55 Venice canals ambience, another at 7:23 of a 9:36 Los Angeles street recording: the minute-seven case, literally. Checked offscreen on that index: the “↳ window @ 2:20.000 (10 s)” row with Type “window”, the waveform’s “5 segments · 18 CLAP windows”, the strip along the bottom and the selected band, the transport caption, the 512-number strip of the window itself, the status bar’s “37347 segments · 6036 CLAP windows”.

**Next**

- The user's choice on the scope-vs-index question above. Phase 5, Phase 9, the rest of Phase 8.

## 2026-09-08 — One Library panel, one Recompute panel; the view follows the scope

**Phase:** 8 (Recompute tab, reshaped) · `b04ab9c`

The user's decision on the scope question: the library root and the scope list become one panel whose folders are the index's own; each folder has a Root tick and an In-scope tick; Add/Remove manage the database; the in-scope folders are what the list and map show; and the three Recompute buttons — whose difference was unclear — become one panel.

**Done**

- **`libraries` table** (schema v9, `library.py`, no Qt): path, `is_root` (at most one), `in_scope`, added/last-scanned stamps. `add_library`, `remove_library` (deletes the folder's samples — the cascades take analysis, segments, embeddings, map positions — and forgets the folder; files untouched), `set_root`, `set_in_scope`, `scope_paths` (None = no folders known → everything; () = none ticked → nothing), `outermost` (Rescan walks each file once), `derive_scan_roots` (a root is a sample's path minus its `folder/filename` tail) and `seed_libraries` (first launch: the index's scan roots plus the old settings' root and scope list; a folder in the old scope is in scope, everything when there was none, the old root is the root). `scan_library` registers every root it walks, so CLI scans show up in the panel.
- **The Library panel** (`recompute.py`): a tree of the folders with Root / In scope ticks, the file count and last-scan tooltip, dormant ones dimmed; *Rescan* (the folders in scope), *Add folder…* (registers, in scope, and scans — a scan job), *Remove folder* (asks, then a job). A tick in the tree updates the index and, for scope, the window; the tree is adjusted in place, never rebuilt from inside its own change signal.
- **The Recompute panel**: three steps as tick boxes with a one-line explanation each and their options on the row below — *Attributes* (new/changed only · everything again; the settings form beneath), *Map layout* (whole scope re-fit · anchor only), *Ranking* (whole scope · visible rows; disabled with a note until there is an anchor) — one *Run*, one *Stop*. `RunPlan` carries the ticked steps to the window, which runs them in order: `run_attributes` → `_run_layout` → `_rank`, each job's `job_ended(name, completed)` starting the next, a stop or failure dropping the rest.
- **The view follows the scope**: `load_samples(conn, scope)`, `index_summary(conn, scope)` ("1,616 samples in scope of 10,140 indexed"), the map's points, text search and ranking (`sample_ids` = the rows in scope), the map layout's scope, all from `scope_paths`. Dormant folders' rows stay, unseen, and come back on a tick.

**Decided**

- The folders live in the index, not in QSettings: they describe the index (which roots it was scanned under), so they travel with it; the old `library/root_path` and `recompute/scope` settings are read once as a seed and not written again.
- Rescan walks the folders in scope — not the root and not the dormant ones: "in scope" means active in every sense (view, Rescan, Recompute), "dormant" means untouched. The root's job is to be the home: where Add folder starts, and what "outside the library" is measured against.
- A `crate-scan --root` of a folder not yet listed adds it in scope; a re-scan of a known folder keeps its flags.
- Ranking runs on the GUI thread (a second or so on the feature table) and so needs no job; the plan runs it last, after the reload that follows the map job.

**Verified**

- `uv run pytest tests -q` → **180 passed, 2 skipped**; pyflakes clean. New (`test_library.py`, `test_gui.py`, `test_catalog.py`): the flags and the two meanings of an empty scope; scanning registers the root and a re-scan keeps the flags; removing deletes rows and cascades, disk untouched; `outermost`; the seed from a pre-v9 index with and without settings; the window built from nothing through Add folder → tick → Run → Remove folder → Rescan, with the status bar's "N samples in scope of M indexed" at each step; one Run executing Attributes → Map layout, then Attributes → anchor placement → Ranking in order; the map test through Run with Map layout ticked; `load_samples` / `index_summary` under None, () and a folder.
- The user's index seeded from their real settings: three folders — `D:\_soundPacks\___GUITAR_INSTR` (root, in scope, 1,616 files), `D:\_soundPacks\Krotos Starter Library` (dormant, 3,956), `D:\_soundPacks\iris2\Samples` (dormant, 4,568); the window shows 1,616 rows and "1616 samples in scope of 10140 indexed"; the tab checked offscreen.

**Next**

- Anchored-only Recompute attributes (the rest of Phase 8); Phase 5; Phase 9.

## 2026-09-08 — ⚓ on every row: anchoring is ranking; the CLAP columns leave the list; UMAP's seed warning

**Phase:** 7 / 9 (anchor and ranking, reshaped) · `acc3a44`

The user reported UMAP's "n_jobs value 1 overridden to 1 by setting random_state" at the end of a recompute cycle, and found the anchor-then-rank ceremony overdone: take the CLAP columns out of the list, put a button at the start of each row that anchors and ranks at once, let the anchored sample jump to the top, un-anchoring changing nothing, a new anchor superseding a running one — with lazy loading, parallelism or a per-sample cache if the computation needs it.

**Done**

- **Measured first.** On the user's index (4,568 samples + 28,664 segments, 3,874 of them CLAP windows): the feature table loads in 0.6 s, the anchor's per-axis distances take 13 ms and a full ranking with the fold 12 ms. Ranking is one vectorised pass; nothing about it needs to be incremental, parallel or cached — only the table's first load after a recompute is worth a thread (it scales linearly: seconds at 110k files).
- **The ⚓ column** (`listmodel.AnchorDelegate`): the File cell is painted shifted right by a 24-px click zone showing ⚓ — accent on the anchored row, dim otherwise, brighter under the mouse — for sample rows and sub-hit rows alike; a click there emits `anchor_clicked`. `ANCHOR_ROLE` tells the delegate which row carries it; the anchor's Similarity sort value is bumped so it sits above even an identical sample. The A key anchors the selected row; the transport's Anchor button is gone, its label and ✕ remain.
- **Anchor = rank** (`main.py`): `_anchor_and_rank` applies the anchor, ranks the scope with the bars as they are, scrolls to the top; the restored anchor at start-up ranks the list the same way, and so does the reload after a job. `_with_features` queues callbacks until the table lands from `_FeatureThread` (own connection, off the GUI thread); `reload` bumps a generation so a table built before it is discarded; a newer ⚓ click drops an older pending one. Text search and the Recompute tab's Ranking step go through the same queue, so nothing blocks the window on a table load.
- **✕ changes nothing but the anchor**: the Similarity column, its order and the map's halo stay; only the anchor mark, the ranges and the difference bars go.
- **The list**: Rhythmic / Melodic / Vocal / Other columns removed (the bars and the "CLAP score at least" filter on the Attributes tab keep the numbers); `COL_SIMILARITY` 8, `COL_MATCH` 9.
- **UMAP**: `n_jobs=1` passed explicitly with the seed — a seeded fit is single-threaded by UMAP's design and it warns when `n_jobs` is left at its default of −1 (its message prints the value after overriding it); the seed stays because it is what makes "layout #N" reproducible.
- The Recompute tab's Ranking step is now described as what it is for: re-ranking after the weight bars move.

**Decided**

- No incremental ranking, no per-sample cache: at 25 ms there is nothing to hide, and a cache of anchor-specific distances (every sample × every item) would be far larger than the table it is derived from. If the first load ever matters at library scale, the table itself is what to cache (a `.npz` next to the layouts), not the rankings.
- Anchoring ranks with the bars as they are; moving a bar does not re-rank live (the §9.6 policy: no work on a slider drag) — the Ranking step, or another ⚓ click, does.
- The reload after a job re-anchors and re-ranks: an anchored list is a ranked list, before and after a recompute.

**Verified**

- `uv run pytest tests -q` → **180 passed, 2 skipped**; pyflakes clean. The anchor test now: zero weights → anchored but told there is nothing to blend; bars up → ranked with the anchor first at 100; ⚓ on another row → anchored and on top in one click; ✕ → the order and the column stay; ⚓ again → back on top; a restarted window comes up ranked against the restored anchor. The map and Run tests wait for the threaded table load.
- The user's index, offscreen: ⚓ on a row of the 4,568 in scope — anchored, ranked, on top — the glyph column drawn.

**Next**

- Anchored-only Recompute attributes; Phase 5; Phase 9's marker editing.

## 2026-09-08 — Ranking leaves the Recompute tab; the weight bars re-rank

**Phase:** 7 / 8 (anchor and ranking, reshaped) · `59c4c6c`

"Great! ranking now should come out of recompute."

**Done**

- The *Ranking* step, its two scopes and its note are gone from the Recompute panel; `RunPlan` is attributes + layout; the anchor's only remaining role on that tab is the *Map layout* step's "anchor only" option (`set_anchor_available`).
- The weight bars re-rank the anchored list on release: `weights_changed` → a 150 ms single-shot timer → `_rank()` through the feature-table queue, so a drag ranks once at its end and a table still loading is waited for. The bars' group says so ("Weights — the list re-ranks as you move them"); `_rank` has no scope argument any more (the "visible rows only" ranking went with the step — the quick filter and the Attributes filters already narrow what is shown).
- CLAUDE.md's "nothing expensive runs automatically" names ranking as the measured exception (~25 ms), with the condition that it goes back behind a button if that ever changes; spec §9.6's policy line and table row say the same; README and the panel's docstring follow.

**Decided**

- Re-ranking on a bar's release rather than on a button: with ranking at 25 ms the button was ceremony, and a bar that changes nothing until a later click would be half-dead. The debounce keeps a drag from ranking per pixel; at library scale (~0.5 s per pass, projected) it is still one pass per release.

**Verified**

- `uv run pytest tests -q` → **180 passed, 2 skipped**; pyflakes clean. The anchor test now ranks by releasing the bars (zero weights → told; bars up → ranked with the anchor first); the Run-order test runs Attributes → anchored placement and finds the list re-ranked after the reload; the settings round-trip no longer stores the step.

**Next**

- Anchored-only Recompute attributes; Phase 5; Phase 9's marker editing.

## 2026-09-08 — The right panel stops moving with the selection

**Phase:** 9 (window polish) · `55bc53d`

The user: the right panel's width became variable — every click on a sample retracts or widens it; it should keep a default size or the size it was dragged to.

**Done**

- **Found by measuring, not guessing.** Offscreen, the right panel held its width across selections; the movement came from the *left*: the transport row's "now playing" and anchor labels are plain QLabels whose minimum width is their text's width. A long file name ("Ambience Los Angeles Street Traffic Cars Pedestrians Dog Night Loop.wav", plus a window-hit anchor label) raised the left pane's minimum from 583 px to 1433 px, the splitter squeezed the right panel to its 360 px floor — and kept the squeeze after the name was short again, because a splitter remembers the sizes it was forced to.
- `theme.ElidedLabel`: a one-line label with a zero minimum width that elides its text in the middle to the space it has, keeps the full text in `text()` (callers read it) and as the tooltip. The two transport labels use it, sharing the row 2:1.
- Both splitters persist: `window/splitter` (list | right panel) and `window/panes` (list | waveform) are saved on close and restored at start-up, so a dragged position is the default from then on.

**Verified**

- `uv run pytest tests -q` → **181 passed, 2 skipped**; pyflakes clean. New: two 150-character labels leave the splitter sizes untouched and the label's minimum width at 0; sizes set "by hand" on both splitters come back in a new window on the same settings.

**Next**

- Anchored-only Recompute attributes; Phase 5; Phase 9's marker editing.

## 2026-09-08 — Phase 5: Qwen2-Audio on this CPU — captions yes (opt-in), latent axis no

**Phase:** 5 · `32ef47b`

"I think we're at a stage of starting phase 5, shall we?"

**Done**

- **The spike** (`scripts/phase5_spike.py`, report `.docs/phase5_spike.md`): 102 files in 16 groups built from the library on disk — six named singers (Amy Kirkpatrick both speaking and singing; Cory Friesenhan, Holly Drummond, Cristina Soto, Veela; CHROMA's Soprano and Ethno singers on the same phrases in A minor), kicks / snares / hats, Krotos footsteps / ambiences / whooshes / gun foley / animals — embedded with CLAP (the app's whole-file vector) and with thirteen Qwen2-Audio latents (layers 4/8/12/16/24 and the encoder output, mean and mean‖std pooled over the frames the clip occupies, plus the projector output), compared leave-one-out (`evaluation.py`: precision@k, Jaccard, group separation, "does X turn up at all"), sixteen files captioned with the full model. `Qwen/Qwen2-Audio-7B-Instruct`, 8.4 B parameters, bf16 on the CPU, no quantisation: 16 GB downloaded to `F:\HF_HOME`, loads in seconds (memory-mapped).
- **Cost, measured:** latent 1.22 s per file regardless of length (a 30-s window every time) = 9× CLAP's 0.14 s, 38 h for the library; caption 6–20 s per file, 1.9 tokens/s decoding, 12 days for the library.
- **Captioning built** (node X1, spec §5.2): `qwen_audio.QwenAudio` (lazy load, `latents`, `caption`), `caption_pending` (one `text_tags` row per sample, source `qwen2audio-caption`, stale when the content changes, per-file isolation, stop, scope, limit; the model's `load` runs once before the loop so a missing stack fails once), the Recompute tab's *Qwen2-Audio captioning* toggle now live (after embedding; `RecomputeSettings.captions`, the report's `[captions]` block, an ImportError as a note), `crate-caption --limit --recaption`, the sentence under the chips on the Attributes tab (`load_caption`).

**Decided**

- **No vocal-semantic axis.** The hypothesis of §5.3 was speaker identity; it does not appear: no latent finds the same voice across speech and singing (0 of 5 — nor does CLAP), same-singer retrieval ties CLAP within noise (0.67 vs 0.64 at best), the two singers on shared phrases are told apart no better than CLAP's 0.92, and everywhere else the latents are the same or worse, with half-overlapping neighbourhoods — different, not better. The one consistent win, drum one-shots (0.78 vs 0.66), the DSP axes already give. Node X2 closed; what would reopen it is a model trained *for* speaker/singer identity (x-vector / ECAPA-style, milliseconds per file), not a captioning model's encoder.
- **Captions stay opt-in and scope-sized:** right and useful beyond a few seconds of material (vocal atmospheres, ambiences, foley actions), wrong on sub-second hits, which the 30-s window drowns in silence — CLAP's chips remain the label for one-shots. The plain prompt "Describe this sound in one sentence." — "as a sound designer would" pulled a sung note into synthesiser vocabulary.
- The seeded UMAP is single-threaded by design (n_jobs=1 passed explicitly), the spike's evaluation lives in `evaluation.py` for the next axis question, and spike scripts live in `scripts/`, never imported by the app.

**Verified**

- `uv run pytest tests -q` → **185 passed, 3 skipped** (the third skip is the opt-in real-Qwen run, `CRATE_REAL_QWEN=1`); pyflakes clean. New: the pooling arithmetic (frames, masked mean and stats, variant names), the retrieval metrics on planted groups, `caption_pending` with a fake captioner (writes, idle, stale-only, one row per sample, a failure keeps the old sentence, stop, scope + limit), the stage in the Recompute run (off by default, on, idle again, a broken stack as a note), the toggle in the settings round trip, the sentence on the Attributes tab.
- The real thing: one clip through the model (9 s load, latents 1.2 s, caption 12 s); `crate-caption --limit 3` on the user's index — three iris2 "Ahh Long" files captioned in 7.8 s each: "a human voice singing a long note".

**Next**

- Anchored-only Recompute attributes (the rest of Phase 8); Phase 9's marker editing; Phase 10 (Bitwig: reveal, crate export).

## 2026-09-08 — Captions in parts and per sample; a Search tab

**Phase:** 5 / 7 (captioning made usable; the right panel re-cut) · `031523c`

The user: captioning is laborious — do it in parts, and for a single sample; and the Attributes tab is crammed — move everything search-related to a third tab.

**Done**

- **Captions as their own step** under Run (`recompute.py`): *Captions* with a *files per Run* batch (default 100, 0 = all) — each Run captions that many samples in scope without a sentence, the next Run continues; `RunPlan.captions`; the window's plan runs it after Attributes and Map layout (`run_captions`). The toggle inside the Attributes settings, and the captions stage inside `recompute_attributes`, are gone again (one way, not two). The model instance is created once by the panel and kept across jobs (16 GB memory-mapped; a job thread reuses it).
- **Caption this sample**: a button next to the sentence on the Attributes tab (`caption_requested`) → `RecomputePanel.caption_sample` → `caption_pending(sample_ids=[…], recaption=True)` as a job, so it shares the log and Stop; a hit's parent is captioned. `caption_pending` gained `sample_ids`.
- **A reload keeps the selection**: `reload()` re-selects the sample that was current (a hit's parent) without replaying the preview (`_quiet_select`), so the sentence appears in place after the job — and any recompute stops throwing the selection away.
- **The Search tab** (`search.py`): the CLAP text search box with Clear, and the filters — type, minimum CLAP scores, length, tempo, the anchor-distance ranges (unlocked by an anchor). **Attributes** keeps the difference bars, the chips (a click hands the tag to the Search tab's box and searches), the caption and its button, the CLAP scores, the stripes, the segments and the weights.

**Verified**

- `uv run pytest tests -q` → **185 passed, 3 skipped**; pyflakes clean. New: a Run with Captions ticked at one file per Run captions one, then the other, then is idle, on one model instance; the button rewrites the selected sample only and leaves the other untouched; the selection survives the reload; `sample_ids` on the stage; the settings round trip persists the step and its batch; every search and filter test now drives the Search tab.
- Offscreen on the user's index: the three tabs, the caption and its button on Attributes, the box and filters on Search, the Captions step on Recompute.

**Next**

- Captions as a search channel (CLAP's text encoder against the sentences — measured complementary to the audio vectors); anchored-only Recompute attributes; Phase 9's marker editing.

## 2026-09-08 — The CLAP box shows the tag scores

**Phase:** 7 (Attributes tab) · `98437a5`

The user: use the chips' scores in the CLAP scores box instead of the four calculated ones.

**Done**

- `EmbedSettings.top_k_tags` defaults to 0 = every tag: `classify` keeps all 32 cosines, best first, and `_write_tags` stores them; `load_tags` takes a `limit`. The user's index reclassified from the stored vectors (`crate-embed --reclassify`): 32 rows per sample.
- The Attributes tab's box is *CLAP tag scores of the selected sample*: ten bars, tag and cosine ×100, best first, the prompt in the tooltip; the chips above stay the top five, clickable. The four prompt-set percentages are no longer displayed; they still feed the Search tab's *CLAP score at least* filter, whose tooltip says so.

**Verified**

- `uv run pytest tests -q` → **185 passed, 3 skipped**; pyflakes clean (the GUI test reads the bars: at most ten, sorted, 0–100).
- The user's index: 4,568 samples × 32 tags; the box checked offscreen.

**Next**

- Captions as a search channel; anchored-only Recompute attributes; Phase 9's marker editing.

## 2026-09-08 — ⚓ Anchored-only Recompute attributes: Phase 8 complete

**Phase:** 8 (the last piece) · `b1c037f`

The user: finish what is left of Phase 8 first.

**Done**

- **Anchor only** as the third choice under the Attributes step (`recompute.py`), greyed until there is an anchor, next to *new/changed only* and *everything again*: every stage — analysis, segmentation, CLAP embedding — again for the anchored sample (a hit's parent) under the settings as they are; no folder in scope needed; one worker. `RecomputeSettings.sample_ids`; `recompute_attributes` skips the folder scope and forces the full mode for those ids; the three stages take `sample_ids` through `db.ids_clause` (which `caption_pending` now shares). The window hands the panel the anchor's sample with its label (`set_anchor_available(…, sample_id)`).
- **One CLAP encoder across runs** (`RecomputePanel._encoder`, keyed on checkpoint + batch size, loaded lazily on the job thread): loading it was most of an anchored-only run.
- **A redone anchor**: an anchored hit whose parent is redone loses its row (re-detection writes new rows); after the reload the anchor moves to the parent, with a line in the log.
- **Schema v10**: `samples.id` and `segments.id` are AUTOINCREMENT. Found on the way: a plain INTEGER PRIMARY KEY hands a new row the largest id in use plus one, so the re-detected segment took the deleted anchor's id and the anchor silently pointed at a different span; a folder scanned in after another was removed could do the same to a sample. Both tables are rebuilt through the v8 rebuild, generalised (`_rebuild_table`, `_table_ddl`), ids kept.
- **Fixed on the way**: the Recompute panel's radios were one exclusive group (one parent widget), so *anchor only* under Map layout unticked *everything again* under Attributes; each step's radios are a `QButtonGroup` now.

**Decided**

- Anchored-only always redoes the sample: the sub-toggle (new/changed, everything) belongs to the library scope, as §9.6's table has it. The anchored path exists to see a changed setting on the one file in front of you.
- The mode persists as `recompute/attributes_mode` (changed | full | anchored); without an anchor at start-up it falls back to *new/changed only*, as the layout's choice does.

**Verified**

- `uv run pytest tests -q` → **189 passed, 3 skipped**; pyflakes clean. New: `ids_clause`; the engine redoes one sample and nothing else under a changed cap, with no scope, on one worker; the GUI: the radio is greyed without an anchor, the layout's choice survives the attributes' (own groups), the run logs "anchor only", caps the anchor's segments, leaves the other sample's analysis stamp alone, the anchored hit falls back to its parent, clearing the anchor greys the radio again; v9 → v10 keeps every row and id and a segment inserted after a delete gets a fresh id.
- The user's index (a copy, via the backup API): the v10 rebuild of 4,568 samples + 31,532 segments in 0.38 s — counts, max ids and foreign keys unchanged, integrity ok; the real index rebuilds itself the same way at the next launch. Anchored-only on a 5-s vocal, offscreen: 24.5 s the first run (the model load), 3.8 s wall the second, reload and re-rank of 4,568 samples included. Screenshot checked: three radios, the anchor named, the waveform down to the capped two segments.

**Next**

- Phase 9's marker editing (drag, Save / Delete segment); captions as a search channel.

## 2026-09-08 — Manual markers on the waveform: Phase 9 complete

**Phase:** 9 (the last piece) · `06a7ec5`

The user: complete Phase 9.

**Done**

- **Markers drag, segments draw** (`waveform.py`, `WaveformView`): a press within 6 px of a segment's begin or end marker grabs it (the cursor turns to a resize arrow over one); a drag elsewhere draws a new segment in either direction; a press that moves under 4 px is still the click it was (select the segment / seek). A marker never crosses its partner; bounds are clipped to the file. Every edit is **staged** — `stage_edit` / `add_draft` / `staged()` / `discard()` — and painted dashed with "unsaved" (a moved segment, green from then on) or "new" (a draft); the header counts them. Esc discards, Del asks to delete the selected segment; loading another sample drops the staging. A moved segment put back where it was leaves nothing to save.
- **`WaveformPanel`**: the view with **Save segment** ("Save N segments"), **Discard** and **Delete segment** under it, enabled by the staging and the selection (a CLAP window is never deletable), and a one-line hint. The window keeps `_waveform` as the view; the panel sits in the bottom splitter pane.
- **Save and Delete as jobs** (`main.py`, `_save_segments` / `_delete_segment`, through `RecomputePanel.start_job`, which now says whether it started): `update_segment` for a moved one — manual and confirmed, review flag cleared, render and vector dropped — and `create_manual_segment` for a drawn one, described on the spot; `delete_segment` after `_confirm` (a Yes/No box; tests replace it). The job's reload shows the result and keeps the selection; while a job runs the markers stay unsaved and the status bar says so.

**Decided**

- Jobs, not direct writes: a segment's descriptors decode the parent (a 16-minute ambience takes seconds), and a job shares the log, Stop and the reload the window already has. The price is that a save waits for a running recompute.
- Staging is per sample and not persisted: an unsaved drag is cheap to redo; §6.3 is about what reaches the index, and nothing does until Save.

**Verified**

- `uv run pytest tests -q` → **192 passed, 3 skipped**; pyflakes clean. New: the view's drag, draw, clamp, click-without-movement, Esc, Del, the drop on load and the clipping; the panel's buttons; the window's save (a moved automatic segment manual + confirmed with its render gone, a drawn one created and described, the table showing both after the reload) and delete (refused, then confirmed, then gone).
- The user's index (the copy): a 4-s vowel with four auto segments — one moved and one drawn, saved in 3.7 s including the reload of 4,568 rows; the drawn one selected in the table and deleted in 1.7 s. Screenshots checked: the dashed unsaved spans with the header's "2 unsaved" and the buttons, then the two green manual segments.

**Next**

- Phase 10 (Bitwig: reveal in Explorer, crate export — native drag-out is done); captions as a search channel; Phase 11's corrections.

## 2026-09-08 — Waveform: zoom, a finer raster, reads off the GUI thread, the caption on top

**Phase:** 9 polish (the waveform panel) · `23591e4`

The user: the waveform needs a zoom for precision; some GPU support for a less crude look; separate the playback from the graphics with parallel rendering; the Qwen2-Audio caption and its Recaption button at the top of the waveform, "—" when missing.

**Done**

- **Zoom and pan** (`waveform.py`): `set_view` / `zoom` / `pan` / `fit` over a (start, end) window — the wheel zooms about the cursor (×1.25 a notch, never narrower than 2 ms), Shift+wheel or a horizontal wheel pans a tenth of the view, right-click or Home fits; the panel has a scrollbar under the plot, synced both ways. The header names the zoom and the seconds shown; the axis picks its step from the view (down to 1 ms); markers, segments, windows, the envelope and the playhead all draw through the view.
- **The read on a thread for every file** (`load` → `_EnvelopeThread`, a newer load wins): the GUI thread only opens the header for the duration. `Envelope` keeps the mono samples for files up to 3 minutes (`KEEP_SAMPLES_SECONDS`) and 32k min/max/RMS columns for every file (`OVERVIEW_COLUMNS`); `peaks_for_view` reduces either to one column per device pixel (`np.*.reduceat`).
- **A cached raster**: the body — peaks as a light fill, RMS as a brighter core — is drawn into a `QPixmap` at the device pixel ratio once per (file, view, size) and blitted after; the overlays stay vector. `wait_for_load(deliver)` for the tests and the close.
- **The caption line** at the top of `WaveformPanel`: the sentence (elided, "—" without one) and a *Caption* / *Recaption* button (`caption_requested`); `set_caption`. The Attributes tab's group is *Tags of the selected sample* now and no longer shows the caption.

**Decided**

- **No GPU.** The crude look came from 1,200 min/max columns stretched over 1,600 px and one flat polygon, not from the paint engine: per-device-pixel columns, an RMS core and an antialiased raster fix it, and Qt's raster engine paints a strip in a few ms. A `QOpenGLWidget` would add a driver dependency (CLAUDE.md: CPU only) for no visible gain.
- **What "parallel" buys.** The audio read is the only heavy part and it now never runs on the GUI thread; rasterising is 16–40 ms once per view change and stays on the GUI thread (a thread would add latency to every wheel notch for nothing). Playback runs in Qt Multimedia's own thread already — what delayed it was the GUI thread's work at selection time, now measured at 2–55 ms.

**Verified**

- `uv run pytest tests -q` → **195 passed, 3 skipped**; pyflakes clean. New: `peaks_for_view` from samples and from columns; zoom about a point, the clip to the file, the 2 ms floor, the scrollbar both ways, the wheel (zoom, Shift-pan, horizontal pan), right-click and Home, the raster reused until the view changes, markers at the zoomed scale; the caption line and its button. The GUI tests wait for the threaded read where they read the waveform.
- Measured (1600 px, 44.1k stereo): read 19 ms (5 s), 31 ms (30 s), 72 ms (2 min), 334 ms (15 min, columns only) — on the thread; raster 16–40 ms; cached repaint 0.3–0.4 ms. On the user's index (offscreen): selecting a 4.7-s vocal costs 2–55 ms on the GUI thread, its waveform lands 100 ms later; a 2.3-minute street ambience lands in 0.22 s. Screenshots checked: the caption line, a ×8 zoom with the RMS core, the scrollbar, a ×50 zoom of the ambience.

**Next**

- The header across the window with the List / Map buttons stacked, the tag-score bars and the CLAP strip; per-column filters in the list's header; the segments table out of Attributes and the weights on the Search tab (the rest of the user's list).

## 2026-09-08 — The header across the window; filters in the column headers

**Phase:** 7 / 9 polish (the list and the header) · `4223b78`

The user: the quick filter should live at each column header — a text field for the name, a pulldown of the types, and so on; the quick filter bar goes; the header should span the window with the List / Map buttons stacked (room for a third), the CLAP tag scores as clickable vertical bars, and the CLAP "DNA" stretched underneath; the three groups stay on the Attributes tab for now.

**Done**

- **Column filters** (`headerfilter.py`): `FilterHeader` replaces the list's header (`install_on`, since a view resets clickability when it adopts a header); a click on a section opens `FilterPopup` under it — *Sort ascending* / *Sort descending*, the editor, *Clear*. `ColumnSpec` per column: text (File, Folder, Tags — "contains"), values (Type, Key — a checklist of the values present, `SampleTreeModel.distinct_values`, "(none)" for an empty cell), range (Length, BPM, Hits, Similarity, Match — min/max with "any" at 0; Similarity and Match shown in % of a raw 0..1, `scale`). `ColumnFilter` (`listmodel.py`) is the filter; `ListProxy.set_column_filter` applies it beside the Search tab's criteria; a filtered section carries a dot. The view's own sorting is off — a header click used to flip the indicator and sort; the header puts the indicator back and the popup's buttons sort. The window's `_on_column_filter` keeps the map in step.
- **The header** (`main.py`): a full-width strip above the body splitter — the List / Map buttons stacked at the left with a stretch for a third, `TagBars` (`tagbars.py`: ten vertical bars, score on top, tag under, hover and a pointing cursor, a click → the Search tab's box) and a compact `VectorStrip` (no caption, thinner rows) underneath. The quick-filter box and `_on_filter_changed` are gone.
- **The Search tab** keeps the search box, the CLAP minimum scores and the anchor-distance ranges; type, length and tempo left for the headers (a note says so). `Criteria` is unchanged — the panel simply no longer sets those.

**Decided**

- A header click opens the popup rather than sorting: one gesture per header, and the popup's first two buttons are the sort. The Similarity / Match columns' programmatic sorts (`sortByColumn`) are unaffected.
- Type, length and tempo were removed from the Search tab rather than duplicated: two filters on the same thing that both apply is a puzzle.

**Verified**

- `uv run pytest tests -q` → **199 passed, 3 skipped**; pyflakes clean. New (`tests/test_headerfilter.py`): `ColumnFilter.accepts` per kind; the proxy with several filters and the model's distinct values; a real click on a header section (QTest) opens the checklist and leaves the sort indicator alone, unticking a value filters the list and marks the section, All restores, the popup's button sorts; the text, range and %-scaled editors; the tag bars' slots, hover tooltip, click and cap at ten. The GUI tests drive the header filters where they used the quick filter.
- Offscreen on the user's index: the header with the stacked buttons, ten bars for a vowel (vocal 62 …), the strip; Type = one-shot and Tags ∋ "vocal" narrow the list; the Type popup lists loop / multi-hit / one-shot; a bar click fills the Search box.

**Next**

- The segments table out of the Attributes tab and the weights over to the Search tab (the last of the user's list).

## 2026-09-08 — The segments table out, the weights to the Search tab

**Phase:** 7 polish (the tabs) · `7aa5310`

The user: "Segments of the selected sample" is not needed; the weights belong on the Search tab.

**Done**

- **The segments table is gone** from the Attributes tab, with `host_segments` and the window's `QTableView`. The window keeps the selected sample's segments as a plain list (`_segment_rows`); a click inside a segment on the waveform goes to `_select_segment`, which does what the table's row selection did — render, current item, transport caption, the waveform's selection, preview on auto-play. `SegmentTableModel` stays in `listmodel.py` (tested) for a drill-down should one come back.
- **Drag into Bitwig ↗** (`DragHandle`, `main.py`) replaces the static label in the transport row: press and drag it to hand the OS the current item's file — the sample, or the rendered segment — so a segment that is not a search hit can still be dragged out now that the table is gone (`mime_data()` for the tests).
- **The weights** (`search.py`): the group, `weights()`, `weights_changed` and the `weights/` settings keys moved as they were; the window re-ranks from the Search tab's signal. **Attributes** keeps the difference bars, the chips, the tag-score bars and the stripes; its docstring says where everything went.

**Verified**

- `uv run pytest tests -q` → **199 passed, 3 skipped**; pyflakes clean. The GUI tests select segments through `_select_segment`, read the drag handle's URLs for the sample and for a rendered segment, and drive the weights on the Search tab (they still persist across a restart).
- Offscreen on the user's index: a vowel's first segment selected from the waveform, the drag handle carrying its rendered clip; the Attributes tab with four groups, the Search tab with the weights under its filters.

**Next**

- Phase 10 (Bitwig: reveal in Explorer, crate export); captions as a search channel; Phase 11's corrections.

## 2026-09-08 — Half-and-half splits by default

**Phase:** 7 polish (the window) · `f2d0f9e`

The user: the list and the waveform should split 50/50 vertically, and the list/waveform pane and the three tabs 50/50 horizontally, by default.

**Done**

- Both splitters (`main.py`) start with equal stretch and equal sizes; a dragged position still persists (§9.2's 2026-09-08 note). The settings keys are `window/splitter2` and `window/panes2`, so the new default shows once even where an older dragged position was saved — a drag after that is kept as before.

**Verified**

- `uv run pytest tests -q` → **199 passed, 3 skipped**; pyflakes clean — the panes test asserts both splits within 8 px of equal on a fresh settings file, then drags away from them and sees the drag survive a restart.
- Offscreen on the user's index with fresh settings at 1600 × 900: list | tabs 798 | 798, list | waveform 362 | 362.

**Next**

- Phase 10 (Bitwig: reveal in Explorer, crate export); captions as a search channel; Phase 11's corrections.

## 2026-09-08 — The window rearranged: list right, waveform and tabs left

**Phase:** 7 polish (the window) · `0d9a645`

The user: the list/map where the tabs are, the waveform where the list/map is, the tabs where the waveform is; the CLAP tags and embedding squeezed to 50 % so the list/map takes the whole right half.

**Done**

- `main.py`: the body splitter is *left column | list-or-map*; the left column is the header (view switch, tag bars, strip — now half the window wide), then a vertical splitter of the waveform panel with the transport row under it and the tabs. Both splits half and half by default; the settings keys are `window/splitter3` / `window/panes3` so the new arrangement is not shaped by a state saved for the old one.

**Verified**

- `uv run pytest tests -q` → **199 passed, 3 skipped**; pyflakes clean (the panes test's "right pane at least 360 wide" and the half-and-half assertions hold for the new contents).
- Offscreen on the user's index at 1600 × 900, fresh settings: left | list 798 | 798, waveform | tabs 375 | 375; screenshot checked.

**Next**

- Phase 10 (Bitwig: reveal in Explorer, crate export); captions as a search channel; Phase 11's corrections.

## 2026-09-08 — Attributes down to the difference bars; the anchor a circle; the transport row gone

**Phase:** 7 polish (the window) · `a0be76c`

The user: the tags, CLAP scores and CLAP embedding groups leave the Attributes tab (they are in the header); the anchor icon looks awful — a radio-button-like circle instead; play and stop as plain icons in front of *Save segment*; *Auto-play on select* in place of the hint; everything else in the row below (the anchor label, *Drag into Bitwig*) out.

**Done**

- `attributes.py` keeps the difference group only; the chips, bars, strip and their methods are gone with them (`TagBars` and the header strip carry the tags and the embedding; `CLASS_HELP` / `_prompts_text` stay for the Search tab).
- `listmodel.py`: `_paint_anchor` draws a ring, filled on the anchored row; the window's `_on_anchor_clicked` clears the anchor when the filled circle is clicked (the ranking stays, as with ✕). Every "press ⚓" text says "the circle at the start of a row" now.
- `waveform.py`: the button row is ▶ ■ *Save segment* *Discard* *Delete segment* … *Auto-play on select*; the hint became the plot's tooltip. `main.py`: the transport row, `DragHandle`, the now-playing and anchor labels are gone — `_current_label` and `_anchor_name` carry their text to the difference readout and the status bar; the window's `_autoplay` / `_play_button` are the panel's widgets.

**Decided**

- Dragging a segment that is not a hit is not possible for now: the drag handle went with the row, as asked. A list row still drags its sample or hit.

**Verified**

- `uv run pytest tests -q` → **200 passed, 3 skipped**; pyflakes clean. New: a second click on the filled circle clears the anchor and keeps the Similarity column, a click on an empty one anchors that row; the long-names test elides the caption line instead of the gone labels; the first GUI test reads the header's bars and strip.
- Offscreen on the user's index: the ring on every row and the dot on the anchored one; the button row under the waveform; the Attributes tab with the difference bars alone.

**Next**

- Phase 10 (Bitwig: reveal in Explorer, crate export); captions as a search channel; Phase 11's corrections.

## 2026-09-08 — Every section under its sample; Folder first, a Caption column; a bigger play icon

**Phase:** 7 polish (the list) · `55b9a99`

The user: the play icon twice as big; a sample should keep all its sections underneath it, and a ranking should order them under the sample by descending similarity; the Folder column to the far left, the Qwen2-Audio caption in its old place.

**Done**

- **Sections as child rows** (`listmodel.py`): `SampleTreeModel.set_rows(rows, sections)` takes every sample's sections (`catalog.load_sections`, `Section`: id, bounds, window, manual) and shows them all under the sample — a CLAP window only while it carries a score — ordered by the current scoring (match while a search is active, else the ranking), best first, unscored last in time order; in time order when nothing is scored (`_rebuild_children` on every reset). The Type cell reads hit / manual / window. The winning-hit logic (`Scores.hits`) is unchanged and now drives two things: the map's badges and which samples the window opens — `_expand_hits` replaces `expandAll`, so the list shows the ranked section at the top of an opened sample and keeps the rest folded. `hit_at` returns the section a child row shows; the anchor circle repaints on the section rows that lost or gained it.
- **Columns**: Folder, File, Caption, Length, Type, BPM, Key, Tags, Hits, Similarity, Match (`COL_*` constants; the score columns sit visually after File). `SampleRow.caption` comes from `text_tags` in the samples query; the header filters are keyed by the constants.
- **The play icon** is 20 pt (twice the base size) with less padding.

**Decided**

- The section rows are loaded with the samples (one query, 27k rows in scope here). At the full library's scale (~110k files, several hundred thousand sections) the list would want a lazier model that fetches a sample's sections when it opens — a Phase 12 item, noted in §13's cost risk; the folder scope keeps it far from that today.

**Verified**

- `uv run pytest tests -q` → **201 passed, 3 skipped**; pyflakes clean. New: a sample's child count equals its segment count, folded and in time order with nothing scored; anchoring one of its sections re-orders them by similarity with the anchored one first and opens the sample; the search and window tests read the best section first and see the sample fold again when the search is cleared; the column tests moved to the constants.
- The user's index (a copy): the window comes up with 4,568 samples and their 27,648 sections in 0.46 s; anchoring a vowel ranks, re-orders every sample's sections and opens the ones with a winning section in 2.2 s cold and 1.8 s warm — it was 2.9 s before three fixes: `index()` checks its bounds itself instead of through `hasIndex()` (600k calls a click), the score column gets a fixed width instead of being sized to its contents, `_expand_hits` no longer collapses first and `set_anchor` signals only the rows that lost or gained the circle (a whole-list signal made the proxy re-sort everything). What remains is the proxy walking 32k rows through Python on every reset. Screenshot checked: Folder first, the caption column, the sections under an opened sample by similarity.

**Next**

- Phase 10 (Bitwig: reveal in Explorer, crate export); captions as a search channel; Phase 11's corrections.

## 2026-09-09 — Review of 2026-09-08's work

**Phase:** review · `e4b6d28`

The user: a quick review before wrapping up. The multi-agent review hit the session limit; this is a direct read of the day's diff (25 files, +2,798 / −772): the anchored-only recompute and schema v10, the marker editing, the waveform zoom and threaded reads, the header and column filters, the tab reshuffle, the sections tree.

**Found and fixed**

- `reload()` re-selects the current item after a job by mapping a current segment to its parent through the index; a segment that the job itself removed — *Delete segment* on the selected one, or an anchored-only recompute re-detecting it — mapped to nothing, so the selection, the preview target and the waveform went stale. It now falls back to the parent remembered at selection time (`_current_sample`); the delete test asserts the parent is current and its waveform loaded afterwards.

**Read and left alone**

- `SampleTreeModel.index()` bounds, `_rebuild_children`, `set_anchor`'s targeted signals, the proxy's child handling; the waveform's press/move/release state machine and `_ordered`'s clipping before the audio lands; `FilterHeader`'s indicator restore around the click; `ids_clause` parameter order in the four stages; the v10 rebuild.
- Known leftovers, not bugs: `Criteria.types` / `duration_s` / `tempo_bpm` are no longer set by any panel (the header filters took over) and `SegmentTableModel` is no longer used by the window — both tested, both cheap to keep until Phase 11/12 touches them. The Hits column counts a sample's segments while its child rows can also carry scored CLAP windows, so the two numbers can differ by a few.

**Verified**

- `uv run pytest tests -q` → **201 passed, 3 skipped**; pyflakes clean.

**Next**

- Phase 10 (Bitwig: reveal in Explorer, crate export); captions as a search channel; Phase 11's corrections; a lazier section model before the list is asked to hold hundreds of thousands of rows (Phase 12).
