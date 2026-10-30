# Speaker Sync: How-To Guide

`speaker_sync.py` measures how late each Sendspin player's sound comes out compared to a reference
player. It then tells you what static delay to give each player so they all play in sync.

You need a MacBook, the Sendspin players you want to sync, and about 2 minutes per speaker.

---

## How it works

- **The MacBook is both the reference player and the microphone.** You carry it to each speaker
  and compare that speaker against the Mac. Each speaker is then synced to the Mac, which puts
  them all in sync with each other.
- **It measures hardware lag, not your distance from the speaker.** You hold the Mac right next to each speaker
  while measuring, so sound travel time is ~0. That gives you the lag of the device itself
  (TV processing, Bluetooth, DACs, Android audio). You can optionally add distance compensation for
  one listening spot at the end (see step 5).
- **Sendspin's delay setting:**
  - A **positive** static delay makes that player output audio **earlier**, which makes up for slow hardware.
  - Allowed range: **0–5000 ms**. Negative values aren't allowed.
  - The `summary` command handles both rules for you.

---

## One-time setup

1. Clone/pull this repo on the Mac and install the shared env from the repo root:
   ```sh
   uv sync
   ```
2. **Microphone permission:** the first time you run `measure`, macOS asks to let your terminal app
   (Terminal, iTerm, VS Code…) use the mic. Allow it. If you missed the prompt, go to System Settings →
   Privacy & Security → Microphone.
3. **Pick the right mic:** the script records from the Mac's **default input**. In System Settings → Sound →
   Input, choose **MacBook Pro Microphone**. AirPods or another headset may have taken over as the input.
4. If Control Center shows a **Mic Mode**, set it to **Standard**, not Voice Isolation.

All commands below run from the **repo root**.

---

## Step 1: Generate the test track

```sh
uv run python speaker-sync/speaker_sync.py generate
```

This writes `speaker-sync/chirps.wav`: a short 50 ms "chirp" sweep once per second, 30 seconds long.
Change the length with `--seconds 60`.

## Step 2: Play the chirps to every player, including the Mac

The Mac **has to be playing as a Sendspin player**, so its timing matches everything else.
Playing the file locally in QuickTime won't work.

**Option A: quick Sendspin party server**
```sh
# terminal 1: the server, which loops the file
uvx sendspin serve speaker-sync/chirps.wav

# terminal 2: make the Mac a player, which finds the server automatically
uvx sendspin
```
Your other players (Pi/ESP/daemon clients) should find the server on the network. If one doesn't
join, point it at the server with `--url ws://<server-ip>:8927/sendspin`, or have the server reach
out to it with `--client ws://<player-ip>:8927/sendspin`.

**Option B: Music Assistant**
Add `chirps.wav` to a local-files library, turn on **repeat**, and play it to a group that includes
the Mac and all the speakers you're syncing.

**Check before measuring:**
- Every speaker is clicking/chirping once per second.
- **Note each player's current static delay.** You'll pass it in with `--delay`, so you don't have
  to reset anything to 0 first.
- The Mac's own Sendspin delay is the `--ref-delay`. Usually it's 0.

## Step 3: Measure each speaker

For each speaker:

1. **Place the MacBook right next to the speaker**, within ~1 ft, with the screen open. The mics are
   near the top/keyboard.
2. **Set the volumes:**
   - The speaker should be clearly louder than the Mac.
   - The Mac should be around 30–50%, clearly audible.
   - Neither should be distorting.
   - **Don't touch the Mac's volume during a measurement.**
3. Run (pick any name for the speaker):
   ```sh
   uv run python speaker-sync/speaker_sync.py measure kitchen --delay 0
   ```
4. Follow the prompts:
   1. **"Mute 'kitchen'…"**: mute or turn down **only that speaker**. Use the player's mute in Music Assistant,
      its volume knob, or input switching. The Mac keeps playing. Press **Enter**. It records the
      Mac alone for 5 s.
   2. **"Unmute 'kitchen'…"**: unmute it, wait until you hear it, then press **Enter**. It records
      both for 8 s.
5. **Stay quiet and still while it records.** Talking or moving the Mac lowers the accuracy.

Example output:
```
kitchen:
  cycles: 7 (7 consistent)
  per-cycle offsets (ms): [88.5, 88.4, 88.6, 88.5, 88.5, 88.4, 88.6]
  target level vs reference: 2.50x
  offset: +88.5 ms (late), spread 0.1 ms
  saved: hardware latency +88.5 ms vs reference -> results.json
```

**Reading the output:**

| Field | Meaning | Good value |
|---|---|---|
| `offset` | How much later (+) or earlier (−) the speaker plays than the Mac, at its current delay | Anything, since this is what you're measuring |
| `spread` | How much the offset varies from chirp to chirp | **< 2 ms** is great. **> 5 ms** means its latency jitters. |
| `cycles (N consistent)` | How many chirps agreed with each other | Most or all of them |
| `target level vs reference` | How loud the speaker was compared with the Mac | **> 1x**. Below ~0.5x, move closer or turn the speaker up. |

**Useful options:**

| Option | Use it when |
|---|---|
| `--delay MS` | The speaker already has a static delay set. **Always pass its current value.** |
| `--ref-delay MS` | The Mac's Sendspin player has a delay other than 0 |
| `--distance-ft FT` | You can't get the Mac close, e.g. a ceiling speaker. Subtracts ~0.9 ms per foot. |
| `--ref-seconds` / `--target-seconds` | You want longer recordings for more chirps. Defaults are 5 and 8. |
| `--plot` | Shows a graph of the detected chirps, to sanity-check a strange result |

Running `measure` again with the same name overwrites that speaker's result.

