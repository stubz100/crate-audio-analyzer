# CLAUDE.md

## What this is

Crate: a standalone local search/mapping tool over a large personal sample library (`D:\_soundPacks`, ~110,000 files / 340GB), built to support a Bitwig Studio 6.1 production workflow. Not a sampler, drum machine, or plugin — a search engine only.

**The spec lives in [.docs/samples_final.md](.docs/samples_final.md).** Read it before making any architectural change — it's the source of truth for the taxonomy, similarity model, segmentation rules, data model, UI design, and roadmap. `.docs/samples001.md` and `.docs/samples002.md` are the historical proposal trail behind it (why decisions were made, what alternatives were rejected); don't treat them as current — `samples_final.md` supersedes both.

## Key constraints (don't relitigate these without asking)

- **Windows only.** Cross-platform is explicitly out of scope.
- **CPU only, no GPU.** Qwen2-Audio captioning and its candidate latent-similarity axis are opt-in and toggle-gated for this reason (spec §5.2).
- **Single-process Python for v1.** No C++/Rust component planned — see spec §10 for the reasoning (the DSP libraries are already native code under Python; the actual bottleneck is model inference, which is Python either way). If a genuine hot path shows up in profiling, the answer is a narrow same-process Rust extension (PyO3/`maturin`), not a rewrite — and only after profiling proves it's needed, not before. A pool of Python *worker processes* for the per-file stages (`parallel.py`, the Recompute tab's "Worker processes") is within this rule: one codebase, one writer of the index, no native component.
- **Nothing expensive runs automatically.** Map layout, ranking, and attribute/embedding recomputation are always explicit, on-demand user actions (spec §9.6) — never on a timer, a slider drag, or in the background.
- **Segments are not samples.** They live in their own tables (`segments`, `segment_analysis`, `segment_embedding`, `segment_classification`), never as self-referential rows in `samples`. Never surface a segment as an independent map point or list row — only as a nested/badged reference to its parent (spec §6.4, §9.3–9.4). Manual segments are exempt from all automatic length/cap constraints (spec §6.3). The third kind, `detection_method = 'window'`, is the embedding stage's 10-s CLAP windows of a long file (spec §6.4): the same rule applies — a hit inside its parent, never a row or a point of its own, never listed or counted as a segment.
- **RX2 (~4.4% of the library) is a known, accepted gap** — skip-and-log, not decoded (spec §3, §13 risk #5). Don't treat this as a bug to silently fix.

## Project layout

```
src/crate/              application package (src layout)
.docs/samples_final.md  the living spec (source of truth for design)
.docs/crate_journal.md  development journal — progress against the phase roadmap
```

Dependencies are grouped by phase in `pyproject.toml` (`analysis`, `ml`, `map` extras) rather than installed all at once — don't add `torch`/`umap-learn` to the base dependency set until the phase that actually needs them (spec §12).

**Don't reintroduce `aubio`.** It was evaluated and dropped deliberately (spec §10): source-only distribution since 2019, so it depends on an MSVC toolchain being present; GPLv3; and it optimizes descriptor extraction, which isn't the bottleneck (per-segment CLAP embedding is). `librosa` covers onset/tempo/pitch.

## Running

```bash
uv sync
uv run crate
```

## Conventions

- Dependency management: `uv`, not pip/poetry directly.
- Src layout (`src/crate/`), not a flat package.
- Build one phase's deliverable at a time per the roadmap in spec §12 — don't jump ahead to a later phase's feature while an earlier one is incomplete.
- **Maintain the dev journal.** Read `.docs/crate_journal.md` at session start for current status. Every work session ends with a journal entry appended (Done / Decided / Verified / Next) *before* the work is committed, and the roadmap status table updated whenever a phase's status changes. Never let it fall behind the git log — it is the record of implementation progress against the phase plan.
