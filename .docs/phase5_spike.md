# Phase 5 spike — Qwen2-Audio on the CPU

*2026-09-08 09:58 · Ryzen 9 9950X (16 cores), 125 GB RAM, bf16, no GPU · `Qwen/Qwen2-Audio-7B-Instruct` · 102 files in 16 groups*

## Evaluation set

| group | files | e.g. |
|---|---|---|
| voice/AK-spoken | 5 | AK_SpokenWord_1.wav |
| voice/AK-ahh | 5 | AK_Ahhhh_B4.wav |
| voice/CF | 8 | CFVA_BassTone_A#_01.wav |
| voice/HD | 8 | HDVA_Adlib_01_Amin_170BPM.wav |
| voice/CS | 8 | CSS_Atmosphere_Am_01.wav |
| voice/Veela | 8 | Siren_CEbDDb_128bpm_Adlib1.wav |
| voice/Soprano | 6 | Soprano_Vocal_Phrase_Female_Amin_001.wav |
| voice/Ethno | 6 | Ethno_Female_Vocal_Phrase_Amin_1.wav |
| drum/kick | 6 | AM_SHIFT2_Kick (1).wav |
| drum/snare | 6 | AM_SHIFT2_Snare (1).wav |
| drum/hat | 6 | AM_SHIFT2_Hat (1).wav |
| foley/footsteps | 6 | Carpet_Boots_FullStep_01.wav |
| foley/ambience | 6 | Ambience Airplane Flight Interior Loop-001.wav |
| foley/whoosh | 6 | Whoosh Airy Thwip Chamber Reverberant-001.wav |
| foley/gun | 6 | AK47_Foley_01.wav |
| foley/animal | 6 | Parrot_African_Grey_bleep_single.wav |

## Cost

| | CLAP (whole-file vector) | Qwen2-Audio encoder + projector (latent) | Qwen2-Audio caption (full model) |
|---|---|---|---|
| model load | 6.7 s | 4 s (shared) | (shared) |
| per file, mean | 0.14 s (decode + windows) | 1.25 s (+ 0.02 s decode) | 9.4 s |
| per file, max | 1.37 s | 1.44 s | 20.3 s |
| decode speed | — | — | 1.90 tokens/s |
| 110,000 files | 4.4 h | 38.9 h | 12.0 days |

## Retrieval: the three latents against CLAP

Leave-one-out, cosine, top-5 unless said. *P@5 artist* asks, among the voice files only, whether a singer's nearest neighbours are the same singer; *AK spoken → AK sung* whether Amy Kirkpatrick speaking finds Amy Kirkpatrick singing; *CHROMA* whether two singers on the same phrases in the same key are told apart.

| representation | P@5 group (all) | P@5 family (all) | P@5 artist (voice only) | P@5 instrument (drums) | P@5 category (foley) | AK spoken → AK sung in top-5 (voice only) | P@3 singer (CHROMA: Soprano vs Ethno) | separation by group | top-5 overlap with CLAP |
|---|---|---|---|---|---|---|---|---|---|
| clap | 0.65 | 0.93 | 0.64 | 0.66 | 0.67 | 0.00 | 0.92 | 0.44 | 1.00 |
| enc_L4_mean | 0.55 | 0.89 | 0.57 | 0.62 | 0.51 | 0.00 | 0.72 | 0.01 | 0.43 |
| enc_L4_stats | 0.56 | 0.89 | 0.59 | 0.62 | 0.53 | 0.00 | 0.78 | 0.01 | 0.44 |
| enc_L8_mean | 0.56 | 0.91 | 0.61 | 0.61 | 0.49 | 0.00 | 0.83 | 0.01 | 0.44 |
| enc_L8_stats | 0.58 | 0.89 | 0.62 | 0.61 | 0.53 | 0.00 | 0.81 | 0.01 | 0.44 |
| enc_L12_mean | 0.59 | 0.91 | 0.64 | 0.60 | 0.52 | 0.00 | 0.86 | 0.01 | 0.47 |
| enc_L12_stats | 0.60 | 0.91 | 0.63 | 0.63 | 0.55 | 0.00 | 0.83 | 0.01 | 0.49 |
| enc_L16_mean | 0.59 | 0.91 | 0.66 | 0.54 | 0.51 | 0.00 | 0.92 | 0.01 | 0.48 |
| enc_L16_stats | 0.61 | 0.91 | 0.67 | 0.61 | 0.53 | 0.00 | 0.92 | 0.01 | 0.49 |
| enc_L24_mean | 0.47 | 0.87 | 0.41 | 0.64 | 0.51 | 0.00 | 0.58 | 0.06 | 0.37 |
| enc_L24_stats | 0.47 | 0.85 | 0.43 | 0.66 | 0.47 | 0.00 | 0.61 | 0.25 | 0.35 |
| enc_last_mean | 0.66 | 0.91 | 0.63 | 0.78 | 0.65 | 0.00 | 0.81 | 0.33 | 0.52 |
| enc_last_stats | 0.63 | 0.89 | 0.62 | 0.74 | 0.58 | 0.00 | 0.78 | 0.15 | 0.51 |
| proj_mean | 0.62 | 0.89 | 0.60 | 0.74 | 0.61 | 0.00 | 0.75 | 0.26 | 0.49 |

