# RV Generator — Cummins EC-AGS+ BLE control
* NOTE: Claude AI was used in the making of this for BLE analysis and decoding. All testing with a real generator
was done by a real person.

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

A browser dashboard (scan / pair / save devices, live status, start/stop, automated start/stop
rules, schedules, history, and stats charts) is in `server.py` + `index.html`.

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
* **Control tab** — live state (Running/Stopped/Cranking/Priming), output voltage, battery volts,
  house SOC, fault code, run time; Start / Stop / Reset Fault buttons; and the **App automation**
  master switch — one server-side gate for all of this app's automated start/stop rules (battery
  voltage + cold start). Turn it off and the app observes but never starts or stops anything on its own.
* **Battery Start/Stop tab** — a server-side rule that starts the generator when the **house battery
  voltage** sags below a threshold and stops it once charged back up (hysteresis gap, minimum run
  time, and it only stops a run it started itself). Runs on this machine, so the server must be up —
  the upside is full visibility and one single place that decides when the genset runs.

  > ⚠️ **The genset's built-in auto mode is deliberately not used** — on the tested unit, enabling
  > it cranks the engine unconditionally. See [Known issue: built-in auto mode](#known-issue-enabling-built-in-auto-mode-cranks-the-engine-unconditionally)
  > for the full write-up. This app sends auto-OFF on every connect to keep it disarmed, and the
  > built-in settings are shown for diagnostics only.
* **Temperature Startup tab** — two separate mechanisms: **Hot start (A/C)** is the genset's own
  feature (zone goes *above* a setpoint → start, for air conditioning; works without this dashboard
  but requires the built-in auto mode — see the warning above). **Cold start (heater)** is a
  server-side rule that starts the generator when it gets *cold* so a heater can run off generator
  power instead of draining the batteries; reads the genset's remote temp sensor or an external
  reading POSTed to `/api/temp`. Same guards as the battery rule, and it deliberately ignores quiet
  hours (freeze protection wins).
* **Quiet Time tab** — the genset's per-day quiet window (a genset-side setting). The app's own
  protection rules intentionally ignore it: deep-discharge and freeze protection outrank quiet hours.
* **History tab** — an **activity log** (every start/stop/fault with *what caused it*: manual,
  voltage rule, temp rule, schedule, or the genset/panel itself) recorded by the server, plus the
  genset's stored fault log (timestamp + decoded fault name) and the current active fault. Fault
  names/descriptions come from `faultcodes.py`.
* **Stats tab** — house-voltage and SOC history charts with generator-run bands, plus runtime and
  start counts per range (24h / 7d / 30d / all). Sampled once a minute while connected into a local
  SQLite file (`stats.db`), kept ~180 days, downsampled server-side so charts stay light. Charting is
  vendored Chart.js served locally, so it works fully offline.
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

### macOS menu bar widget (optional)

`menubar.py` puts live genset state in the macOS menu bar (🟢/🟠/⚪️ + state) with quick
Start / Stop / Reset-fault actions and a link to the dashboard. It's a thin client of the
dashboard server — it never opens its own BLE connection (the genset only allows one) — so start
`server.py` first. `pip install rumps`, then `.venv/bin/python menubar.py`; point `AGS_DASH` at
the server if it isn't on `localhost:8722`.

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

## Known issue: enabling built-in auto mode cranks the engine unconditionally

On the tested unit (Cummins Onan QG 4000 with the EC-AGS+ controller), **turning on the built-in
auto (AGS) mode starts the engine about 2 seconds later, every time, regardless of whether any
auto-start condition is met**. An unexpected engine start is a safety concern: an owner may enable
auto mode expecting it to *arm monitoring*, not to crank the engine — possibly while servicing the
unit or with the rig in an enclosed space.

**Reproduction** (instrumented, repeated on demand, June 2026):

1. Generator stopped, fault log cleared (fault = 0).
2. Every auto-start trigger disabled and verified via the controller's own advanced parameters:
   battery sense **off**, A/C sense **off**, all three temperature zones **off**.
3. House battery at a healthy 12.5–12.9 V — well above the 11.7 V auto-start threshold.
4. Enable auto mode. The engine cranks and starts ~2 s later.

This reproduces with the **official vendor app's own Auto toggle** — it sends the same enable-auto
command (opcode 2572) this project does, and the command provably contains no start instruction
(start is a separate opcode, 2509). The controller starts the engine purely on receiving "enter
auto mode."

**Scope:** confirmed on one unit and firmware; other EC-AGS+ installs may behave differently.
If yours does (or doesn't), a report is welcome — but test with the genset somewhere a surprise
start is safe.

**Disclosure timeline:** reported to the vendor through their support channel on June 9, 2026 with
reproduction steps; the ticket was closed in August 2026, unactioned, for having been in the queue
longer than a month.

**Mitigation:** leave built-in auto mode off. This dashboard sends auto-OFF on every connect to
keep it disarmed, and provides its own condition-checked start/stop rules and scheduler instead.

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
* **Codec tests:** `.venv/bin/python -m unittest test_codec` exercises the RPC encoder/parser, CRC,
  auth derivation, and telemetry decoders against frames frozen from the hardware-verified encoder —
  no BLE or generator needed.

