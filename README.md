# RV Generator — Cummins EC-AGS+ BLE control

Control a Cummins EC-AGS+ generator from a **Mac** or **Raspberry Pi** over Bluetooth LE — a CLI, a
web dashboard, and a scheduler, as an alternative to the official phone app. The BLE protocol was
reverse-engineered for interoperability; see [`PROTOCOL.md`](PROTOCOL.md) for the full spec and
[`agscli.py`](agscli.py) for the implementation.

> ⚠️ **Unofficial & unaffiliated.** This is an independent, hobby interoperability project. It is not
> affiliated with, endorsed by, or supported by Cummins, Onan, or Saucon. "Cummins" and "EC-AGS+" are
> used only to describe compatibility. No proprietary app code is included here — the protocol notes are
> an independent description. You need the official app once to set your device password.
>
> **Use at your own risk.** This starts and stops a real internal-combustion generator. Only operate a
> generator that is properly installed and vented (engine exhaust is carbon monoxide), never remotely
> without appropriate CO detection and safety measures, and never in a way that endangers people,
> property, or violates local rules. No warranty; you assume all responsibility. See `LICENSE`.

## Setup

Python 3.9+ and [`bleak`](https://github.com/hbldh/bleak):

```bash
python3 -m venv .venv
.venv/bin/pip install bleak
```

* **macOS** — works out of the box via CoreBluetooth. Grant Bluetooth permission to your terminal
  (System Settings → Privacy & Security → Bluetooth) the first time. Device "addresses" are
  CoreBluetooth UUIDs, not MACs.
* **Raspberry Pi / Linux** — uses BlueZ. `sudo apt install bluetooth bluez`. Addresses are MACs.
  If a write is rejected with an authentication error, pre-pair once:
  `bluetoothctl` → `pair <MAC>` → `trust <MAC>`.

## Usage

```bash
# 1. Find the generator (also shows whether a password is set)
.venv/bin/python agscli.py scan

# 2. Talk to it.  Use the macId from scan as the password if it shows UNREGISTERED.
.venv/bin/python agscli.py -a <ADDRESS> -p <PASSWORD> status
.venv/bin/python agscli.py -a <ADDRESS> -p <PASSWORD> start
.venv/bin/python agscli.py -a <ADDRESS> -p <PASSWORD> stop
.venv/bin/python agscli.py -a <ADDRESS> -p <PASSWORD> monitor      # live stream
.venv/bin/python agscli.py -a <ADDRESS> -p <PASSWORD> info         # model/serial

# add -v to see the raw RPC/telemetry traffic
```

Commands: `scan, status, start, stop, auto-on, auto-off, reset-fault, info, monitor`.

## Web dashboard

A browser dashboard (scan / pair / save devices, live status, start/stop, and the auto start/stop
+ quiet-time settings) is in `server.py` + `index.html`.

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py            # open http://localhost:8722
```

* **Devices tab** — Scan finds nearby generators, "Save / Connect" stores it (with password) and
  connects. Saved devices persist in `devices.json`. On a Pi, the **Pair** button runs
  `bluetoothctl pair/trust/connect`; on macOS pairing is automatic. **Auto-connect** (toggle here,
  on by default) reconnects to the last-used device automatically on server startup and whenever the
  BLE link drops — unless you hit **Disconnect** yourself, which stays disconnected until you
  reconnect or re-arm the toggle. Last device + the toggle persist in `state.json`.
* **Control tab** — live state (Running/Stopped/Cranking/Priming), output V/Hz/load, battery,
  house SOC, fault code, run time; Start / Stop / Auto ON-OFF / Reset Fault buttons.
* **Auto Start/Stop tab** — battery-voltage start/stop thresholds + min run time, and the three
  temperature (A/C) zones. (Remember to also turn Auto mode **ON**.)
* **Quiet Time tab** — per-day quiet window during which the genset's own auto mode is suppressed.
* **History tab** — the genset's stored fault/event log (timestamp + decoded fault name) plus the
  current active fault. Fault names/descriptions come from `faultcodes.py` (extracted from the app).
* **Settings tab (⚙)** — choose which telemetry rows appear on the Control tab; the rest stay hidden
  until you want them (saved per-browser in localStorage). Defaults show the common stuff; "Show all"
  reveals manifold/oil/inverter temps, averaged voltages, engine hours, etc. for troubleshooting.
* **Schedule tab** — time-based start/stop that runs on *this machine's* clock, independent of the
  genset's built-in auto mode. Add entries like "Start at 07:30 Mon–Fri" / "Stop at 22:00 daily".
  The server checks every ~20s; when an entry is due it connects to the saved device (using its
  stored password) and sends the command. Recent fire results are shown on the tab. Entries persist
  in `schedules.json`. This is the "don't rely on auto mode" path — great on an always-on Pi.

One BLE connection is shared by all browser tabs. `AGS_PORT` env var changes the port. For the
scheduler to fire reliably, leave the server running (use the systemd unit below on the Pi).

> **Security note:** `devices.json` stores the generator password in plaintext for convenience on a
> trusted home machine. The dashboard binds to `0.0.0.0` (so you can reach the Pi from your phone) and
> has no auth — keep it on your private network, or change `host` to `127.0.0.1` in `server.py`.

### Run it on the Raspberry Pi at boot

Edit paths in `ags-dashboard.service`, then:

```bash
sudo cp ags-dashboard.service /etc/systemd/system/
sudo systemctl enable --now ags-dashboard
```

Now the dashboard is always available at `http://<pi-ip>:8722` — point your phone or laptop at it,
no flaky app required.

## The password

The auth is `base64(SHA256("ST3094…vjGS" + seed + password))`, where `seed` is fetched live from the
genset. The `password` is whatever you set in the official app. If the generator was **never** assigned
a password (the `scan` output says `UNREGISTERED`), the password is the device's **macId** shown by
`scan`.

## Status / notes

* **Tested hardware:** confirmed working on a **Cummins Onan QG 4000** (with the EC-AGS+ controller) —
  connect/auth, live telemetry, and Start/Stop all verified end-to-end. Other EC-AGS+-equipped gensets
  very likely work too (same BLE module); reports welcome. Run with `-v` and share output if a step hangs.
* **What telemetry you get depends on your genset's integration.** On the tested unit, the EC-AGS+
  reports **run state, output voltage, battery DC volts, SOC, fault code, and run-time** — but **not**
  engine RPM, output frequency, load, engine/oil/manifold/inverter temps, or lifetime hours (those came
  back as `0xFF` "not available" even while running — the controller isn't wired into the engine ECU for
  them on that genset). Those fields are decoded but **unverified**, so they live in an explicit
  **⚠ UNTESTED** section at the bottom of the dashboard's Settings, off by default. A different install
  with deeper engine integration may populate them — enable to try. Missing values show as `—`, never a
  fabricated number.
* The app only ever uses a 20-byte MTU and paces multi-chunk writes via a counter characteristic;
  `agscli.py` replicates this. Control commands (start/stop/status) are single-chunk.
* Bring your own device: you'll need the official app once to set/learn your generator's password.
  No decompiled app source is included in this repo.
