"""Measure how late each Sendspin speaker plays relative to a reference player.

Workflow:
  1. generate  -> chirps.wav (one short chirp per second), played to every player via Sendspin
  2. measure   -> put the MacBook (reference player + mic) next to one speaker, follow the prompts
  3. summary   -> recommended static delay for every player

Sendspin sign convention (checked in aiosendspin's compute_play_time): a positive delay makes
the player output audio *earlier*, to compensate for hardware lag. Allowed range is 0-5000 ms.
"""

import argparse
import json
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile
from scipy.signal.windows import tukey

HERE = Path(__file__).parent
RESULTS = HERE / "results.json"
RECORDINGS = HERE / "recordings"

FS = 48_000
PERIOD = FS  # one chirp per second, so offsets up to +/-500 ms are unambiguous
CHIRP_S = 0.05
FT_PER_MS = 1.125  # speed of sound, ~343 m/s


def make_chirp():
    t = np.arange(int(CHIRP_S * FS)) / FS
    chirp = signal.chirp(t, f0=500, f1=8000, t1=CHIRP_S, method="logarithmic")
    return chirp * tukey(len(chirp), 0.2)


def matched_envelope(x, chirp):
    """Index i of the result = how strongly a chirp starting at sample i is present."""
    return np.abs(signal.hilbert(signal.fftconvolve(x, chirp[::-1], mode="valid")))


def generate(path, seconds):
    chirp = make_chirp()
    mono = np.zeros(PERIOD * seconds)
    for i in range(seconds):
        mono[i * PERIOD : i * PERIOD + len(chirp)] = chirp
    stereo = np.column_stack([mono, mono])
    wavfile.write(path, FS, (stereo * 0.8 * 32767).astype(np.int16))
    print(f"Wrote {path} ({seconds}s). Play it to all players, e.g.: uvx sendspin serve {path}")


def analyze(rec, ref_end, target_start, plot=False):
    """Return per-cycle offsets (ms) of the target relative to the reference.

    rec[:ref_end] contains only the reference player. rec[target_start:] has both. The
    reference's average recorded cycle is subtracted from the second part, which leaves only
    the target. That way the measurement still works when the two are already nearly in sync.
    """
    chirp = make_chirp()
    half = PERIOD // 2

    env = matched_envelope(rec[:ref_end], chirp)
    peaks, _ = signal.find_peaks(env, distance=int(0.8 * PERIOD), height=0.3 * env.max())
    if len(peaks) < 3:
        raise SystemExit("Couldn't find the reference chirps. Is the reference playing and audible?")
    # Fit the chirp grid (start + true period) so it can be extrapolated into the second part
    n = np.round((peaks - peaks[0]) / PERIOD)
    period, start = np.polyfit(n, peaks, 1)

    def cycle_starts(lo, hi):
        ks = np.arange(-1, int(len(rec) / period) + 2)
        starts = np.round(start + ks * period).astype(int) - half
        return [s for s in starts if s >= lo and s + PERIOD <= hi]

    ref_cycles = [rec[s : s + PERIOD] for s in cycle_starts(0, ref_end)]
    if len(ref_cycles) < 2:
        raise SystemExit("Reference-only section too short; record it for longer.")
    template = np.mean(ref_cycles, axis=0)
    ref_level = matched_envelope(template, chirp).max()

    offsets, levels, found = [], [], []
    for s in cycle_starts(target_start, len(rec)):
        residual_env = matched_envelope(rec[s : s + PERIOD] - template, chirp)
        i = int(np.argmax(residual_env))
        offsets.append((i - half) / FS * 1000)
        levels.append(residual_env[i] / ref_level)
        found.append(s + i)
    if not offsets:
        raise SystemExit("Target section too short; record it for longer.")

    if plot:
        import matplotlib.pyplot as plt

        full = matched_envelope(rec, chirp)
        t = np.arange(len(full)) / FS
        plt.figure(figsize=(12, 4))
        plt.plot(t, full, lw=0.6, label="matched filter")
        plt.vlines([(start + k * period) / FS for k in range(int(len(rec) / period) + 1)],
                   0, full.max(), colors="tab:green", alpha=0.4, label="reference")
        plt.vlines(np.array(found) / FS, 0, full.max(), colors="tab:red", alpha=0.6, label="target")
        plt.axvline(ref_end / FS, color="k", ls="--")
        plt.axvline(target_start / FS, color="k", ls="--")
        plt.xlabel("seconds")
        plt.legend()
        plt.tight_layout()
        plt.show()

    return np.array(offsets), np.array(levels)


def report(offsets, levels):
    median = float(np.median(offsets))
    good = np.abs(offsets - median) < 5  # drop cycles that locked onto something else
    spread = float(np.std(offsets[good])) if good.sum() > 1 else 0.0
    print(f"  cycles: {len(offsets)} ({good.sum()} consistent)")
    print(f"  per-cycle offsets (ms): {np.round(offsets, 1).tolist()}")
    print(f"  target level vs reference: {np.median(levels):.2f}x")
    print(f"  offset: {median:+.1f} ms ({'late' if median > 0 else 'early'}), spread {spread:.1f} ms")
    if good.sum() < len(offsets) * 0.7 or spread > 5:
        print("  WARNING: inconsistent cycles. The target may be too quiet, or its latency jitters (Bluetooth/Android).")
    return float(np.median(offsets[good])), spread, int(good.sum())


