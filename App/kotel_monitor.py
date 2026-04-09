"""
Kotel Monitor - Sledování teplot kotle, AKU nádrže a spalin
Sériový port COM3, 300 baud, 8N1

Závislosti:
    pip install pyserial

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
import winsound  # Windows beep (na Linuxu zakomentovat)

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
ACCENT_K  = "#f97316"
ACCENT_A  = "#3b82f6"
ACCENT_S  = "#22c55e"
ACCENT_SW = "#ef4444"

GAUGE_MIN = {"A0": 0,   "A1": 0,   "A2": 0}
GAUGE_MAX = {"A0": 100, "A1": 100, "A2": 250}

WARNING_AKU     = 70
WARNING_SPALINY = 160


class GaugeCanvas(tk.Canvas):
    """Kruhový gauge – překresluje se proporcionálně při každé změně velikosti."""

    def __init__(self, parent, channel, color, **kw):
        super().__init__(parent, bg=PANEL_BG, highlightthickness=0, **kw)
        self.ch        = channel
        self.color     = color
        self._temp     = None
        self._warn_clr = None
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, event=None):
        self._redraw()

    def update_value(self, temp: float, warn_color=None):
        self._temp     = temp
        self._warn_clr = warn_color
        self._redraw()

    def _redraw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 10 or h < 10:
            return

        size = min(w, h)
        cx   = w / 2
        cy   = h / 2

        pad    = size * 0.10
        arc_w  = max(4, int(size * 0.055))
        x0, y0 = cx - size / 2 + pad, cy - size / 2 + pad
        x1, y1 = cx + size / 2 - pad, cy + size / 2 - pad

        # Pozadí oblouku (šedá dráha)
        self.create_arc(x0, y0, x1, y1, start=225, extent=-270,
                        style="arc", outline=BORDER, width=arc_w)

        # Min / max popisky
        font_small = ("Courier New", max(7, int(size * 0.07)))
        vmin = GAUGE_MIN[self.ch]
        vmax = GAUGE_MAX[self.ch]
        self.create_text(x0 + 2, y1 + 4, text=f"{vmin}°",
                         fill=MUTED, font=font_small, anchor="nw")
        self.create_text(x1 - 2, y1 + 4, text=f"{vmax}°",
                         fill=MUTED, font=font_small, anchor="ne")

        if self._temp is None:
            font_val = ("Courier New", max(10, int(size * 0.14)), "bold")
            self.create_text(cx, cy, text="–", fill=MUTED, font=font_val)
            return

        arc_color = self._warn_clr if self._warn_clr else self.color
        frac      = max(0.0, min(1.0, (self._temp - vmin) / (vmax - vmin)))
        extent    = -270 * frac

        # Barevný oblouk
        if frac > 0:
            self.create_arc(x0, y0, x1, y1, start=225, extent=extent,
                            style="arc", outline=arc_color, width=arc_w)

        # Jehla
        angle_rad = math.radians(225 - 270 * frac)
        r_needle  = (size / 2 - pad) * 0.80
        nx = cx + r_needle * math.cos(angle_rad)
        ny = cy - r_needle * math.sin(angle_rad)
        needle_w = max(2, int(size * 0.018))
        self.create_line(cx, cy, nx, ny, fill=arc_color, width=needle_w)
        dot = max(3, int(size * 0.025))
        self.create_oval(cx - dot, cy - dot, cx + dot, cy + dot,
                         fill=arc_color, outline="")

        # Číselná hodnota a jednotka
        font_val  = ("Courier New", max(10, int(size * 0.14)), "bold")
        font_unit = ("Courier New", max(7,  int(size * 0.07)))
        self.create_text(cx, cy + size * 0.18,
                         text=f"{self._temp:.1f}",
                         fill=arc_color, font=font_val)
        self.create_text(cx, cy + size * 0.32,
                         text="°C",
                         fill=arc_color, font=font_unit)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("🔥 Kotel Monitor")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(480, 320)

        self._temps       = {"A0": None, "A1": None, "A2": None}
        self._alert_shown = False
        self._running     = True

        self._build_ui()
        self._start_serial()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Hlavička
        header = tk.Frame(self, bg=BG, pady=10)
        header.pack(fill="x", padx=20)
        tk.Label(header, text="KOTEL MONITOR", bg=BG, fg=TEXT_FG,
                 font=("Courier New", 18, "bold")).pack(side="left")
        self._status_lbl = tk.Label(header, text="● ODPOJENO",
                                    bg=BG, fg="#ef4444",
                                    font=("Courier New", 10))
        self._status_lbl.pack(side="right", padx=4)

        # Tři panely s gaugy – grid, roztahují se s oknem
        panels = tk.Frame(self, bg=BG)
        panels.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        panels.columnconfigure(0, weight=1)
        panels.columnconfigure(1, weight=1)
        panels.columnconfigure(2, weight=1)
        panels.rowconfigure(0, weight=1)

        channels = [
            ("A0", "KOTEL",     ACCENT_K),
            ("A1", "AKU NÁDRŽ", ACCENT_A),
            ("A2", "SPALINY",   ACCENT_S),
        ]
        self._gauges    = {}
        self._volt_lbls = {}

        for col, (ch, name, color) in enumerate(channels):
            panel = tk.Frame(panels, bg=PANEL_BG,
                             highlightbackground=BORDER,
                             highlightthickness=1)
            panel.grid(row=0, column=col, sticky="nsew", padx=6, pady=4)
            panel.columnconfigure(0, weight=1)
            panel.rowconfigure(1, weight=1)  # gauge row se roztahuje

            tk.Label(panel, text=name, bg=PANEL_BG, fg=color,
                     font=("Courier New", 12, "bold"), pady=6).grid(
                     row=0, column=0, sticky="ew")

            g = GaugeCanvas(panel, ch, color)
            g.grid(row=1, column=0, sticky="nsew", padx=10, pady=4)
            self._gauges[ch] = g

            v_lbl = tk.Label(panel, text="– V", bg=PANEL_BG,
                             fg=MUTED, font=("Courier New", 10), pady=6)
            v_lbl.grid(row=2, column=0)
            self._volt_lbls[ch] = v_lbl

        # Log okno
        log_frame = tk.Frame(self, bg=PANEL_BG,
                             highlightbackground=BORDER, highlightthickness=1)
        log_frame.pack(fill="x", padx=12, pady=(0, 10))
        tk.Label(log_frame, text="LOG", bg=PANEL_BG, fg=MUTED,
                 font=("Courier New", 9), anchor="w").pack(
                 fill="x", padx=8, pady=(4, 0))
        self._log = tk.Text(log_frame, height=4, bg="#0d1117",
                            fg=MUTED, font=("Courier New", 9),
                            insertbackground=TEXT_FG,
                            relief="flat", state="disabled", wrap="word")
        self._log.pack(fill="x", padx=6, pady=(0, 6))

    # ── Sériová komunikace ────────────────────────────────────────────────────

    def _start_serial(self):
        threading.Thread(target=self._read_serial, daemon=True).start()

    def _read_serial(self):
        port = "COM3"
        while self._running:
            try:
                self._log_msg(f"Připojuji se k {port} 300 baud…")
                with serial.Serial(port, baudrate=300, bytesize=8,
                                   parity="N", stopbits=1, timeout=3) as ser:
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
        matches = re.findall(r"A(\d):\s*([\d.]+)\s*V", line)
        if not matches:
            return
        data = {}
        for idx, volt_str in matches:
            ch = f"A{idx}"
            if ch in CALIB:
                v = float(volt_str)
                data[ch] = (v, voltage_to_temp(ch, v))
        if data:
            self.after(0, self._update_display, data)

    # ── Aktualizace UI ────────────────────────────────────────────────────────

    def _update_display(self, data: dict):
        for ch, (volt, temp) in data.items():
            self._temps[ch] = temp
            self._volt_lbls[ch].config(text=f"{volt:.3f} V")

        aku_temp     = self._temps.get("A1")
        spaliny_temp = self._temps.get("A2")
        spaliny_warn = (aku_temp is not None and aku_temp < WARNING_AKU and
                        spaliny_temp is not None and spaliny_temp < WARNING_SPALINY)

        for ch, (volt, temp) in data.items():
            warn_color = ACCENT_SW if (ch == "A2" and spaliny_warn) else None
            self._volt_lbls[ch].config(fg=warn_color if warn_color else MUTED)
            self._gauges[ch].update_value(temp, warn_color)

        if spaliny_warn and not self._alert_shown:
            self._alert_shown = True
            self._trigger_alert(spaliny_temp)
        elif not spaliny_warn:
            self._alert_shown = False

    def _trigger_alert(self, spaliny_temp: float):
        def _show():
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
