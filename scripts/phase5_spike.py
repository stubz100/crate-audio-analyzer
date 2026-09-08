"""Phase 5 spike (spec §5.2, §5.3): Qwen2-Audio on this CPU.

Builds a labelled evaluation set from the library on disk (singers by name,
the same singer speaking and singing, two singers on the same phrases and
key, drum one-shots by instrument, foley by category), embeds it with CLAP
(the app's whole-file vector) and with three Qwen2-Audio latents, compares
them as retrieval representations, captions a handful of files with the
full model and times everything. Writes a Markdown report.

Stages are cached in --work-dir (npz / json) so a second run resumes.

    uv run python scripts/phase5_spike.py --work-dir <dir> --out .docs/phase5_spike.md
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crate.evaluation import (  # noqa: E402
    cosine_matrix,
    hits_in_top_k,
    jaccard_at_k,
    precision_at_k,
    separation,
    top_k,
)

log = logging.getLogger("phase5")

LIB = Path(r"D:\_soundPacks")
VOCALS = LIB / "___VOCALS"
CHROMA = LIB / "___CINEMATIC" / "CHROMA_New_Age_&_Cinematic_Vocals_Audiomodern"
DRUMS = LIB / "___DRUMS" / "AUDIOMODERN_SHIFT2_AM036" / "Percussive One Shot Samples"
KROTOS = LIB / "Krotos Starter Library" / "Audio Files"

# group -> (family, artist-or-None, glob, how many)
GROUPS: dict[str, tuple[str, str | None, str, int]] = {
    "voice/AK-spoken": ("voice", "AK", str(VOCALS / "Black_Octopus_Sound_Amy_Kirkpatrick_Fortune" / "Vocal - Spoken Word" / "*.wav"), 5),
    "voice/AK-ahh": ("voice", "AK", str(VOCALS / "Black_Octopus_Sound_Amy_Kirkpatrick_Fortune" / "Vocal - Ooooh and Ahhhhh" / "*.wav"), 5),
    "voice/CF": ("voice", "CF", str(VOCALS / "Black Octopus Sound - Vocal Atmospheres by Cory Friesenhan" / "*" / "*.wav"), 8),
    "voice/HD": ("voice", "HD", str(VOCALS / "Black Octopus Sound - Vocal Atmospheres by Holly Drummond" / "*" / "*" / "*.wav"), 8),
    "voice/CS": ("voice", "CS", str(VOCALS / "Cristina_Soto_Black_Octopus" / "Vocals - Atmospheres" / "*.wav"), 8),
    "voice/Veela": ("voice", "Veela", str(VOCALS / "Black Octopus Sound - Siren by Veela" / "*" / "*.wav"), 8),
    "voice/Soprano": ("voice", "Soprano", str(CHROMA / "Soprano_Vocal_Phrases_Amin" / "*.wav"), 6),
    "voice/Ethno": ("voice", "Ethno", str(CHROMA / "Ethno_Vocal_Phrases_Amin" / "*.wav"), 6),
    "drum/kick": ("drum", None, str(DRUMS / "AM_SHIFT2_Kicks" / "*.wav"), 6),
    "drum/snare": ("drum", None, str(DRUMS / "AM_SHIFT2_Snares" / "*.wav"), 6),
    "drum/hat": ("drum", None, str(DRUMS / "AM_SHIFT2_Hats" / "*.wav"), 6),
    "foley/footsteps": ("foley", None, str(KROTOS / "Footsteps" / "**" / "*.wav"), 6),
    "foley/ambience": ("foley", None, str(KROTOS / "Ambiences" / "**" / "*.wav"), 6),
    "foley/whoosh": ("foley", None, str(KROTOS / "Whooshes" / "**" / "*.wav"), 6),
    "foley/gun": ("foley", None, str(KROTOS / "Gun Foley" / "**" / "*.wav"), 6),
    "foley/animal": ("foley", None, str(KROTOS / "Animals & Monsters" / "**" / "*.wav"), 6),
}
CAPTION_GROUPS = list(GROUPS)   # one caption per group, the first file of each


def spread(items: list, n: int) -> list:
    """`n` items spread evenly through a sorted list (not the first n: they
    tend to be one take's variations)."""
    if len(items) <= n:
        return items
    idx = np.linspace(0, len(items) - 1, n).round().astype(int)
    return [items[i] for i in sorted(set(idx.tolist()))]


def select(work: Path) -> list[dict]:
    path = work / "set.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict] = []
    for group, (family, artist, pattern, n) in GROUPS.items():
        files = sorted(f for f in glob.glob(pattern, recursive=True) if not os.path.basename(f).startswith("."))
        chosen = spread(files, n)
        if len(chosen) < n:
            log.warning("%s: only %d files for %s", group, len(chosen), pattern)
        for f in chosen:
            rows.append({"path": f, "group": group, "family": family, "artist": artist or group})
    path.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    log.info("evaluation set: %d files in %d groups", len(rows), len(GROUPS))
    return rows


def clap_stage(work: Path, rows: list[dict]) -> dict:
    path = work / "clap.npz"
    if path.exists():
        data = np.load(path, allow_pickle=True)
        return {"vectors": data["vectors"], "seconds": data["seconds"], "chips": data["chips"].tolist(), "load_seconds": float(data["load_seconds"])}
    from crate.embedding import ClapEncoder, EmbedSettings, Prompts, classify, load_audio_for_clap, sample_windows

    encoder = ClapEncoder()
    t0 = time.perf_counter()
    encoder.embed_text(["warm-up"])
    load_seconds = time.perf_counter() - t0
    prompts = Prompts.build(encoder)
    settings = EmbedSettings()
    vectors, seconds, chips = [], [], []
    for i, row in enumerate(rows):
        t0 = time.perf_counter()
        y, sr = load_audio_for_clap(row["path"])
        windows = sample_windows(y, sr)
        vecs = encoder.embed_audio(windows, sr)
        mean = vecs.astype(np.float64).mean(axis=0)
        vec = (mean / (np.linalg.norm(mean) or 1.0)).astype(np.float32)
        seconds.append(time.perf_counter() - t0)
        vectors.append(vec)
        result = classify(vec, prompts, encoder.logit_scale, settings, None)
        chips.append(", ".join(f"{t} {s:.2f}" for t, s in result.tags[:3]))
        if (i + 1) % 20 == 0:
            log.info("CLAP %d/%d (%.2f s/file)", i + 1, len(rows), float(np.mean(seconds)))
    out = {"vectors": np.stack(vectors), "seconds": np.array(seconds), "chips": chips, "load_seconds": load_seconds}
    np.savez(path, vectors=out["vectors"], seconds=out["seconds"], chips=np.array(chips, dtype=object), load_seconds=load_seconds)
    return out


def qwen_stage(work: Path, rows: list[dict], model) -> dict:
    path = work / "qwen.npz"
    if path.exists():
        data = np.load(path)
        return {k: data[k] for k in data.files}
    from crate.qwen_audio import LATENT_VARIANTS, load_audio_for_qwen

    model.load()
    per_variant: dict[str, list[np.ndarray]] = {v: [] for v in LATENT_VARIANTS}
    seconds, decode_seconds = [], []
    for i, row in enumerate(rows):
        t0 = time.perf_counter()
        y, sr = load_audio_for_qwen(row["path"])
        decode_seconds.append(time.perf_counter() - t0)
        result = model.latents(y, sr)
        for v in LATENT_VARIANTS:
            per_variant[v].append(result.vectors[v])
        seconds.append(result.seconds)
        if (i + 1) % 10 == 0:
            log.info("Qwen latents %d/%d (%.2f s/file encoder, %.2f s decode)", i + 1, len(rows), float(np.mean(seconds)), float(np.mean(decode_seconds)))
    out = {v: np.stack(per_variant[v]) for v in LATENT_VARIANTS}
    out["seconds"] = np.array(seconds)
    out["decode_seconds"] = np.array(decode_seconds)
    out["load_seconds"] = np.array(model.load_seconds)
    np.savez(path, **out)
    return out


def caption_stage(work: Path, rows: list[dict], model, which: list[int], max_new_tokens: int) -> list[dict]:
    path = work / "captions.json"
    done = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    have = {c["index"] for c in done}
    from crate.qwen_audio import load_audio_for_qwen

    for i in which:
        if i in have:
            continue
        model.load()
        y, sr = load_audio_for_qwen(rows[i]["path"])
        result = model.caption(y, sr, max_new_tokens=max_new_tokens)
        entry = {
            "index": i, "group": rows[i]["group"], "file": os.path.basename(rows[i]["path"]),
            "duration_s": round(y.size / sr, 1), "caption": result.text, "seconds": round(result.seconds, 1),
            "prompt_tokens": result.prompt_tokens, "new_tokens": result.new_tokens,
            "tokens_per_second": round(result.tokens_per_second, 2),
        }
        done.append(entry)
        path.write_text(json.dumps(done, indent=1, ensure_ascii=False), encoding="utf-8")
        log.info("caption %s: %.1f s, %d tokens: %s", entry["file"], entry["seconds"], entry["new_tokens"], entry["caption"][:80])
    return sorted(done, key=lambda c: c["index"])


def evaluate(rows: list[dict], reps: dict[str, np.ndarray], k: int = 5) -> tuple[list[dict], dict]:
    groups = np.array([r["group"] for r in rows])
    families = np.array([r["family"] for r in rows])
    artists = np.array([r["artist"] for r in rows])
    voice = np.flatnonzero(families == "voice")
    drums = np.flatnonzero(families == "drum")
    foley = np.flatnonzero(families == "foley")
    ak_spoken = np.flatnonzero(groups == "voice/AK-spoken")
    ak_ahh = groups == "voice/AK-ahh"
    chroma = np.flatnonzero(np.isin(groups, ["voice/Soprano", "voice/Ethno"]))
    sims = {name: cosine_matrix(v) for name, v in reps.items()}
    table = []
    for name, s in sims.items():
        # Neighbours among the whole set for the family rows; within-family
        # questions use the family's own sub-matrix.
        s_voice = s[np.ix_(voice, voice)]
        s_chroma = s[np.ix_(chroma, chroma)]
        row = {
            "representation": name,
            "P@5 group (all)": float(precision_at_k(s, groups, k).mean()),
            "P@5 family (all)": float(precision_at_k(s, families, k).mean()),
            "P@5 artist (voice only)": float(precision_at_k(s_voice, artists[voice], k).mean()),
            "P@5 instrument (drums)": float(precision_at_k(s, groups, k, drums).mean()),
            "P@5 category (foley)": float(precision_at_k(s, groups, k, foley).mean()),
            "AK spoken → AK sung in top-5 (voice only)": float(
                hits_in_top_k(s_voice, ak_ahh[voice], k, np.searchsorted(voice, ak_spoken)).mean()
            ),
            "P@3 singer (CHROMA: Soprano vs Ethno)": float(precision_at_k(s_chroma, groups[chroma], 3).mean()),
            "separation by group": separation(s, groups),
            "top-5 overlap with CLAP": float(jaccard_at_k(sims["clap"], s, k).mean()) if name != "clap" else 1.0,
        }
        table.append(row)
    return table, sims


def neighbours_text(rows: list[dict], sims: np.ndarray, idx: int, k: int = 3) -> str:
    return "; ".join(f"{rows[j]['group'].split('/')[1]}:{os.path.basename(rows[j]['path'])[:28]}" for j in top_k(sims, k)[idx])


def report(out: Path, rows, clap, qwen, captions, table, sims, model_load_seconds: float) -> None:
    lines = ["# Phase 5 spike — Qwen2-Audio on the CPU", "",
             f"*{time.strftime('%Y-%m-%d %H:%M')} · Ryzen 9 9950X (16 cores), 125 GB RAM, bf16, no GPU · "
             f"`Qwen/Qwen2-Audio-7B-Instruct` · {len(rows)} files in {len(GROUPS)} groups*", "",
             "## Evaluation set", "", "| group | files | e.g. |", "|---|---|---|"]
    for group in GROUPS:
        members = [r for r in rows if r["group"] == group]
        lines.append(f"| {group} | {len(members)} | {os.path.basename(members[0]['path']) if members else '—'} |")
    lines += ["", "## Cost", "", "| | CLAP (whole-file vector) | Qwen2-Audio encoder + projector (latent) | Qwen2-Audio caption (full model) |", "|---|---|---|---|"]
    cap_sec = np.array([c["seconds"] for c in captions]) if captions else np.array([np.nan])
    cap_tps = np.array([c["tokens_per_second"] for c in captions]) if captions else np.array([np.nan])
    lines.append(f"| model load | {clap['load_seconds']:.1f} s | {model_load_seconds:.0f} s (shared) | (shared) |")
    lines.append(f"| per file, mean | {clap['seconds'].mean():.2f} s (decode + windows) | {qwen['seconds'].mean():.2f} s (+ {qwen['decode_seconds'].mean():.2f} s decode) | {np.nanmean(cap_sec):.1f} s |")
    lines.append(f"| per file, max | {clap['seconds'].max():.2f} s | {qwen['seconds'].max():.2f} s | {np.nanmax(cap_sec):.1f} s |")
    lines.append(f"| decode speed | — | — | {np.nanmean(cap_tps):.2f} tokens/s |")
    lines.append(f"| 110,000 files | {clap['seconds'].mean() * 110_000 / 3600:.1f} h | {(qwen['seconds'].mean() + qwen['decode_seconds'].mean()) * 110_000 / 3600:.1f} h | {np.nanmean(cap_sec) * 110_000 / 86400:.1f} days |")
    lines += ["", "## Retrieval: the three latents against CLAP", "",
              "Leave-one-out, cosine, top-5 unless said. *P@5 artist* asks, among the voice files only, whether a singer's nearest neighbours are the same singer; *AK spoken → AK sung* whether Amy Kirkpatrick speaking finds Amy Kirkpatrick singing; *CHROMA* whether two singers on the same phrases in the same key are told apart.", ""]
    keys = [k for k in table[0] if k != "representation"]
    lines.append("| representation | " + " | ".join(keys) + " |")
    lines.append("|---|" + "---|" * len(keys))
    for row in table:
        lines.append(f"| {row['representation']} | " + " | ".join(f"{row[k]:.2f}" for k in keys) + " |")
    lines += ["", "### Nearest neighbours, a few queries", ""]
    picks = [i for i, r in enumerate(rows) if r["group"] in ("voice/AK-spoken", "voice/CS", "voice/Soprano", "drum/snare", "foley/footsteps")][::2][:8]
    lines += ["| query | CLAP | enc_last_mean | proj_mean |", "|---|---|---|---|"]
    for i in picks:
        lines.append(f"| {rows[i]['group']}: {os.path.basename(rows[i]['path'])[:30]} | {neighbours_text(rows, sims['clap'], i)} | {neighbours_text(rows, sims['enc_last_mean'], i)} | {neighbours_text(rows, sims['proj_mean'], i)} |")
    lines += ["", "## Captions", "", "| group | file | length | Qwen2-Audio caption | time | tokens/s | CLAP chips |", "|---|---|---|---|---|---|---|"]
    for c in captions:
        lines.append(f"| {c['group']} | {c['file'][:34]} | {c['duration_s']} s | {c['caption'].replace('|', '/')} | {c['seconds']} s | {c['tokens_per_second']} | {clap['chips'][c['index']]} |")
    lines += ["", "## Reading", "", "*(filled in by hand after the run — see the journal entry)*", ""]
    out.write_text("\n".join(lines), encoding="utf-8")
    log.info("report written: %s", out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path(".docs/phase5_spike.md"))
    parser.add_argument("--captions", type=int, default=len(CAPTION_GROUPS), help="how many groups to caption (first file each)")
    parser.add_argument("--max-new-tokens", type=int, default=48)
    parser.add_argument("--skip-qwen", action="store_true", help="CLAP stage only (the weights are still downloading)")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):        # the report has arrows; a cp1252 console has not
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("transformers", "huggingface_hub", "numba", "urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    args.work_dir.mkdir(parents=True, exist_ok=True)

    rows = select(args.work_dir)
    clap = clap_stage(args.work_dir, rows)
    log.info("CLAP: %.2f s/file over %d files", clap["seconds"].mean(), len(rows))
    if args.skip_qwen:
        return 0
    from crate.qwen_audio import LATENT_VARIANTS, QwenAudio

    model = QwenAudio()
    qwen = qwen_stage(args.work_dir, rows, model)
    load_seconds = float(qwen["load_seconds"]) if float(qwen["load_seconds"]) else model.load_seconds
    which = []
    for group in CAPTION_GROUPS[: args.captions]:
        which.append(next(i for i, r in enumerate(rows) if r["group"] == group))
    captions = caption_stage(args.work_dir, rows, model, which, args.max_new_tokens) if args.captions else []
    reps = {"clap": clap["vectors"], **{v: qwen[v] for v in LATENT_VARIANTS}}
    table, sims = evaluate(rows, reps)
    for row in table:
        log.info("%s", "  ".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{v}" for k, v in row.items()))
    report(args.out, rows, clap, qwen, captions, table, sims, load_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
