# Take Back the Track! 🏁
### UHF RFID Race Timing Mat & Leaderboard System

[![CI](https://github.com/tykosho/timing_mat/actions/workflows/ci.yml/badge.svg)](https://github.com/tykosho/timing_mat/actions/workflows/ci.yml)

An end-to-end race timing mat and bib tracking system built for track & running club events. Designed to run on a Raspberry Pi powered by a portable USB battery, or locally on your computer inside Docker.

---

## 🚀 Quick Start (Docker)

To build and run the entire system with the built-in **RFID Simulator**:

```bash
docker compose up --build
```

Open your browser to:
- **Trackside Live Crossing Feed**: [http://localhost:8000/crossings](http://localhost:8000/crossings)
- **Live Big-Screen Leaderboard**: [http://localhost:8000/leaderboard](http://localhost:8000/leaderboard)
- **Prepare Bibs & Tags**: [http://localhost:8000/prepare](http://localhost:8000/prepare)
- **Race Admin & Control Center**: [http://localhost:8000/admin](http://localhost:8000/admin)

*(The simulator runs with no admin PIN, so every page is open. To try the login and the spectator view, put `ADMIN_PIN=123456` in a `.env` file next to `docker-compose.yml` and restart. The `HOTSPOT_*` settings from the Race Wi-Fi section below work there too.)*

*(In simulator mode the first event is pre-seeded with 5 demo runners: Bibs 101 to 105. On real hardware nothing is seeded; set `SEED_DEMO=true` or `event.seed_demo: true` in `config.yaml` to force it.)*

---

## 🗓️ Event Workflow

Everything (runners, bibs, tags, laps, raw reads) belongs to an **event**. The reader times the *current* event.

1. **Create the event** — Admin → *Events* → *New Event*. New events copy the lap rules and track length of the current one.
2. **Collect registrations** — use a free online form (Google Forms works well) and export the responses as CSV.
3. **Import & tag** — open **Prepare Bibs** (`/prepare`):
   - Import the CSV. A `Name` (or `First Name` + `Last Name`) column is required; `Email`, `Bib`, `Notes` and `Tag EPC` are optional. Blank bibs are auto-numbered, and people already registered (same email) are skipped, never overwritten.
   - Click **Start Assigning**, then hold each bib's tag at the antenna. It is paired with the next runner in the list and the page advances. Keep other tags out of range while you do this; if two new tags are read at once it asks you to try again. Click *Tag this* on any row to choose who gets the next tag (or to replace one).
   - Download the *Runner list (CSV)* to print bibs.
4. **Race day** — Admin → **Start** (gun), **Pause**/**Resume**, and **End** when the event is over. End freezes the results and writes a database backup. *Reopen* undoes an End pressed by mistake.
5. **After the event** — Admin → *Events* has CSV downloads per event: **Results**, **Laps** (every lap with its exact crossing time), **Runners**, and **Raw reads** (every tag read, kept so laps can be audited or re-derived later). Switch to another event with *Switch to* (not possible while one is running).

**Safety nets:** *Clear Results* is refused while an event is running and saves a backup first. Backups go to `data/backups/`. Databases from before events existed are migrated automatically (a copy of the old file is saved first).

---

## 🎛️ How It Works

```
[ UHF RFID Antenna + R200 Module ]
               │ (UART / USB-Serial)
               ▼
[ Raspberry Pi / Docker Container ]
   ├── 1. Serial Driver / Mock Reader (Emits tag read bursts)
   ├── 2. Lap Engine (Peak RSSI detection & debounce filtering)
   ├── 3. SQLite Database (`data/race.db` volume persisted)
   └── 4. FastAPI Server (WebSockets for zero-latency live updates)
               │ (Local Wi-Fi)
               ▼
[ Display Screen / Phones ]
   ├── TV / Projector: Live ranked standings, fastest splits, audio chime
   ├── Admin Phone: Gun start/pause, tag sniffer auto-registration
   └── Runner Phones: Individual split breakdown (pace per km)
```

### 1. Lap Detection & Anti-Double-Counting (Debounce)
- When a runner crosses the antenna, the reader registers 5–20 reads within ~1.5 seconds.
- The **Lap Engine** groups these reads, calculates the **Peak RSSI** timestamp as the crossing instant, and ignores all subsequent reads for that runner within the configurable **Min Lap Threshold** (e.g. 45–60 seconds).
- Runners standing or chatting near the antenna will not trigger duplicate laps.
- **Missed start:** in the crowd at the gun a tag may not be read at all. That runner's first lap crossing would then become their start and they'd lose a lap. A first crossing between the min lap time and ~2.5 typical laps after the gun (10 min before there are enough laps) shows in the admin page's **Needs Review** card. **Started at gun** moves their start to the gun and adds the missing first lap; **Joined late** keeps it as recorded.

### 2. Live RFID Tag Sniffer (Admin Feature)
- In the Admin panel (`/admin`), when you wave any RFID sticker/bib near the antenna, the system instantly detects the tag's hex EPC and shows an **"Auto-fill into Registration"** button so you never have to manually type a 24-character hex code.
- To check the reader from a terminal (shows each read as it happens): `python -m hardware.sniffer` (add `--raw` to also print the raw frame bytes).

### 3. Raw Read Log
- While an event is running or paused, every tag read (including tags not assigned to anyone) is stored with its timestamp and RSSI. Laps are derived from these reads, so a missed or doubled lap can be investigated afterwards.

---

## 🍓 Deploying to Raspberry Pi (Field / Race Day)

1. Plug your USB-to-UART adapter (connected to the R200 board) into the Raspberry Pi.
2. Clone or copy this repository to the Pi.
3. Choose an admin PIN (6+ digits) and put it in a `.env` file next to `docker-compose.pi.yml` (it's git-ignored):
   ```bash
   echo "ADMIN_PIN=482913" > .env
   ```
   Compose refuses to start without it. See [Admin access](#-admin-access) below.
   Also add the reader's stable device name, so a replugged adapter can't come back as `ttyUSB1`:
   ```bash
   ls -l /dev/serial/by-id/        # e.g. usb-1a86_USB_Serial-if00-port0 -> ../../ttyUSB0
   echo "SERIAL_PORT=/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0" >> .env
   ```
   If the reader is unplugged and plugged back in mid-race, the app reconnects by itself (no restart needed).
4. Run with the Raspberry Pi profile (which maps `/dev/ttyUSB0` into Docker):
   ```bash
   docker compose -f docker-compose.pi.yml up --build -d
   ```
5. If the Pi reboots or power is briefly interrupted, Docker's `restart: unless-stopped` policy will automatically bring the race timer and database back online. `docker ps` shows the container as `healthy` once the app is answering.

### 📶 Race Wi-Fi (the Pi as its own hotspot)

The Pi can run its own Wi-Fi network, so the TV, the admin's phone and runners' phones don't need a phone hotspot or the venue's Wi-Fi. (A phone hotspot with internet is also what makes the Pi's clock jump; see #2.)

1. Add the network to `.env` (next to `docker-compose.pi.yml`):
   ```bash
   HOTSPOT_SSID=TimingMat
   HOTSPOT_PASSWORD='choose-8-to-63-chars'   # single-quote it if it contains $
   HOTSPOT_COUNTRY=US                        # your Wi-Fi country; Pi OS blocks Wi-Fi without it
   HOTSPOT_HOSTNAME=race.lan                 # what people type in the browser
   ```
2. With the Pi on **Ethernet, or with a keyboard and screen** (an SSH session over Wi-Fi drops when the hotspot starts):
   ```bash
   sudo deploy/pi/hotspot.sh setup      # also starts it on every boot
   docker compose -f docker-compose.pi.yml up -d
   ```
3. Open the admin page → **Join QR** (`/join`) and print it. One QR code joins the Wi-Fi, the other opens `http://race.lan/leaderboard`.

Good to know:
- `sudo deploy/pi/hotspot.sh status` shows whether it's up and how many devices are connected. `sudo deploy/pi/hotspot.sh remove` goes back to normal Wi-Fi. `DRY_RUN=1` prints what it would do without doing it.
- Phones will say the network has **no internet**. Tell people to stay connected. If the Pi also has Ethernet with internet, connected devices get internet through it.
- **How many devices:** the Pi's built-in Wi-Fi is commonly reported to handle only around 8–10 devices well. Test with the phones you expect. For a big field, use a cheap travel router as the Wi-Fi instead: plug the Pi into it by Ethernet, skip `hotspot.sh`, and either open the Pi's IP or add a local DNS entry for `race.lan` on the router (most have one).
- It uses 2.4 GHz (channel 6 by default, `HOTSPOT_CHANNEL` to change it) for range outdoors. Keep the Pi up off the ground and away from the antenna mat's cable if you can.
- Needs Raspberry Pi OS Bookworm or later (NetworkManager).
- **Testing without phones:** `sudo deploy/pi/hotspot-sim.sh run 10` starts the hotspot on a simulated radio (the kernel's `mac80211_hwsim`) and has 10 simulated phones join it at once. Each one joins with WPA2, gets an address, looks up `race.lan` through the Pi and checks `/` → leaderboard, `/leaderboard` and that `/admin` asks for the PIN. It also checks internet through the Pi's Ethernet. The real Wi-Fi isn't touched, so it's safe over SSH. It needs `sudo apt install iw wpasupplicant udhcpc dnsutils`, the app on port 80 and `ADMIN_PIN` set. `sudo deploy/pi/hotspot-sim.sh clean` removes everything. It tests the setup (DHCP, DNS, WPA2, the app); it can't tell you range or how many phones the Pi's own Wi-Fi chip handles.

### 🔒 Admin access

Runners' phones are on the same Wi-Fi as the timer, so race control is behind a PIN:

- **Open to everyone:** the leaderboard (`/leaderboard`), runner pages (`/runner/<bib>`), the live WebSocket, and the read-only `GET /api/leaderboard` and `GET /api/event`.
- **Admin only:** `/admin`, `/prepare`, every change (start/pause/end, runners, tags, laps, settings), runner lists and CSV exports (they contain emails). Opening an admin page asks for the PIN, and the login lasts 7 days on that device. The log-out button is in the admin header.
- **Setting the PIN:** `ADMIN_PIN` env var (or `admin.pin` in `config.yaml`). Changing it logs everyone out.
- **No PIN set:** in mock mode the admin is left open (with a warning banner) for local development. With a real reader, the app makes up a PIN and prints it in the log (`docker compose logs race-timer`).
- Five wrong PINs from one device lock it out for a minute.

---

## 📁 Project Structure

```
├── Dockerfile                   # Python 3.11 container definition
├── docker-compose.yml           # Dev / Mock mode container setup
├── docker-compose.pi.yml        # Production Pi setup with /dev/ttyUSB0
├── config.yaml                  # Race rules, port, baud rate
├── requirements.txt             # FastAPI, Uvicorn, PySerial, etc.
│
├── core/
│   ├── db.py                    # SQLite persistence (events, participants, laps, breaks, raw reads) + migration
│   ├── lap_engine.py            # Debounce, peak RSSI, lap timing engine, raw read log
│   ├── registration_import.py   # Registration CSV parser (Google Forms style headers)
│   ├── exports.py               # Results / laps / runners / raw-read CSV exports
│   └── models.py                # Data classes
│
├── hardware/
│   ├── base.py                  # Abstract RFID reader interface
│   ├── mock_reader.py           # Realistic runner simulator with RSSI bell curve
│   ├── serial_reader.py         # PySerial background thread with auto-reconnect
│   ├── r200_protocol.py         # MagicRF R200 frame builder, splitter and tag parser
│   └── sniffer.py               # Terminal tool: print every tag read (uses the same parser)
│
├── web/
│   ├── app.py                   # FastAPI application & WebSockets
│   └── templates/
│       ├── crossings.html       # Trackside live crossing feed & quick +1/-1 lap correction
│       ├── leaderboard.html     # High-contrast, dark-mode TV display view
│       ├── admin.html           # Event control (start/pause/end), events, runner registration
│       ├── prepare.html         # CSV import + tag assignment before the event
│       └── runner_detail.html   # Detailed split breakdown & pace analysis
│
└── tests/
    ├── test_engine.py           # Lap engine, debounce, R200 protocol framing
    ├── test_events.py           # Event lifecycle, migration, CSV import, tags, exports
    └── test_api.py              # HTTP API and page rendering (needs requirements-dev.txt)
```

---

## 🧪 Testing

```bash
pip install -r requirements.txt -r requirements-dev.txt
python3 -m unittest discover tests
```

No local Python? Run the tests in Docker from the project folder:
```bash
docker run --rm -v "$PWD:/app" -w /app python:3.11-slim sh -c "pip install -q -r requirements.txt -r requirements-dev.txt && python -B -m unittest discover tests"
```

### Continuous integration

Every pull request (and every push to `main`) runs `.github/workflows/ci.yml`:

| Check | What it runs |
|---|---|
| `tests` | `python -m unittest discover tests` on Python 3.11 |
| `lint` | `ruff check .`, using the bug-only rules in `ruff.toml` (syntax errors, undefined names) |
| `browser` | starts the app in mock mode and checks every page in headless Chromium: no JS errors or failed requests, login, live leaderboard updates, race clock, admin-only tag reads (`tests/test_browser.py`) |
| `docker` | builds the `Dockerfile` (nothing is pushed) |

Run the same checks before you push:
```bash
python3 -m unittest discover tests
pip install ruff==0.17.5 && ruff check .
docker build -t timing-mat .
```

Browser tests (skipped by `unittest discover` when Playwright isn't installed):
```bash
pip install -r requirements-browser.txt && python -m playwright install chromium
python -B -m unittest -v tests.test_browser
```

> **Verified on real hardware:** the R200 frame layout, checksum, PC/EPC length and RSSI-to-dBm conversion (`hardware/r200_protocol.py`) are checked against traffic captured from the reader on the Pi (`tests/fixtures/r200_capture_2026-11-15.log`, `tests/test_real_frames.py`). To check another reader, run `python -m hardware.sniffer --raw` or use the capture below.

**Fake R200 (no reader needed):** `python -m hardware.fake_r200` pretends to be the reader on a virtual serial port and prints its path. It sends the same bytes as the real module (checked against the capture above): about 35 polls a second, a tag frame per read or a "no tag" response. It stops after the 65,535 polls the app asks for, about 31 minutes, as we believe the real one does (#5). Start the app in physical mode against it, so the real serial code runs: `MOCK_HARDWARE=false SEED_DEMO=true SERIAL_PORT=<path> python -m uvicorn web.app:app` (the 5 demo runners lap the mat). Options: `--runners N --lap SECONDS`, faults with `--corrupt 0.05 --drop 0.05 --noise 0.02 --rssi-jitter 3`, `--poll-limit 200` to reach the poll stop in seconds, `--hang-after 60` for a module that stops answering (the admin page should turn amber), and `--replay <capture file> [--speed 10] [--loop]` to play back a recorded session. `python -m hardware.sniffer --port <path>` works too. Tests use the same fake through `SerialReader(serial_factory=FakeR200().serial_factory)` (`tests/test_fake_r200.py`). Needs macOS or Linux.

**Soak test (a whole race on a laptop):** `python -m tools.soak fast --hours 3` races 60 runners (half of them in packs of 4) through the fake R200 on a simulated clock, with 5% corrupted and 5% dropped frames: 3 race hours take about 2 minutes. It checks every lap against what the runners actually did, and reports memory, database size and leaderboard query time per race hour. `python -m tools.soak live --minutes 130 --clients 40` runs the real app in real time on the fake reader's serial port, with 40 WebSocket clients, and writes a CSV every minute: server memory, open files, database size, leaderboard latency, reader health, WebSocket traffic per client. Both fail on a miscounted lap, a ghost tag, or a start missed in the crowd that the app didn't flag for review (#72). A pass the reader never reported at all (e.g. in the crowd at the start) is listed separately, since no software can count it. A 15-minute fast race runs in CI (`tests/test_soak.py`).

**Capturing reader traffic without stopping the server:** the sniffer needs the serial port to itself. To record what the reader sends while the app keeps running (for a soak test, or to capture real frames as a test fixture), put `RAW_CAPTURE_PATH=data/r200-capture.log` in the Pi's `.env` (or set `hardware.raw_capture_path`) and restart the container. Every chunk received (`rx`), command sent (`tx`) and inventory re-arm (`# arm routine|silence|connect`) is logged with wall-clock and monotonic times; it stops at `raw_capture_max_mb` (default 50). `hardware.frame_capture.load_rx_bytes()` replays the received bytes, and `python -m hardware.capture_report <file>` summarises it for a soak test (each arm/re-arm and why, responses per second, time to first answer, gaps in data). Remove the setting again for race day.
