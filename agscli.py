#!/usr/bin/env python3
"""
agscli — control a Cummins EC-AGS+ generator over BLE from macOS or a Raspberry Pi.

Protocol reverse-engineered from the official 'Cummins EC-AGS+' Android app (Saucon TDS).
See PROTOCOL.md for the full write-up. Uses `bleak`, which works on CoreBluetooth (macOS)
and BlueZ (Linux/Raspberry Pi) with the same code.

Examples:
    python3 agscli.py scan
    python3 agscli.py --address <ADDR> --password <PW> status
    python3 agscli.py --address <ADDR> --password <PW> start
    python3 agscli.py --address <ADDR> --password <PW> stop
    python3 agscli.py --address <ADDR> --password <PW> monitor

On macOS, <ADDR> is the CoreBluetooth UUID printed by `scan`. On Linux it is the MAC (AA:BB:..).
If the generator was never assigned a password in the app, use the macId shown by `scan` as the
password.
"""
import argparse
import asyncio
import base64
import hashlib
import struct
import sys

from bleak import BleakClient, BleakScanner

# ---- UUIDs (see PROTOCOL.md §1) -------------------------------------------------
BASE = "5355-5253-4454-4e4f43554153"
def _u(short): return f"0000{short}-{BASE}"

SVC                 = _u("1523")
FROM_GENSET_COUNTER = _u("1541")   # we write acks here
FROM_GENSET_DATA    = _u("1542")   # notify: genset -> app RPC
FROM_APP_COUNTER    = _u("1543")   # notify: paces our chunked writes
FROM_APP_DATA       = _u("1544")   # we write RPC here
CH_DCVOLTS          = _u("1710")
CH_STATUS           = _u("1711")
CH_TEMP             = _u("1712")
CH_READINGS         = _u("1713")
CH_MONDATA          = _u("1715")

TELEMETRY = {
    CH_STATUS:   "status",
    CH_READINGS: "readings",
    CH_DCVOLTS:  "dcvolts",
    CH_MONDATA:  "mondata",
    CH_TEMP:     "temp",
}

# ---- RPC opcodes (see PROTOCOL.md §5) -------------------------------------------
RPC_START_GEN        = 2509
RPC_PREHEAT_GEN      = 2510
RPC_STOP_GEN         = 2511
RPC_GEN_STATUS       = 2513
RPC_PRIME_START      = 2574
RPC_PRIME_STOP       = 2575
RPC_AUTO_ON          = 2572
RPC_AUTO_OFF         = 2573
RPC_RESETFAULT       = 2546
RPC_FAULTCODEALL     = 2549   # request full fault/event history
RPC_MODELSERIAL_GET  = 2570
RPC_ADV_PARAMS_GET   = 2564   # auto start/stop (charge) params
RPC_ADV_PARAMS_PUT   = 2565   # also the opcode the genset replies with
RPC_QUIET_GET        = 2568
RPC_QUIET_PUT        = 2569
RPC_GET_SEED         = 2427
RPC_TEMP_SEED        = 2482
RPC_PASS_PHRASE      = 2433
RPC_PASS_PHRASE_STAT = 2434
RPC_AUTH_CHECK       = 2486
RPC_AUTH_VALID       = 2487
RPC_AUTH_REQUIRED    = 2488

SALT = b"ST3094#n353u~tn0yng^^e@4*53vjGS"   # AuthManager.validatePassword

GEN_STATUS_LABELS = {0: "Off/Unknown", 1: "Stopped", 2: "Running", 3: "Cranking", 4: "Priming"}

# ================================================================================
# RPC (de)serialization  — all ints little-endian. See PROTOCOL.md §3.
# ================================================================================
def _le32(n): return struct.pack("<I", n & 0xFFFFFFFF)
def _pad4(b): return b + b"\x00" * ((-len(b)) % 4)

def _crc(buf):
    c = 0xFF
    for byte in buf:
        c ^= byte
    return c & 0xFF

def build_rpc(opcode, params=None):
    """params: list of ('L', int) | ('S', str) | ('B', bytes). Returns the RPC buffer."""
    params = params or []
    types = ""
    pdata = b""
    for t, v in params:
        if t == "L":
            types += "L"; pdata += _le32(int(v))
        elif t == "S":
            types += "S"; s = v.encode(); pdata += _pad4(_le32(len(s) + 1) + s + b"\x00")
        elif t == "B":
            types += "B"; pdata += _pad4(_le32(len(v)) + v)
        else:
            raise ValueError(f"bad param type {t!r}")
    marshal = _pad4(_le32(len(types))) + _pad4(types.encode())
    total_len = 4 + 4 + len(marshal) + len(pdata) + 4
    body = _le32(total_len) + _le32(opcode) + marshal + pdata
    return body + _le32(_crc(body))

