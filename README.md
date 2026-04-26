# BirdHeatmap

Annual species-activity heatmaps from your [BirdWeather](https://birdweather.com) station.

## Available plots (Samples below)

| Plot | Description |
|------|-------------|
| **Annual Song Observations** | Year-long activity grid. Each dot is a 5-minute window with at least one detection. Sunrise/sunset curves overlaid. |
| **All Years (overlay)** | Same axes as the annual heatmap, every available year overlaid in distinct colours for direct comparison. |
| **Species Presence Calendar** | Horizontal bar chart showing when each species is present across the year. Bars span first–last detection; bright overlay shows the middle 50% (IQR) of detection dates.|
| **Species Potrait** | Vertical violin plot of annual presence alongside seasonal time-of-day activity violin plots, combined across all years' data.|
| **Seasonal Succession (Ridge)** | Ridge/joy plot of seasonal activity peaks. Each ridge is a peak-normalized KDE of detection day-of-year, sorted by timing so the cascade reads Jan→Dec.|
| **Dawn Chorus** | Who sings first? Small-multiple histograms of detection time relative to local sunrise (−60 to +180 min), one panel per species, sorted earliest-to-latest singer.|
| **Time-of-Day Activity (Violin)** | Horizontal violin plot of each species' daily activity rhythm, sorted by median detection time. Supports year × season filtering — including cross-year seasonal pooling (e.g. "all springs combined").|

---

## Quickstart (local development)

```bash
git clone <repo>
cd birdheatmap

# 1. Create a local .env with your config (copy the example and edit)
cp deploy/birdheatmap.env.example .env
# Edit .env — set STATION_ID to your BirdWeather station ID
# For local dev, also set:  DB_PATH=./dev_data/birdweather.sqlite
#                           CACHE_PATH=./dev_data/cache

# 2. Set up the venv and install the package
make install

# 3. Fetch two pages and print the raw API response (no DB writes)
make sync-dry

# 4. Run a test sync — roughly 30 days of data, takes ~40 seconds
BACKFILL_PAGE_SIZE=500 .venv/bin/python -m birdheatmap sync --max-pages 40

# 5. Start the web UI
make serve
# → open http://localhost:8765
```

---

## Syncing

### Data volume

A full backfill for an active station with ~400k detections takes roughly
**15–30 minutes** at 500 detections/page with a 1-second rate limit.
The backfill is resumable — interrupt it any time and restart; it picks up
from the last successfully fetched page.

### Manual one-shot sync

```bash
# Full backfill or incremental, whichever is needed:
make sync

# Same thing directly (useful on the server as the service user):
sudo -u birdheatmap /opt/birdheatmap/venv/bin/python -m birdheatmap sync
```

### Automatic incremental sync

When `serve` is running (or the systemd service is active), an in-process
scheduler runs a sync **immediately at startup**, then again every
`SYNC_INTERVAL_MINUTES` (default: 60). No cron jobs or timers needed.

---

## Deployment

### Prerequisites on the server

- Debian 11+ or Ubuntu 22.04+
- Python 3.11+ **and** `python3-venv` (`apt install python3-venv`)
- SSH access as a user with `sudo` rights

### First deploy — step by step

**1. On your workstation**, set the target host in the Makefile:

```bash
# In Makefile:
DEPLOY_HOST := <server-ip>
DEPLOY_USER := <your-ssh-user>
```

**2. Run the deploy target:**

```bash
make deploy
```

This tars the repo and pipes it to the server over SSH, then runs
`sudo deploy/install.sh`.  The script:

1. Checks Python 3.11+ and `python3-venv` are present.
2. Creates the `birdheatmap` system user (no login shell, no home dir).
3. Builds a Python venv at `/opt/birdheatmap/venv` and installs the package.
4. Pre-compiles all `.pyc` files so the service never needs to write to the
   code directory at runtime (required by the `ProtectSystem=strict` hardening).
5. Creates `/var/lib/birdheatmap/` (SQLite + PNG cache) owned by `birdheatmap`.
6. Creates `/etc/birdheatmap/birdheatmap.env` from the example **on first
   install only** — never overwrites an existing config file.
7. Installs and enables the systemd unit.
8. Starts (or restarts) the service.

**Config and database are never touched on upgrade.**

**3. On first install**, the script will print an ACTION REQUIRED notice.
SSH into the server and edit the env file with your station details:

```bash
sudo nano /etc/birdheatmap/birdheatmap.env
# Set STATION_ID to your BirdWeather station ID
# Set BACKFILL_FROM_DATE to a few days before your station came online
```

Then restart the service so it picks up the config:

```bash
sudo systemctl restart birdheatmap
```

**4. Watch the first backfill:**

```bash
journalctl -u birdheatmap -f
# You'll see "Backfill: page N  inserted=…" every 10 pages.
```

**5. Open the UI** from anywhere on your LAN:

```
http://<server-ip>:8765
```

### Subsequent upgrades

```bash
# From your workstation — updates code and restarts the service:
make deploy
```

### Paths on the server

| Path | Purpose |
|------|---------|
| `/opt/birdheatmap/` | Code + Python venv |
| `/var/lib/birdheatmap/birdweather.sqlite` | Local detection cache |
| `/var/lib/birdheatmap/cache/` | Rendered PNG cache |
| `/etc/birdheatmap/birdheatmap.env` | Runtime configuration |
| `/etc/systemd/system/birdheatmap.service` | Systemd unit |

### Useful commands on the server

```bash
# Follow live logs
journalctl -u birdheatmap -f

# Trigger a manual sync right now (runs as the service user)
sudo -u birdheatmap /opt/birdheatmap/venv/bin/python -m birdheatmap sync

# Check service status
systemctl status birdheatmap

# Stop / start / restart
sudo systemctl stop birdheatmap
sudo systemctl start birdheatmap
sudo systemctl restart birdheatmap
```

---

## Configuration

All settings live in `/etc/birdheatmap/birdheatmap.env` (production) or `.env`
(local dev).  See `deploy/birdheatmap.env.example` for the full list with comments.

| Variable | Default | Description |
|----------|---------|-------------|
| `STATION_ID` | *(required)* | Your BirdWeather station ID |
| `BACKFILL_FROM_DATE` | `2020-01-01` | Earliest date to fetch during backfill — set to just before your station came online |
| `DB_PATH` | `/var/lib/birdheatmap/birdweather.sqlite` | SQLite database path |
| `CACHE_PATH` | `/var/lib/birdheatmap/cache` | PNG render cache directory |
| `BIND_HOST` | `0.0.0.0` | Address to listen on |
| `BIND_PORT` | `8765` | Web UI port |
| `SYNC_INTERVAL_MINUTES` | `60` | Incremental sync frequency |
| `BACKFILL_PAGE_SIZE` | `500` | Detections per GraphQL request |
| `BACKFILL_RATE_LIMIT_SECONDS` | `1.0` | Delay between backfill requests |

---

## Sample Plots:
Annual Song Observations:
<img width="989" height="802" alt="image" src="https://github.com/user-attachments/assets/15c37ab8-cef8-4466-95b6-c4a4a1ee566d" />

All Years (overlay):
<img width="989" height="802" alt="image" src="https://github.com/user-attachments/assets/6f28c55f-eb56-4a06-b5b8-f70898514ea4" />

Dawn Chorus:
<img width="1389" height="908" alt="image" src="https://github.com/user-attachments/assets/1fa9d6ae-841b-4432-a467-28be43237a9f" />

Season Presence:
<img width="1389" height="2978" alt="image" src="https://github.com/user-attachments/assets/d2e9cced-c3d1-47da-aae0-fb82c0edad47" />

Seasonal Succession:
<img width="1389" height="902" alt="image" src="https://github.com/user-attachments/assets/7ad408f4-dd25-4d30-881f-8117687a64c4" />

Daily Activity Rhythms:
<img width="1189" height="1201" alt="image" src="https://github.com/user-attachments/assets/55c91a2f-64ca-44a9-9974-f71a1b4b6301" />

Species Portrail:
<img width="2091" height="1035" alt="image" src="https://github.com/user-attachments/assets/e27cb40e-2d04-402a-a9cc-28cdd05e7bb0" />

---

## CLI reference

```bash
python -m birdheatmap --help

python -m birdheatmap sync [--dry-run] [--max-pages N]
python -m birdheatmap serve
python -m birdheatmap render --plot annual_heatmap --species "Black-capped Chickadee" --year 2025 --out out.png
python -m birdheatmap plots
python -m birdheatmap species
python -m birdheatmap reset-backfill   # clear backfill state to re-fetch all history
```

---

## Adding a new plot type

1. Create `src/birdheatmap/plots/my_new_plot.py`.
2. Expose the required interface:

```python
NAME: str = "my_new_plot"
DISPLAY_NAME: str = "My New Plot"
DESCRIPTION: str = "One sentence description shown in the UI."
PARAMS: list[dict] = [
    {"name": "year", "type": "int", "label": "Year", "default": None, "choices": None},
]

def render(db, species_id: int, **params) -> bytes:
    ...  # return PNG bytes
```

3. Restart the service — the registry auto-discovers it. No other files to edit.

See `src/birdheatmap/plots/README.md` for the full parameter spec and a list of
planned future plot types, and `_PALETTES` in any plot file to see how light/dark
colour themes are organised.

---

## Troubleshooting

**Service won't start**
```bash
journalctl -u birdheatmap -n 50 --no-pager
# Common causes: missing env vars, /var/lib/birdheatmap not owned by birdheatmap.
```

**Backfill looks stalled**
```bash
journalctl -u birdheatmap -f
# Progress is logged every 10 pages.
# The backfill is resumable — stop and restart the service at any time.
```

**Backfill completed but old data is missing**
```bash
sudo -u birdheatmap /opt/birdheatmap/venv/bin/python -m birdheatmap reset-backfill
sudo systemctl restart birdheatmap
# The backfill will restart from scratch; existing data is kept (duplicates skipped).
```

**Plot not updating after new detections arrive**
The render cache is keyed on the newest detection timestamp, so it refreshes
automatically once new data is synced in. To force an immediate refresh:
```bash
sudo rm -f /var/lib/birdheatmap/cache/*.png
```

**"Species not found in cache"**
The backfill hasn't reached that species yet, or sync hasn't run.
```bash
sudo -u birdheatmap /opt/birdheatmap/venv/bin/python -m birdheatmap sync
sudo -u birdheatmap /opt/birdheatmap/venv/bin/python -m birdheatmap species
```

**Permission denied writing to /var/lib/birdheatmap**
```bash
sudo chown -R birdheatmap:birdheatmap /var/lib/birdheatmap
```

---

## Acknowledgements

Inspired by [parsingphase/socialSensorSummaries](https://github.com/parsingphase/socialSensorSummaries).