def record_guided(name, ref_seconds, target_seconds):
    import sounddevice as sd

    chunks = []
    recorded = lambda: sum(len(c) for c in chunks)
    with sd.InputStream(samplerate=FS, channels=1, callback=lambda d, *_: chunks.append(d[:, 0].copy())):
        input(f"Mute '{name}' so only the reference (this Mac) plays, then press Enter...")
        a = recorded()
        print(f"Recording reference for {ref_seconds}s...")
        sd.sleep(ref_seconds * 1000)
        ref_end = recorded()
        input(f"Unmute '{name}' (leave the reference playing) and press Enter once it's audible...")
        target_start = recorded()
        print(f"Recording both for {target_seconds}s...")
        sd.sleep(target_seconds * 1000)
    rec = np.concatenate(chunks)[a:]
    return rec, ref_end - a, target_start - a


def load_results():
    return json.loads(RESULTS.read_text()) if RESULTS.exists() else {}


def measure(args):
    rec, ref_end, target_start = record_guided(args.name, args.ref_seconds, args.target_seconds)
    RECORDINGS.mkdir(exist_ok=True)
    wav = RECORDINGS / f"{args.name}.wav"
    wavfile.write(wav, FS, rec.astype(np.float32))

    print(f"\n{args.name}:")
    offset, spread, n = report(*analyze(rec, ref_end, target_start, args.plot))
    offset -= args.distance_ft / FT_PER_MS  # sound travel time from the speaker to the mic
    # Hardware lateness relative to the reference, with both players' current delays removed
    latency = offset + args.delay - args.ref_delay

    results = load_results()
    results[args.name] = dict(latency_ms=round(latency, 2), spread_ms=round(spread, 2), cycles=n,
                              measured_with_delay_ms=args.delay, ref_delay_ms=args.ref_delay,
                              recording=str(wav.relative_to(HERE)), ref_end=ref_end, target_start=target_start)
    RESULTS.write_text(json.dumps(results, indent=2))
    print(f"  saved: hardware latency {latency:+.1f} ms vs reference -> {RESULTS.name}")


def analyze_file(args):
    fs, rec = wavfile.read(args.wav)
    if fs != FS:
        raise SystemExit(f"Expected {FS} Hz, got {fs}")
    rec = rec.astype(np.float64)
    if rec.ndim > 1:
        rec = rec.mean(axis=1)
    report(*analyze(rec, int(args.ref_end * FS), int(args.target_start * FS), args.plot))


def summary(args):
    results = load_results()
    latency = {args.ref_name: 0.0} | {k: v["latency_ms"] for k, v in results.items()}

    spot = {}
    for item in args.spot or []:
        k, v = item.split("=")
        spot[k] = float(v)
    farthest = max(spot.values(), default=0.0)
    # A speaker closer to the listening spot must play later, i.e. get less delay
    adjust = {k: -(farthest - spot[k]) / FT_PER_MS if k in spot else 0.0 for k in latency}
    if spot and (missing := set(latency) - set(spot)):
        print(f"No distance given for {sorted(missing)}; they get no distance adjustment.")

    raw = {k: latency[k] + adjust[k] for k in latency}
    shift = -min(raw.values())  # Sendspin delays can't be negative
    print(f"{'player':<16}{'latency vs ref':>16}{'distance adj':>14}{'set delay to':>14}")
    for k in sorted(raw, key=lambda k: raw[k]):
        print(f"{k:<16}{latency[k]:>+13.1f} ms{adjust[k]:>+11.1f} ms{raw[k] + shift:>11.1f} ms")
    print("\nSet these as each player's static/output delay (Music Assistant player settings,")
    print("--static-delay-ms, or static_delay_ms in settings-daemon.json). Re-measure to verify: offsets should be ~0.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="write the chirp test track")
    g.add_argument("--out", type=Path, default=HERE / "chirps.wav")
    g.add_argument("--seconds", type=int, default=30)

    m = sub.add_parser("measure", help="record and measure one speaker against the reference")
    m.add_argument("name")
    m.add_argument("--delay", type=float, default=0.0, help="target's current static delay (ms)")
    m.add_argument("--ref-delay", type=float, default=0.0, help="reference's current static delay (ms)")
    m.add_argument("--distance-ft", type=float, default=0.0, help="mic-to-speaker distance to subtract")
    m.add_argument("--ref-seconds", type=int, default=5)
    m.add_argument("--target-seconds", type=int, default=8)
    m.add_argument("--plot", action="store_true")

    a = sub.add_parser("analyze", help="re-analyze a saved recording")
    a.add_argument("wav", type=Path)
    a.add_argument("--ref-end", type=float, required=True, help="seconds where the reference-only part ends")
    a.add_argument("--target-start", type=float, required=True, help="seconds where the target is audible")
    a.add_argument("--plot", action="store_true")

    s = sub.add_parser("summary", help="recommended delay for every measured player")
    s.add_argument("--ref-name", default="macbook")
    s.add_argument("--spot", nargs="*", metavar="NAME=FEET",
                   help="distance from each speaker to the spot to align at, e.g. living=14 kitchen=22")

    args = p.parse_args()
    {"generate": lambda: generate(args.out, args.seconds), "measure": lambda: measure(args),
     "analyze": lambda: analyze_file(args), "summary": lambda: summary(args)}[args.cmd]()


if __name__ == "__main__":
    main()
