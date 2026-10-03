# PrintWatch

**AI print-failure ("spaghetti") detection for Bambu Lab P1-series printers, driven by Home Assistant.** It uses [Obico](https://www.obico.io/)'s trained failure detector, runs on CPU with zero VRAM, and can pause the print and alert you when it sees a failure.

![Docker](https://img.shields.io/badge/runs%20in-Docker-2496ED)
![Python](https://img.shields.io/badge/python-3.12-3776AB)
![Home Assistant](https://img.shields.io/badge/Home%20Assistant-package-41BDF5)
![License](https://img.shields.io/badge/license-MIT-green)

## Why this exists

Obico (The Spaghetti Detective) has **no Bambu Lab support**, neither in its cloud app nor in the self-hosted server. Its printer agents only cover OctoPrint, Klipper and Duet.

So PrintWatch deploys **only Obico's `ml_api`** (the failure-detection model) and skips the rest of the Obico stack (web, tasks, Postgres, Redis). A small bridge then speaks the printer's two proprietary LAN protocols.

Why not a general vision model? Tested on real frames from a P1S:

| Model | Healthy print | Empty plate @39% | Black frame |
|---|---|---|---|
| `gemma3:4b` | "ok" | **"ok" (wrong)** | **"ok", conf 0.95, hallucinated detail** |
| `llama3.2-vision:11b` | "ok" | **"ok" (wrong)** | — |
| **Obico `ml_api`** | **0.000** | 0.000 | — |

Both local vision LLMs rubber-stamped every input and cost 6.7–8 GB of VRAM. Obico's detector uses **no GPU at all** (ONNX Runtime on CPU) and answers in ~135 ms.

## Architecture

```
  Bambu P1S (LAN mode)
    :8883 MQTT/TLS  ─┐
    :6000 camera    ─┴─▶  printwatch-bridge  ──▶  printwatch-ml-api
                            (:8099, LAN)            (internal network only)
                                  ▲
                                  │  REST
                          Home Assistant   ← cadence, smoothing, decisions
```

- The **bridge** (`bambu_bridge.py`) keeps one persistent connection to each printer protocol, holds only the newest camera frame in memory, and exposes a small HTTP API.
- **`ml_api`** gets **no host port** on purpose. Its auth decorator degrades to *no authentication* if `ML_API_TOKEN` is unset, so it only lives on the internal Docker network.
- **Home Assistant** decides when to check, smooths the scores over several frames ("strikes"), pauses the print and notifies you.

---

## Requirements

- A **Bambu Lab P1S** with **LAN Mode** enabled; you need its IP, access code and serial number. Other P1-series printers use the same LAN protocols but are untested.
- **Docker** (Docker Desktop on Windows, or any Linux host)
- **Home Assistant** for the automation side (optional, since the bridge API works standalone)
- **Git**, to fetch Obico's source for building the `ml_api` image

## Installation

### 1. Build Obico's detector image (once)

```bash
git clone --depth 1 https://github.com/TheSpaghettiDetective/obico-server.git
docker build -f obico-server/ml_api/Containerfile.cpu -t obico-ml-api:cpu obico-server/ml_api
```

### 2. Configure the printer credentials

```bash
git clone https://github.com/deniskostadinov1995/printwatch.git
cd printwatch
cp .env.example .env
```

Edit `.env`:

| Variable | Where to find it |
|---|---|
| `BAMBU_HOST` | Printer screen → **Settings → WLAN → IP address** |
| `BAMBU_CODE` | Printer screen → **Settings → WLAN → Access code** |
| `BAMBU_SERIAL` | Printer screen → **Settings → Device** |
| `ML_API_TOKEN` | Any long random string you make up |

### 3. Start it

```bash
docker compose up -d --build
```

Check it's alive: open `http://<this-pc-ip>:8099/healthz` and `http://<this-pc-ip>:8099/snapshot.jpg`.

### 4. Home Assistant

1. Copy [`homeassistant/printwatch.yaml`](homeassistant/printwatch.yaml) to `/config/packages/printwatch.yaml`.
2. In that file, replace **`192.168.1.50`** with the LAN IP of the machine running the bridge. Also replace `notify.mobile_app_your_phone` and `media_player.kitchen_echo` with your own notify service and speaker, or remove those steps.
3. Make sure `configuration.yaml` loads packages:
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
4. Restart Home Assistant and turn on `input_boolean.printwatch_enabled`.

For a live view, add a **Generic Camera** integration pointing at `http://<bridge-ip>:8099/snapshot.jpg` (or the MJPEG stream).

---

## Bridge API

| Endpoint | Purpose |
|---|---|
| `GET /snapshot.jpg` | Latest camera frame |
| `GET /stream.mjpeg` | Live MJPEG stream |
| `GET /status.json` | Printer telemetry |
| `GET /detect` | Run the detector, returns `{ok, score, n, ms}` |
| `POST /light/on` · `POST /light/off` | Chamber light |
| `POST /pause` · `POST /resume` · `POST /stop` | Pause / resume / stop the print |
| `POST /rediscover` | Rescan the LAN and adopt the printer's new IP |
| `GET /healthz` | Liveness |

## Calibration: do this before trusting it

The alert threshold (`input_number.printwatch_threshold`, default **0.30**) is a **starting guess, not a tuned value**. Healthy frames scored **0.000** across several layers, and Obico's own `detached-1` test fixture scores **0.265**.

Run a few prints with `input_boolean.printwatch_learn_mode` **on** and watch `input_text.printwatch_last_score`. Set the threshold above the highest score your healthy prints produce, then turn learn mode off.

---

## Troubleshooting

### "Bridge disconnected"

Almost always the printer got a **new DHCP address** after a router reboot.

- **Automatic:** after 6 consecutive connection failures (~30 s) the bridge rescans the LAN by itself. It proves which host is your printer using the access code and reconnects. The new address is saved to `./state/printer_ip`, which overrides `BAMBU_HOST` and survives container recreation.
- **From Home Assistant:** press **Find printer** (`rest_command.printwatch_rediscover`).
- **From a terminal:**
  ```bash
  python fix_printer_ip.py            # find the printer, update .env, recreate the bridge
  python fix_printer_ip.py --dry-run  # look without changing anything
  ```
- **By hand:** set the new `BAMBU_HOST` in `.env`, then run `docker compose up -d --force-recreate bridge`.
  Use `--force-recreate`, **not** `restart`, because `restart` keeps the old environment.

**The real fix:** give the printer a **DHCP reservation** (static lease) in your router.
If the scan finds nothing, the printer is off, off Wi-Fi, or LAN Mode was disabled. Re-enabling LAN Mode **generates a new access code**, so update `.env` too.

### Surviving reboots: `watchdog.py`

The bridge can re-find the printer, but it can't recover from Docker Desktop not running, because the recovery code lives inside the container. `watchdog.py` is the outer loop: it checks the bridge and starts Docker Desktop / the stack if needed.

```bash
python watchdog.py                # check once, repair if needed
python watchdog.py --status       # report only
python watchdog.py --install-help # prints the schtasks command to run it every 10 minutes
```

Also enable **"Start Docker Desktop when you sign in"**. Both containers are `restart: unless-stopped`, but that only helps once the Docker daemon is running.

## Things that will bite

- **The chamber light.** In the dark the detector sees nothing; one timelapse had 133 of 149 frames effectively black. The automation turns the light on before every check.
- **What the model cannot see.** `ml_api` has exactly one class: *failure* (spaghetti). A print that **detached and vanished** or **stalled** is invisible to it. Those need telemetry checks on progress and layer advancement.
- **A quiet monitor looks like a healthy print.** If the bridge dies, every score reads 0.0 forever. That's why `binary_sensor.printwatch_bridge_healthy` has its own alert.
- **`localhost` vs `127.0.0.1`.** On Windows, `localhost` can cost ~200 ms in an IPv6 fallback, which blows `ml_api`'s 0.1 s connect budget. Use explicit IPv4 addresses.

## Files

| File | Purpose |
|---|---|
| `bambu_bridge.py` | The bridge service (MQTT telemetry + camera + HTTP API) |
| `Dockerfile` | Bridge image |
| `docker-compose.yml` | Bridge + `ml_api` containers |
| `.env.example` | Template for your printer credentials |
| `homeassistant/printwatch.yaml` | Home Assistant package (sensors, helpers, automations) |
| `fix_printer_ip.py` | Finds the printer after a DHCP move and updates `.env` |
| `watchdog.py` | Restarts Docker Desktop / the stack if they're down |

## Credits

Failure detection by [Obico / The Spaghetti Detective](https://github.com/TheSpaghettiDetective/obico-server) (AGPL-3.0). You build its `ml_api` image from upstream source; none of its code is included here.

## License

[MIT](LICENSE) © Denis Kostadinov
