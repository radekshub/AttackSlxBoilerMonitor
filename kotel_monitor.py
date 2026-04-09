"""
Kotel Monitor - Sledování teplot kotle, AKU nádrže a spalin
Sériový port COM3, 300 baud, 8N1

Závislosti:
    pip install pyserial matplotlib

Spuštění:
    python kotel_monitor.py
"""

import tkinter as tk
from tkinter import messagebox
import threading
import serial
import re
import time
import math
import winsound  # Windows beep

# ── Kalibrace (lineární interpolace mezi dvěma body) ──────────────────────────
CALIB = {
    "A0": {"v": [1.906, 2.546], "t": [15, 90]},   # Kotel
    "A1": {"v": [2.058, 2.644], "t": [30, 100]},  # AKU nádrž
    "A2": {"v": [1.300, 1.857], "t": [16, 200]},  # Komín / spaliny
}

def voltage_to_temp(channel: str, voltage: float) -> float:
    c = CALIB[channel]
    v0, v1 = c["v"]
    t0, t1 = c["t"]
    return t0 + (voltage - v0) * (t1 - t0) / (v1 - v0)

# ── Barvy ─────────────────────────────────────────────────────────────────────
BG        = "#0d1117"
PANEL_BG  = "#161b22"
BORDER    = "#30363d"
TEXT_FG   = "#e6edf3"
MUTED     = "#8b949e"
ACCENT_K  = "#f97316"   # oranžová – kotel
ACCENT_A  = "#3b82f6"   # modrá – AKU
ACCENT_S  = "#22c55e"   # zelená – spaliny (normální)
ACCENT_SW = "#ef4444"   # červená – spaliny (varování)
FONT_MONO = ("Courier New", 11)
FONT_VAL  = ("Courier New", 28, "bold")
FONT_LBL  = ("Courier New", 10)
FONT_HEAD = ("Courier New", 13, "bold")

# Meze pro gauge
GAUGE_MIN = {"A0": 0,  "A1": 0,  "A2": 0}
GAUGE_MAX = {"A0": 120, "A1": 120, "A2": 400}

WARNING_AKU   = 70    # °C – pod touto hranicí hlídat spaliny
WARNING_SPALINY = 160  # °C – pod touto hranicí varování

