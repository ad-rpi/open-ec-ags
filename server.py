#!/usr/bin/env python3
"""
Web dashboard for the Cummins EC-AGS+ generator.

Runs on macOS or Raspberry Pi. Talks BLE via agscli.AGS, serves a single-page dashboard
and a small JSON API. One BLE connection is held open and shared by all browser clients.

    .venv/bin/pip install -r requirements.txt
    .venv/bin/python server.py            # then open http://localhost:8722

Saved devices (incl. password) live in devices.json next to this file.
"""
import asyncio
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import uuid
from datetime import datetime, timedelta

from bleak import BleakClient, BleakScanner
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agscli as ag
import faultcodes

HERE = os.path.dirname(os.path.abspath(__file__))
DEVICES_PATH = os.path.join(HERE, "devices.json")
SCHEDULES_PATH = os.path.join(HERE, "schedules.json")
STATE_PATH = os.path.join(HERE, "state.json")    # remembers last device + autoconnect pref
TEMPCTL_PATH = os.path.join(HERE, "tempctl.json")  # cold-start (heater) temperature rule
SOCCTL_PATH = os.path.join(HERE, "socctl.json")    # low-house-SOC auto start/stop rule
STATS_PATH = os.path.join(HERE, "stats.db")        # telemetry history (SQLite) for the Stats tab
PRIMECTL_PATH = os.path.join(HERE, "primectl.json")  # opt-in manual fuel-prime feature

# ----------------------------------------------------------------------------- JSON stores
# Every persisted config/data file goes through these two helpers. _load_json hands back a fresh
# copy of `default` when the file is missing or corrupt; merge=True overlays the file onto
# `default`, so keys added in code later still get sane values on files written by older versions.
def _load_json(path, default, merge=False):
    try:
        with open(path) as f:
            data = json.load(f)
        return {**default, **data} if merge else data
    except (FileNotFoundError, json.JSONDecodeError):
        return json.loads(json.dumps(default))   # fresh copy — callers may mutate then save

def _atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)

def load_devices():     return _load_json(DEVICES_PATH, {})
def save_devices(d):    _atomic_write(DEVICES_PATH, d)
def load_schedules():   return _load_json(SCHEDULES_PATH, [])
def save_schedules(s):  _atomic_write(SCHEDULES_PATH, s)
def load_state():       return _load_json(STATE_PATH, {})
def save_state(s):      _atomic_write(STATE_PATH, s)

def automation_enabled():
    """Master server-side gate: is THIS app allowed to issue automated start/stop (voltage + temp rules)?
    Persisted in state.json, default on. Completely independent of the genset's built-in auto mode, which
    we never use (enabling it cranks the engine unconditionally — confirmed bug)."""
    return load_state().get("automation_enabled", True)

# States in which the engine is in its start/run cycle (i.e. NOT stopped). One source of truth for
# the auto rules and the activity watcher so they can't drift apart (the copy-pasted literal is what
# let the fault-logging check desync). "Priming" is only ever entered by an EXPLICIT prime — holding
# the physical start switch, or the official app's prime+start; the genset does not self-prime and
# our stack never commands one, so a rule-started run never reaches it. It's included only to keep
# the rules consistent with the watcher (and harmless if a panel prime is ever seen on the link).
# The stats sampler intentionally uses its own narrower set for run-time accounting.
RUN_CYCLE_STATES = ("Running", "Cranking", "Priming")

# ----------------------------------------------------------------------------- BLE manager
class Manager:
    def __init__(self):
        self.client = None
        self.ags = None
        self.address = None
        self.name = None
        self.state = "disconnected"   # disconnected | connecting | connected | error
        self.error = None
        self.lock = asyncio.Lock()    # serializes connect + every BLE op
        self.autoconnect = True       # auto-connect on startup + reconnect on drop
        self.user_disconnected = False  # set when the user hits Disconnect (suppresses reconnect)

    def _on_disconnect(self, _client):
        self.state = "disconnected"
        self.ags = None

    async def _teardown(self):
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        self.client = None
        self.ags = None

    async def connect(self, address, password, name=None):
        async with self.lock:
            await self._teardown()
            self.state, self.error = "connecting", None
            self.address, self.name = address, name
            try:
                self.client = BleakClient(address, timeout=20.0,
                                          disconnected_callback=self._on_disconnect)
                await self.client.connect()
                self.ags = ag.AGS(self.client, password)
                await self.ags.setup()
                await self.ags.authenticate()
                await self.ags.send_rpc(ag.RPC_GEN_STATUS)
                self.state = "connected"
                try:
                    await self.ags.send_rpc(ag.RPC_AUTO_OFF)   # disarm genset built-in auto (it cranks on enable)
                except Exception:
                    pass
                self.user_disconnected = False
                st = load_state(); st["last"] = address; save_state(st)  # remember for autoconnect
            except Exception as e:
                self.state, self.error = "error", str(e)
                await self._teardown()
                raise

    async def disconnect(self):
        async with self.lock:
            self.user_disconnected = True   # don't let autoconnect immediately undo this
            await self._teardown()
            self.state = "disconnected"

    def autoconnect_target(self):
        """The (address, device) to auto-(re)connect to, or None."""
        addr = load_state().get("last")
        dev = load_devices().get(addr) if addr else None
        return (addr, dev) if (dev and dev.get("password")) else None

    def require(self):
        if self.state != "connected" or not self.ags:
            raise HTTPException(409, "not connected")
        return self.ags

    async def op(self, coro_fn):
        """Run a BLE operation under the lock, mapping disconnects to clean errors."""
        ags = self.require()
        async with self.lock:
            ags = self.require()
            try:
                return await coro_fn(ags)
            except Exception as e:
                raise HTTPException(502, f"BLE op failed: {e}")

mgr = Manager()
app = FastAPI(title="EC-AGS+ Dashboard")
# Vendored Chart.js + date adapter live here (served locally so the Stats page works fully offline).
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

# ----------------------------------------------------------------------------- models
class SaveDevice(BaseModel):
    address: str
    name: str = ""
    password: str = ""

class ConnectReq(BaseModel):
    address: str
    password: str | None = None   # optional override; else use saved

class AutoParams(BaseModel):
    ac_sense_enabled: bool
    zones: list
    battery_sense_enabled: bool
    start_volts: float
    stop_volts: float
    start_time_sec: int
    stop_time_sec: int

class QuietParams(BaseModel):
    enabled: bool
    day: int
    start_sec: int
    stop_sec: int

# ----------------------------------------------------------------------------- API
@app.get("/api/state")
async def api_state():
    return {"state": mgr.state, "error": mgr.error, "address": mgr.address,
            "name": mgr.name, "telemetry": (mgr.ags.telemetry if mgr.ags else {}),
            "autoconnect": mgr.autoconnect, "last": load_state().get("last"),
            "automation_enabled": automation_enabled(), "priming": load_primectl(),
            "quickrun": _quickrun_status(), "fuel_run": _current_run_fuel(),
            "next_start": next_scheduled_start()}

class AutomationReq(BaseModel):
    enabled: bool

@app.get("/api/automation")
async def api_automation_get():
    return {"enabled": automation_enabled()}

@app.post("/api/automation")
async def api_automation_set(r: AutomationReq):
    st = load_state(); st["automation_enabled"] = r.enabled; save_state(st)
    await log_event("automation_on" if r.enabled else "automation_off", "manual")
    return {"ok": True}

@app.get("/api/scan")
async def api_scan(timeout: float = 8.0):
    found = await BleakScanner.discover(timeout=timeout, return_adv=True, service_uuids=[ag.SVC])
    saved = load_devices()
    out = []
    for addr, (dev, adv) in found.items():
        mac_id, status, registered = None, None, None
        if adv.manufacturer_data:
            raw = next(iter(adv.manufacturer_data.values()))
            if raw:
                registered = raw[0] != 0
                status = "registered" if registered else "UNREGISTERED"
                mac_id = raw[1:].hex().upper()
        out.append({"address": addr, "name": adv.local_name or dev.name, "rssi": adv.rssi,
                    "status": status, "registered": registered, "mac_id": mac_id,
                    "saved": addr in saved})
    out.sort(key=lambda d: d["rssi"] or -999, reverse=True)
    return out

@app.get("/api/devices")
async def api_devices_list():
    # never leak passwords to the browser
    return [{"address": a, "name": d.get("name", ""), "has_password": bool(d.get("password"))}
            for a, d in load_devices().items()]

@app.post("/api/devices")
async def api_devices_save(d: SaveDevice):
    devices = load_devices()
    devices[d.address] = {"name": d.name, "password": d.password}
    save_devices(devices)
    return {"ok": True}

@app.delete("/api/devices/{address}")
async def api_devices_delete(address: str):
    devices = load_devices()
    devices.pop(address, None)
    save_devices(devices)
    return {"ok": True}