### Nearest neighbours, a few queries

| query | CLAP | enc_last_mean | proj_mean |
|---|---|---|---|
| voice/AK-spoken: AK_SpokenWord_1.wav | AK-spoken:AK_SpokenWord_no2.wav; AK-spoken:AK_SpokenWord_Stars.wav; AK-spoken:AK_SpokenWord_Lets_go.wav | AK-spoken:AK_SpokenWord_no2.wav; AK-spoken:AK_SpokenWord_Lets_go.wav; AK-spoken:AK_SpokenWord_Stars.wav | AK-spoken:AK_SpokenWord_no2.wav; AK-spoken:AK_SpokenWord_Stars.wav; AK-spoken:AK_SpokenWord_Lets_go.wav |
| voice/AK-spoken: AK_SpokenWord_Lets_go.wav | AK-spoken:AK_SpokenWord_Stars.wav; AK-spoken:AK_SpokenWord_Disappear.wav; AK-spoken:AK_SpokenWord_1.wav | AK-spoken:AK_SpokenWord_Disappear.wav; AK-spoken:AK_SpokenWord_Stars.wav; AK-spoken:AK_SpokenWord_no2.wav | AK-spoken:AK_SpokenWord_Disappear.wav; AK-spoken:AK_SpokenWord_Stars.wav; Veela:Siren_Spoken_Trust_Me.wav |
| voice/AK-spoken: AK_SpokenWord_no2.wav | AK-spoken:AK_SpokenWord_1.wav; AK-spoken:AK_SpokenWord_Stars.wav; AK-spoken:AK_SpokenWord_Lets_go.wav | AK-spoken:AK_SpokenWord_1.wav; AK-spoken:AK_SpokenWord_Lets_go.wav; AK-spoken:AK_SpokenWord_Disappear.wav | AK-spoken:AK_SpokenWord_1.wav; AK-spoken:AK_SpokenWord_Lets_go.wav; AK-spoken:AK_SpokenWord_Stars.wav |
| voice/CS: CSS_Atmosphere_Am_05.wav | CS:CSS_Atmosphere_Am_01.wav; CS:CSS_Atmosphere_D#m_02.wav; CS:CSS_Atmosphere_Dm_01.wav | CS:CSS_Atmosphere_Am_01.wav; CS:CSS_Atmosphere_D#m_02.wav; CS:CSS_Atmosphere_F#m_01.wav | CS:CSS_Atmosphere_Am_01.wav; CS:CSS_Atmosphere_F#m_01.wav; CS:CSS_Atmosphere_D#m_02.wav |
| voice/CS: CSS_Atmosphere_Dm_01.wav | CS:CSS_Atmosphere_Fm_02.wav; CS:CSS_Atmosphere_Am_01.wav; CS:CSS_Atmosphere_D#m_02.wav | CS:CSS_Atmosphere_Fm_02.wav; CS:CSS_Atmosphere_Dm_06.wav; CS:CSS_Atmosphere_Fm_06.wav | CS:CSS_Atmosphere_Fm_02.wav; CS:CSS_Atmosphere_Dm_06.wav; CS:CSS_Atmosphere_Fm_06.wav |
| voice/CS: CSS_Atmosphere_F#m_01.wav | CS:CSS_Atmosphere_Dm_06.wav; CS:CSS_Atmosphere_Fm_02.wav; CS:CSS_Atmosphere_Dm_01.wav | CS:CSS_Atmosphere_Dm_06.wav; CS:CSS_Atmosphere_Dm_01.wav; CS:CSS_Atmosphere_Fm_02.wav | CS:CSS_Atmosphere_Dm_06.wav; CS:CSS_Atmosphere_Dm_01.wav; CS:CSS_Atmosphere_Fm_02.wav |
| voice/CS: CSS_Atmosphere_Fm_06.wav | CS:CSS_Atmosphere_Fm_02.wav; CS:CSS_Atmosphere_Am_01.wav; CS:CSS_Atmosphere_Dm_01.wav | CS:CSS_Atmosphere_Fm_02.wav; CS:CSS_Atmosphere_Dm_01.wav; CS:CSS_Atmosphere_Dm_06.wav | CS:CSS_Atmosphere_Fm_02.wav; CS:CSS_Atmosphere_Dm_01.wav; CS:CSS_Atmosphere_F#m_01.wav |
| voice/Soprano: Soprano_Vocal_Phrase_Female_Am | Soprano:Soprano_Vocal_Phrase_Female_; Ethno:Ethno_Female_Vocal_Phrase_Am; Soprano:Soprano_Vocal_Phrase_Female_ | Ethno:Ethno_Female_Vocal_Phrase_Am; Soprano:Soprano_Vocal_Phrase_Female_; Soprano:Soprano_Vocal_Phrase_Female_ | Ethno:Ethno_Female_Vocal_Phrase_Am; Soprano:Soprano_Vocal_Phrase_Female_; Ethno:Ethno_Female_Vocal_Phrase_Am |

