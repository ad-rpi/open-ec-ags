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
import shutil
import subprocess
import uuid
from datetime import datetime

from bleak import BleakClient, BleakScanner
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import agscli as ag
import faultcodes

HERE = os.path.dirname(os.path.abspath(__file__))
DEVICES_PATH = os.path.join(HERE, "devices.json")
SCHEDULES_PATH = os.path.join(HERE, "schedules.json")
STATE_PATH = os.path.join(HERE, "state.json")    # remembers last device + autoconnect pref
TEMPCTL_PATH = os.path.join(HERE, "tempctl.json")  # cold-start (heater) temperature rule
SOCCTL_PATH = os.path.join(HERE, "socctl.json")    # low-house-SOC auto start/stop rule

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

def _atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)

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
            "autoconnect": mgr.autoconnect, "last": load_state().get("last")}

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
    "preheat": ag.RPC_PREHEAT_GEN, "prime-start": ag.RPC_PRIME_START,
    "prime-stop": ag.RPC_PRIME_STOP, "auto-on": ag.RPC_AUTO_ON, "auto-off": ag.RPC_AUTO_OFF,
    "reset-fault": ag.RPC_RESETFAULT,
}

@app.post("/api/command/{cmd}")
async def api_command(cmd: str):
    if cmd not in COMMANDS:
        raise HTTPException(400, f"unknown command {cmd}")
    await mgr.op(lambda ags: ags.send_rpc(COMMANDS[cmd]))
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

_sched_fired = {}   # entry id -> "YYYY-MM-DDTHH:MM" of last fire (dedupe within a minute)
_sched_log = []     # recent fire results, newest last

async def _scheduler_fire(entry):
    addr = entry["device"]
    saved = load_devices().get(addr, {})
    if entry["action"] not in COMMANDS:
        raise ValueError(f"bad action {entry['action']}")
    # Make sure we're connected to the right device, then send.
    if not (mgr.state == "connected" and mgr.address == addr):
        await mgr.connect(addr, saved.get("password", ""), saved.get("name"))
    async with mgr.lock:
        ags = mgr.require()
        await ags.send_rpc(COMMANDS[entry["action"]])

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
    asyncio.create_task(soc_control_loop())

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
    if s.action not in COMMANDS:
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
            if cfg.get("enabled") and mgr.state == "connected":
                temp = _current_temp()
                state = (mgr.ags.telemetry.get("status") or {}).get("state") if mgr.ags else None
                running = state in ("Running", "Cranking")
                if temp is not None:
                    sb, sa = cfg["start_below"], cfg["stop_above"]
                    minrun = cfg.get("min_run_min", 20) * 60
                    if temp <= sb and not running and not _temp_started:
                        try:
                            async with mgr.lock:
                                await mgr.require().send_rpc(ag.RPC_START_GEN)
                            _temp_started, _temp_start_ts = True, datetime.now()
                            _log_temp(f"START — temp {temp} ≤ {sb}")
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
            if cfg.get("enabled") and mgr.state == "connected":
                soc = _current_soc()
                state = (mgr.ags.telemetry.get("status") or {}).get("state") if mgr.ags else None
                running = state in ("Running", "Cranking")
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

@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(HERE, "index.html")) as f:
        return f.read()

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("AGS_PORT", "8722"))
    print(f"EC-AGS+ dashboard → http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