@app.post("/api/pair")
async def api_pair(d: ConnectReq):
    """Best-effort BLE pairing. On Linux uses bluetoothctl; on macOS pairing is automatic."""
    if platform.system() != "Linux":
        return {"ok": True, "note": "pairing is automatic on this platform"}
    if not shutil.which("bluetoothctl"):
        raise HTTPException(500, "bluetoothctl not found")
    results = {}
    for action in ("pair", "trust", "connect"):
        try:
            p = subprocess.run(["bluetoothctl", action, d.address],
                               capture_output=True, text=True, timeout=30)
            results[action] = (p.stdout + p.stderr).strip()
        except Exception as e:
            results[action] = f"error: {e}"
    return {"ok": True, "results": results}

@app.post("/api/connect")
async def api_connect(req: ConnectReq):
    saved = load_devices().get(req.address, {})
    password = req.password if req.password is not None else saved.get("password", "")
    name = saved.get("name")
    try:
        await mgr.connect(req.address, password, name)
    except Exception as e:
        raise HTTPException(502, f"connect failed: {e}")
    return {"ok": True, "state": mgr.state}

@app.post("/api/disconnect")
async def api_disconnect():
    await mgr.disconnect()
    return {"ok": True}

COMMANDS = {
    "start": ag.RPC_START_GEN, "stop": ag.RPC_STOP_GEN, "status": ag.RPC_GEN_STATUS,
    "preheat": ag.RPC_PREHEAT_GEN, "auto-on": ag.RPC_AUTO_ON, "auto-off": ag.RPC_AUTO_OFF,
    "reset-fault": ag.RPC_RESETFAULT,
}
# NOTE: raw prime-start/prime-stop are intentionally NOT exposed here — an unguarded prime-start
# leaves the fuel pump running until a separate stop. Use the bounded POST /api/prime instead.

@app.post("/api/command/{cmd}")
async def api_command(cmd: str):
    if cmd not in COMMANDS:
        raise HTTPException(400, f"unknown command {cmd}")
    # Don't fire a redundant start: the controller ignores a start while it's already in a run cycle, but
    # sending it anyway logs a spurious manual-start hint (which the event watcher then mis-attributes) and
    # falsely confirms "sent." No-op cleanly so the UI can say "already running" instead.
    if cmd == "start":
        state = (mgr.ags.telemetry.get("status") or {}).get("state") if mgr.ags else None
        if state in RUN_CYCLE_STATES:
            return {"ok": True, "noop": "already running"}
    global _prime_cancel
    if cmd == "stop" and _priming:
        _prime_cancel = True            # abort an in-progress prime (and its pending start)
    await mgr.op(lambda ags: ags.send_rpc(COMMANDS[cmd]))
    if cmd in ("start", "stop"):
        note_command(cmd, "manual")                 # watcher attributes the resulting state change
    elif cmd in ("auto-on", "auto-off"):
        await log_event(cmd.replace("-", "_"), "manual")  # no state change → log the toggle directly
    return {"ok": True}

@app.get("/api/auto")
async def api_auto_get():
    return await mgr.op(lambda ags: ags.get_auto_params())

@app.post("/api/auto")
async def api_auto_set(p: AutoParams):
    await mgr.op(lambda ags: ags.set_auto_params(p.model_dump()))
    return {"ok": True}

@app.get("/api/history")
async def api_history(raw: bool = False):
    ags = mgr.require()
    if raw:   # diagnostic: inspect the unprocessed FAULTCODEALL response. Empty blob => opcode returns
              # nothing on this unit; all-zero blob => supported but no retained events. (protocol docs)
        params = await mgr.op(lambda a: a.send_rpc(
            ag.RPC_FAULTCODEALL, expect=ag.RPC_FAULTCODEALL, timeout=12.0))
        blob = next((v for t, v in params if t == "B"), b"")
        return {"param_types": "".join(t for t, _ in params), "raw_len": len(blob),
                "all_zero": (all(x == 0 for x in blob) if blob else None), "raw_hex": blob.hex()}
    events = await mgr.op(lambda a: a.get_fault_history())
    for e in events:                      # annotate each event with the fault name
        e["name"] = faultcodes.lookup(e["code"])["name"]
    active_code = (ags.telemetry.get("status") or {}).get("fault_code")
    active = faultcodes.lookup(active_code) if active_code else None
    return {"active_fault": active, "events": events}

@app.get("/api/version")
async def api_version():
    return await mgr.op(lambda a: a.get_sw_version())

# ----------------------------------------------------------------------------- remote temp sensors
class AddSensor(BaseModel):
    mac: str
    zone: str = ""

@app.get("/api/sensors")
async def api_sensors_list():
    return {"registered": await mgr.op(lambda a: a.list_temp_sensors())}

@app.get("/api/sensors/scan")
async def api_sensors_scan(timeout: float = 15.0):
    """Have the genset BLE-scan for nearby unpaired sensors (the step the app's add flow skips)."""
    return {"candidates": await mgr.op(lambda a: a.scan_temp_sensors(timeout=timeout))}

@app.post("/api/sensors")
async def api_sensors_add(s: AddSensor):
    zone = (s.zone or "").strip()
    if not zone or len(zone) > 8 or not re.fullmatch(r"[A-Za-z0-9' ]+", zone):
        raise HTTPException(400, "zone name must be 1-8 chars, letters/digits/space/apostrophe only")
    await mgr.op(lambda a: a.add_temp_sensor(s.mac, zone))
    await asyncio.sleep(1.0)                       # let the genset commit before we read it back
    return {"ok": True, "registered": await mgr.op(lambda a: a.list_temp_sensors())}

@app.delete("/api/sensors/{mac}")
async def api_sensors_del(mac: str):
    await mgr.op(lambda a: a.del_temp_sensor(mac))
    return {"ok": True}

@app.get("/api/quiet")
async def api_quiet_get():
    return await mgr.op(lambda ags: ags.get_quiet())

@app.post("/api/quiet")
async def api_quiet_set(p: QuietParams):
    await mgr.op(lambda ags: ags.set_quiet(p.model_dump()))
    return {"ok": True}

# ----------------------------------------------------------------------------- scheduler
# A schedule entry fires a command at a wall-clock time on chosen weekdays, independent of the
# genset's built-in auto mode. days: list of ints, 0=Monday .. 6=Sunday (Python weekday()).
class Schedule(BaseModel):
    id: str | None = None
    enabled: bool = True
    action: str                     # "start" | "stop"
    device: str                     # saved device address to act on
    time: str                       # "HH:MM" (24h, local time on this machine)
    days: list[int]                 # 0=Mon .. 6=Sun
    prime: bool = False             # if a "start", fuel-prime first (only when priming is enabled)

_sched_fired = {}   # entry id -> "YYYY-MM-DDTHH:MM" of last fire (dedupe within a minute)
_sched_log = []     # recent fire results, newest last

async def _scheduler_fire(entry):
    addr = entry["device"]
    saved = load_devices().get(addr, {})
    # Only start/stop may be scheduled — notably NOT auto-on, which cranks the engine on this unit.
    # Checked here as well as at save time so a hand-edited schedules.json can't bypass it.
    if entry["action"] not in ("start", "stop"):
        raise ValueError(f"bad action {entry['action']}")
    # Make sure we're connected to the right device, then send.
    if not (mgr.state == "connected" and mgr.address == addr):
        await mgr.connect(addr, saved.get("password", ""), saved.get("name"))
    act = entry["action"]
    if (act == "start" and entry.get("prime") and load_primectl().get("enabled")
            and _gen_state() == "Stopped"):
        await _do_prime(then_start=True, cause="schedule")   # prime first, then start
    else:
        async with mgr.lock:
            ags = mgr.require()
            await ags.send_rpc(COMMANDS[act])
        note_command(act, "schedule")

async def scheduler_loop():
    while True:
        try:
            now = datetime.now()
            hhmm = now.strftime("%H:%M")
            stamp = now.strftime("%Y-%m-%dT%H:%M")
            # One-shot user cancel of a specific upcoming start fire (POST /api/schedules/skip_next).
            # Purge once its minute has passed so a marker orphaned by a schedule edit can't linger.
            skip = load_state().get("skip_next_start")
            if skip and skip.get("stamp", "") < stamp:
                st = load_state(); st.pop("skip_next_start", None); save_state(st)
                skip = None
            for e in load_schedules():
                if not e.get("enabled") or e.get("time") != hhmm:
                    continue
                if now.weekday() not in e.get("days", []):
                    continue
                if _sched_fired.get(e["id"]) == stamp:
                    continue
                _sched_fired[e["id"]] = stamp
                if (skip and e["action"] == "start"
                        and skip.get("id") == e["id"] and skip.get("stamp") == stamp):
                    st = load_state(); st.pop("skip_next_start", None); save_state(st)
                    skip = None
                    _sched_log.append(f"{stamp}  start → {e.get('device')}  SKIPPED (cancelled by user)")
                    del _sched_log[:-50]
                    continue
                # An explicit quick-run timer outranks a standing scheduled stop: someone asked for
                # "run N minutes", so let that timer do the stopping. The suppressed stop is spent
                # (marked fired above), not deferred — it won't fire late when the timer expires.
                if e["action"] == "stop" and _quickrun_until is not None and _quickrun_until > now:
                    left = int((_quickrun_until - now).total_seconds() / 60)
                    _sched_log.append(f"{stamp}  stop → {e.get('device')}  SUPPRESSED (quick run: {left}m left)")
                    del _sched_log[:-50]
                    continue
                try:
                    await _scheduler_fire(e)
                    msg = f"{stamp}  {e['action']} → {e.get('device')}  OK"
                except Exception as ex:
                    msg = f"{stamp}  {e['action']} → {e.get('device')}  FAILED: {ex}"
                _sched_log.append(msg)
                del _sched_log[:-50]
        except Exception:
            pass
        await asyncio.sleep(20)

