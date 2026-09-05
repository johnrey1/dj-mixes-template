# Track-transition detection for DJ mixes — approaches considered

Research notes for `--suggest-timestamps` in [`scripts/add_mix.py`](../scripts/add_mix.py).
Analysis only — **no behaviour in the script changes as part of this document.** The
appendix sketches a recommended implementation for a future change; it is not applied.

---

## 1. Background: what the tool does today and how it fails

`--suggest-timestamps` proposes a start time for each tracklist entry that has no
explicit `[mm:ss]` prefix. The pipeline:

1. **One ffmpeg decode** to mono 8 kHz signed-16 PCM (`decode_mono_pcm`), reused for the
   waveform peaks.
2. **`_novelty_curve`** — a log-energy envelope in 0.5 s bins, 3-tap smoothed, then the
   **positive first difference**. Peaks mark moments where *loudness rises*.
3. **`suggest_timestamps`** — evenly split the un-anchored span between two known
   timestamps across the tracks in that gap, then **`_nudge_to_novelty`** moves each
   guess to the strongest loudness-rise spike within ± half a slot, without crossing a
   neighbour, falling back to the even split when nothing stands out.

### The observed failure

Against a real live set with a known-correct human tracklist, the tool produced:

```
6:55  Mind of the Wonderful - Blank & Jones (Hiver & Hammer Remix)
6:56  Summer Calling - Andain (Airwave Club Mix)
```

Two adjacent tracks one second apart, and the real transition for the second track was
**noticeably earlier** with **no loudness rise at all** at the suggested point.

This is the expected outcome, not a bug in the arithmetic. **Beatmatching and
gain-matching are the craft of mixing** — a competent transition holds the perceived
loudness flat while one record is swapped under another over 8–64 bars. A "loudness goes
up" detector is looking for the one artefact a good DJ is specifically working to remove.
When it does fire, it fires on drops, breakdown re-entries and crowd noise — features
that have nothing to do with track boundaries.

### What actually cues a transition

Human listeners (and DJs reading a waveform) rely on **spectral and harmonic** change,
not level:

- a new **kick / bass pattern** entering (sub-band energy *distribution* shifts even when
  total level does not),
- a **key change** or new chord movement,
- a new **vocal or melodic hook**,
- a broad **timbre / instrumentation** shift.

The user's hypothesis — that frequency-content change is detectable under a
level-matched blend — is correct, and is exactly what the music-information-retrieval
(MIR) field calls *structure segmentation*. Everything below is about picking a better
signal than `log-energy delta`.

---

## 2. Executive summary & recommendation

