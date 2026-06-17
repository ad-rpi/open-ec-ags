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
from datetime import datetime

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

# ----------------------------------------------------------------------------- devices store
def load_devices():
    try:
        with open(DEVICES_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_devices(d):
    _atomic_write(DEVICES_PATH, d)

def load_schedules():
    try:
        with open(SCHEDULES_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_schedules(s):
    _atomic_write(SCHEDULES_PATH, s)

def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_state(s):
    _atomic_write(STATE_PATH, s)

def automation_enabled():
    """Master server-side gate: is THIS app allowed to issue automated start/stop (voltage + temp rules)?
    Persisted in state.json, default on. Completely independent of the genset's built-in auto mode, which
    we never use (enabling it cranks the engine unconditionally — confirmed bug)."""
    return load_state().get("automation_enabled", True)

def _atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)

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
            "automation_enabled": automation_enabled(), "priming": load_primectl()}

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

# ----------------------------------------------------------------------------- remote temp sensors
class AddSensor(BaseModel):
    mac: str
    zone: str = ""

@app.get("/api/version")
async def api_version():
    return await mgr.op(lambda a: a.get_sw_version())

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
            for e in load_schedules():
                if not e.get("enabled") or e.get("time") != hhmm:
                    continue
                if now.weekday() not in e.get("days", []):
                    continue
                if _sched_fired.get(e["id"]) == stamp:
                    continue
                _sched_fired[e["id"]] = stamp
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

@app.on_event("startup")
async def _on_startup():
    mgr.autoconnect = load_state().get("autoconnect", True)
    asyncio.create_task(scheduler_loop())
    asyncio.create_task(autoconnect_loop())
    asyncio.create_task(temp_control_loop())
    # SOC rule retired in favour of the voltage rule below (single battery authority). The loop +
    # endpoints are kept for the lithium-era revert (flat LFP curve → SOC+shunt beats voltage); re-add
    # this line and its UI card then. Not registering it means a stale socctl.json can't reactivate it.
    # asyncio.create_task(soc_control_loop())
    asyncio.create_task(volt_control_loop())
    asyncio.create_task(stats_sampler_loop())
    asyncio.create_task(event_watch_loop())

class AutoConnectReq(BaseModel):
    enabled: bool

@app.post("/api/autoconnect")
async def api_autoconnect(a: AutoConnectReq):
    mgr.autoconnect = a.enabled
    st = load_state(); st["autoconnect"] = a.enabled; save_state(st)
    if a.enabled:
        mgr.user_disconnected = False   # re-arm; the loop will reconnect
    return {"ok": True}

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

def load_tempctl():
    try:
        with open(TEMPCTL_PATH) as f:
            return {**TEMPCTL_DEFAULT, **json.load(f)}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(TEMPCTL_DEFAULT)

def save_tempctl(c):
    _atomic_write(TEMPCTL_PATH, c)

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

async def temp_control_loop():
    global _temp_started, _temp_start_ts
    while True:
        try:
            cfg = load_tempctl()
            if cfg.get("enabled") and automation_enabled() and mgr.state == "connected":
                temp = _current_temp()
                state = (mgr.ags.telemetry.get("status") or {}).get("state") if mgr.ags else None
                running = state in RUN_CYCLE_STATES
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

def load_socctl():
    try:
        with open(SOCCTL_PATH) as f:
            return {**SOCCTL_DEFAULT, **json.load(f)}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(SOCCTL_DEFAULT)

def save_socctl(c):
    _atomic_write(SOCCTL_PATH, c)

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

def load_voltctl():
    try:
        with open(VOLTCTL_PATH) as f:
            return {**VOLTCTL_DEFAULT, **json.load(f)}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(VOLTCTL_DEFAULT)

def save_voltctl(c):
    _atomic_write(VOLTCTL_PATH, c)

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
    """House-battery voltage (V) from DC-volts telemetry, or None. avg=True returns the genset's smoothed
    value (long→short avg) for the START decision so a momentary load sag can't false-trigger a run;
    falls back to the instant reading when no average is reported."""
    if not mgr.ags:
        return None
    dc = mgr.ags.telemetry.get("dcvolts") or {}
    if avg:
        for k in ("house_v_long", "house_v_short", "house_v"):
            if dc.get(k) is not None:
                return dc[k]
        return None
    return dc.get("house_v")

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

def load_primectl():
    try:
        with open(PRIMECTL_PATH) as f:
            return {**PRIMECTL_DEFAULT, **json.load(f)}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(PRIMECTL_DEFAULT)

def save_primectl(c):
    _atomic_write(PRIMECTL_PATH, c)

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

# ----------------------------------------------------------------------------- telemetry history (Stats)
# Log house voltage / SOC / run-state to SQLite every minute so the Stats tab can chart trends and
# charge cycles. SQLite (stdlib) survives restarts and stays tiny: 1 row/min ≈ 0.5M rows/yr. We keep
# raw samples for STATS_RETENTION_DAYS and downsample server-side per query so charts stay light.
STATS_SAMPLE_SEC = 60
STATS_RETENTION_DAYS = 180
STATS_RANGES = {"24h": 86400, "7d": 7*86400, "30d": 30*86400, "all": None}

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

# ----------------------------------------------------------------------------- maintenance log
# A user-entered service logbook (oil, filters, plugs, etc.) — manual notes only, distinct from the
# auto activity log. The tested genset doesn't report engine hours, so this is date + free text, and
# back-dating is allowed. Stored in maintenance.json (gitignored — personal rig data, not for the repo).
MAINT_PATH = os.path.join(HERE, "maintenance.json")

def load_maintenance():
    try:
        with open(MAINT_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_maintenance(m):
    _atomic_write(MAINT_PATH, m)

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

@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(HERE, "index.html")) as f:
        return f.read()

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("AGS_PORT", "8722"))
    print(f"EC-AGS+ dashboard → http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