## Captions

| group | file | length | Qwen2-Audio caption | time | tokens/s | CLAP chips |
|---|---|---|---|---|---|---|
| voice/AK-spoken | AK_SpokenWord_1.wav | 0.5 s | It is the sound of a single-lens reflex camera. | 7.2 s | 1.81 | glitch 0.18, bell 0.12, noise 0.11 |
| voice/AK-ahh | AK_Ahhhh_B4.wav | 3.5 s | It is the sound of a human voice singing in a high pitch with a long release and reverb. | 10.5 s | 2.1 | vocal 0.66, vocal chop 0.36, brass 0.32 |
| voice/CF | CFVA_BassTone_A#_01.wav | 24.0 s | It is a haunting, ethereal sound that seems to come from another world. | 10.8 s | 1.57 | synth pad 0.53, vocal 0.43, riser 0.37 |
| voice/HD | HDVA_Adlib_01_Amin_170BPM.wav | 12.7 s | The sound is that of a female singing in a keyless manner with a slight breathy quality and no specific mood. | 12.6 s | 1.99 | vocal 0.64, vocal chop 0.40, brass 0.36 |
| voice/CS | CSS_Atmosphere_Am_01.wav | 74.1 s | This sound is a blend of ethereal female vocals, string instruments, and synthesizers creating an atmospheric, cinematic soundscape reminiscent of something you might hear in a science fiction movie. | 20.3 s | 1.82 | synth pad 0.48, vocal 0.41, synth lead 0.36 |
| voice/Veela | Siren_CEbDDb_128bpm_Adlib1.wav | 7.5 s | The sound is of a female singing in English with a neutral mood. | 7.9 s | 1.9 | vocal 0.53, vocal chop 0.24, synth lead 0.22 |
| voice/Soprano | Soprano_Vocal_Phrase_Female_Amin_0 | 9.9 s | The sound is that of a female singing with a long release and autotune effect. | 9.8 s | 1.94 | vocal 0.58, vocal chop 0.15, synth pad 0.14 |
| voice/Ethno | Ethno_Female_Vocal_Phrase_Amin_1.w | 9.0 s | The sound is that of an instrumental slow and dark ambient music piece featuring a synthesizer, guitar, bass, and drums. | 12.7 s | 2.05 | vocal 0.46, vocal chop 0.21, brass 0.18 |
| drum/kick | AM_SHIFT2_Kick (1).wav | 1.6 s | It is a sound effect with no specific source. | 5.8 s | 1.9 | kick drum 0.65, tom drum 0.56, 808 bass 0.53 |
| drum/snare | AM_SHIFT2_Snare (1).wav | 0.5 s | It is the sound of a liquid being sprayed. | 5.7 s | 1.94 | glitch 0.50, snare drum 0.38, vocal chop 0.35 |
| drum/hat | AM_SHIFT2_Hat (1).wav | 0.4 s | It is the sound of a glass being tapped. | 5.7 s | 1.94 | hi-hat 0.69, shaker 0.41, 808 bass 0.30 |
| foley/footsteps | Carpet_Boots_FullStep_01.wav | 0.4 s | It is the sound of someone walking on a hard surface with their boots. | 7.7 s | 2.09 | kick drum 0.25, foley 0.23, tom drum 0.20 |
| foley/ambience | Ambience Airplane Flight Interior  | 250.9 s | It is the sound of industrial machinery, specifically a large fan or an engine running on idle. | 12.8 s | 1.57 | drone 0.31, noise 0.22, ambience 0.19 |
| foley/whoosh | Whoosh Airy Thwip Chamber Reverber | 4.1 s | It is the sound of a swishing noise followed by a long silence. | 8.1 s | 1.97 | whoosh 0.57, vocal chop 0.39, impact 0.33 |
| foley/gun | AK47_Foley_01.wav | 2.7 s | The sound is of a person opening and closing a car door. | 7.3 s | 1.91 | foley 0.28, synth pad 0.13, riser 0.13 |
| foley/animal | Parrot_African_Grey_bleep_single.w | 0.3 s | It is the sound of a duck quacking. | 5.8 s | 1.91 | vocal chop 0.18, tom drum 0.18, shaker 0.13 |