def next_scheduled_start(now=None):
    """Soonest upcoming enabled 'start' fire across all schedules, searched over the next 7 days.
    Returns {"ts": epoch, "time": "HH:MM", "in_sec": n, "device": addr} or None if nothing scheduled.
    Wall-clock/local time, matching scheduler_loop. Same-minute is treated as already fired (>= skips)."""
    now = now or datetime.now()
    best = None
    for e in load_schedules():
        if not e.get("enabled") or e.get("action") != "start":
            continue
        try:
            hh, mm = (int(x) for x in e["time"].split(":"))
        except (ValueError, KeyError):
            continue
        days = e.get("days", [])
        for d in range(8):                       # today .. +7 days (covers weekly wrap-around)
            cand = (now + timedelta(days=d)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            if cand <= now or cand.weekday() not in days:
                continue
            if best is None or cand < best[0]:
                best = (cand, e)
            break                                # earliest matching day for THIS entry
    if best is None:
        return None
    cand, e = best
    skip = load_state().get("skip_next_start") or {}
    stamp = cand.strftime("%Y-%m-%dT%H:%M")
    return {"ts": int(cand.timestamp()), "time": e["time"], "id": e["id"],
            "in_sec": int((cand - now).total_seconds()), "device": e.get("device"),
            "skipped": skip.get("id") == e["id"] and skip.get("stamp") == stamp}

class SkipNextReq(BaseModel):
    enabled: bool = True    # true = arm skip for the current next start; false = un-skip

@app.post("/api/schedules/skip_next")
async def api_skip_next(r: SkipNextReq):
    """One-shot cancel of the next upcoming scheduled start (schedule entries stay untouched).
    Arms a marker for that specific fire (entry id + minute); the scheduler consumes or expires it."""
    st = load_state()
    if not r.enabled:
        st.pop("skip_next_start", None); save_state(st)
        return {"ok": True, "skipped": None}
    ns = next_scheduled_start()
    if not ns:
        raise HTTPException(404, "no upcoming scheduled start")
    st["skip_next_start"] = {"id": ns["id"],
                             "stamp": datetime.fromtimestamp(ns["ts"]).strftime("%Y-%m-%dT%H:%M")}
    save_state(st)
    return {"ok": True, "skipped": ns}

@app.get("/api/schedules")
async def api_sched_list():
    return {"schedules": load_schedules(), "log": _sched_log[-15:]}

@app.post("/api/schedules")
async def api_sched_save(s: Schedule):
    if s.action not in ("start", "stop"):
        raise HTTPException(400, "action must be start or stop")
    schedules = load_schedules()
    d = s.model_dump()
    if d["id"]:
        schedules = [d if x["id"] == d["id"] else x for x in schedules]
        if not any(x["id"] == d["id"] for x in schedules):
            schedules.append(d)
    else:
        d["id"] = uuid.uuid4().hex[:8]
        schedules.append(d)
    save_schedules(schedules)
    return {"ok": True, "id": d["id"]}

@app.delete("/api/schedules/{sid}")
async def api_sched_delete(sid: str):
    save_schedules([x for x in load_schedules() if x["id"] != sid])
    _sched_fired.pop(sid, None)
    return {"ok": True}

# ----------------------------------------------------------------------------- temperature rule
# Start the genset when it gets COLD (the genset's own temp feature only starts when HOT, for A/C).
# Reads temp from the genset's remote-temp telemetry OR an externally POSTed reading, so the oil
# heater runs off generator power instead of draining the EcoFlow. Hysteresis + min-run-time; only
# stops a genset THIS rule started. Freeze protection deliberately ignores quiet hours.
TEMPCTL_DEFAULT = {"enabled": False, "source": "genset",
                   "start_below": 5.0, "stop_above": 12.0, "min_run_min": 20}

def load_tempctl():  return _load_json(TEMPCTL_PATH, TEMPCTL_DEFAULT, merge=True)
def save_tempctl(c): _atomic_write(TEMPCTL_PATH, c)

class TempRule(BaseModel):
    enabled: bool = False
    source: str = "genset"          # "genset" (remote-temp telemetry) | "external" (POST /api/temp)
    start_below: float              # start genset when temp <= this
    stop_above: float               # stop when temp >= this (must be > start_below)
    min_run_min: int = 20           # don't stop until it's run this long (anti short-cycle)

class ExternalTemp(BaseModel):
    temp: float

_temp_log = []
_temp_started = False               # did THIS rule start the genset? (only-stop-what-we-started)
_temp_start_ts = None
_temp_prev_running = False          # detect running→stopped transitions (release ownership promptly)
_external_temp = None
_external_temp_ts = None

def _log_temp(msg):
    _temp_log.append(f"{datetime.now().strftime('%Y-%m-%dT%H:%M')}  {msg}")
    del _temp_log[:-50]

def _current_temp():
    """Latest temperature from the configured source, or None if unavailable/stale."""
    cfg = load_tempctl()
    if cfg.get("source") == "external":
        if (_external_temp is not None and _external_temp_ts
                and (datetime.now() - _external_temp_ts).total_seconds() < 600):
            return _external_temp
        return None
    if mgr.ags:
        return (mgr.ags.telemetry.get("temp") or {}).get("remote_temp")
    return None

# GUARDS (vs the volt rule, the most-evolved sibling): HAS hysteresis, min-run, only-stop-what-we-
# started, external-stop release, stale-ownership release, inactive release. DELIBERATELY OMITS the
# min-off cooldown — temperature doesn't rebound the way surface charge does, and freeze protection
# shouldn't sit out a cooldown. If a guard is added to the volt loop, decide explicitly whether it
# belongs here too; don't assume.
async def temp_control_loop():
    global _temp_started, _temp_start_ts, _temp_prev_running
    while True:
        try:
            cfg = load_tempctl()
            if cfg.get("enabled") and automation_enabled() and mgr.state == "connected":
                temp = _current_temp()
                state = (mgr.ags.telemetry.get("status") or {}).get("state") if mgr.ags else None
                running = state in RUN_CYCLE_STATES
                if _temp_prev_running and not running:
                    # was running under us, now stopped → release ownership immediately (don't wait
                    # the 90s never-took grace) so a fresh start can't inherit a stale timestamp.
                    _temp_started, _temp_start_ts = False, None
                _temp_prev_running = running
                if temp is not None:
                    sb, sa = cfg["start_below"], cfg["stop_above"]
                    minrun = cfg.get("min_run_min", 20) * 60
                    if temp <= sb and not running and not _temp_started:
                        try:
                            async with mgr.lock:
                                await mgr.require().send_rpc(ag.RPC_START_GEN)
                            _temp_started, _temp_start_ts = True, datetime.now()
                            _log_temp(f"START — temp {temp} ≤ {sb}")
                            note_command("start", "temp_rule", f"temp {temp} ≤ {sb}")
                        except Exception as ex:
                            _log_temp(f"start FAILED: {ex}")
                    elif temp >= sa and _temp_started:
                        elapsed = (datetime.now() - _temp_start_ts).total_seconds() if _temp_start_ts else 1e9
                        if elapsed >= minrun:
                            try:
                                async with mgr.lock:
                                    await mgr.require().send_rpc(ag.RPC_STOP_GEN)
                                _temp_started, _temp_start_ts = False, None
                                _log_temp(f"STOP — temp {temp} ≥ {sa}")
                                note_command("stop", "temp_rule", f"temp {temp} ≥ {sa}")
                            except Exception as ex:
                                _log_temp(f"stop FAILED: {ex}")
                # we think we started it, but it's not running → someone else stopped it; let go
                if (_temp_started and not running and _temp_start_ts
                        and (datetime.now() - _temp_start_ts).total_seconds() > 90):
                    _temp_started, _temp_start_ts = False, None
                    _log_temp("released (genset stopped externally)")
            elif _temp_started:
                # not actively managing (rule disabled / automation off / disconnected): own nothing.
                _temp_started, _temp_start_ts, _temp_prev_running = False, None, False
                _log_temp("released (rule/automation inactive)")
        except Exception:
            pass
        await asyncio.sleep(30)

@app.post("/api/temp")
async def api_temp(t: ExternalTemp):
    """Feed an external temperature reading (e.g. from a separate BLE sensor) for source=external."""
    global _external_temp, _external_temp_ts
    _external_temp, _external_temp_ts = t.temp, datetime.now()
    return {"ok": True}

@app.get("/api/tempctl")
async def api_tempctl_get():
    return {**load_tempctl(), "current_temp": _current_temp(),
            "running": ((mgr.ags.telemetry.get("status") or {}).get("state") if mgr.ags else None),
            "rule_active": _temp_started, "log": _temp_log[-15:]}

@app.post("/api/tempctl")
async def api_tempctl_set(r: TempRule):
    if r.stop_above <= r.start_below:
        raise HTTPException(400, "stop_above must be greater than start_below (need a hysteresis gap)")
    save_tempctl(r.model_dump())
    return {"ok": True}

# ----------------------------------------------------------------------------- state-of-charge rule
# Start the genset when the HOUSE battery's state-of-charge drops too low, stop once it's recharged.
# Keys off the genset's OWN reported house SOC (soc_house_%, confirmed-working telemetry) — unlike the
# built-in battery auto-start, which reads a DC-voltage sense lead that isn't wired on this rig. Same
# pattern as the cold-start rule: hysteresis + min-run-time, and it only stops a genset THIS rule
# started. Like freeze protection it ignores quiet hours — protecting the bank from deep discharge wins.
# NOTE: on the tested unit soc_house_% is a coarse 4-level gauge (~0/33/66/100 = empty/⅓/⅔/full), so
# thresholds must straddle those buckets — defaults start at ⅓ (≤33) and stop at ⅔ (≥66).
SOCCTL_DEFAULT = {"enabled": False, "start_below": 33, "stop_above": 66, "min_run_min": 30}

def load_socctl():  return _load_json(SOCCTL_PATH, SOCCTL_DEFAULT, merge=True)
def save_socctl(c): _atomic_write(SOCCTL_PATH, c)

class SocRule(BaseModel):
    enabled: bool = False
    start_below: int                # start genset when house SOC <= this (%)
    stop_above: int                 # stop when house SOC >= this (must be > start_below)
    min_run_min: int = 30           # don't stop until it's run this long (anti short-cycle)

_soc_log = []
_soc_started = False                # did THIS rule start the genset? (only-stop-what-we-started)
_soc_start_ts = None

def _log_soc(msg):
    _soc_log.append(f"{datetime.now().strftime('%Y-%m-%dT%H:%M')}  {msg}")
    del _soc_log[:-50]

def _current_soc():
    """Latest house-battery SOC (%) from the genset, or None if unavailable."""
    if mgr.ags:
        return (mgr.ags.telemetry.get("status") or {}).get("soc_house_%")
    return None

async def soc_control_loop():
    global _soc_started, _soc_start_ts
    while True:
        try:
            cfg = load_socctl()
            if cfg.get("enabled") and automation_enabled() and mgr.state == "connected":
                soc = _current_soc()
                state = (mgr.ags.telemetry.get("status") or {}).get("state") if mgr.ags else None
                running = state in RUN_CYCLE_STATES
                if soc is not None:
                    sb, sa = cfg["start_below"], cfg["stop_above"]
                    minrun = cfg.get("min_run_min", 30) * 60
                    if soc <= sb and not running and not _soc_started:
                        try:
                            async with mgr.lock:
                                await mgr.require().send_rpc(ag.RPC_START_GEN)
                            _soc_started, _soc_start_ts = True, datetime.now()
                            _log_soc(f"START — house SOC {soc}% ≤ {sb}%")
                        except Exception as ex:
                            _log_soc(f"start FAILED: {ex}")
                    elif soc >= sa and _soc_started:
                        elapsed = (datetime.now() - _soc_start_ts).total_seconds() if _soc_start_ts else 1e9
                        if elapsed >= minrun:
                            try:
                                async with mgr.lock:
                                    await mgr.require().send_rpc(ag.RPC_STOP_GEN)
                                _soc_started, _soc_start_ts = False, None
                                _log_soc(f"STOP — house SOC {soc}% ≥ {sa}%")
                            except Exception as ex:
                                _log_soc(f"stop FAILED: {ex}")
                # we think we started it, but it's not running → someone else stopped it; let go
                if (_soc_started and not running and _soc_start_ts
                        and (datetime.now() - _soc_start_ts).total_seconds() > 90):
                    _soc_started, _soc_start_ts = False, None
                    _log_soc("released (genset stopped externally)")
        except Exception:
            pass
        await asyncio.sleep(30)

@app.get("/api/socctl")
async def api_socctl_get():
    return {**load_socctl(), "current_soc": _current_soc(),
            "running": ((mgr.ags.telemetry.get("status") or {}).get("state") if mgr.ags else None),
            "rule_active": _soc_started, "log": _soc_log[-15:]}

@app.post("/api/socctl")
async def api_socctl_set(r: SocRule):
    if r.stop_above <= r.start_below:
        raise HTTPException(400, "stop_above must be greater than start_below (need a hysteresis gap)")
    save_socctl(r.model_dump())
    return {"ok": True}

# ----------------------------------------------------------------------------- battery-voltage rule
# Start the genset when the HOUSE battery VOLTAGE sags too low, stop once it's charged back up. With the
# grey sense lead now wired to the house bank, the genset reports real house volts (decode_dcvolts /
# char 1710) instead of the old ~0.4V ghost — so voltage is a finer, more honest trigger than the coarse
# 0/33/66/100 SOC gauge (which read 100% at a resting 12.4V). This runs on the host (needs the server up)
# and is the SOLE battery auto-start authority: the genset's own battery_sense stays OFF and the SOC rule
# above is left disabled, so only ONE controller acts on the bank (two of them short-cycled before). Same
# guards as the other rules: hysteresis + min-run-time, only stops a genset THIS rule started, and ignores
# quiet hours (protecting the bank from deep discharge wins).
# CAVEAT: terminal voltage rises fast under charge and can't see tail current, so a voltage-only stop is a
# rough "full" proxy — set stop_above to a voltage the converter actually reaches, and lean on min-run for
# real absorption time. (True SOC would want a coulomb-counting shunt; see the lithium-era plan.)
VOLTCTL_PATH = os.path.join(HERE, "voltctl.json")
VOLTCTL_DEFAULT = {"enabled": False, "start_below": 12.2, "stop_above": 14.4,
                   "min_run_min": 45, "min_off_min": 20}

def load_voltctl():  return _load_json(VOLTCTL_PATH, VOLTCTL_DEFAULT, merge=True)
def save_voltctl(c): _atomic_write(VOLTCTL_PATH, c)

class VoltRule(BaseModel):
    enabled: bool = False
    start_below: float              # start genset when house volts <= this (V)
    stop_above: float               # stop when house volts >= this (must be > start_below)
    min_run_min: int = 45           # don't stop until it's run this long (anti short-cycle + charge time)
    min_off_min: int = 20           # after a stop (ours OR external), wait this long before auto-starting
                                    # again — kills surface-charge re-trigger thrash, lets V settle to a
                                    # true resting reading, and stops the rule fighting a manual/safety stop

_volt_log = []
_volt_started = False               # did THIS rule start the genset? (only-stop-what-we-started)
_volt_start_ts = None
_volt_stop_ts = None                # last running→stopped moment; gates the min-off cooldown
_volt_prev_running = False          # to detect running→stopped transitions (ours or external)

def _log_volt(msg):
    _volt_log.append(f"{datetime.now().strftime('%Y-%m-%dT%H:%M')}  {msg}")
    del _volt_log[:-50]

def _house_volts(avg=False):
    """House-battery voltage (V) from DC-volts telemetry, or None. avg=True returns the genset's SHORT
    moving average for the START decision — debounced enough that a single-sample blip can't false-trigger,
    but responsive to a genuine sustained sag. (The long avg lagged so far behind a weak, wobbling bank that
    real lows never crossed the threshold — instant would read 11.9 while long sat at 12.3.) Falls back to
    the long avg, then the instant reading, if the short average isn't reported."""
    if not mgr.ags:
        return None
    dc = mgr.ags.telemetry.get("dcvolts") or {}
    if avg:
        for k in ("house_v_short", "house_v_long", "house_v"):
            if dc.get(k) is not None:
                return dc[k]
        return None
    return dc.get("house_v")

# GUARDS: the full set — hysteresis, min-run, min-off cooldown (any stop, ours or external), only-
# stop-what-we-started, external-stop release, stale-ownership release, inactive release. This loop
# is the reference implementation; the temp loop above carries a documented subset (no min-off).
async def volt_control_loop():
    global _volt_started, _volt_start_ts, _volt_stop_ts, _volt_prev_running
    while True:
        try:
            cfg = load_voltctl()
            if cfg.get("enabled") and automation_enabled() and mgr.state == "connected":
                state = (mgr.ags.telemetry.get("status") or {}).get("state") if mgr.ags else None
                running = state in RUN_CYCLE_STATES
                # any running→stopped transition (our stop OR an external/manual/safety stop) opens the
                # off-cooldown — so we don't re-crank on the surface-charge collapse and don't fight a
                # human (or interlock) that just shut it down.
                if _volt_prev_running and not running:
                    _volt_stop_ts = datetime.now()
                    # the run we owned is over → release ownership NOW instead of waiting out the 90s
                    # never-took grace below, so a fresh start (quick-run/manual/schedule) inside that
                    # window can't inherit a stale _volt_start_ts and be stopped mid-warm-up.
                    _volt_started, _volt_start_ts = False, None
                _volt_prev_running = running
                v_start = _house_volts(avg=True)   # smoothed — rides through transient load sag
                v_now = _house_volts()             # instant — responsive to the charge voltage rising
                sb, sa = cfg["start_below"], cfg["stop_above"]
                minrun = cfg.get("min_run_min", 45) * 60
                minoff = cfg.get("min_off_min", 20) * 60
                cooled = (_volt_stop_ts is None
                          or (datetime.now() - _volt_stop_ts).total_seconds() >= minoff)
                if v_start is not None and v_start <= sb and not running and not _volt_started and cooled:
                    try:
                        async with mgr.lock:
                            await mgr.require().send_rpc(ag.RPC_START_GEN)
                        _volt_started, _volt_start_ts = True, datetime.now()
                        _log_volt(f"START — house {v_start:.2f}V ≤ {sb}V")
                        note_command("start", "voltage_rule", f"house {v_start:.2f}V ≤ {sb}V")
                    except Exception as ex:
                        _log_volt(f"start FAILED: {ex}")
                elif v_now is not None and v_now >= sa and _volt_started:
                    elapsed = (datetime.now() - _volt_start_ts).total_seconds() if _volt_start_ts else 1e9
                    if elapsed >= minrun:
                        try:
                            async with mgr.lock:
                                await mgr.require().send_rpc(ag.RPC_STOP_GEN)
                            _volt_started, _volt_start_ts, _volt_stop_ts = False, None, datetime.now()
                            _log_volt(f"STOP — house {v_now:.2f}V ≥ {sa}V")
                            note_command("stop", "voltage_rule", f"house {v_now:.2f}V ≥ {sa}V")
                        except Exception as ex:
                            _log_volt(f"stop FAILED: {ex}")
                # we think we started it, but it's not running → someone else stopped it, or the start
                # never took (e.g. a safety cutoff killed the fuel pump). Let go AND start the cooldown,
                # so we don't hammer the starter retrying every cycle.
                if (_volt_started and not running and _volt_start_ts
                        and (datetime.now() - _volt_start_ts).total_seconds() > 90):
                    _volt_started, _volt_start_ts, _volt_stop_ts = False, None, datetime.now()
                    _log_volt("released (genset stopped externally)")
            elif _volt_started:
                # not actively managing (rule disabled / automation off / disconnected): we own
                # nothing. Drop stale ownership so re-enabling automation can't stop a run started
                # meanwhile (e.g. a quick-run) using a stale start-timestamp — the reported bug.
                _volt_started, _volt_start_ts, _volt_prev_running = False, None, False
                _log_volt("released (rule/automation inactive)")
        except Exception:
            pass
        await asyncio.sleep(30)

@app.get("/api/voltctl")
async def api_voltctl_get():
    cfg = load_voltctl()
    cooldown_sec = 0
    if _volt_stop_ts is not None:
        cooldown_sec = max(0, int(cfg.get("min_off_min", 20) * 60
                                  - (datetime.now() - _volt_stop_ts).total_seconds()))
    return {**cfg, "current_v": _house_volts(),
            "running": ((mgr.ags.telemetry.get("status") or {}).get("state") if mgr.ags else None),
            "rule_active": _volt_started, "cooldown_sec": cooldown_sec, "log": _volt_log[-15:]}

@app.post("/api/voltctl")
async def api_voltctl_set(r: VoltRule):
    if r.stop_above <= r.start_below:
        raise HTTPException(400, "stop_above must be greater than start_below (need a hysteresis gap)")
    save_voltctl(r.model_dump())
    return {"ok": True}

@app.get("/api/changelog")
async def api_changelog():
    """Serve the human-curated CHANGELOG.md so the Settings tab can show users what's changed
    without making them dig through the repo or git history."""
    try:
        with open(os.path.join(HERE, "CHANGELOG.md"), encoding="utf-8") as f:
            return {"markdown": f.read()}
    except FileNotFoundError:
        return {"markdown": "# Changelog\n\nNo changelog available yet."}

# ----------------------------------------------------------------------------- manual fuel prime
# A bounded, server-driven fuel prime (opt-in via Settings). The official app primes only while you
# HOLD its button (mirroring the physical switch) and never auto-starts; replicating a hold over HTTP
# is unreliable and could leave the fuel pump running, so instead we prime for a fixed, capped number
# of seconds with the STOP guaranteed in a finally, then optionally START. Only valid when stopped,
# and never invoked by the auto rules. Use case: clear air from the lines after the genset has run dry
# or sat unused, when a plain start would just crank without catching.
PRIMECTL_DEFAULT = {"enabled": False, "duration_sec": 5}
PRIME_MAX_SEC = 60

def load_primectl():  return _load_json(PRIMECTL_PATH, PRIMECTL_DEFAULT, merge=True)
def save_primectl(c): _atomic_write(PRIMECTL_PATH, c)

class PrimeRule(BaseModel):
    enabled: bool = False
    duration_sec: int = 5

class PrimeReq(BaseModel):
    then_start: bool = False

_priming = False
_prime_cancel = False

def _gen_state():
    return (mgr.ags.telemetry.get("status") or {}).get("state") if mgr.ags else None

async def _do_prime(then_start, cause, detail=""):
    """Run the bounded prime sequence; assumes the caller verified preconditions (connected, stopped,
    enabled). PRIME_STOP is guaranteed via finally so the pump never stays on. Aborts (and skips the
    start) if _prime_cancel is set — e.g. the user hits Stop mid-prime."""
    global _priming, _prime_cancel
    dur = max(1, min(int(load_primectl().get("duration_sec", 5)), PRIME_MAX_SEC))
    _priming, _prime_cancel = True, False
    try:
        async with mgr.lock:
            ags = mgr.require()
            await ags.send_rpc(ag.RPC_PRIME_START)
        await log_event("prime", cause, f"{dur}s")
        waited = 0.0
        while waited < dur and not _prime_cancel:    # poll cancel so Stop can abort promptly
            await asyncio.sleep(0.5)
            waited += 0.5
    finally:
        try:
            async with mgr.lock:
                if mgr.ags:
                    await mgr.ags.send_rpc(ag.RPC_PRIME_STOP)
        except Exception:
            pass
        _priming = False
    if then_start and not _prime_cancel:
        async with mgr.lock:
            ags = mgr.require()
            await ags.send_rpc(ag.RPC_START_GEN)
        note_command("start", cause, detail or "after prime")
    return {"ok": True, "cancelled": _prime_cancel, "duration_sec": dur,
            "started": bool(then_start and not _prime_cancel)}

@app.get("/api/primectl")
async def api_primectl_get():
    return load_primectl()

@app.post("/api/primectl")
async def api_primectl_set(r: PrimeRule):
    if not (1 <= r.duration_sec <= PRIME_MAX_SEC):
        raise HTTPException(400, f"duration_sec must be 1..{PRIME_MAX_SEC}")
    save_primectl(r.model_dump())
    return {"ok": True}

@app.post("/api/prime")
async def api_prime(r: PrimeReq):
    if not load_primectl().get("enabled"):
        raise HTTPException(403, "priming is disabled (enable it in Settings)")
    if mgr.state != "connected" or not mgr.ags:
        raise HTTPException(409, "not connected")
    if _priming:
        raise HTTPException(409, "already priming")
    if _gen_state() != "Stopped":
        raise HTTPException(409, "can only prime while the generator is stopped")
    return await _do_prime(r.then_start, "manual")

# ----------------------------------------------------------------------------- quick run (timed)
# One-tap timed runs from the Control tab: start now, run for a fixed number of minutes, then issue a
# NORMAL stop (RPC_STOP_GEN, so the controller runs its usual cool-down) — never a hard kill. The point is
# convenience WITHOUT a "not fully warm" shutdown: the shortest preset is the warm-up floor below, so a
# quick run structurally can't be a damaging cold-short-cycle. Manual (user-initiated), so unlike the auto
# rules it is NOT gated by automation_enabled or quiet hours; it goes through the normal start path, so a
# safety lockout (e.g. no CO sensor → interlock cuts the fuel pump) simply keeps it from running and the
# loop drops the timer. State is in-memory like the auto rules: a server restart mid-run drops the stop
# timer, leaving the genset running until a rule or manual stop — acceptable for a short attended run, and
# the voltage rule (if enabled) still backstops it.
QUICKRUN_MIN_MIN = 15   # warm-up floor: never allow a run short enough to be a cold-short-cycle
QUICKRUN_MAX_MIN = 240

_quickrun_until = None       # datetime the timed run should stop, or None when no quick run is armed
_quickrun_started_ts = None  # when the current quick run began (grace window for "did the start take?")
_quickrun_min = None         # requested minutes, for display
_quickrun_log = []

class QuickRunReq(BaseModel):
    minutes: int

def _log_quickrun(msg):
    _quickrun_log.append(f"{datetime.now().strftime('%Y-%m-%dT%H:%M')}  {msg}")
    del _quickrun_log[:-50]

def _quickrun_status():
    if _quickrun_until is None:
        return {"active": False}
    remaining = max(0, int((_quickrun_until - datetime.now()).total_seconds()))
    return {"active": True, "minutes": _quickrun_min, "remaining_sec": remaining,
            "until_ts": int(_quickrun_until.timestamp())}

@app.post("/api/quickrun")
async def api_quickrun(r: QuickRunReq):
    global _quickrun_until, _quickrun_started_ts, _quickrun_min
    if not (QUICKRUN_MIN_MIN <= r.minutes <= QUICKRUN_MAX_MIN):
        raise HTTPException(400, f"minutes must be {QUICKRUN_MIN_MIN}..{QUICKRUN_MAX_MIN}")
    if mgr.state != "connected" or not mgr.ags:
        raise HTTPException(409, "not connected")
    running = _gen_state() in RUN_CYCLE_STATES
    until = datetime.now() + timedelta(minutes=r.minutes)
    if running:
        # Already running — for ANY reason (a rule, the panel, a plain start, or an existing quick run).
        # Adopt it: arm/re-arm the auto-stop timer so "time left to run" now applies to whatever is up. We
        # do NOT re-issue a start (it's already going) and there's no cold-short-cycle concern to warm up
        # for — it's already warm. If a rule started it and owns it, the quick-run stop fires a normal
        # RPC_STOP_GEN and the rule's transition-release drops its ownership, so nothing fights us.
        adopting = _quickrun_until is None
        _quickrun_until, _quickrun_min = until, r.minutes
        if _quickrun_started_ts is None:
            _quickrun_started_ts = datetime.now()   # anchor the "did the start take?" grace window
        _log_quickrun(f"{'adopted running genset' if adopting else 'timer re-armed'} → {r.minutes} min")
        return {"ok": True, "extended": True, "adopted": adopting, "minutes": r.minutes}
    try:
        async with mgr.lock:
            await mgr.require().send_rpc(ag.RPC_START_GEN)
    except Exception as e:
        raise HTTPException(502, f"start failed: {e}")
    _quickrun_until, _quickrun_started_ts, _quickrun_min = until, datetime.now(), r.minutes
    note_command("start", "quick_run", f"{r.minutes} min")
    _log_quickrun(f"START — {r.minutes} min")
    return {"ok": True, "started": True, "minutes": r.minutes}

@app.delete("/api/quickrun")
async def api_quickrun_cancel():
    """Cancel the pending auto-stop but leave the genset running (you'll stop it yourself). To stop now,
    use the normal Stop — it shuts down properly, and the loop clears the now-stale timer."""
    global _quickrun_until, _quickrun_started_ts, _quickrun_min
    was = _quickrun_until is not None
    _quickrun_until, _quickrun_started_ts, _quickrun_min = None, None, None
    if was:
        _log_quickrun("auto-stop cancelled (left running)")
    return {"ok": True, "cancelled": was}

async def quickrun_loop():
    global _quickrun_until, _quickrun_started_ts, _quickrun_min
    while True:
        try:
            if _quickrun_until is not None and mgr.state == "connected" and mgr.ags:
                running = _gen_state() in RUN_CYCLE_STATES
                now = datetime.now()
                if now >= _quickrun_until:
                    if running:
                        try:
                            async with mgr.lock:
                                await mgr.require().send_rpc(ag.RPC_STOP_GEN)  # normal stop → controller cool-down
                            note_command("stop", "quick_run", "timer elapsed")
                            _log_quickrun("STOP — timer elapsed")
                        except Exception as ex:
                            _log_quickrun(f"stop FAILED: {ex}")
                    else:
                        _log_quickrun("timer elapsed — already stopped")
                    _quickrun_until, _quickrun_started_ts, _quickrun_min = None, None, None
                elif (not running and _quickrun_started_ts
                      and (now - _quickrun_started_ts).total_seconds() > 90):
                    # start never took (safety cutoff / no CO sensor) or it was stopped externally → drop the
                    # timer so it can't fire on some later run
                    _quickrun_until, _quickrun_started_ts, _quickrun_min = None, None, None
                    _log_quickrun("released (genset not running)")
        except Exception:
            pass
        await asyncio.sleep(10)

# ----------------------------------------------------------------------------- telemetry history (Stats)
# Log house voltage / SOC / run-state to SQLite every minute so the Stats tab can chart trends and
# charge cycles. SQLite (stdlib) survives restarts and stays tiny: 1 row/min ≈ 0.5M rows/yr. We keep
# raw samples for STATS_RETENTION_DAYS and downsample server-side per query so charts stay light.
STATS_SAMPLE_SEC = 60
STATS_RETENTION_DAYS = 180
STATS_RANGES = {"4h": 4*3600, "8h": 8*3600, "12h": 12*3600, "24h": 86400, "7d": 7*86400, "30d": 30*86400, "all": None}

def _stats_db():
    conn = sqlite3.connect(STATS_PATH, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")     # readers and the writer don't block each other
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("CREATE TABLE IF NOT EXISTS samples ("
                 "ts INTEGER PRIMARY KEY, house_v REAL, soc INTEGER, running INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS events ("
                 "ts INTEGER, event TEXT, cause TEXT, detail TEXT)")
    return conn

# SQLite calls are BLOCKING, so every DB touch runs in a worker thread (asyncio.to_thread) — a slow or
# lock-contended query can never stall the event loop, and thus never stalls the BLE handling. WAL (above)
# keeps reads and the single writer from blocking each other. These _db_* helpers are the ONLY code that
# talks to SQLite; callers always reach them via asyncio.to_thread.
def _db_insert_sample(ts, hv, soc, running):
    conn = _stats_db()
    try:
        with conn:
            conn.execute("INSERT OR REPLACE INTO samples (ts, house_v, soc, running) VALUES (?,?,?,?)",
                         (ts, hv, soc, running))
            conn.execute("DELETE FROM samples WHERE ts < ?", (ts - STATS_RETENTION_DAYS*86400,))
    finally:
        conn.close()

def _db_log_event(ts, event, cause, detail):
    conn = _stats_db()
    try:
        with conn:
            conn.execute("INSERT INTO events (ts,event,cause,detail) VALUES (?,?,?,?)",
                         (ts, event, cause, detail))
            conn.execute("DELETE FROM events WHERE ts < ?", (ts - STATS_RETENTION_DAYS*86400,))
    finally:
        conn.close()

def _db_query_events(limit):
    conn = _stats_db()
    try:
        rows = conn.execute("SELECT ts,event,cause,detail FROM events ORDER BY ts DESC LIMIT ?",
                            (limit,)).fetchall()
    finally:
        conn.close()
    return [{"ts": ts*1000, "event": ev, "cause": c, "detail": d} for ts, ev, c, d in rows]

def _db_query_stats(now, span):
    conn = _stats_db()
    series = {"t": [], "v": [], "soc": [], "run": []}
    summary = {"runtime_sec": 0, "starts": 0, "v_min": None, "v_max": None, "v_avg": None, "n": 0}
    try:
        if span:
            start = now - span
        else:                                    # "all" → span the actual logged data, not epoch 0
            row = conn.execute("SELECT MIN(ts) FROM samples").fetchone()
            start = row[0] if row and row[0] is not None else now
        # bucket width so we return ~720 points max (raw at 1/min for short ranges, coarser beyond)
        iv = max(STATS_SAMPLE_SEC, ((now - start) // 720) or STATS_SAMPLE_SEC)
        for bucket, vavg, socmax, runmax in conn.execute(
                "SELECT (ts/?)*? AS b, AVG(house_v), MAX(soc), MAX(running) FROM samples "
                "WHERE ts >= ? GROUP BY b ORDER BY b", (iv, iv, start)):
            series["t"].append(bucket * 1000)                       # ms for Chart.js time axis
            series["v"].append(round(vavg, 3) if vavg is not None else None)
            series["soc"].append(socmax)
            series["run"].append(runmax or 0)
        prev_run, last_start_ts = 0, None
        for ts, hv, run in conn.execute(
                "SELECT ts, house_v, running FROM samples WHERE ts >= ? ORDER BY ts", (start,)):
            summary["n"] += 1
            if run:
                summary["runtime_sec"] += STATS_SAMPLE_SEC
            if run and not prev_run:
                summary["starts"] += 1
                last_start_ts = ts
            prev_run = run
            if hv is not None:
                summary["v_min"] = hv if summary["v_min"] is None else min(summary["v_min"], hv)
                summary["v_max"] = hv if summary["v_max"] is None else max(summary["v_max"], hv)
        avg = conn.execute("SELECT AVG(house_v) FROM samples WHERE ts >= ? AND house_v IS NOT NULL",
                           (start,)).fetchone()[0]
        summary["v_avg"] = round(avg, 2) if avg is not None else None
        summary["last_start_ts"] = (last_start_ts * 1000) if last_start_ts else None
        summary["fuel_gal"] = round(summary["runtime_sec"] / 3600.0 * load_fuelctl()["gal_per_hr"], 1)
    finally:
        conn.close()
    return iv, series, summary

async def stats_sampler_loop():
    while True:
        try:
            if mgr.state == "connected" and mgr.ags:
                st = mgr.ags.telemetry.get("status") or {}
                dc = mgr.ags.telemetry.get("dcvolts") or {}
                hv, soc, state = dc.get("house_v"), st.get("soc_house_%"), st.get("state")
                if hv is not None or soc is not None:   # skip empty frames (e.g. just after connect)
                    running = 1 if state in ("Running", "Cranking") else 0
                    ts = int(datetime.now().timestamp())
                    await asyncio.to_thread(_db_insert_sample, ts, hv, soc, running)
        except Exception:
            pass
        await asyncio.sleep(STATS_SAMPLE_SEC)

# ----------------------------------------------------------------------------- activity / event log
# A single watcher loop is the ONLY writer: it observes real state + fault transitions and logs an
# attributed event. Whoever issues a start/stop (manual endpoint, scheduler, a rule) calls note_command()
# first to leave a "who did this" hint; the watcher consumes it to attribute the resulting transition, and
# falls back to "manual/genset" for changes we didn't cause (panel button or the genset's own auto mode).
# This keeps every start/stop logged exactly once with a cause. Auto-mode toggles have no state change, so
# those are logged directly via log_event(). Stored in stats.db (events table), pruned with the samples.
_pending_cmd = None      # {"action","cause","detail","ts"} — hint for the watcher
_evt_runningish = None   # last observed running-ish state (None = no baseline yet / disconnected)
_evt_fault = None        # last observed active fault code

def note_command(action, cause, detail=""):
    global _pending_cmd
    _pending_cmd = {"action": action, "cause": cause, "detail": detail, "ts": datetime.now()}

async def log_event(event, cause, detail=""):
    try:
        ts = int(datetime.now().timestamp())
        await asyncio.to_thread(_db_log_event, ts, event, cause, detail)
    except Exception:
        pass

async def event_watch_loop():
    global _evt_runningish, _evt_fault, _pending_cmd
    while True:
        try:
            if mgr.state == "connected" and mgr.ags:
                st = mgr.ags.telemetry.get("status") or {}
                state = st.get("state")
                if state:
                    runningish = state in RUN_CYCLE_STATES
                    if _evt_runningish is None:
                        _evt_runningish = runningish            # silent baseline — don't log on connect
                    elif runningish != _evt_runningish:
                        action = "start" if runningish else "stop"
                        p = _pending_cmd
                        if p and p["action"] == action and (datetime.now()-p["ts"]).total_seconds() < 150:
                            cause, detail, _pending_cmd = p["cause"], p["detail"], None
                        else:
                            cause, detail = "manual/genset", ""   # not us → panel button or genset auto
                        await log_event("started" if runningish else "stopped", cause, detail)
                        _evt_runningish = runningish
                    fc = st.get("fault_code")
                    if _evt_fault is None:
                        _evt_fault = fc                     # silent baseline — don't re-log a fault that
                                                            # was already standing when we (re)connected
                    elif fc != _evt_fault:
                        if fc:                              # new active fault; a clear (→0) isn't logged
                            await log_event("fault", "genset",
                                            faultcodes.lookup(fc).get("name", f"code {fc}"))
                        _evt_fault = fc
            else:
                _evt_runningish, _evt_fault = None, None          # reset baseline on disconnect
        except Exception:
            pass
        await asyncio.sleep(10)

@app.get("/api/events")
async def api_events(limit: int = 50):
    limit = max(1, min(limit, 500))
    try:
        events = await asyncio.to_thread(_db_query_events, limit)
    except Exception as e:
        raise HTTPException(500, f"events query failed: {e}")
    return {"events": events}

@app.get("/api/stats")
async def api_stats(range: str = "24h"):
    if range not in STATS_RANGES:
        raise HTTPException(400, f"range must be one of {list(STATS_RANGES)}")
    now = int(datetime.now().timestamp())
    span = STATS_RANGES[range]
    try:
        iv, series, summary = await asyncio.to_thread(_db_query_stats, now, span)
    except Exception as e:
        raise HTTPException(500, f"stats query failed: {e}")
    st = (mgr.ags.telemetry.get("status") or {}) if mgr.ags else {}
    dc = (mgr.ags.telemetry.get("dcvolts") or {}) if mgr.ags else {}
    now_vals = {"house_v": dc.get("house_v"), "soc": st.get("soc_house_%"), "state": st.get("state")}
    return {"range": range, "bucket_sec": iv, "series": series, "summary": summary, "now": now_vals}

# ----------------------------------------------------------------------------- fuel-use estimate
# There's no fuel-level sensor on this rig (the genset's ¼-tank cutoff is a physical fuel-pickup height,
# not telemetry), so fuel is ESTIMATED from logged run-time × a calibrated burn rate. The default rate is
# padded deliberately high (measured ~0.43 gal/hr → 0.45) so the estimate over-reports burn and
# under-reports what's left — it errs toward "less fuel than you think," never toward stranding you.
# Descriptive, not authoritative: your physical fuel check stays ground truth. Caveat: the run-time log
# only accrues while the server is up, so genset runs with the dashboard off are invisible and undercount;
# re-anchor with "filled up" (or set a level off the dash gauge) after fueling.
FUELCTL_PATH = os.path.join(HERE, "fuelctl.json")
FUELCTL_DEFAULT = {"gal_per_hr": 0.45, "tank_gal": 55.0, "reserve_frac": 0.25,
                   "fill_gal": 55.0, "fill_ts": None, "gas_price": None}

def load_fuelctl():  return _load_json(FUELCTL_PATH, FUELCTL_DEFAULT, merge=True)
def save_fuelctl(c): _atomic_write(FUELCTL_PATH, c)

class FuelConfig(BaseModel):
    gal_per_hr: float
    tank_gal: float
    reserve_frac: float = 0.25      # genset starves at this fraction (physical pickup) → drive-away reserve
    gas_price: float | None = None  # $/gal; None hides the cost readouts

class FuelFill(BaseModel):
    gal: float | None = None        # current tank level in gal; None = filled to full (tank_gal)

def _db_run_seconds_since(ts_from):
    """Total logged run-time (seconds) since a unix ts — each running sample counts one sample interval."""
    conn = _stats_db()
    try:
        row = conn.execute("SELECT SUM(running) FROM samples WHERE ts >= ?", (int(ts_from),)).fetchone()
    finally:
        conn.close()
    return (row[0] or 0) * STATS_SAMPLE_SEC

async def _fuel_status():
    cfg = load_fuelctl()
    gph, tank, rf = cfg["gal_per_hr"], cfg["tank_gal"], cfg["reserve_frac"]
    price = cfg.get("gas_price")
    reserve_gal = round(tank * rf, 1)
    out = {**cfg, "reserve_gal": reserve_gal, "used_since_fill_gal": None,
           "remaining_gal": None, "usable_gal": None, "hours_left": None,
           "gal_per_day": None, "days_since_fill": None, "days_left": None,
           "cost_since_fill": None, "cost_per_day": None}
    if cfg.get("fill_ts"):
        run_sec = await asyncio.to_thread(_db_run_seconds_since, cfg["fill_ts"])
        used = run_sec / 3600.0 * gph
        remaining = max(0.0, cfg.get("fill_gal", tank) - used)
        usable = max(0.0, remaining - reserve_gal)
        # floor the span at 1h so a just-logged fill doesn't divide by ~0 and report a wild rate
        days = max(1 / 24.0, (int(datetime.now().timestamp()) - int(cfg["fill_ts"])) / 86400.0)
        out.update({"used_since_fill_gal": round(used, 1), "remaining_gal": round(remaining, 1),
                    "usable_gal": round(usable, 1),
                    "hours_left": round(usable / gph, 1) if gph > 0 else None,
                    "gal_per_day": round(used / days, 2), "days_since_fill": round(days, 1)})
        # days of usable fuel left AT the average burn since fill (not the padded gph — this uses actual
        # observed use/day). Needs some run history to be meaningful; None until the genset has burned some.
        gpd = used / days
        if gpd > 0:
            out["days_left"] = round(usable / gpd, 1)
        if price:
            out["cost_since_fill"] = round(used * price, 2)
            out["cost_per_day"] = round(used * price / days, 2)
    return out

def _current_run_fuel():
    """Estimated fuel burned so far in the CURRENT run, or None if not running. Uses the genset's own
    last-started timestamp (no DB) so it's cheap enough for the 2s state poll."""
    if not mgr.ags:
        return None
    st = mgr.ags.telemetry.get("status") or {}
    if st.get("state") not in RUN_CYCLE_STATES or not st.get("last_started"):
        return None
    sec = max(0, int(datetime.now().timestamp()) - int(st["last_started"]))
    cfg = load_fuelctl()
    gal = sec / 3600.0 * cfg["gal_per_hr"]
    return {"sec": sec, "gal": round(gal, 2),
            "cost": round(gal * cfg["gas_price"], 2) if cfg.get("gas_price") else None}

@app.get("/api/fuel")
async def api_fuel_get():
    return await _fuel_status()

@app.post("/api/fuel")
async def api_fuel_set(c: FuelConfig):
    if not 0 < c.gal_per_hr < 5:
        raise HTTPException(400, "gal_per_hr must be 0..5")
    if not 0 < c.tank_gal <= 200:
        raise HTTPException(400, "tank_gal must be 0..200")
    if not 0 <= c.reserve_frac < 1:
        raise HTTPException(400, "reserve_frac must be 0..1")
    if c.gas_price is not None and not 0 < c.gas_price < 20:
        raise HTTPException(400, "gas_price must be 0..20")
    cfg = load_fuelctl()
    cfg.update({"gal_per_hr": c.gal_per_hr, "tank_gal": c.tank_gal, "reserve_frac": c.reserve_frac,
                "gas_price": c.gas_price})
    if cfg.get("fill_gal") is not None:              # a smaller tank can't hold more than its capacity
        cfg["fill_gal"] = min(cfg["fill_gal"], c.tank_gal)
    save_fuelctl(cfg)
    return await _fuel_status()

@app.post("/api/fuel/fill")
async def api_fuel_fill(f: FuelFill):
    cfg = load_fuelctl()
    lvl = cfg["tank_gal"] if f.gal is None else f.gal
    if not 0 <= lvl <= cfg["tank_gal"]:
        raise HTTPException(400, f"level must be 0..{cfg['tank_gal']} gal")
    cfg["fill_gal"], cfg["fill_ts"] = lvl, int(datetime.now().timestamp())
    save_fuelctl(cfg)
    return await _fuel_status()

# ----------------------------------------------------------------------------- maintenance log
# A user-entered service logbook (oil, filters, plugs, etc.) — manual notes only, distinct from the
# auto activity log. The tested genset doesn't report engine hours, so this is date + free text, and
# back-dating is allowed. Stored in maintenance.json (gitignored — personal rig data, not for the repo).
MAINT_PATH = os.path.join(HERE, "maintenance.json")

def load_maintenance():  return _load_json(MAINT_PATH, [])
def save_maintenance(m): _atomic_write(MAINT_PATH, m)

class MaintNote(BaseModel):
    note: str
    date: str | None = None        # "YYYY-MM-DD"; defaults to today if omitted
    hours: int | None = None       # engine hours at service; optional (not every note needs it)

@app.get("/api/maintenance")
async def api_maint_list():
    notes = sorted(load_maintenance(), key=lambda n: (n.get("date", ""), n.get("created", "")),
                   reverse=True)
    return {"notes": notes}

@app.post("/api/maintenance")
async def api_maint_add(m: MaintNote):
    note = (m.note or "").strip()
    if not note:
        raise HTTPException(400, "note can't be empty")
    if len(note) > 200:
        raise HTTPException(400, "note too long (200 char max)")
    date = (m.date or "").strip() or datetime.now().strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(400, "date must be YYYY-MM-DD")
    if m.hours is not None and not (0 <= m.hours <= 100000):
        raise HTTPException(400, "hours out of range")
    notes = load_maintenance()
    notes.append({"id": uuid.uuid4().hex[:8], "date": date, "note": note, "hours": m.hours,
                  "created": datetime.now().isoformat(timespec="seconds")})
    save_maintenance(notes)
    return {"ok": True}

@app.delete("/api/maintenance/{nid}")
async def api_maint_del(nid: str):
    save_maintenance([n for n in load_maintenance() if n.get("id") != nid])
    return {"ok": True}

# ----------------------------------------------------------------------------- estimated engine hours
# The tested genset reports no hour meter, so this ESTIMATES current engine hours: an anchor reading
# (the physical Hobbs, or the hours on the most recent maintenance note) PLUS all run-time the dashboard
# has logged since that anchor. Re-anchor to the real Hobbs any time — same idea as the fuel "Set level".
# Only counts run-time in the stats DB, so an anchor older than the sample history under-counts; re-anchor
# to the Hobbs to true it up. Anchor lives in hoursctl.json (gitignored — personal rig data).
HOURSCTL_PATH = os.path.join(HERE, "hoursctl.json")

def load_hoursctl():  return _load_json(HOURSCTL_PATH, {"hours": None, "ts": None})
def save_hoursctl(c): _atomic_write(HOURSCTL_PATH, c)

class HoursReset(BaseModel):
    hours: float | None = None    # physical Hobbs reading; omit to snapshot the current estimate

def _maint_hours_anchor():
    """Most recent maintenance note carrying an hours reading → (hours, unix_ts), or None."""
    best = None
    for n in load_maintenance():
        if n.get("hours") is None:
            continue
        d = (n.get("date") or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            continue
        try:
            ts = datetime.strptime(d, "%Y-%m-%d").timestamp()
        except ValueError:
            continue
        if best is None or ts > best[1]:
            best = (float(n["hours"]), ts)
    return best

async def _hours_status():
    cfg = load_hoursctl()
    anchor_hours = anchor_ts = source = None
    if cfg.get("hours") is not None and cfg.get("ts"):
        anchor_hours, anchor_ts, source = float(cfg["hours"]), int(cfg["ts"]), "reset"
    else:
        m = _maint_hours_anchor()
        if m:
            anchor_hours, anchor_ts, source = m[0], int(m[1]), "maintenance"
    out = {"est_hours": None, "anchor_hours": None, "anchor_ts": None,
           "run_since_hours": None, "source": source}
    if source:
        run_sec = await asyncio.to_thread(_db_run_seconds_since, anchor_ts)
        run_hrs = run_sec / 3600.0
        out.update({"est_hours": round(anchor_hours + run_hrs, 1),
                    "anchor_hours": round(anchor_hours, 1), "anchor_ts": anchor_ts,
                    "run_since_hours": round(run_hrs, 1)})
    return out

@app.get("/api/hours")
async def api_hours_get():
    return await _hours_status()

@app.post("/api/hours")
async def api_hours_reset(r: HoursReset):
    """Re-anchor the hours estimate. Pass the physical Hobbs reading, or omit `hours` to snapshot the
    current estimate as the new anchor. Either way, logged run-time starts accruing again from now."""
    if r.hours is not None:
        if not (0 <= r.hours <= 100000):
            raise HTTPException(400, "hours out of range (0..100000)")
        hours = float(r.hours)
    else:
        cur = await _hours_status()
        if cur["est_hours"] is None:
            raise HTTPException(400, "no estimate yet — enter the current Hobbs reading")
        hours = cur["est_hours"]
    save_hoursctl({"hours": hours, "ts": int(datetime.now().timestamp())})
    return await _hours_status()

# ----------------------------------------------------------------------------- lifecycle & autoconnect
# Registered last so every background loop above is already defined. Startup spins up the loops;
# shutdown cancels them cleanly (no "Task was destroyed but it is pending!" on restart).
async def autoconnect_loop():
    while True:
        try:
            if (mgr.autoconnect and not mgr.user_disconnected
                    and mgr.state in ("disconnected", "error")):
                tgt = mgr.autoconnect_target()
                if tgt:
                    addr, dev = tgt
                    try:
                        await mgr.connect(addr, dev.get("password", ""), dev.get("name"))
                    except Exception:
                        pass   # out of range / asleep — try again next cycle
        except Exception:
            pass
        await asyncio.sleep(15)

class AutoConnectReq(BaseModel):
    enabled: bool

@app.post("/api/autoconnect")
async def api_autoconnect(a: AutoConnectReq):
    mgr.autoconnect = a.enabled
    st = load_state(); st["autoconnect"] = a.enabled; save_state(st)
    if a.enabled:
        mgr.user_disconnected = False   # re-arm; the loop will reconnect
    return {"ok": True}

_bg_tasks = []      # background loops, tracked so shutdown can cancel them cleanly

@app.on_event("startup")
async def _on_startup():
    mgr.autoconnect = load_state().get("autoconnect", True)
    _bg_tasks.append(asyncio.create_task(scheduler_loop()))
    _bg_tasks.append(asyncio.create_task(autoconnect_loop()))
    _bg_tasks.append(asyncio.create_task(temp_control_loop()))
    # SOC rule retired in favour of the voltage rule (single battery authority). The loop +
    # endpoints are kept for the lithium-era revert (flat LFP curve → SOC+shunt beats voltage); re-add
    # this line and its UI card then. Not registering it means a stale socctl.json can't reactivate it.
    # _bg_tasks.append(asyncio.create_task(soc_control_loop()))
    _bg_tasks.append(asyncio.create_task(volt_control_loop()))
    _bg_tasks.append(asyncio.create_task(stats_sampler_loop()))
    _bg_tasks.append(asyncio.create_task(event_watch_loop()))
    _bg_tasks.append(asyncio.create_task(quickrun_loop()))

@app.on_event("shutdown")
async def _on_shutdown():
    for t in _bg_tasks:
        t.cancel()
    await asyncio.gather(*_bg_tasks, return_exceptions=True)     # let each unwind its CancelledError

@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(HERE, "index.html")) as f:
        return f.read()

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("AGS_PORT", "8722"))
    print(f"EC-AGS+ dashboard → http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
