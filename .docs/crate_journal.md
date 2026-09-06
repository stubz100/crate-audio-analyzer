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
| 1 | Ingestion & Metadata | ✅ done — `58da501` |
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