## Reading

**Cost.** The latent pass costs 1.2 s per file whatever the length — the encoder always sees a 30-s window — nine times CLAP's 0.14 s, 38 h for the library. A caption costs 6–20 s per file (a longer clip is more audio tokens to prefill; decoding runs at 1.9 tokens/s): 12 days for the library, a folder's worth per hour.

**The latent axis: no-go.** Thirteen poolings — five encoder layers (4, 8, 12, 16, 24) × mean and mean‖std, the encoder output both ways, the projector output — against CLAP on 102 files in 16 groups:

- *Speaker identity*, the hypothesis of §5.3, does not appear. Among the 52 voice files no latent beats CLAP on "same singer in the top 5" beyond noise (the best is 0.67 at layer 16 with statistics pooling, CLAP 0.64); **none** finds Amy Kirkpatrick singing from Amy Kirkpatrick speaking — 0 of 5 queries, for every representation, CLAP included; on the two singers sharing phrases and key, CLAP's 0.92 is matched (layer 16) and never beaten. Content and style dominate this encoder as much as they dominate CLAP.
- Elsewhere the latents are the same or worse: family separation 0.85–0.91 against 0.93, foley categories 0.47–0.65 against 0.67, group separation far weaker (0.01–0.33 against 0.44). The one consistent win is drum one-shots (0.78 against 0.66 for the encoder output), which the DSP axes already cover at no model cost.
- Their neighbourhoods overlap CLAP's by about half (Jaccard 0.35–0.52): different, not better — noise in §5.3's terms, not complementary divergence.

Decision: no vocal-semantic axis; §5.1's optional row stays unbuilt and node X2 is closed. What would reopen it is a model trained *for* speaker or singer identity (an x-vector / ECAPA-style embedding: ~20 M parameters, milliseconds per file), not a captioning model's encoder.

**Captions: go — opt-in, scope-sized.** Right and useful on material longer than a few seconds (a breathy female adlib; ethereal vocals with strings and synths; an engine idling; boots on a hard surface; a swish, then silence); wrong on sub-second hits, which the 30-s window drowns in silence (a kick "with no specific source", a snare "liquid being sprayed", a parrot "duck quacking", a 0.5-s spoken word "an SLR camera"). CLAP's chips are the better label for one-shots; the sentence adds most where the chips are crudest — long, layered, vocal material. Built as the Recompute tab's *Qwen2-Audio captioning* toggle (off by default) and `crate-caption`: one row per sample in `text_tags`, shown on the Attributes tab, stale when the file's content changes.