def parse_rpc(buf):
    """Inverse of build_rpc. Returns (opcode, [(type, value), ...]) or raises ValueError."""
    if len(buf) < 12:
        raise ValueError("rpc too short")
    total = struct.unpack_from("<I", buf, 0)[0]
    opcode = struct.unpack_from("<I", buf, 4)[0]
    if struct.unpack_from("<I", buf, total - 4)[0] != _crc(buf[:total - 4]):
        raise ValueError("CRC mismatch")
    count = struct.unpack_from("<I", buf, 8)[0]
    off = 12 + count
    off += (-off) % 4
    types = buf[12:12 + count].decode("latin1")
    params = []
    for t in types:
        if t == "L":
            params.append(("L", struct.unpack_from("<I", buf, off)[0])); off += 4
        elif t == "S":
            slen = struct.unpack_from("<I", buf, off)[0]; off += 4
            params.append(("S", buf[off:off + slen - 1].decode("utf-8", "replace")))
            off += slen; off += (-off) % 4
        elif t == "B":
            blen = struct.unpack_from("<I", buf, off)[0]; off += 4
            params.append(("B", bytes(buf[off:off + blen]))); off += blen; off += (-off) % 4
    return opcode, params

# ================================================================================
# Telemetry decode  — see PROTOCOL.md §7
# ================================================================================
def _u16(b, i):  # little-endian u16, None if invalid (0xFFFF)
    v = b[i] | (b[i + 1] << 8)
    return None if v == 0xFFFF else v
def _u8(b, i):
    return None if b[i] == 0xFF else b[i]

def decode_status(b):
    if len(b) < 14: return {}
    return {
        "state": GEN_STATUS_LABELS.get(b[0] & 0x0F, b[0] & 0x0F),
        "auto_mode": bool((b[0] >> 4) & 0x0F),
        "soc_house_%": b[1], "soc_engine_%": b[2],
        "quiet_time": bool(b[3] & 1),
        "fault_code": b[4],
        "last_started": struct.unpack_from("<I", b, 6)[0],
        "run_seconds": struct.unpack_from("<I", b, 10)[0],
    }

def decode_readings(b):
    if len(b) < 14: return {}
    bv = _u16(b, 8)
    return {
        "engine_rpm": _u16(b, 0), "output_hz": _u8(b, 2), "load_%": _u8(b, 3),
        "engine_temp": _u16(b, 4), "output_vac": _u16(b, 6),
        "battery_v": (bv / 10.0) if bv is not None else None,
        "load_current_a": _u16(b, 10), "engine_hours": _u16(b, 12),
    }

def decode_dcvolts(b):
    # 6× u16 LE ÷100 → volts: house(instant), engine(instant), house short/long avg, engine short/long avg.
    # An unwired sense lead floats to a small "ghost voltage"; treat <1V as not-connected (None).
    def f(i):
        if len(b) < i + 2:
            return None
        v = struct.unpack_from("<H", b, i)[0] / 100.0
        return v if v >= 1.0 else None
    return {"house_v": f(0), "engine_v": f(2),
            "house_v_short": f(4), "house_v_long": f(6),
            "engine_v_short": f(8), "engine_v_long": f(10)}

def decode_mondata(b):
    if len(b) < 9: return {}
    flags = b[8]
    return {"manifold_pressure": _u16(b, 0), "manifold_temp": _u16(b, 2),
            "oil_temp": _u16(b, 4), "inverter_temp": _u16(b, 6),
            "fuel_pump": (flags & 1) if not (flags >> 4) & 1 else None,
            "starter": ((flags >> 1) & 1) if not (flags >> 5) & 1 else None}

DECODERS = {"status": decode_status, "readings": decode_readings,
            "dcvolts": decode_dcvolts, "mondata": decode_mondata}

