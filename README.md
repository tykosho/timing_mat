# Take Back the Track! 🏁
### UHF RFID Race Timing Mat & Leaderboard System

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

*(The database is automatically pre-seeded with 5 demo runners: Bibs 101 to 105).*

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
│   ├── db.py                    # SQLite persistence (Participants, Laps, Event)
│   ├── lap_engine.py            # Debounce, peak RSSI, lap timing engine
│   └── models.py                # Data classes
│
├── hardware/
│   ├── base.py                  # Abstract RFID reader interface
│   ├── mock_reader.py           # Realistic runner simulator with RSSI bell curve
│   ├── serial_reader.py         # PySerial background thread with auto-reconnect
│   └── r200_protocol.py         # MagicRF R200 frame builder and EPC parser
│
├── web/
│   ├── app.py                   # FastAPI application & WebSockets
│   └── templates/
│       ├── leaderboard.html     # High-contrast, dark-mode TV display view
│       ├── admin.html           # Race gun, tag sniffer, runner registration
│       └── runner_detail.html   # Detailed split breakdown & pace analysis
│
└── tests/
    └── test_engine.py           # Unit tests for lap engine, debounce & framing
```

---

## 🧪 Testing Locally (Without Docker)

You can also run tests directly:
```bash
python3 -m unittest discover tests
```
