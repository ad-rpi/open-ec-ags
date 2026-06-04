# Cummins EC-AGS+ BLE Protocol

Reverse-engineered from `Cummins EC-AGS+_1.0.58` APK (vendor: **Saucon TDS**, package
`saucon.android.android_ec30plus_app.cummins`). All offsets/UUIDs below come directly from the
decompiled app (`jadx_out/sources/saucon/android/android_ec30plus_app/Bluetooth/`).

The genset runs a Nordic-style BLE GATT server. Everything is a custom RPC layer carried over four
GATT characteristics, plus five "push" telemetry characteristics that stream live readings.

---

## 1. GATT layout

Base UUID suffix: `5355-5253-4454-4E4F43554153` (ASCII of the bytes reversed spells `SAUCON TDS`).

**Primary service:** `00001523-5355-5253-4454-4e4f43554153`

| Characteristic UUID (16-bit) | Name              | Dir | Use |
|------------------------------|-------------------|-----|-----|
| `00001541` | RPC_FROM_GENSET_COUNTER | app **writes** | ack/flow-control for long reads from genset |
| `00001542` | RPC_FROM_GENSET_DATA    | **notify**     | genset → app RPC bytes (chunked) |
| `00001543` | RPC_FROM_APP_COUNTER    | **notify**     | genset paces app's chunked writes |
| `00001544` | RPC_FROM_APP_DATA       | app **writes** | app → genset RPC bytes (chunked) |
| `00001710` | GensetDCVolts   | notify | DC battery voltages |
| `00001711` | GensetStatus    | notify | run state, mode, SOC, fault code, run seconds |
| `00001712` | GensetTempData  | notify | remote temp sensor reading |
| `00001713` | GensetReadings  | notify | RPM, Hz, load, volts, battery, hours |
| `00001715` | GensetMonData   | notify | manifold/oil/inverter temps, pump status |
| `00001566` | MAIN_V          | read   | firmware/main version |

**DFU service** (firmware update, Nordic): `0000fe59-0000-1000-8000-00805f9b34fb` — ignore for control.

All notify characteristics are enabled by writing `0100` to their CCCD (`0x2902`); `bleak`'s
`start_notify` does this for you.

---

## 2. Integer encoding

Everything multi-byte is **little-endian**. The app's `UtilFunctions.intToByte` does a byte-swap then a
big-endian `putInt`, which is just LE. `getInt` zero-pads to 4 bytes then swaps. So:

* 32-bit fields: 4-byte LE.
* The 2-byte transport length prefix: 2-byte LE.
* Telemetry uses 1- or 2-byte LE unsigned values (a field of all `0xFF` means "invalid / no data").

---

## 3. RPC wire format

An RPC = an opcode (int) + ordered list of typed parameters. Parameter types:

* `L` — 32-bit int (4 bytes LE)
* `S` — UTF-8 string: `le32(len+1)` then bytes then a `0x00`, then zero-pad to a 4-byte boundary
* `B` — byte buffer: `le32(len)` then bytes, then zero-pad to a 4-byte boundary

The serialized buffer (`RPC.generateBufferVersion`) is:

```
+-------------+----------+------------------+------------------+----------+----------+
| totalLen(4) | opcode(4)| paramCount(4)    | typeString + pad | paramData| crc(4)   |
+-------------+----------+------------------+------------------+----------+----------+
                         \---- marshal block (padded to 4) ---/
```

* `totalLen` = LE length of the **entire** buffer including itself and the CRC.
* `paramCount` = number of params; `typeString` = the type chars e.g. `"L"`, `"LS"`, `"B"` (then 0-pad to 4).
* `paramData` = each param encoded as above, in order.
* `crc` (4 bytes LE, only low byte significant) = XOR-fold of every preceding byte
  (`totalLen..paramData`) starting from `0xFF`, masked to 8 bits.

A no-param command (start/stop/status) is therefore 16 bytes: `len(4)+op(4)+count=0(4)+crc(4)`.

### Transport framing & chunking

Before sending, the app prepends a **2-byte LE length** of the RPC buffer, then writes the result to
`RPC_FROM_APP_DATA` in **20-byte chunks** (default ATT MTU; the app never negotiates a bigger MTU):

```
[ le16(len(buffer)) ][ buffer bytes ... ]   split into <=20-byte GATT writes
```

Flow control: after the first chunk, the genset sends a notification on `RPC_FROM_APP_COUNTER` with a
value > 0; each such notification means "send the next 20-byte chunk." Most control commands fit in one
chunk (18 bytes) so no pacing is needed.

Reads from the genset arrive on `RPC_FROM_GENSET_DATA`, also `[le16(len)][buffer]`, possibly chunked.
The app accumulates chunks; the message is complete once it has `len` bytes. After **each** chunk it
acks by writing one byte (a running counter, starting at 1) to `RPC_FROM_GENSET_COUNTER`; on the final
chunk it writes `0x00` and then parses the RPC. The CRC is verified on parse.

---

## 4. Authentication handshake (required before commands work)

On connect, after enabling notifications, the app authenticates:

1. App → `RPC_GET_SEED` (opcode **2427**, no params).
2. Genset → `RPC_TEMP_SEED` (opcode **2482**) with one `S` param = a seed string.
3. App computes:
   ```
   passphrase = base64( SHA256( SALT + seed + password ) )
   SALT = "ST3094#n353u~tn0yng^^e@4*53vjGS"   (UTF-8, literal, from AuthManager)
   ```
   (concatenation order is salt, then seed, then password, all UTF-8.)