| Question | Short answer |
|---|---|
| Is a better signal than loudness-delta available? | **Yes.** Spectral flux, and especially an **MFCC + chroma self-similarity "checkerboard" novelty** (Foote 2000), are the standard, well-understood tools and are robust to level-matching. |
| Will it hit the exact second? | **No.** Human annotators disagree by **~9 s (std)** on mix boundaries; the best published unsupervised methods report results that are *usable*, not second-accurate; even methods that align against the original tracks show ~5–14 s median error. |
| Is there a genuinely accurate route? | Only two: (a) the DJ's own software history export (Rekordbox / Serato / Traktor already logged every track load with a timestamp), or (b) **audio fingerprinting against a reference library** — accurate *and* gives the track identity, but online, quota-bound, and blind to unreleased / white-label / edited / self-produced tracks. |
| Recommended change to the script | Replace the log-energy delta inside `_novelty_curve` with a **combined MFCC+chroma checkerboard-novelty curve** (optionally summed with the existing energy novelty as a hybrid). Keep `_nudge_to_novelty` and the even-split fallback **unchanged**. Bump the shared decode to 22 kHz. Add **NumPy** (not librosa) as the one new dependency, or hand-roll the STFT. Reframe the output in help text and README as a *rough draft to hand-correct*, never ground truth. |
| Not worth doing | `aubio` (onsets aren't boundary-selective + install pain); `madmom` as a core dependency (heavy, for a marginal downbeat-snap nicety); `essentia` `SBic` (well-suited algorithm, worst install friction of the group). |

The recommendation is deliberately conservative: **the change is worth making because
the new signal is strictly more relevant to the problem**, but it raises the hit rate
from "usually wrong" to "a decent starting point," not to "correct." Manual entry and DJ
software exports remain the source of truth.

---

## 3. Frequency / spectral-domain signals

### 3.1 Spectral flux (a drop-in replacement for the energy delta)

Spectral flux is the summed positive frame-to-frame increase in magnitude **per
frequency bin**, rather than in total energy. New broadband or percussive content
entering under a flat-level blend still produces flux even when the energy delta is
zero. This is `librosa.onset.onset_strength`:

```python
onset_strength(*, y=None, sr=22050, S=None, lag=1, max_size=1, ref=None,
               detrend=False, center=True, feature=None, aggregate=None, **kwargs)
```

- default `feature` is `melspectrogram`; you can pass `feature=librosa.feature.chroma_stft`
  or `mfcc` to get flux of a different representation;
- `lag=N` computes the difference against the frame *N* steps back — a **long lag
  (1–4 s)** turns beat-level flux into structural novelty;
- `aggregate=np.median` across bins suppresses single-bin spikes.

**Caveat for 4/4 dance music:** at a short lag, flux peaks on *every kick*. It only reads
as *structural* after heavy smoothing or a multi-second lag, at which point it is
essentially a coarse version of the SSM novelty below. Useful, cheap, but blunt on its
own.

**Complexity:** low. One STFT, a difference, a sum, a smooth. Runs comfortably on a
1-hour mono file.

### 3.2 Foote checkerboard novelty over a self-similarity matrix — recommended core

The canonical MIR structure-segmentation method (Foote, *Automatic Audio Segmentation
Using a Measure of Audio Novelty*, 2000):

1. Frame the audio (1–2 s hop) and compute a feature per frame — **MFCC** (timbre) and/or
   **chroma** (harmony); normalise each frame vector.
2. Build the **self-similarity matrix** `S[i,j] = cos(f_i, f_j)` (librosa:
   `librosa.segment.recurrence_matrix(..., mode='affinity')`, or a plain cosine Gram
   matrix).
3. Slide a **Gaussian-tapered checkerboard kernel** (`[[+,−],[−,+]]`) down the diagonal.
   The correlation is high where the region *before* `i` is internally similar, the
   region *after* `i` is internally similar, and the two are dissimilar to each other —
   i.e. a boundary.
4. The kernel half-width sets the timescale. For DJ blends you want **20–45 s**, longer
   than the pop-structure default, so the detector integrates over the whole transition
   region instead of chasing bar-level detail.
5. Peak-pick the novelty curve.

**Why it fits this problem:** it is invariant to level (features are per-frame
normalised), it explicitly models "one homogeneous thing, then a different homogeneous
thing," and MFCC+chroma between them captures exactly the timbre/harmony cues a listener
uses. It is the same family of technique the DJ-specific research below is built on.

**Complexity:** moderate. Feature extraction + an `N×N` matrix where `N` ≈ frames. At a
2 s hop a 1-hour mix is `N ≈ 1800`, so `S` is ~3.2M floats (~26 MB) — trivial. The
novelty curve then plugs into the **existing** `_nudge_to_novelty` unchanged (it already
takes an arbitrary per-bin novelty array + bin size).

### 3.3 Cheaper timbral proxies

Per-frame **spectral centroid / bandwidth / rolloff**, then the smoothed absolute delta.
A new bright hi-hat pattern or a sub-heavy drop moves the centroid even at matched
level. Weaker and noisier than the SSM but ~10 lines and no matrix. Reasonable as an
*extra term* in a hybrid sum, not as the primary signal.

A closely related zero-library trick that works even at 8 kHz: split each 0.5 s bin into
**3–4 log-spaced bands** (sub, low-mid, high-mid, top), take each band's share of total
energy, and detect change in the *ratio vector*. "New kick/bassline enters" shows up
cleanly as the sub band's share jumping while total level holds. This is the cheapest
meaningful improvement over the current curve.

### 3.4 Harmonic-change detection

A **chroma-vector distance** curve (or the Harte *Harmonic Change Detection Function*:
project chroma onto the tonnetz, take the derivative norm). Peaks when the incoming
track sits in a **different key** from the outgoing one — a strong, clean cue.

**Caveat:** harmonic mixing is a thing. A DJ mixing in key (Camelot-adjacent) produces
*little to no* chroma change at the boundary, so this signal is high-precision but
low-recall. Best as a confirming term, not a primary detector. Chroma needs real
low-frequency resolution — **the current 8 kHz decode is too low**; chroma wants
≥ 22 kHz.

---

## 4. Purpose-built prior art for DJ mixes

This exact problem — segment a continuous, beatmatched mix into its tracks — has a small
dedicated literature.

### 4.1 Long-range self-similarity + dynamic programming

Scarfe & Boag, *A Long-Range Self-similarity Approach to Segmenting DJ Mixed Music
Streams* (2013). Unsupervised, deterministic: build a similarity matrix from
kernel-transformed Fourier features designed for EDM, then use **dynamic programming to
find the globally optimal segmentation** of the derived cost matrix, treating long-term
self-similarity as a first-class term (a track that returns to a motif 3 minutes later
should still be "the same segment"). Evaluated on a large hand-labelled corpus of radio
shows / podcasts; the authors report that **~90 % of the cut points produced are usable
in the context of a DJ mix** — "usable," explicitly not sample-accurate. Reference
implementation: `github.com/ecsplendid/DanceMusicSegmentation`.

Takeaway for us: the SSM idea in §3.2 plus a **global DP** over the whole mix (instead of
the current greedy per-gap nudge) is the state of the art for the no-reference case. DP
is a realistic future step but a larger change than swapping the novelty curve.

### 4.2 Mix-to-track subsequence alignment (needs the source tracks)

Kim et al., *A Computational Analysis of Real-World DJ Mixes using Mix-To-Track
Subsequence Alignment*, ISMIR 2020. They built the **1001Tracklists dataset** (1,557
mixes, 13,728 unique tracks, 20,765 transitions, ~1,570 h) and align each *known* source
track against the mix with **subsequence DTW** over **beat-synchronous CENS chroma +
12-D MFCC**, testing all 12 chroma rotations for key-shifted plays. Results: cue-in
points land within a 30 s tolerance **> 80 %** of the time; **median cue-in error
11.4–14.3 s**; best-of-three cue types median **4.2–5.3 s**. They state plainly that the
"ambiguous definition of the boundary and long transitions make it difficult to
annotate," and cite the **~9 s standard deviation of human disagreement** on boundary
position.

Takeaway: even with the original audio of every track in hand, the boundary is a
~5–15 s-wide estimate. Without the source tracks — our case — it is necessarily worse.

### 4.3 Other work

- *Cue Point Estimation using Object Detection* (arXiv 2407.06823, 2024) — treats a
  spectrogram as an image and detects cue regions with a CNN detector. Needs a trained
  model and GPU-ish tooling; out of scope for a local zero-infra CLI.
- *Methods and Datasets for DJ-Mix Reverse Engineering* — survey of unmixing (recovering
  fader curves, EQ, cue points); mostly assumes annotations exist.
- *MixSense: AI Optimisation for Contiguous Music Segmentation at Scale* — production
  segmentation, ML-heavy.

### 4.4 Audio fingerprinting against a reference library (a different problem shape)

Instead of *inferring* a boundary from the signal, **identify each track** and read the
boundary off as a side effect. Slide an overlapping window (10–20 s, ~50 % overlap) along
the mix, fingerprint each window against a catalogue, and mark a boundary where the
winning match ID changes.

- **Services / tools:** [setlist.id](https://setlist.id), [Scanamix](https://www.scanamix.com),
  and the open-source [`tracklistify`](https://github.com/betmoar/tracklistify) all do
  this, most on top of **ACRCloud** (100M+ track index) or Shazam / AcoustID.
- **Pros:** you get the *actual track name*, not just a time; robust to EQ, pitch, layering
  (segment-by-segment beats a single Shazam snapshot); the timestamp is real.
- **Cons:** requires network + an API key + per-query quota/cost; **only finds
  commercially released recordings** — white-labels, unreleased IDs, bootlegs, the DJ's
  own edits, and their own productions simply don't match; and it breaks the tool's
  current *fully offline, four-pip-package* property.
- **Fit:** best offered as a **separate opt-in mode** (`--identify`) for
  released-music sets, not as the default path. It answers a different question ("what is
  this track") that happens to also yield timestamps.

### 4.5 The actually-correct answer, when it exists: DJ software history

If the set was played or recorded in **Rekordbox, Serato, or Traktor**, the software
already logged every track load with a wall-clock timestamp:

- Serato: *History* → export;
- Traktor: the `.nml` history / collection has `PLAYED` entries with timings;
- Rekordbox: the auto-created *History* playlist, per session.

Converting that export to `[mm:ss] Title` prefixes is pure bookkeeping — no DSP, no
error bar. Worth a prominent mention in the README: **if you have a history export, use
it; `--suggest-timestamps` is for sets that were recorded off a mixer with no software
in the chain.**

---

## 5. Python libraries — concrete comparison

Current dependency baseline: `boto3`, `mutagen`, `python-slugify`, `python-dotenv`, plus
the `ffmpeg` binary. Analysis uses **stdlib only** (`array`, `math`). Any option below is
a step up from that; they are ordered by how big a step.

| Library | Functions that apply | Install weight / friction | 1-hour mono OK? | Sample-rate note |
|---|---|---|---|---|
| **NumPy only** (hand-rolled STFT/MFCC/chroma + kernel) | `np.fft.rfft`, a mel/chroma filterbank (~40 lines), cosine SSM, checkerboard correlation | ~15 MB, universal wheels, **no build, no transitive deps** | Yes — STFT of ~28 M samples in a few seconds | Decode at **22 kHz** for chroma; 8 kHz fine for MFCC/flux only |
| **librosa** | `onset.onset_strength` (spectral flux, custom `feature=`, `lag=`), `feature.mfcc`, `feature.chroma_cqt`, `feature.spectral_*`, `segment.recurrence_matrix`, `segment.agglomerative`, Laplacian-segmentation recipe, `beat.beat_track` | Heavy: pulls `numpy`, `scipy`, `numba` (LLVM), `soundfile`, `pooch`, `lazy_loader`, `audioread`. Pure-pip, wheels exist for all major platforms, but first install is ~150 MB and numba can lag new Python releases by weeks | Yes; designed for it | Defaults to 22050; feed it a 22 kHz decode |
| **aubio** | `aubio.onset` (several methods incl. `specflux`), `aubio.tempo` | Light C core **in principle**, but pip wheels are **routinely broken on macOS ARM and Python 3.12+** — needs a source build (`pip install git+https://git.aubio.org/aubio/aubio/`) | Yes | Any |
| **essentia** | **`SBic`** — BIC segmentation over an MFCC feature matrix (coarse `size1/inc1`, fine `size2/inc2`, validation `minLength`); also `NoveltyCurve`, `SpectralPeaks` | Heaviest to install: pip wheels (`essentia`) exist but are **notoriously fragile on macOS Apple Silicon**; conda-forge is the reliable path | Yes | Any |
| **madmom** | RNN/LSTM `OnsetPeakPickingProcessor`, `DBNDownBeatTrackingProcessor` (best-in-class downbeats) | Heavy: Cython build, historically **pinned to old NumPy**, trails new Python versions; needs `cython`, `scipy` | Yes but slower (RNN inference over 1 h) | 44.1 kHz internally |

### Reading of the table

- **NumPy-only** keeps the project's minimal-dependency character while unlocking the SSM
  approach. This is the recommended path. The feature/kernel code is ~60–100 lines and
  well documented in textbooks (Müller, *Fundamentals of Music Processing*, ch. 4).
- **librosa** is the pragmatic choice if the maintainer doesn't mind a fat dependency:
  every building block is one call, well-tested, and you also get `beat_track` for the
  optional downbeat snap. The cost is a genuinely large install and occasional
  Python-version lag from numba.
- **aubio / madmom** only add *onset/beat* tooling. Onsets are not boundary-selective in
  4/4 music, and beat tracking is a usability nicety (see §6), so neither earns its
  install cost as a core dependency.
- **essentia `SBic`** is arguably the most *on-the-nose* algorithm (it is literally
  "segment a feature-matrix into homogeneous regions"), and if it installed cleanly it
  would be a strong contender. On the target platform (a DJ's laptop, often Apple
  Silicon) the install experience is the worst of the group, which disqualifies it for a
  tool meant to be `pip install -r` and go.

---

## 6. Hybrid signal and beat alignment

### 6.1 Combining novelty curves

The energy novelty is not worthless — a *hard cut* (no blend) really is a level jump,
and those exist in plenty of sets. A robust combined signal:

```
novelty = w_ssm * norm(ssm_novelty)          # timbre+harmony change  (primary)
        + w_flux * norm(spectral_flux)        # new content entering
        + w_energy * norm(energy_novelty)     # catches hard cuts
```

with each curve min-max or z-scored to `[0,1]` first. Start `w_ssm=0.6, w_flux=0.25,
w_energy=0.15`. An "agreement" variant — only nudge where at least two curves have a
local peak within ±2 bins — trades recall for precision and is worth exposing as a
toggle. Implementation cost is low once the curves exist; `_nudge_to_novelty` consumes
the summed curve with no change.

### 6.2 Snap to a downbeat

Detection accuracy and *perceived* accuracy are different. A suggestion that lands on a
**phrase boundary** (a 16- or 32-beat multiple) reads as correct to a DJ even when it is
a bar or two off the "true" mix-in, because that is where transitions actually happen.
So: after `_nudge_to_novelty`, quantise each time to the nearest estimated downbeat.

- Cheap version: `librosa.beat.beat_track` → nearest beat (no true downbeat, but
  removes sub-beat jitter).
- Good version: `madmom` `DBNDownBeatTrackingProcessor` → real downbeats, snap to the
  nearest bar or 4-bar mark.

This does **not** improve the hit rate against ground truth, but it markedly improves
how the output *feels*, and it is the single highest-value add-on after the SSM swap.
It does, however, require a beat-tracking dependency, so it is a separate decision from
§5.

---

## 7. Honest assessment of the ceiling

**For hard cuts and short (< 8 bar) blends:** an SSM/flux novelty curve will land within
a few seconds most of the time. A real improvement over today, genuinely useful.

**For long seamless beatmatched, key-matched blends (the 32–64 bar kind):** there is no
"the" transition point *in the signal*, because the mix was engineered not to have one.
For 20–45 seconds, both tracks are audibly present at comparable level. The best you can
honestly report is a **region**, and any single number inside it is a convention, not a
measurement. This is not a tuning problem:

- human annotators disagree by **~9 s (std)**;
- the strongest *no-reference* published method calls 90 % of its points "usable," not
  accurate;
- methods that **align against the original track audio** still show **~5–15 s median
  error**.

A local DSP-only script cannot beat those numbers and should not imply that it does.

### Recommendation

1. **Make the swap (§3.2 + §6.1).** Replace the log-energy delta in `_novelty_curve` with
   a combined MFCC+chroma checkerboard novelty (hybrid-summed with the existing energy
   novelty). This is a contained change: same function boundary, same
   `_nudge_to_novelty`, same even-split fallback. It moves the tool from "detecting the
   wrong thing" to "detecting the right thing, roughly." Add **NumPy** as the one new
   dependency, or hand-roll the STFT to stay at zero.

2. **Bump the shared decode to 22 kHz** in `decode_mono_pcm`. Waveform peaks are
   unaffected (they are downsampled anyway); chroma needs it.

3. **Reframe the output as a draft.** Update `--suggest-timestamps` help text and the
   README: the suggestion is a starting point to hand-correct in `git diff`, not a
   measurement; long blends will be off by 10–30 s; if you have a Rekordbox/Serato/Traktor
   history export, use that instead.

4. **Optional, separately decided:** a `--identify` fingerprinting mode (§4.4) for
   released-music sets, and/or a downbeat snap (§6.2) if a beat-tracker dependency is
   acceptable.

5. **Do not** adopt `aubio`, `madmom`, or `essentia` as core dependencies for this
   feature. If you want the librosa convenience and don't mind the weight, use librosa
   for everything in step 1 instead of hand-rolling — that is the only library trade
   worth making.

Net: the change is worth doing because the signal becomes relevant to the actual task,
but the honest framing is **"better rough draft," not "now it's correct."** Manual entry
and software history exports stay the source of truth.

---

## Appendix — recommended implementation sketch (NOT applied)

Illustrative only. Interfaces match the current code so `_nudge_to_novelty`,
`suggest_timestamps`, and the even-split fallback are untouched.

### A. Decode change

```python
DECODE_SAMPLE_RATE = 22050          # was 8000; chroma needs the extra bandwidth.
                                    # peaks (downsample_maxabs) are unaffected.
# decode_mono_pcm() body is unchanged apart from the constant.
# Memory: ~1 h mono s16 @ 22 kHz ≈ 175 MB in the array('h') — acceptable.
```

### B. New novelty curve (NumPy)

```python
import numpy as np

FEAT_HOP_SECONDS   = 1.0            # feature frame hop
KERNEL_HALF_SECONDS = 32.0          # checkerboard half-width -> ~64 s window,
                                    # long enough to span a full blend

def _spectral_features(samples, ar):
    """(n_frames, n_feat) L2-normalised MFCC+chroma matrix, one row per FEAT_HOP_SECONDS."""
    x = np.frombuffer(samples, dtype=np.int16).astype(np.float32) / 32768.0
    n_fft = 1 << int(np.ceil(np.log2(ar * 0.09)))     # ~90 ms window
    hop   = int(ar * FEAT_HOP_SECONDS)
    win   = np.hanning(n_fft).astype(np.float32)

    frames = []
    for start in range(0, len(x) - n_fft, hop):
        seg = x[start:start + n_fft] * win
        mag = np.abs(np.fft.rfft(seg))
        frames.append(mag)
    S = np.asarray(frames)                             # (n_frames, n_fft/2+1)

    mfcc   = _mfcc(S, ar, n_fft, n_mfcc=13)            # mel filterbank -> log -> DCT
    chroma = _chroma(S, ar, n_fft, n_chroma=12)        # pitch-class filterbank -> sum
    feat = np.hstack([_zscore_cols(mfcc), _zscore_cols(chroma)])
    return feat / (np.linalg.norm(feat, axis=1, keepdims=True) + 1e-9)

def _checkerboard_novelty(feat):
    """Foote 2000: correlate a Gaussian-tapered checkerboard kernel down the SSM diagonal."""
    S = feat @ feat.T                                  # cosine SSM (feat already unit-norm)
    L = int(KERNEL_HALF_SECONDS / FEAT_HOP_SECONDS)
    g = np.exp(-0.5 * (np.arange(-L, L + 1) / (L / 2.0)) ** 2)
    sign = np.outer(np.sign(np.arange(-L, L + 1)), np.sign(np.arange(-L, L + 1)))
    kernel = sign * np.outer(g, g)                     # (2L+1, 2L+1), +/- checkerboard

    nov = np.zeros(len(S))
    for i in range(L, len(S) - L):
        nov[i] = np.sum(S[i - L:i + L + 1, i - L:i + L + 1] * kernel)
    nov[nov < 0] = 0.0
    return nov / (nov.max() + 1e-9)

def _novelty_curve(samples, ar=DECODE_SAMPLE_RATE, bin_seconds=FEAT_HOP_SECONDS):
    """Drop-in replacement. Returns one value per `bin_seconds`; same contract as before
    (list-like, index * bin_seconds == time). Falls back to None when too short."""
    if not samples:
        return None
    feat = _spectral_features(samples, ar)
    if len(feat) < 8:
        return None
    ssm_nov  = _checkerboard_novelty(feat)

    # hybrid: add the old energy delta and spectral flux, each normalised to [0,1]
    energy_nov = _energy_delta_novelty(samples, ar, bin_seconds)   # today's curve, kept
    flux       = _spectral_flux(feat)                              # sum of positive row-diffs
    nov = 0.60 * ssm_nov \
        + 0.25 * _resample_to(flux,       len(ssm_nov)) \
        + 0.15 * _resample_to(energy_nov, len(ssm_nov))
    return list(nov)
```

`_nudge_to_novelty`, `suggest_timestamps`, `bin_secs` derivation, and the even-split
fallback stay **exactly as they are** — they already treat the novelty array as an
opaque per-bin signal.

### C. If librosa is acceptable instead of hand-rolling B

```python
import librosa, numpy as np

def _novelty_curve(samples, ar=DECODE_SAMPLE_RATE, bin_seconds=1.0):
    y = np.frombuffer(samples, dtype=np.int16).astype(np.float32) / 32768.0
    hop = int(ar * bin_seconds)
    mfcc   = librosa.feature.mfcc(y=y, sr=ar, hop_length=hop, n_mfcc=13)
    chroma = librosa.feature.chroma_cqt(y=y, sr=ar, hop_length=hop)
    feat = np.vstack([librosa.util.normalize(mfcc, axis=1),
                      librosa.util.normalize(chroma, axis=1)]).T
    R = librosa.segment.recurrence_matrix(feat.T, mode='affinity', sym=True)
    nov = _checkerboard_novelty_from_ssm(R, half=int(32 / bin_seconds))  # ~10 lines
    flux = librosa.onset.onset_strength(y=y, sr=ar, hop_length=hop,
                                        lag=int(2 / bin_seconds), aggregate=np.median)
    nov = 0.7 * _norm(nov) + 0.3 * _norm(flux[:len(nov)])
    return list(nov)
```

### D. Optional downbeat snap (needs a beat tracker)

```python
# after _nudge_to_novelty in suggest_timestamps:
#   downbeats = beat_tracker(samples, ar)          # librosa.beat.beat_track or madmom DBN
#   t = min(downbeats, key=lambda d: abs(d - t))   # only if |d - t| < ~4 s
```

### E. Docs / help-text change (the important non-code part)

- `--suggest-timestamps` help: "Proposes a **rough** start time per track from a
  timbre/harmony novelty curve. Seamless blends can be off by 10–30 s — review and
  correct in `git diff`. If you have a Rekordbox/Serato/Traktor history export, prefix
  the tracklist with `[mm:ss]` from that instead."
- README "Adding a mix": same caveat, plus a sentence pointing at history exports as the
  accurate path.

---

## Sources

- I. Foote. *Automatic Audio Segmentation Using a Measure of Audio Novelty.* IEEE ICME, 2000.
- T. Scarfe, W. Boag et al. *A Long-Range Self-similarity Approach to Segmenting DJ Mixed
  Music Streams.* AIAI, 2013. Springer:
  <https://link.springer.com/chapter/10.1007/978-3-642-41142-7_24> ·
  ref impl <https://github.com/ecsplendid/DanceMusicSegmentation>
- T. Kim, M. Choi, E. Sacks, Y.-H. Yang, J. Nam. *A Computational Analysis of Real-World
  DJ Mixes using Mix-To-Track Subsequence Alignment.* ISMIR 2020.
  <https://archives.ismir.net/ismir2020/paper/000352.pdf> ·
  <https://arxiv.org/abs/2008.10267>
- *Cue Point Estimation using Object Detection.* arXiv:2407.06823, 2024.
  <https://arxiv.org/abs/2407.06823>
- *Methods and Datasets for DJ-Mix Reverse Engineering.*
  <https://www.researchgate.net/publication/349933466>
- *A Basic Tutorial on Novelty and Activation Functions for Music Structure Analysis.*
  Transactions of ISMIR. <https://transactions.ismir.net/articles/202/files/66ec2062992e8.pdf>
- librosa docs: `onset.onset_strength`
  <https://librosa.org/doc/0.11.0/generated/librosa.onset.onset_strength.html> ·
  `segment.recurrence_matrix`
  <https://librosa.org/doc/0.11.0/generated/librosa.segment.recurrence_matrix.html> ·
  Laplacian segmentation example
  <https://librosa.org/doc/latest/auto_examples/plot_segmentation.html>
- Essentia `SBic`: <https://essentia.upf.edu/reference/streaming_SBic.html>
- madmom: S. Böck et al. *madmom: A New Python Audio and Music Signal Processing Library.*
  ACM MM 2016. <https://github.com/CPJKU/madmom>
- aubio install issues on macOS / new Python: <https://github.com/aubio/aubio/issues/328>,
  <https://github.com/aubio/aubio/issues/344>
- Fingerprinting / tracklist tools: <https://setlist.id/about> ·
  <https://www.scanamix.com/en/> · <https://github.com/betmoar/tracklistify> ·
  ACRCloud <https://en.wikipedia.org/wiki/ACRCloud>
- M. Müller. *Fundamentals of Music Processing* (2nd ed.), ch. 4 (structure & segmentation),
  and the FMP notebooks — reference for the checkerboard-novelty implementation.