class GaugeCanvas(tk.Canvas):
    """Kruhový gauge s obloukem a číselnou hodnotou."""

    def __init__(self, parent, channel, color, **kw):
        size = 220
        super().__init__(parent, width=size, height=size,
                         bg=PANEL_BG, highlightthickness=0, **kw)
        self.ch     = channel
        self.color  = color
        self.size   = size
        self.value  = None
        self._draw_static()

    def _draw_static(self):
        s = self.size
        pad = 22
        self._arc_bbox = (pad, pad, s - pad, s - pad)
        # Pozadí oblouku (šedá dráha)
        self.create_arc(*self._arc_bbox, start=225, extent=-270,
                        style="arc", outline=BORDER, width=12,
                        tags="static")

    def update_value(self, temp: float, warn_color: str | None = None):
        self.delete("dynamic")
        s    = self.size
        vmin = GAUGE_MIN[self.ch]
        vmax = GAUGE_MAX[self.ch]
        frac = max(0.0, min(1.0, (temp - vmin) / (vmax - vmin)))

        arc_color = warn_color if warn_color else self.color
        extent = -270 * frac

        # Barevný oblouk
        if frac > 0:
            self.create_arc(*self._arc_bbox, start=225, extent=extent,
                            style="arc", outline=arc_color, width=12,
                            tags="dynamic")

        # Hodnota
        cx, cy = s / 2, s / 2 + 10
        self.create_text(cx, cy - 10,
                         text=f"{temp:.1f}°C",
                         fill=arc_color,
                         font=FONT_VAL,
                         tags="dynamic")
        self.value = temp

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("🔥 Kotel Monitor")
        self.configure(bg=BG)
        self.resizable(False, False)

        self._temps = {"A0": None, "A1": None, "A2": None}
        self._alert_shown = False
        self._serial_thread = None
        self._running = True

        self._build_ui()
        self._start_serial()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Hlavička
        header = tk.Frame(self, bg=BG, pady=12)
        header.pack(fill="x", padx=20)
        tk.Label(header, text="KOTEL MONITOR", bg=BG, fg=TEXT_FG,
                 font=("Courier New", 18, "bold")).pack(side="left")
        self._status_lbl = tk.Label(header, text="● ODPOJENO", bg=BG,
                                    fg="#ef4444",
                                    font=FONT_LBL)
        self._status_lbl.pack(side="right", padx=4)

        # Tři panely
        panels = tk.Frame(self, bg=BG)
        panels.pack(padx=16, pady=(0, 16))

        channels = [
            ("A0", "KOTEL",      ACCENT_K),
            ("A1", "AKU NÁDRŽ",  ACCENT_A),
            ("A2", "SPALINY",    ACCENT_S),
        ]
        self._gauges = {}
        self._volt_lbls = {}

        for ch, name, color in channels:
            panel = tk.Frame(panels, bg=PANEL_BG,
                             highlightbackground=BORDER,
                             highlightthickness=1,
                             padx=14, pady=14)
            panel.pack(side="left", padx=8)

            tk.Label(panel, text=name, bg=PANEL_BG, fg=color,
                     font=FONT_HEAD).pack()

            g = GaugeCanvas(panel, ch, color)
            g.pack(pady=4)
            self._gauges[ch] = g

            v_lbl = tk.Label(panel, text="– V", bg=PANEL_BG,
                             fg=MUTED, font=FONT_MONO)
            v_lbl.pack()
            self._volt_lbls[ch] = v_lbl

        # Log okno
        log_frame = tk.Frame(self, bg=PANEL_BG,
                             highlightbackground=BORDER,
                             highlightthickness=1)
        log_frame.pack(fill="x", padx=16, pady=(0, 16))
        tk.Label(log_frame, text="LOG", bg=PANEL_BG, fg=MUTED,
                 font=FONT_LBL, anchor="w").pack(fill="x", padx=8, pady=(4, 0))
        self._log = tk.Text(log_frame, height=5, bg="#0d1117",
                            fg=MUTED, font=("Courier New", 9),
                            insertbackground=TEXT_FG,
                            relief="flat", state="disabled",
                            wrap="word")
        self._log.pack(fill="x", padx=6, pady=(0, 6))

    # ── Sériová komunikace ────────────────────────────────────────────────────

    def _start_serial(self):
        self._serial_thread = threading.Thread(target=self._read_serial,
                                                daemon=True)
        self._serial_thread.start()

    def _read_serial(self):
        port = "COM3"
        while self._running:
            try:
                self._log_msg(f"Připojuji se k {port} 300 baud…")
                with serial.Serial(port, baudrate=300, bytesize=8,
                                   parity="N", stopbits=1,
                                   timeout=3) as ser:
                    self.after(0, self._set_connected, True)
                    self._log_msg("Připojeno.")
                    while self._running:
                        line = ser.readline().decode("ascii", errors="ignore").strip()
                        if line:
                            self._log_msg(f"← {line}")
                            self._parse_line(line)
            except serial.SerialException as e:
                self.after(0, self._set_connected, False)
                self._log_msg(f"Chyba portu: {e} — zkusím znovu za 5 s")
                time.sleep(5)

    def _parse_line(self, line: str):
        """Parsuje 'A0: 2.087 V  |  A1: 2.488 V  |  A2: 1.349 V'"""
        pattern = r"A(\d):\s*([\d.]+)\s*V"
        matches = re.findall(pattern, line)
        if not matches:
            return
        data = {}
        for idx, volt_str in matches:
            ch = f"A{idx}"
            if ch in CALIB:
                v = float(volt_str)
                t = voltage_to_temp(ch, v)
                data[ch] = (v, t)
        if data:
            self.after(0, self._update_display, data)

    # ── Aktualizace UI ────────────────────────────────────────────────────────

    def _update_display(self, data: dict):
        for ch, (volt, temp) in data.items():
            self._temps[ch] = temp
            self._volt_lbls[ch].config(text=f"{volt:.3f} V")

        # Určit barvu spalin
        spaliny_warn = False
        aku_temp = self._temps.get("A1")
        spaliny_temp = self._temps.get("A2")

        if (aku_temp is not None and aku_temp < WARNING_AKU and
                spaliny_temp is not None and spaliny_temp < WARNING_SPALINY):
            spaliny_warn = True

        for ch, (volt, temp) in data.items():
            warn_color = None
            if ch == "A2" and spaliny_warn:
                warn_color = ACCENT_SW
                # Přebarvit panel – upravit barvu popisku
                self._volt_lbls[ch].config(fg=ACCENT_SW)
            else:
                if ch == "A2":
                    self._volt_lbls[ch].config(fg=MUTED)
            self._gauges[ch].update_value(temp, warn_color)

        # Varování
        if spaliny_warn and not self._alert_shown:
            self._alert_shown = True
            self._trigger_alert(spaliny_temp)
        elif not spaliny_warn:
            self._alert_shown = False

    def _trigger_alert(self, spaliny_temp: float):
        def _show():
            # Pipnutí (Windows)
            try:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                pass
            messagebox.showwarning(
                "⚠️  Varování – nízká teplota spalin",
                f"Teplota AKU nádrže je pod {WARNING_AKU} °C\n"
                f"a teplota spalin je pouze {spaliny_temp:.1f} °C\n"
                f"(minimum: {WARNING_SPALINY} °C).\n\n"
                "Hrozí kondenzace a poškození komína!",
                parent=self,
            )
        self.after(0, _show)

    def _set_connected(self, connected: bool):
        if connected:
            self._status_lbl.config(text="● PŘIPOJENO", fg="#22c55e")
        else:
            self._status_lbl.config(text="● ODPOJENO",  fg="#ef4444")

    def _log_msg(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        def _append():
            self._log.config(state="normal")
            self._log.insert("end", f"[{ts}] {msg}\n")
            self._log.see("end")
            self._log.config(state="disabled")
        self.after(0, _append)

    def _on_close(self):
        self._running = False
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
