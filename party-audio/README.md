# Party Whole-Home Audio Plan

## Gear
- **Living room:** Sony STR-DN1080 AVR + 5.1. TV → AVR over (e)ARC. Sources plug into the TV.
- **Kitchen (20–25 ft away, wall in between):** Vizio soundbar + wireless sub (HDMI / optical / BT)
- **Extras:** JBL Pulse + other Bluetooth speakers
- **Controller:** MacBook Pro (+ phones)

## Main idea
Use **Wi-Fi streaming as the backbone, not Bluetooth or the TV.**

- Wi-Fi protocols (Chromecast/Google Cast, AirPlay 2) buffer and timestamp audio, so
  every speaker in a group plays in sync.
- Bluetooth adds 100–300 ms of latency that differs per speaker, with no shared clock.
  Mixed-brand BT speakers in the same room will echo.
- The TV/ARC path doesn't help: the DN1080 generally won't send ARC/digital-input audio
  to Zone 2 or network zones, and 25 ft of BT through a wall drops out.

The DN1080 has **Chromecast built-in, AirPlay 2 (after firmware update), and Spotify Connect.**
That makes it the anchor.

## Step 1: Living room (DN1080)
1. Update the AVR firmware (Setup → System Settings → Software Update). AirPlay 2 needs this.
2. Connect it to Wi-Fi. Ethernet is better if the router is nearby.
3. Add it in the **Google Home** app (Chromecast built-in setup), name it "Living Room".
4. Turn on "Network Standby" so it wakes when you cast.
5. Optional: use **2ch Stereo** or **Multi Stereo** sound field so music comes out of all 5.1 speakers.

## Step 2: Kitchen (Vizio soundbar). Which path depends on the model
Look up the model number on the back.

**A. The soundbar has Chromecast built-in** (most SmartCast / Elevate / M-Series / V-Series 2020+)
- Add it in Google Home, name it "Kitchen". Done, go to Step 3.

**B. No Chromecast/Wi-Fi (most likely for older or basic models)**
- Buy a **WiiM Mini (~$90)** or **WiiM Pro (~$150)**. The Pro syncs better and has a nicer DAC.
  Both do Chromecast + AirPlay 2 + Spotify Connect.
- WiiM **optical out → soundbar optical in**. Set the soundbar input to Optical.
- Add the WiiM in Google Home as "Kitchen".
- This is the most reliable fix by far.

**C. Free fallback, but worse**
- Run a 25 ft optical cable (~$10) from the **TV's optical out** to the soundbar. Many TVs turn
  off optical out while ARC/eARC is active, so test it first.
- Or pair the soundbar to the DN1080's **Bluetooth transmit** mode. Range through a wall is
  iffy, the kitchen will lag a bit, and on some Sony units this mutes the AVR speakers. Test before the party.

## Step 3: Create the speaker group
- Google Home → Create speaker group → "Party" = Living Room + Kitchen.
- Cast to "Party" from Spotify, YouTube Music, etc. on any phone or the Mac (Chrome).
- If something sounds off: Google Home → device → Audio → **Group delay correction**
  lets you nudge each speaker's timing.
- AirPlay 2 alternative from the Mac/iPhone: pick both the DN1080 and the WiiM in the
  Music app / Control Center. Both are AirPlay 2, so they stay synced.

## Step 4: What's the source?
**Best: Spotify (or YouTube Music) on a phone, cast to the "Party" group.**
- The audio streams from the cloud to each speaker, not from the phone.
  A phone call or walking out of range won't stop the music.
- Guests can queue songs with a **Spotify Jam** / shared queue, no handing over phones.
- Keep the Mac as the backup / "DJ station" for local files or apps that can't cast.

**If the Mac must be the source** (local files, a DJ app, a browser tab, any app audio):
- Get **Airfoil** (Rogue Amoeba, ~$39, free trial adds noise after 10 min).
  It sends any Mac audio to AirPlay, Chromecast, **and Bluetooth** speakers at once,
  and lets you **adjust the delay per speaker**. It's the only good way to sync mixed gear.

## Step 5: Bluetooth speakers (JBL Pulse, etc.)
- Put them in **separate zones**: patio, bathroom, garage. Not in the living room or kitchen.
  Small lag between separate rooms is unnoticeable. In the same room it's an echo.
- Multiple JBLs: link them with **PartyBoost/Connect+** (JBL app). One phone feeds the
  first, the rest follow it in sync. This only works with compatible JBL models.
- To feed them from the main stream, pair them to the Mac and use Airfoil with per-speaker
  delay. Otherwise just run them standalone from a phone playing the same playlist
  (they won't match the main zone, which is fine in a separate area).
- The Mac can only drive a few BT speakers at once. Keep them close to it.

## Alternative: Mac as the hub with Airfoil (ChatGPT's suggestion)
Mac → HDMI → AVR, and Airfoil sends the same audio over AirPlay/BT to everything else.
- **Works:** Airfoil delays local output to match the remote speakers, and you can adjust each speaker's timing.
  It needs no new hardware and lets you use every speaker at once.
- **Weak spots:**
  - The HDMI cable is unnecessary because the DN1080 is an AirPlay target in Airfoil.
    Without the cable, all network speakers share one timing path, and the Mac isn't tethered to the TV.
  - BT from the Mac to the kitchen soundbar through a wall is the most likely dropout.
    Put the Mac near the kitchen, or between rooms, to shorten the BT link.
  - The Mac becomes a single point of failure. Disable sleep (Amphetamine/caffeinate),
    turn on Do Not Disturb, and keep it plugged in. System sounds play everywhere.
  - Airfoil costs ~$39. The free trial adds noise after 10 min.
- **Better version:** Airfoil → DN1080 via AirPlay (no HDMI) + kitchen via WiiM/Chromecast or BT
  + JBLs via BT in their own zones.

## Pre-party checklist
- [ ] AVR firmware updated, on Wi-Fi, visible in Google Home + AirPlay
- [ ] Kitchen path set up (A/B/C) and tested for 30+ min, no dropouts
- [ ] "Party" group created, delay checked by standing in the doorway between rooms
- [ ] Volume balanced: kitchen a bit lower so people can talk
- [ ] Turn off the soundbar's auto-standby/eco if it sleeps on optical silence
- [ ] Download the playlist for offline use as a backup; check that Wi-Fi reaches the kitchen
- [ ] Charge all Bluetooth speakers
- [ ] Turn on crossfade in Spotify so there are no gaps between songs
