# Trackside AprilTag Lap Counter & Live Tracker

A local-first computer vision lap-counting prototype built for **macOS (MacBook Pro)** using the built-in FaceTime HD camera (or an external USB webcam) and **Google Chrome** as the trackside UI + offline voice announcer.

---

## Quick Start

```bash
cd apriltag_tracker
./run.sh
```

`./run.sh` creates `.venv`, installs dependencies from `requirements.txt`, starts the FastAPI + OpenCV vision server on `http://localhost:8000` (set `PORT=8001 ./run.sh` to use another port), and opens Google Chrome.

> **macOS Camera Permission:** The first time OpenCV opens the webcam (`cv2.VideoCapture(0)`), macOS will prompt your terminal/IDE for Camera access. Approve it once; if no camera is available (or if you toggle **Sim Camera** in the UI), the server automatically renders a live 30 FPS simulated track scene with virtual runners wearing real `tag16h5` bibs.

---

## Printable Race Bibs (`tag16h5` IDs 0–29)

- **In Chrome:** Click **Print Bibs (0–29)** in the top bar (`http://localhost:8000/api/tags/printable?per_page=2`), switch between `1 / page`, `2 / page`, or `6 / page`, and print or **Save as PDF**.
- **From CLI:**
  ```bash
  .venv/bin/python generate_tags.py --output ./data/printable_tags.html --per-page 2
  ```

---

## Instant Testing Without Printing Tags

Use the toolbar under the camera feed or keyboard shortcuts when no text field is focused:

- **`U`** — **Simulate Unknown Tag:** Injects the next unused `tag16h5` ID, crops a portrait, and pops the non-blocking registration overlay.
- **`L`** — **Simulate Lap Crossing:** Logs a lap for a registered runner and queues dynamic Web Speech API commentary (PR hype, standard splits, or spicy roasts based on the **Burn Freq** slider).
- **`T`** — **Test Roast:** Previews the selected macOS announcer voice (*Samantha*, *Daniel*, *Karen*, etc.) immediately.
- **`D`** — **Toggle Debug Overlay:** Toggles green tag polygons, `decision_margin` scores, virtual finish line & trigger zone shading, cooldown badges, and the top-left FPS/resolution/exposure HUD.

---

## Persistence & Tests

- Active session state and runners persist to `./data/session.json` and face portraits to `./data/faces/{tag_id}.png`, so refreshing Chrome mid-race keeps every runner and split intact.
- Run the test suite:
  ```bash
  .venv/bin/python -m unittest discover -s tests -v
  ```
