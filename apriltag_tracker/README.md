# Trackside AprilTag Lap Counter & Live Tracker

A local-first computer vision lap-counting system built for **macOS (MacBook Pro)** using the built-in FaceTime HD camera (or an external USB webcam) and **Google Chrome** as the trackside dashboard + offline voice announcer.

---

## 1. Quick Start

```bash
cd apriltag_tracker
./run.sh
```

`./run.sh` automatically creates `.venv`, installs dependencies from `requirements.txt`, starts the FastAPI + OpenCV vision server on `http://localhost:8000`, and opens Google Chrome.

### Useful Launch Options
- **Custom port** (e.g., if the RFID server is already running on port `8000`):
  ```bash
  PORT=8001 ./run.sh
  ```
- **External USB webcam** (OpenCV camera index `1`, `2`, etc. instead of built-in `0`):
  ```bash
  CAMERA_INDEX=1 ./run.sh
  ```
- **Start directly in Simulated Camera mode** (no physical webcam required):
  ```bash
  SIMULATED_CAMERA=1 ./run.sh
  ```
- **macOS Camera Permission:** On first launch, macOS will prompt your terminal or IDE (e.g., Terminal, iTerm2, VS Code, Antigravity) to allow Camera access. Approve it once in **System Settings → Privacy & Security → Camera**.

---

## 2. Printing & Wearing `tag16h5` Bibs (IDs `0–29`)

This tracker uses the **`tag16h5`** AprilTag family (a 4×4 data grid with 30 unique IDs, `0`–`29`). Larger black/white squares allow a laptop webcam to resolve bibs from across multiple track lanes.

### How to Print
1. With the app running, click **Print Bibs (0–29)** in the top bar (`http://localhost:8000/api/tags/printable?per_page=6`), or generate the standalone HTML file from the terminal:
   ```bash
   .venv/bin/python generate_tags.py --per-page 6 --output ./data/printable_tags.html
   ```
2. Select your layout in the top toolbar and click **Print / Save PDF**:
   - **`1 / page` (30 sheets):** `7.4 in` XL stadium bibs for maximum multi-lane distance.
   - **`2 / page` (15 sheets):** `4.85 in` half-sheet chest bibs (recommended for outdoor track workouts).
   - **`6 / page` (5 sheets, default):** `3.04 in` compact bibs with zero-waste shared cut lines.
   - **`15 / page` (2 sheets):** `1.76 in` small cards for close-range indoor/hallway testing.
   - **`30 / page` (1 sheet):** All 30 tags (`#00–#29`) on a single sheet for desk testing.

### Bib Tips for Best Detection
- **Use matte white paper** (avoid glossy photo paper or shiny plastic sleeves that reflect sunlight into the lens).
- **Keep the white border uncovered:** AprilTags require a white "quiet zone" around the black square. Place safety pins at the outer corners outside the white border.
- **Pin flat on the chest/torso** (4 pins) so the tag doesn't fold in half in the wind.

---

## 3. Trackside Camera Setup

1. **Position the camera:** Set your MacBook (or tripod-mounted USB webcam) near the track curb aimed across the finish line so runners' chests face toward/angled slightly toward the lens as they pass.
2. **Align the Virtual Finish Line:** Turn **Debug Overlay** ON (`D`) to see the dashed vertical finish line and top/bottom trigger zone brackets. Use the **Virtual Finish Line X (`10%–90%`)** slider to align the trigger line with a physical cone or track line.
3. **Set Cooldown (`5s–120s`):** Adjust the **Cooldown** slider in the top bar to slightly below your fastest expected lap time (default `25s`; e.g., set `45s–60s` for a standard 400m outdoor track, or `8s–12s` for short shuttle/indoor loops) so runners lingering near the laptop aren't double-counted.
4. **Mirror View:** Toggle **Mirror View** ON during self-service walk-up registration if runners are looking at the laptop screen like a mirror; AprilTag decoding runs on the un-mirrored raw frame first so detection accuracy is unaffected.

---

## 4. Runner Registration & Fixing Mistakes

