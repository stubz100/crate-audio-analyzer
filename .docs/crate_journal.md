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
| 2 | Heuristic Analysis | ⬜ not started |
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
