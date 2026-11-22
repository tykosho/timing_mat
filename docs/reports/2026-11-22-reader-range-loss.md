# Reader range loss during track testing (reported 2026-11-22)

**Status:** symptom confirmed in the raw capture, root cause not yet confirmed.
**Related:** #5 (watchdog), #18 (power check).

## Summary

During track testing the R200's range dropped from a few feet to a few inches, and came back after
the reader was power cycled. The raw serial capture on the Pi shows this clearly:

- For about **46 minutes** weak (distant) tag reads almost disappeared: 7% of reads were at or below
  -60 dBm. After a 3-second USB unplug that became **91%**, with **about 11 times as many reads per
  minute**.
- All seven tags seen both before and after that replug shifted in the same direction.
- An earlier replug also improved range, but it **relapsed** within about 12 minutes.
- The serial link was healthy throughout. The reader kept answering about 35 times a second, so the
  app's watchdog and health display had nothing to react to.
- The app's routine stop/start re-arm **did not** restore range.
- The software never sets RF power. The only commands in the whole capture are "start inventory" and
  "stop inventory".

The most likely area is **the reader's power path** (supply, USB cable, connector) or **the module's
state after power-up**. The antenna connection is not ruled out. The capture cannot tell these
apart; the tests [below](#how-to-find-the-root-cause) can.

## Setup at the time

| Item | State |
|---|---|
| Computer | Raspberry Pi 4 Model B Rev 1.5 |
| Reader | R200 behind a CH340 USB-serial adapter, 115200 baud |
| Code running | Image built 2026-11-15, identical to `dd35844` (31 source and template files compared) |
| Code checked out | `f855687`, pulled 2026-11-22 09:56, **but the image was not rebuilt** |
| Raw capture | On since 2026-11-15 (`RAW_CAPTURE_PATH=data/r200-capture.log`) |

None of the fixes merged in the week before the test were running. The reader-related ones (#105,
#120) concern write timeouts and shutdown, so they would not be expected to change range. The lap
debounce change from #124 was not running either, so it did not cause this.

## Evidence

### How to read the numbers

The reader doesn't report distance. **The share of reads at or below -60 dBm** is used as a proxy
for range: when range collapses to inches, only strong reads are left. Where people chose to stand
also affects it, so it supports the observation made at the track rather than replacing it.

### The incident

All five rows are one uninterrupted boot, with the same app process and no app restart.

| Phase | Length | Reads/min | Median RSSI | At or below -60 dBm | Frames/s |
|---|--:|--:|--:|--:|--:|
| Start of the boot | 3.8 min | 15.5 | -44 | **1%** | 35.6 |
| After replug 1 (17 s out) | 4.1 min | 28.9 | -59 | 36% | 34.0 |
| Relapse | 45.9 min | 12.2 | -51 | **7%** | 35.5 |
| After replug 2 (3 s out) | 4.1 min | 131.0 | -67 | **91%** | 35.8 |
| After replug 3 (3 s out) | 3.4 min | 71.6 | -67 | 94% | 39.9 |

Range started to return within about 10 seconds of replug 2, and was fully back within about a
minute. Replug 3 made no difference, because range was already good.

The boundary between "after replug 1" and "relapse" is approximate: the decline happened somewhere
between about 4 and 12 minutes after that replug.

### Same tags, before and after replug 2

"Before" is everything earlier in that boot.

| Tag | Reads before | Median before | Reads after | Median after | Change |
|---|--:|--:|--:|--:|--:|
| A | 412 | -50 | 418 | -66 | -16 dB |
| B | 188 | -57 | 74 | -67 | -10 dB |
| C | 38 | -45 | 26 | -66 | -21 dB |
| D | 32 | -47 | 24 | -68 | -21 dB |
| E | 31 | -45 | 23 | -52 | -7 dB |
| F | 28 | -43 | 14 | -70 | -27 dB |
| G | 6 | -57 | 23 | -64 | -7 dB |

A lower median after the replug is the *good* direction: the reader is hearing weaker, more distant
tags.

### When it happened

The Pi has no real-time clock, and this boot never synchronized, so its timestamps (2026-11-15
15:03 to 16:05) can't be trusted.

The next boot did synchronize, which dates its start to **2026-11-22 09:48:26 MST**. If the incident
was that morning, as reported, these are the **latest possible** times:

| Event | Stamped | Latest possible real time (Nov 22) |
|---|---|---|
| Boot being captured | 15:03:32 | 08:46:56 |
| Replug 1 | 15:07:20 | 08:50:45 |
| Relapse under way | by about 15:19 | by about 09:03 |
| Replug 2, range restored | 15:57:26 | 09:40:51 |
| Replug 3 | 16:01:34 | 09:44:59 |
| Capture ends (power cut) | 16:05:01 | 09:48:26 |

The capture alone can't prove this boot was on Nov 22. Someone who was at the track should confirm
that a replug at about 09:40 or a little earlier matches what they remember.

### Before and after that boot

- **Three short boots just before it** also had almost no weak reads (0%, 1% and 0% at or below
  -60 dBm), with power cuts between them. These are small samples (31, 62 and 3 reads, one or two
  tags), but they suggest that **restarting the Pi did not reliably clear the problem**.
- **The two boots after it** had normal range: 92% and 71% weak reads, down to -77 and -83 dBm. The
  second ran from 09:57 until at least 10:30 with six routine re-arms and no reader disconnect.

### What the serial link showed

- The only response other than tag reads was error `0x15`, the normal "no tag in this poll" answer.
  There were no other error codes, bad checksums or stray bytes between frames.
- The only gaps over 0.5 s in the incident boot were the three replugs.
- At least seven routine re-arms (stop, then start) ran while range was clearly bad. None restored it.

## Possible causes

| Cause | For | Against or unknown | Assessment |
|---|---|---|---|
| Low voltage or bad contact in the reader's USB power path | A replug helped twice; it relapsed; Pi restarts didn't reliably help | Voltage at the reader was never measured; how the reader is powered needs confirming | **Most likely, unproven** |
| Module in a bad RF state after power-up | A power cycle cures it, a re-arm doesn't | It should then also clear on every Pi power cut, if those really cut the reader's power | Possible |
| Antenna cable or connector | Handling the reader during a replug could reseat it | Two improvements, each coinciding with a replug | Possible |
| Heat | The module transmits continuously | Recovery after only 3 s without power | Less likely |
| Interference or surroundings | RF conditions at a track vary | Recovery coincided with the replug | Less likely |
| App changed the power setting | None | Only start/stop commands were ever sent | Ruled out |
| Lost serial data or a stuck reader thread | None | Steady frame rate, no gaps or corruption | Ruled out |
| This week's merged changes | None | They weren't running | Ruled out |

`vcgencmd get_throttled` read `0x0` when checked, but it only covers the current boot and the Pi's
own supply. It says nothing about the incident boot, whose system journal is gone (the journal is
kept in RAM), or about the voltage at the reader.

## Why the app didn't notice

`SerialReader` resets its silence timer on **any** frame, and the R200 sends a "no tag" frame for
every empty poll. A reader that can barely hear therefore looks the same as an empty track.

This was reproduced against both `dd35844` and main at `f855687` by feeding two simulated hours of
"no tag" frames at 35 per second: zero silence re-arms, 24 routine re-arms, and a reported silence of
0 seconds, which the admin page shows as green.

## How to find the root cause

### If it happens again

Do these in order, and test range after each one:

1. Write down the time from a phone. The Pi's clock may be wrong.
2. Without unplugging anything, measure 5 V at the reader while it is scanning.
3. Wiggle, then reseat the antenna connector, leaving USB connected.
4. Wiggle the USB connector at both ends without unplugging it.
5. Only then unplug and replug USB. Note which step restored range.

### On the bench

1. Fix a tag at a distance that only works with good range, about 1 m.
2. Do 10 Pi cold boots and 10 USB replugs, recording reads per minute after each.
3. Repeat with a short, thick USB cable, with a separately powered reader or powered hub, and on the
   battery used at the track (#18).
4. Leave the best setup running for an hour to look for a relapse.

## Suggested software follow-ups

- **Detect range loss:** mount a reference tag at a known distance, exclude it from timing, and
  alert when it stops being read. Frame counts alone can't detect this failure.
- **Record reader settings:** query and log RF power, region and channel on connect and
  periodically. This shows the configured values, not the power actually radiated.
- **Consider automatic recovery:** switch USB power off and on, but only after the bench test shows
  that it is reliable. A serial reconnect alone doesn't power cycle the module.

## Other problems found during the investigation

1. **Lap times are badly wrong across a reboot without a clock.** A boot started with a week-old
   time. When NTP corrected it by 582,295 s during the race, the race clock ignored the jump, as
   designed. The next boot then started from the correct time, so the first laps after it were
   recorded as about 582,700 s (roughly 9,700 minutes) long. This reproduces with `core/clock.py` on
   main at `f855687`.
2. **Pulling didn't deploy.** The Pi was pulled to main and rebooted, but kept running the Nov 15
   image. It needs `docker compose -f docker-compose.pi.yml up -d --build`.
3. **Power cuts are damaging logs.** The capture has five runs of NUL bytes where power was lost.
   Docker's log is damaged too: `docker logs --since` returns nothing for Nov 22, and only a short
   `--tail` works. Shut down with `sudo shutdown -h now` before pulling power. The database's
   integrity was not checked.
4. **The capture will stop by itself.** It was 9.5 MB of its 50 MB cap and growing about 4.5 MB an
   hour, so it has roughly 9 hours of running time left. Copy it off before then.

## Method and limits

- The analysis ran read-only on the Pi, over `data/r200-capture.log` as of 2026-11-22 10:31 MST.
  Nothing was restarted, changed or sent to the reader.
- Boots were separated by resets of the capture's monotonic clock. Durations use that clock, not the
  wall clock. NUL runs were stripped before parsing.
- Tag reads are type `0x02`, command `0x22` frames, with RSSI as signed dBm. Frames/s includes the
  "no tag" answers.
- Tags are anonymized. The capture and database are not committed.
- No RF power, voltage, current or distance measurements were available.