### Walk-Up Registration
1. Hold an unregistered `tag16h5` bib steady in front of the camera for ~0.2–0.5 seconds (the engine requires **`hamming == 0`** and **3 high-confidence frames** so fleeting motion blur won't false-trigger).
2. A **non-blocking registration card** appears in the bottom-right corner with an automatic **face portrait crop** (laps for already-registered runners keep counting in the background).
3. Check that the **Bib / AprilTag ID** dropdown matches the printed `#XX` number on the bib:
   - Click **Snap New Photo** if the runner wants to step closer and smile.
   - Click **Re-scan Tag** if the tag was angled when first detected, or manually pick any `#00–#29` ID from the dropdown.
   - Click **Ignore Stray Tag** if a spare printed sheet is sitting in the background (ignored tags can be restored anytime from the leaderboard header).
4. Enter **Runner Name** and press `Enter` (or **Save Runner**). The announcer welcomes them to the track and puts their tag on initial cooldown so they can walk to the start line without logging an accidental 1-second lap.

### Fixing Mistakes Mid-Session
- **`+1` / `-1` Lap buttons:** Instantly add a missed lap or remove an accidental lap on any leaderboard row.
- **`Edit / Tag` button:** Opens the runner editor where you can:
  - **Edit Name** or **Retake Live Photo**.
  - **Move to an unused Bib ID** (if a tag was misread at sign-up—all completed laps, best/average splits, and face photo move over seamlessly).
  - **Swap Bibs with another runner** (if two runners accidentally grabbed each other's bibs—selecting the other runner's tag swaps their bib assignments while preserving each runner's own laps and photo).
  - **Delete Runner** (two-click confirmation removes a test/stray entry and frees its tag ID).
- **Session Controls:** **Start**, **Pause / Resume** (pause duration is automatically excluded from in-progress lap splits), and **Reset** (choose between **Reset Laps & Clock** for a new heat vs **Wipe Runners & Laps**).

---

## 5. Simultaneous Pack Crossings & Voice Announcer

- **Multiple runners at once:** OpenCV + `pupil-apriltags` detects all visible bibs in every frame simultaneously. Each runner has independent line-straddle tracking and cooldowns, so 2, 3, or 4 runners crossing side-by-side all have their exact splits recorded.
- **Pack-aware commentary (`AnnouncerService`):** Uses Chrome's built-in offline `window.speechSynthesis` voices (*Samantha*, *Daniel*, *Karen*, *Moira*, etc.):
  - **Solo runner:** Announces splits, personal records, fast-cadence hype, or roasts.
  - **2+ runners together:** Crossings within a `420ms` window (or queued while a prior line is speaking) are coalesced into a **Duo / Pack Callout** that names **every runner** and their lap/split so nobody gets skipped and the announcer never falls behind real time.
  - Adjust **Burn Freq (`0%` = strictly business, `25%` = occasional roasts, `75%+` = ruthless)**, **Speech Rate**, **Voice**, or **Audio: ON / MUTED** anytime in the top bar.

---

## 6. Testing Without Printed Tags & Keyboard Shortcuts

When focus is not inside a text input, use these hotkeys (or the buttons under the camera viewport):

| Key | Action |
|---|---|
| `U` | **Simulate Unknown Tag** — prompts registration for the next unused `tag16h5` ID |
| `L` | **Simulate Lap** — records a lap for a registered runner and speaks commentary |
| `P` | **Simulate Pack (2+)** — crosses 2–3 registered runners together and triggers a joint pack callout |
| `T` | **Test Roast** — previews the current announcer voice & roast lines |
| `D` | **Debug Overlay** — toggles green tag polygons, `decision_margin`, finish line, cooldown pills, and FPS HUD |

---

## 7. Persistence & Automated Tests

- Active session state and runners are saved atomically to `./data/session.json` and cropped `256×256` face portraits to `./data/faces/{tag_id}.png`. Refreshing Chrome or restarting `./run.sh` preserves all runners and lap history.
- Run the unit, computer-vision loop, and API/WebSocket test suite:
  ```bash
  .venv/bin/python -m unittest discover -s tests -v
  ```