# ================================================================================
# BLE client wrapper
# ================================================================================
class AGS:
    def __init__(self, client: BleakClient, password: str, verbose=False):
        self.c = client
        self.password = password
        self.verbose = verbose
        self._rx = bytearray()         # accumulates FROM_GENSET_DATA chunks
        self._rx_counter = 0
        self._app_counter = asyncio.Event()  # set when genset asks for next write chunk
        self._rpc_waiters = {}         # opcode -> asyncio.Future
        self.telemetry = {}            # name -> decoded dict

    def _log(self, *a):
        if self.verbose: print("  ·", *a, file=sys.stderr)

    async def setup(self):
        # Subscribe to RPC + counter + telemetry notifications.
        await self.c.start_notify(FROM_GENSET_DATA, self._on_genset_data)
        await self.c.start_notify(FROM_APP_COUNTER, self._on_app_counter)
        for uuid, name in TELEMETRY.items():
            try:
                await self.c.start_notify(uuid, self._make_telemetry_cb(name))
            except Exception as e:
                self._log(f"telemetry {name} subscribe failed: {e}")

    # ---- notification handlers --------------------------------------------------
    def _on_app_counter(self, _ch, data):
        if data and int.from_bytes(data, "little") > 0:
            self._app_counter.set()

    def _make_telemetry_cb(self, name):
        def cb(_ch, data):
            dec = DECODERS.get(name, lambda b: {"raw": bytes(b).hex()})(bytes(data))
            self.telemetry[name] = dec
            self._log(f"{name}: {dec}")
        return cb

    def _on_genset_data(self, _ch, data):
        self._rx += bytes(data)
        # complete when we have the le16-prefixed length
        if len(self._rx) >= 2:
            want = self._rx[0] | (self._rx[1] << 8)   # length of the RPC buffer following the prefix
            if want and len(self._rx) >= want + 2:
                buf = bytes(self._rx[2:want + 2])
                self._rx = bytearray()
                self._rx_counter = 0
                asyncio.create_task(self._ack_genset(0))
                self._dispatch_rpc(buf)
                return
        # partial: ack with running counter to request next chunk
        if self._rx_counter < 255:
            self._rx_counter += 1
            asyncio.create_task(self._ack_genset(self._rx_counter))

    async def _ack_genset(self, n):
        try:
            await self.c.write_gatt_char(FROM_GENSET_COUNTER, bytes([n]), response=True)
        except Exception as e:
            self._log(f"genset-counter ack failed: {e}")

    def _dispatch_rpc(self, buf):
        try:
            opcode, params = parse_rpc(buf)
        except Exception as e:
            self._log(f"rpc parse error: {e} ({buf.hex()})"); return
        self._log(f"RPC in: opcode={opcode} params={params}")
        fut = self._rpc_waiters.pop(opcode, None)
        if fut and not fut.done():
            fut.set_result(params)

    # ---- sending ----------------------------------------------------------------
    async def send_rpc(self, opcode, params=None, expect=None, timeout=10.0):
        """Send an RPC; if `expect` (opcode int or iterable of ints) is given, wait for the
        first matching response and return its params."""
        fut = None
        expects = ()
        if expect is not None:
            expects = (expect,) if isinstance(expect, int) else tuple(expect)
            fut = asyncio.get_event_loop().create_future()
            for op in expects:
                self._rpc_waiters[op] = fut

        buf = build_rpc(opcode, params)
        payload = struct.pack("<H", len(buf)) + buf
        chunks = [payload[i:i + 20] for i in range(0, len(payload), 20)]
        self._log(f"RPC out: opcode={opcode} ({len(payload)}B, {len(chunks)} chunk(s))")

        await self.c.write_gatt_char(FROM_APP_DATA, chunks[0], response=True)
        for chunk in chunks[1:]:
            self._app_counter.clear()
            try:
                await asyncio.wait_for(self._app_counter.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._log("no FROM_APP_COUNTER pacing notify; writing next chunk anyway")
            await self.c.write_gatt_char(FROM_APP_DATA, chunk, response=True)

        if fut is not None:
            try:
                return await asyncio.wait_for(fut, timeout=timeout)
            finally:
                for op in expects:
                    if self._rpc_waiters.get(op) is fut:
                        del self._rpc_waiters[op]

    # ---- auto start/stop & quiet-time parameters (see PROTOCOL.md §5) ------------
    async def get_auto_params(self):
        """Read the auto start/stop (charge) parameters from the genset."""
        params = await self.send_rpc(RPC_ADV_PARAMS_GET,
                                     expect=(RPC_ADV_PARAMS_PUT, RPC_ADV_PARAMS_GET), timeout=10.0)
        v = [val for _, val in params]
        if len(v) < 12:
            raise RuntimeError(f"unexpected auto-params response: {params}")
        return {
            "ac_sense_enabled": bool(v[0]),
            "zones": [{"name": v[1], "temp": v[2]},
                      {"name": v[3], "temp": v[4]},
                      {"name": v[5], "temp": v[6]}],
            "battery_sense_enabled": v[7] == 1,
            "start_volts": v[8] / 10.0,
            "stop_volts": v[9] / 10.0,
            "start_time_sec": v[10],
            "stop_time_sec": v[11],
        }

    async def set_auto_params(self, a):
        """Write auto start/stop params. `a` has the same shape get_auto_params returns."""
        z = a["zones"]
        params = [
            ("L", 1 if a["ac_sense_enabled"] else 0),
            ("S", z[0]["name"]), ("L", int(z[0]["temp"])),
            ("S", z[1]["name"]), ("L", int(z[1]["temp"])),
            ("S", z[2]["name"]), ("L", int(z[2]["temp"])),
            ("L", 1 if a["battery_sense_enabled"] else 2),    # 1=on, 2=off (per app)
            ("L", int(round(float(a["start_volts"]) * 10))),
            ("L", int(round(float(a["stop_volts"]) * 10))),
            ("L", int(a["start_time_sec"])),
            ("L", int(a["stop_time_sec"])),
        ]
        await self.send_rpc(RPC_ADV_PARAMS_PUT, params)

    async def get_fault_history(self):
        """Return the genset's fault/event log as [{time, code}, ...] (newest order as stored).
        Each record on the wire is 4-byte LE unix timestamp + 1-byte fault code."""
        params = await self.send_rpc(RPC_FAULTCODEALL, expect=RPC_FAULTCODEALL, timeout=12.0)
        blob = next((v for t, v in params if t == "B"), b"")
        events = []
        for i in range(0, len(blob) - 4, 5):
            ts = struct.unpack_from("<I", blob, i)[0]
            if ts == 0:
                continue
            events.append({"time": ts, "code": blob[i + 4]})
        return events

    async def get_quiet(self):
        params = await self.send_rpc(RPC_QUIET_GET,
                                     expect=(RPC_QUIET_PUT, RPC_QUIET_GET), timeout=10.0)
        v = [val for _, val in params]
        return {"enabled": bool(v[0]) if v else False,
                "day": v[1] if len(v) > 1 else 0,
                "start_sec": v[2] if len(v) > 2 else 0,
                "stop_sec": v[3] if len(v) > 3 else 0}

    async def set_quiet(self, q):
        start, stop = int(q["start_sec"]), int(q["stop_sec"])
        if start > stop:
            stop += 86400
        await self.send_rpc(RPC_QUIET_PUT, [("L", 1 if q["enabled"] else 0),
                                            ("L", int(q["day"])), ("L", start), ("L", stop)])

    # ---- authentication ---------------------------------------------------------
    async def authenticate(self):
        params = await self.send_rpc(RPC_GET_SEED, expect=RPC_TEMP_SEED, timeout=10.0)
        seed = next((v for t, v in params if t == "S"), None)
        if seed is None:
            raise RuntimeError(f"no seed in RPC_TEMP_SEED response: {params}")
        self._log(f"seed={seed!r}")
        digest = hashlib.sha256(SALT + seed.encode() + self.password.encode()).digest()
        passphrase = base64.b64encode(digest).decode()
        result = await self.send_rpc(RPC_PASS_PHRASE, [("S", passphrase)],
                                     expect=RPC_PASS_PHRASE_STAT, timeout=10.0)
        ok = any(t == "L" and v == 1 for t, v in result)
        if not ok:
            raise RuntimeError(f"login failed (wrong password?) status={result}")
        return True

# ================================================================================
# Connection helper + commands
# ================================================================================
async def connect(address, password, verbose):
    print(f"Connecting to {address} …")
    async with BleakClient(address, timeout=20.0) as client:
        ags = AGS(client, password, verbose)
        await ags.setup()
        print("Authenticating …")
        await ags.authenticate()
        print("Authenticated ✓")
        return client, ags  # NOTE: caller must run inside the context; see run_command

async def run_command(args):
    async with BleakClient(args.address, timeout=20.0) as client:
        ags = AGS(client, args.password, args.verbose)
        await ags.setup()
        print("Authenticating …")
        await ags.authenticate()
        print("Authenticated ✓\n")

        cmd = args.command
        if cmd == "status":
            await ags.send_rpc(RPC_GEN_STATUS)            # nudge a fresh push
            await asyncio.sleep(1.5)
            _print_telemetry(ags)
        elif cmd == "start":
            await ags.send_rpc(RPC_START_GEN)
            print("→ START sent. Watching state…")
            await _watch_state(ags, ("Cranking", "Running"))
        elif cmd == "stop":
            await ags.send_rpc(RPC_STOP_GEN)
            print("→ STOP sent. Watching state…")
            await _watch_state(ags, ("Stopped",))
        elif cmd == "auto-on":
            await ags.send_rpc(RPC_AUTO_ON);  print("→ Auto mode ON")
        elif cmd == "auto-off":
            await ags.send_rpc(RPC_AUTO_OFF); print("→ Auto mode OFF")
        elif cmd == "reset-fault":
            await ags.send_rpc(RPC_RESETFAULT); print("→ Reset fault sent")
        elif cmd == "info":
            params = await ags.send_rpc(RPC_MODELSERIAL_GET, expect=RPC_MODELSERIAL_GET, timeout=8.0)
            strs = [v for t, v in params if t == "S"]
            print("Model: ", strs[0] if strs else "?")
            print("Serial:", strs[1] if len(strs) > 1 else "?")
        elif cmd == "monitor":
            print("Streaming telemetry — Ctrl-C to stop.\n")
            try:
                while True:
                    await asyncio.sleep(2.0)
                    _print_telemetry(ags, oneline=True)
            except (asyncio.CancelledError, KeyboardInterrupt):
                pass
        else:
            print(f"unknown command {cmd}")

def _print_telemetry(ags, oneline=False):
    if not ags.telemetry:
        print("(no telemetry received yet)"); return
    if oneline:
        s = ags.telemetry.get("status", {}); r = ags.telemetry.get("readings", {})
        dc = ags.telemetry.get("dcvolts", {})
        print(f"[{s.get('state','?'):8}] rpm={r.get('engine_rpm')} "
              f"{r.get('output_vac')}VAC {r.get('output_hz')}Hz load={r.get('load_%')}% "
              f"house={dc.get('house_v', r.get('battery_v'))}V/{s.get('soc_house_%')}% "
              f"start={dc.get('engine_v')}V/{s.get('soc_engine_%')}% "
              f"fault={s.get('fault_code')} run_s={s.get('run_seconds')}")
    else:
        for name, dec in ags.telemetry.items():
            print(f"{name}:")
            for k, v in dec.items():
                print(f"    {k:16} {v}")

async def _watch_state(ags, targets, timeout=45.0):
    loop = asyncio.get_event_loop(); end = loop.time() + timeout
    last = None
    while loop.time() < end:
        st = ags.telemetry.get("status", {}).get("state")
        if st != last:
            print(f"    state: {st}"); last = st
        if st in targets:
            print("    ✓ reached"); return
        await asyncio.sleep(1.0)
    print("    (timeout waiting for target state)")

# ================================================================================
# scan
# ================================================================================
async def do_scan(timeout):
    print(f"Scanning {timeout}s for EC-AGS+ (service {SVC}) …\n")
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True,
                                          service_uuids=[SVC])
    if not devices:
        print("No EC-AGS+ found. Make sure the generator is powered and the phone app is closed.")
        return
    for addr, (dev, adv) in devices.items():
        mac_id, status = "?", "?"
        if adv.manufacturer_data:
            raw = next(iter(adv.manufacturer_data.values()))
            if raw:
                status = {0: "UNREGISTERED (password = macId below)",
                          1: "registered", 2: "registered", 3: "registered"}.get(raw[0], raw[0])
                mac_id = raw[1:].hex().upper()
        print(f"  address : {addr}")
        print(f"  name    : {adv.local_name or dev.name}")
        print(f"  rssi    : {adv.rssi} dBm")
        print(f"  status  : {status}")
        print(f"  macId   : {mac_id}   (use as --password if UNREGISTERED)\n")

# ================================================================================
def main():
    p = argparse.ArgumentParser(description="Control a Cummins EC-AGS+ generator over BLE.")
    p.add_argument("-a", "--address", help="BLE address (CoreBluetooth UUID on macOS, MAC on Linux)")
    p.add_argument("-p", "--password", default="", help="app password (or macId if unregistered)")
    p.add_argument("-v", "--verbose", action="store_true", help="log raw RPC/telemetry traffic")
    p.add_argument("--scan-timeout", type=float, default=8.0)
    p.add_argument("command", choices=["scan", "status", "start", "stop", "auto-on", "auto-off",
                                        "reset-fault", "info", "monitor"])
    args = p.parse_args()

    if args.command == "scan":
        asyncio.run(do_scan(args.scan_timeout)); return
    if not args.address:
        p.error("--address is required (run `scan` first to find it)")
    try:
        asyncio.run(run_command(args))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr); sys.exit(1)

if __name__ == "__main__":
    main()
