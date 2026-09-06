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
| 2 | Heuristic Analysis | 🚧 in progress — nodes B/C, descriptors, Facet B (tuned on 500 labeled library files), smpl/ACID reader, stale-analysis flagging, one-shot cap setting — `PENDING_H2` (+ `PENDING_H1` line endings). Remaining before ✅: real-DB run + loop-pack review |
| 3 | Transient Segmentation | ⬜ not started |
| 4 | Embeddings & Classification | ⬜ not started |
| 4.5 | "Listen and grab" (pull-forward) | ⬜ not started |
| 5 | Qwen2-Audio + Latent-Similarity Spike | ⬜ not started |
| 6 | Map View | ⬜ not started |
| 7 | List, Search, Filter | ⬜ not started |
| 8 | Recompute Tab | ⬜ not started |
| 9 | Header Interactions | ⬜ not started |
| 10 | Bitwig Integration | ⬜ not started |
| 11 | Correction Workflow | ⬜ not started |
| 12 | Scale & Polish Hardening | ⬜ not started |
| 13 | Stretch | ⬜ not started |

**Target: first daily-drivable milestone = Phases 1–4 + 4.5 + 7** (scan → analyze → embed → list/search/filter → audition → drag into Bitwig), per spec §12.

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
- **Committed** (all on `master`, like every earlier session): `PENDING_H1` — `.gitattributes` + whitespace-only renormalization of the files that had no other change; `PENDING_H2` — Phase 2 build + every fix in this entry. Hashes cited in a follow-up journal commit per the contract.