4. App → `RPC_PASS_PHRASE` (opcode **2433**) with one `S` param = that base64 string.
5. Genset → `RPC_PASS_PHRASE_STATUS` (opcode **2434**) with one `L` param: **1 = success, 0 = fail**.
6. Optionally App → `RPC_AUTHORIZATION_CHECK` (2486) → genset replies `RPC_AUTHORIZATION_VALID` (2487)
   or `RPC_AUTHORIZATION_REQUIRED` (2488).

### What is the "password"?

* If the genset was **never registered** (advertised status byte = 0, see §6), the password is the
  device's **macId** — the advertised manufacturer-data hex string (see §6). This is what
  `AssignPasswordActivity` uses for the initial login.
* Once a password has been assigned in the app, it is whatever **you** set. Setting one uses
  `RPC_SET_PASSPHRASE` (2480) with params `[L=0, S=base64(password_plaintext)]` — note assignment sends
  the base64 of the *plaintext* password (not hashed); the seed-hash is only used for *login*.

Insufficient-authentication GATT errors (status 5/15) trigger Android bonding (`createBond`). The link
may therefore require BLE pairing ("Just Works"). macOS pairs automatically; on BlueZ pre-pair with
`bluetoothctl` if a write is rejected.

---

## 5. RPC opcodes (subset; full list in `RPCDef.java`)

Base `2500`. Commands take no params unless noted.

| Opcode | Name | Meaning |
|-------:|------|---------|
| 2509 | RPC_START_GEN | start the generator |
| 2510 | RPC_PREHEAT_GEN | preheat (glow) |
| 2511 | RPC_STOP_GEN | stop the generator |
| 2513 | RPC_GEN_STATUS | request a status push |
| 2574 | RPC_GEN_PRIME_START | start fuel prime |
| 2575 | RPC_GEN_PRIME_STOP | stop fuel prime |
| 2572 / 2573 | RPC_ON_OFF_MODE_ON / _OFF | enable / disable Auto mode |
| 2546 | RPC_RESETFAULT | clear active fault |
| 2548 / 2549 | RPC_FAULTCODEREAD / _ALL | read fault code(s) |
| 2570 | RPC_MODELSERIAL_GET | model + serial (returns 2× `S`) |
| 2580 / 2581 | RPC_GEN_NICKNAME_GET / _PUT | nickname |
| 2516 / 2517 | RPC_VSTART / RPC_VSTOP | auto start/stop voltage thresholds |
| 2427 | RPC_GET_SEED | request auth seed |
| 2482 | RPC_TEMP_SEED | seed response (`S`) |
| 2433 | RPC_PASS_PHRASE | send hashed passphrase (`S`) |
| 2434 | RPC_PASS_PHRASE_STATUS | login result (`L`: 1=ok) |
| 2480 | RPC_SET_PASSPHRASE | set password (`L=0`, `S=base64(pw)`) |
| 2486/2487/2488 | RPC_AUTHORIZATION_CHECK / _VALID / _REQUIRED | auth state |

---

## 6. Advertisement / scanning

The app scans filtering on the service UUID `00001523-...`. The first manufacturer-specific data
record encodes:

* **byte[0]** = registration/password status: `0` = not yet registered (no password set),
  `1`/`2`/`3` = registered (password type).
* **bytes[1:]** = the **macId**, formatted as uppercase hex. This is the device identity and the
  default login password when unregistered. The display name is `"EC-AGS+ " + last 4 hex chars`.

(Temp-sensor accessories advertise under manufacturer ID 2203 with prefix `5E A6` — not the genset.)

---

## 7. Telemetry decode

### GensetStatus (`00001711`, ≥14 bytes)
| Bytes | Field | Notes |
|------|-------|------|
| 0 low nibble | genStatus | **1=Stopped, 2=Running, 3=Cranking, 4=Priming** (0=off/unknown) |
| 0 high nibble | genMode | 1 = Auto mode on |
| 1 | socHouse | house battery state-of-charge % |
| 2 | socEngine | engine battery SOC % |
| 3 bit0 | quietStatus | in quiet-time |
| 3 bit1 | c21Prog | |
| 3 bit2 | hvacSense | |
| 3 bit3 | breakinEnabled | |
| 3 bit4 | accelFault | |
| 4 | faultCode | 0 = none |
| 5 low nibble | autoMode | |
| 5 bit4/5/6/7 | onTemp / onBatt / onPrefill / onMinRT | auto-run reasons |
| 6..9 | lastStarted | unix seconds (LE) ×1000 |
| 10..13 | runSeconds | total run seconds (LE) |

### GensetReadings (`00001713`, ≥14 bytes)
| Bytes | Field | Scale |
|------|-------|------|
| 0..1 | engineRpm | — |
| 2 | outputFrequency | Hz |
| 3 | load | % |
| 4..5 | engineTemp | — |
| 6..7 | outputVolts | VAC |
| 8..9 | batteryVolts | ÷10 → V |
| 10..11 | loadCurrent | A |
| 12..13 | engineHours | — |

A field of all `0xFF` = invalid/not-present.

### GensetDCVolts (`00001710`, ≥8 bytes), all ÷100 → V
`0..1` housebattDC, `2..3` enginebattDC, `4..5` housebattST, `6..7` housebattLT,
`8..9` enginebattST, `10..11` enginebattLT.

### GensetMonData (`00001715`, ≥9 bytes)
`0..1` manifoldPressure, `2..3` manifoldTemp, `4..5` oilTemp, `6..7` invertorTemp;
byte 8 bits: bit0 fuelPump, bit1 starter, bit2 liftPump, bit3 fuelType (each gated by a
"valid" bit in 4..7).

### GensetTempData (`00001712`, ≥17 bytes)
`0..8` nickname (ASCII, trimmed), `9..10` remoteTemp ÷100, `11..12` remoteBattery ÷1000,
`13..16` lastReading unix seconds (LE), optional `17..18` threshold, `19` order.
