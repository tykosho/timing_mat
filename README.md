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
- **Live Big-Screen Leaderboard**: [http://localhost:8000/leaderboard](http://localhost:8000/leaderboard)
- **Race Admin & Control Center**: [http://localhost:8000/admin](http://localhost:8000/admin)

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

### 2. Live RFID Tag Sniffer (Admin Feature)
- In the Admin panel (`/admin`), when you wave any RFID sticker/bib near the antenna, the system instantly detects the tag's hex EPC and shows an **"Auto-fill into Registration"** button so you never have to manually type a 24-character hex code.
- To check the reader from a terminal (shows each read as it happens): `python -m hardware.sniffer` (add `--raw` to also print the raw frame bytes).

### 3. Raw Read Log
- While an event is running or paused, every tag read (including tags not assigned to anyone) is stored with its timestamp and RSSI. Laps are derived from these reads, so a missed or doubled lap can be investigated afterwards.

---

## 🍓 Deploying to Raspberry Pi (Field / Race Day)

1. Plug your USB-to-UART adapter (connected to the R200 board) into the Raspberry Pi.
2. Clone or copy this repository to the Pi.
3. Run with the Raspberry Pi profile (which maps `/dev/ttyUSB0` into Docker):
   ```bash
   docker compose -f docker-compose.pi.yml up --build -d
   ```
4. If the Pi reboots or power is briefly interrupted, Docker's `restart: unless-stopped` policy will automatically bring the race timer and database back online.

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
| `docker` | builds the `Dockerfile` (nothing is pushed) |

Run the same checks before you push:
```bash
python3 -m unittest discover tests
pip install ruff==0.17.5 && ruff check .
docker build -t timing-mat .
```

> **Not yet verified on real hardware:** the R200 frame parsing and the RSSI-to-dBm conversion (`raw_rssi_to_dbm` in `hardware/r200_protocol.py`) are covered by tests built from the documented frame layout, not from captured frames. Run `python -m hardware.sniffer --raw` with the reader attached to check them.