## Step 4: Get the recommended delays

```sh
uv run python speaker-sync/speaker_sync.py summary
```
```
player            latency vs ref  distance adj  set delay to
macbook                  +0.0 ms       +0.0 ms        0.0 ms
kitchen                 +88.5 ms       +0.0 ms       88.5 ms
```
- The **latest** speaker gets the **largest** delay, so it starts earlier and catches up.
- Everything is shifted so the smallest delay is 0.
- If the Mac isn't called `macbook` in your setup, the name doesn't matter. Change it with `--ref-name`.

## Step 5 (optional): Align at one listening spot

Step 4 syncs the **speakers themselves**, which is the right choice for whole-house audio where people
are moving around. If you want the sound to line up at one spot, like the dance floor or a doorway between
rooms, measure the distance in feet from each speaker to that spot:

```sh
uv run python speaker-sync/speaker_sync.py summary --spot macbook=6 living=14 kitchen=22
```

Closer speakers get less delay, about 0.9 ms per foot closer, so all the sound arrives at that spot at
the same moment. It's only correct at that spot. Use this only if the Mac will stay part of the group
(include the Mac in `--spot`), or skip it entirely if the Mac was just the measuring tool.

## Step 6: Apply the delays

Set each player's static/output delay to the **"set delay to"** value, using whichever applies:
- **Music Assistant:** the player's settings, where it's the delay/sync offset setting.
- **Sendspin CLI/daemon:** `--static-delay-ms 88.5`, or `static_delay_ms` in
  `~/.config/sendspin/settings-daemon.json`, then restart the service.
- **Sendspin terminal client (`uvx sendspin`):** it has keyboard controls for adjusting the delay live.

**Don't forget the Mac (reference) itself** if its recommended value isn't 0.

## Step 7: Verify

Re-measure each speaker, passing the **new** delays:
```sh
uv run python speaker-sync/speaker_sync.py measure kitchen --delay 88.5 --ref-delay 0
```
The `offset` should now read **~0 ms (±1–2 ms)**. If a speaker is still off by a consistent amount, the
delay probably didn't apply. Check that the player restarted or picked up the new setting.

---

## How far off is acceptable?

| Offset | In the same room | Between rooms |
|---|---|---|
| < 5 ms | Sounds perfect | Perfect |
| 10–20 ms | Slightly blurry or "wide" sounding | Unnoticeable |
| > 30 ms | Distinct echo | Usually fine up to ~30 ms |

---

## Bluetooth speakers and other jittery devices

Bluetooth (e.g. a Pi bridging to a JBL) and Android devices often have **inconsistent** latency:
- `spread` is large, or you get the "inconsistent cycles" warning.
- The latency can change **every time the device reconnects**, so measure again after re-pairing or rebooting.
- A static delay fixes the average, not the jitter. Keep those speakers in their own areas so the
  remaining sync error isn't noticeable.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Couldn't find the reference chirps` | The Mac isn't playing through Sendspin, its volume is too low, or the mic is muted, wrong or blocked by permissions. Re-check setup steps 2–3. Run with `--plot` and check the first section for regular spikes. |
| `Reference-only section too short` / `Target section too short` | Increase `--ref-seconds` / `--target-seconds`. You need at least 2–3 chirps in each part. |
| `WARNING: inconsistent cycles` | The speaker is too quiet (check `target level`), the room is noisy, someone touched the volume, or the latency really is jittery. Move closer, turn the speaker up and try again. |
| `target level` well below 1x | Turn the speaker up or the Mac down, and move the Mac closer to the speaker. |
| Result is ~0 ms but it clearly sounds off | The speaker wasn't actually muted during the first part, so it got mixed into the "Mac only" recording. Make sure you mute **the speaker being measured**, not the Mac. |
| Offset near **±500 ms** | Chirps are 1 s apart, so offsets beyond ±500 ms wrap around and look like a smaller value. That's rare, but it can happen with very slow Bluetooth or Android setups. Set a rough delay by ear first, then measure. |
| `PortAudio` / input device errors | Check the default input in System Settings → Sound, close apps holding the mic (Zoom, etc.), and re-run. |
| Strange result you want to look at again | Recordings are saved to `speaker-sync/recordings/<name>.wav`. Re-analyze one with `uv run python speaker-sync/speaker_sync.py analyze speaker-sync/recordings/kitchen.wav --ref-end 5 --target-start 6 --plot`. Read the exact split points from `results.json` as `ref_end` / `target_start`, which are in samples at 48 kHz, and divide by 48000 to get seconds. |

---

## Files

| File | What it is |
|---|---|
| `speaker_sync.py` | The script (`generate`, `measure`, `analyze`, `summary`) |
| `chirps.wav` | The test track, created by `generate` |
| `results.json` | Measured latency per speaker, used by `summary` |
| `recordings/` | Raw mic recordings for each measurement (git-ignored) |

## How the measurement works

1. **Find the reference.** The chirp is a 500 Hz–8 kHz sweep. The script finds each chirp in the recording
   by matching it against the known sweep. This finds it to well under 1 ms, even with room echoes.
2. **Learn the Mac's pattern.** From the Mac-only part, it fits the exact chirp spacing (correcting for
   tiny differences between clocks) and averages the Mac's recorded cycle, including the room's echoes.
3. **Isolate the speaker.** In the both-playing part, it subtracts that Mac-only cycle from each second
   of recording. Only the speaker being measured is left, so its chirp can be found even if it lands right on
   top of the Mac's.
4. **Calculate the delay.** The gap between where the speaker's chirp lands and where the Mac's should be,
   checked over every chirp, is the offset. The script takes the median and removes outliers.
