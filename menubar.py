#!/usr/bin/env python3
"""
macOS menu bar app for the Cummins EC-AGS+ generator.

Lives in the top-right menu bar: shows live genset state at a glance and gives quick
Start / Stop / Auto / Reset-fault actions plus a link to the full dashboard.

It is a THIN CLIENT of the dashboard server (server.py) — it never opens its own BLE
connection (the genset only allows one), it just calls the local REST API. So run the
dashboard server first, then this.

    .venv/bin/pip install rumps
    .venv/bin/python menubar.py

Env: AGS_DASH (default http://localhost:8722) points at the running server.
"""
import json
import os
import urllib.request

import rumps

BASE = os.environ.get("AGS_DASH", "http://localhost:8722").rstrip("/")
POLL_SECONDS = 3
ICON = "⚡"

STATE_GLYPH = {"Running": "🟢", "Cranking": "🟠", "Priming": "🟠",
               "Stopped": "⚪️", "Off/Unknown": "⚪️"}


def _get(path, timeout=2.5):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.load(r)


def _post(path, timeout=8):
    req = urllib.request.Request(BASE + path, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


class AGSBar(rumps.App):
    def __init__(self):
        super().__init__(ICON, quit_button=None)
        self.connected = False
        self.last_fault = 0

        # status block (non-clickable info lines)
        self.line_state = rumps.MenuItem("Starting…")
        self.line_batt = rumps.MenuItem("")
        self.line_out = rumps.MenuItem("")

        # action items
        self.it_start = rumps.MenuItem("Start Generator", callback=self.cmd_start)
        self.it_stop = rumps.MenuItem("Stop Generator", callback=self.cmd_stop)
        self.it_auto_on = rumps.MenuItem("Auto Mode: ON", callback=self.cmd_auto_on)
        self.it_auto_off = rumps.MenuItem("Auto Mode: OFF", callback=self.cmd_auto_off)
        self.it_reset = rumps.MenuItem("Reset Fault", callback=self.cmd_reset)
        # remember real callbacks so we can disable (grey out) and restore them
        self._actions = [(self.it_start, self.cmd_start), (self.it_stop, self.cmd_stop),
                         (self.it_auto_on, self.cmd_auto_on), (self.it_auto_off, self.cmd_auto_off),
                         (self.it_reset, self.cmd_reset)]

        self.menu = [
            self.line_state, self.line_batt, self.line_out,
            None,
            self.it_start, self.it_stop,
            self.it_auto_on, self.it_auto_off, self.it_reset,
            None,
            rumps.MenuItem("Open Dashboard…", callback=self.open_dash),
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]
        rumps.Timer(self.refresh, POLL_SECONDS).start()

    # ---- polling ---------------------------------------------------------------
    def refresh(self, _):
        try:
            s = _get("/api/state")
        except Exception:
            self.title = f"{ICON} ✕"
            self.connected = False
            self.line_state.title = "Dashboard offline — start server.py"
            self.line_batt.title = self.line_out.title = ""
            self._set_actions_enabled(False)
            return

        st = (s.get("telemetry") or {}).get("status") or {}
        rd = (s.get("telemetry") or {}).get("readings") or {}
        dc = (s.get("telemetry") or {}).get("dcvolts") or {}
        connected = s.get("state") == "connected"
        self.connected = connected

        if not connected:
            self.title = f"{ICON} —"
            self.line_state.title = f"Not connected ({s.get('state')})"
            self.line_batt.title = self.line_out.title = ""
            self._set_actions_enabled(False)
            return

        state = st.get("state", "?")
        glyph = STATE_GLYPH.get(state, "")
        house_v = dc.get("house_v")
        self.title = f"{ICON} {state}" + (f"  {house_v}V" if house_v is not None else "")
        self.line_state.title = f"{glyph} {state}" + ("  ·  Auto ON" if st.get("auto_mode") else "")
        hv = f"House {house_v}V" if house_v is not None else "House —"
        soc = st.get("soc_house_%")
        self.line_batt.title = hv + (f" · {soc}%" if soc is not None else "")
        self.line_out.title = (f"Out {rd.get('output_vac','—')}VAC {rd.get('output_hz','—')}Hz"
                               f" · load {rd.get('load_%','—')}%")
        self._set_actions_enabled(True)

        # one-shot notification when a new fault appears
        fault = st.get("fault_code") or 0
        if fault and fault != self.last_fault:
            rumps.notification("EC-AGS+ Generator", f"Fault code {fault}",
                               "Open the dashboard → History for details.")
        self.last_fault = fault

    def _set_actions_enabled(self, on):
        for it, cb in self._actions:
            it.set_callback(cb if on else None)

    # ---- actions ---------------------------------------------------------------
    def _cmd(self, name, label):
        try:
            _post(f"/api/command/{name}")
            rumps.notification("EC-AGS+ Generator", label, "Command sent ✓")
        except Exception as e:
            rumps.notification("EC-AGS+ Generator", label, f"Failed: {e}")

    def cmd_start(self, _):    self._cmd("start", "Start")
    def cmd_stop(self, _):     self._cmd("stop", "Stop")
    def cmd_auto_on(self, _):  self._cmd("auto-on", "Auto Mode ON")
    def cmd_auto_off(self, _): self._cmd("auto-off", "Auto Mode OFF")
    def cmd_reset(self, _):    self._cmd("reset-fault", "Reset Fault")

    def open_dash(self, _):
        import webbrowser
        webbrowser.open(BASE)


if __name__ == "__main__":
    AGSBar().run()
