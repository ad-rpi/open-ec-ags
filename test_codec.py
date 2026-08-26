#!/usr/bin/env python3
"""Codec tests for agscli.py — the pure (de)serialization layer only, no BLE.

The golden frames below were produced by the encoder AFTER it was byte-verified against the
official app's traffic on real hardware (see PROTOCOL.md). They freeze that verified behavior:
if a refactor changes any of these bytes, it no longer speaks the protocol the genset speaks.

    .venv/bin/python -m unittest test_codec -v
"""
import base64
import hashlib
import struct
import unittest

import agscli as ag


class TestCrc(unittest.TestCase):
    def test_known_values(self):
        # XOR-fold from 0xFF (PROTOCOL.md §3)
        self.assertEqual(ag._crc(b""), 0xFF)
        self.assertEqual(ag._crc(b"\xff"), 0x00)
        self.assertEqual(ag._crc(b"\x01\x02\x03"), 0xFF ^ 0x01 ^ 0x02 ^ 0x03)


class TestGoldenFrames(unittest.TestCase):
    """Byte-for-byte freezes of hardware-verified frames."""

    def test_start_gen(self):
        self.assertEqual(ag.build_rpc(ag.RPC_START_GEN).hex(),
                         "10000000cd090000000000002b000000")

    def test_stop_gen(self):
        self.assertEqual(ag.build_rpc(ag.RPC_STOP_GEN).hex(),
                         "10000000cf0900000000000029000000")

    def test_l_params(self):
        # ADV_PARAMS_PUT-style: two L params
        self.assertEqual(ag.build_rpc(2565, [("L", 1), ("L", 117)]).hex(),
                         "1c000000050a0000020000004c4c000001000000750000009a000000")

    def test_s_param(self):
        # PASS_PHRASE-style: one S param — length includes the NUL, data padded to 4
        self.assertEqual(ag.build_rpc(2433, [("S", "abc")]).hex(),
                         "1c00000081090000010000005300000004000000616263005d000000")

    def test_b_param(self):
        # SET_TSENSOR-style: one B param (6-byte MAC), padded to 4
        self.assertEqual(ag.build_rpc(2554, [("B", bytes.fromhex("0102030405ff"))]).hex(),
                         "20000000fa0900000100000042000000060000000102030405ff000097000000")


class TestRoundTrip(unittest.TestCase):
    def test_no_params(self):
        op, params = ag.parse_rpc(ag.build_rpc(ag.RPC_GEN_STATUS))
        self.assertEqual((op, params), (ag.RPC_GEN_STATUS, []))

    def test_mixed_params(self):
        sent = [("L", 0), ("L", 0xFFFFFFFF), ("S", "zone1"), ("B", b"\x00\x01\x02")]
        op, params = ag.parse_rpc(ag.build_rpc(2565, sent))
        self.assertEqual(op, 2565)
        self.assertEqual(params, sent)

    def test_string_lengths_across_pad_boundaries(self):
        # S encoding pads to 4 — every length mod 4 must survive the trip
        for n in range(1, 10):
            s = "x" * n
            _, params = ag.parse_rpc(ag.build_rpc(2433, [("S", s)]))
            self.assertEqual(params, [("S", s)], f"len {n}")

    def test_crc_mismatch_raises(self):
        buf = bytearray(ag.build_rpc(ag.RPC_START_GEN))
        buf[5] ^= 0x01                       # corrupt the opcode, CRC now wrong
        with self.assertRaises(ValueError):
            ag.parse_rpc(bytes(buf))

    def test_too_short_raises(self):
        with self.assertRaises(ValueError):
            ag.parse_rpc(b"\x01\x02\x03")


class TestAuth(unittest.TestCase):
    def test_passphrase_derivation(self):
        # base64(SHA256(SALT + seed + password)) — mirrors AGS auth (agscli.py) exactly.
        # Golden value freezes the SALT constant and the SALT|seed|password ordering.
        seed, password = "SEED1234", "pw"
        digest = hashlib.sha256(ag.SALT + seed.encode() + password.encode()).digest()
        self.assertEqual(base64.b64encode(digest).decode(),
                         "ra9AjpBzAIeuz0BK/Mo9wCEx0ipqMMdZ55U4Pa+e078=")


class TestDecoders(unittest.TestCase):
    def test_status(self):
        b = bytes([0x12, 66, 0, 0x01, 7]) + b"\x00" + struct.pack("<I", 1750000000) \
            + struct.pack("<I", 67260)
        d = ag.decode_status(b)
        self.assertEqual(d["state"], "Running")        # low nibble 2
        self.assertTrue(d["auto_mode"])                # high nibble 1
        self.assertEqual(d["soc_house_%"], 66)
        self.assertTrue(d["quiet_time"])
        self.assertEqual(d["fault_code"], 7)
        self.assertEqual(d["last_started"], 1750000000)
        self.assertEqual(d["uptime_seconds"], 67260)

    def test_status_short_frame_empty(self):
        self.assertEqual(ag.decode_status(b"\x00" * 5), {})

    def test_dcvolts_scaling_and_ghost(self):
        # u16 LE ÷100; <1V = unwired ghost → None
        b = struct.pack("<6H", 1234, 40, 1220, 1250, 0, 0)
        d = ag.decode_dcvolts(b)
        self.assertEqual(d["house_v"], 12.34)
        self.assertIsNone(d["engine_v"])               # 0.40V ghost
        self.assertEqual(d["house_v_short"], 12.20)
        self.assertEqual(d["house_v_long"], 12.50)
        self.assertIsNone(d["engine_v_short"])

    def test_dcvolts_truncated_frame(self):
        d = ag.decode_dcvolts(struct.pack("<2H", 1234, 1300))
        self.assertEqual(d["house_v"], 12.34)
        self.assertIsNone(d["house_v_short"])          # missing bytes → None, no crash

    def test_readings_not_available_markers(self):
        # 0xFFFF / 0xFF mean "not available" and must decode to None, never a number
        b = struct.pack("<HBBHHHHH", 0xFFFF, 0xFF, 0xFF, 0xFFFF, 1250, 128, 0xFFFF, 0xFFFF)
        d = ag.decode_readings(b)
        self.assertIsNone(d["engine_rpm"])
        self.assertIsNone(d["output_hz"])
        self.assertIsNone(d["load_%"])
        self.assertEqual(d["output_vac"], 1250)
        self.assertEqual(d["battery_v"], 12.8)
        self.assertIsNone(d["engine_hours"])

    def test_mondata_validity_flags(self):
        # bit0=fuel pump, bit1=starter; bits 4/5 are the corresponding invalid markers
        d = ag.decode_mondata(struct.pack("<HHHH", 0, 0, 0, 0) + bytes([0b00000011]))
        self.assertEqual(d["fuel_pump"], 1)
        self.assertEqual(d["starter"], 1)
        d = ag.decode_mondata(struct.pack("<HHHH", 0, 0, 0, 0) + bytes([0b00110011]))
        self.assertIsNone(d["fuel_pump"])              # invalid bits set → None
        self.assertIsNone(d["starter"])

    def test_temp_sensor(self):
        b = b"bedroom\x00\x00" + struct.pack("<HH", 2150, 2987) + struct.pack("<I", 1750000000)
        d = ag.decode_temp(b)
        self.assertEqual(d["nickname"], "bedroom")
        self.assertEqual(d["remote_temp"], 21.50)
        self.assertEqual(d["remote_battery"], 2.987)
        self.assertEqual(d["last_reading"], 1750000000)


if __name__ == "__main__":
    unittest.main()
