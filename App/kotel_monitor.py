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
import collections
import winsound  # Windows beep (na Linuxu zakomentovat)

# ── Kalibrace ─────────────────────────────────────────────────────────────────
CALIB = {
    "A0": {"v": [1.906, 2.546], "t": [15, 90]},
    "A1": {"v": [2.058, 2.644], "t": [30, 100]},
    "A2": {"v": [1.300, 1.857], "t": [16, 200]},
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
GRID_CLR  = "#21262d"
ACCENT_K  = "#f97316"
ACCENT_A  = "#3b82f6"
ACCENT_S  = "#22c55e"
ACCENT_SW = "#ef4444"

GAUGE_MIN = {"A0": 0,   "A1": 0,   "A2": 0}
GAUGE_MAX = {"A0": 100, "A1": 100, "A2": 250}

WARNING_AKU     = 70
WARNING_SPALINY = 160

HISTORY_SECONDS  = 8 * 3600   # 8 hodin
RECORD_INTERVAL  = 30          # každých 30 s zaznamenat bod


# ══════════════════════════════════════════════════════════════════════════════
class GaugeCanvas(tk.Canvas):
    """Kruhový gauge – proporcionálně se překresluje."""

    def __init__(self, parent, channel, color, **kw):
        super().__init__(parent, bg=PANEL_BG, highlightthickness=0, **kw)
        self.ch        = channel
        self.color     = color
        self._temp     = None
        self._warn_clr = None
        self.bind("<Configure>", lambda e: self._redraw())

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
        cx, cy = w / 2, h / 2
        pad   = size * 0.10
        arc_w = max(4, int(size * 0.055))
        x0, y0 = cx - size/2 + pad, cy - size/2 + pad
        x1, y1 = cx + size/2 - pad, cy + size/2 - pad

        self.create_arc(x0, y0, x1, y1, start=225, extent=-270,
                        style="arc", outline=BORDER, width=arc_w)

        font_s = ("Courier New", max(7, int(size * 0.07)))
        vmin, vmax = GAUGE_MIN[self.ch], GAUGE_MAX[self.ch]
        self.create_text(x0+2, y1+4, text=f"{vmin}°", fill=MUTED,
                         font=font_s, anchor="nw")
        self.create_text(x1-2, y1+4, text=f"{vmax}°", fill=MUTED,
                         font=font_s, anchor="ne")

        font_v = ("Courier New", max(10, int(size * 0.14)), "bold")
        if self._temp is None:
            self.create_text(cx, cy, text="–", fill=MUTED, font=font_v)
            return

        color = self._warn_clr or self.color
        frac  = max(0.0, min(1.0, (self._temp - vmin) / (vmax - vmin)))
        if frac > 0:
            self.create_arc(x0, y0, x1, y1, start=225, extent=-270*frac,
                            style="arc", outline=color, width=arc_w)

        angle_rad = math.radians(225 - 270 * frac)
        r = (size/2 - pad) * 0.80
        nx = cx + r * math.cos(angle_rad)
        ny = cy - r * math.sin(angle_rad)
        self.create_line(cx, cy, nx, ny, fill=color,
                         width=max(2, int(size * 0.018)))
        d = max(3, int(size * 0.025))
        self.create_oval(cx-d, cy-d, cx+d, cy+d, fill=color, outline="")

        self.create_text(cx, cy + size*0.18, text=f"{self._temp:.1f}",
                         fill=color, font=font_v)
        self.create_text(cx, cy + size*0.32, text="°C",
                         fill=color,
                         font=("Courier New", max(7, int(size*0.07))))


# ══════════════════════════════════════════════════════════════════════════════
class HistoryChart(tk.Canvas):
    """
    Čárový graf s osou X = čas (8 h), levá osa Y = 10–100 °C (kotel + AKU),
    pravá osa Y = 100–250 °C (spaliny). Překresluje se při resize.
    """

    # okraje plátna
    PAD_L  = 52   # místo pro levou osu
    PAD_R  = 52   # místo pro pravou osu
    PAD_T  = 14
    PAD_B  = 36   # místo pro osu X

    Y_LEFT_MIN,  Y_LEFT_MAX  = 10,  100
    Y_RIGHT_MIN, Y_RIGHT_MAX = 100, 250

    def __init__(self, parent, **kw):
        super().__init__(parent, bg=PANEL_BG, highlightthickness=0, **kw)
        # deques: každý prvek je (timestamp_float, temp_float)
        maxlen = HISTORY_SECONDS // RECORD_INTERVAL + 10
        self._hist = {
            "A0": collections.deque(maxlen=maxlen),
            "A1": collections.deque(maxlen=maxlen),
            "A2": collections.deque(maxlen=maxlen),
        }
        self._colors = {"A0": ACCENT_K, "A1": ACCENT_A, "A2": ACCENT_S}
        self._warn_spaliny = False
        self.bind("<Configure>", lambda e: self._redraw())

    def add_point(self, temps: dict, warn_spaliny: bool):
        """Přidá bod do historie. temps = {"A0": float, ...}"""
        now = time.time()
        for ch, t in temps.items():
            if t is not None:
                self._hist[ch].append((now, t))
        self._warn_spaliny = warn_spaliny
        self._redraw()

    def _redraw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 40 or h < 40:
            return

        pl = self.PAD_L
        pr = self.PAD_R
        pt = self.PAD_T
        pb = self.PAD_B
        cw = w - pl - pr   # šířka grafu
        ch = h - pt - pb   # výška grafu

        now    = time.time()
        t_from = now - HISTORY_SECONDS

        def x_pos(ts):
            return pl + (ts - t_from) / HISTORY_SECONDS * cw

        def y_left(val):
            frac = (val - self.Y_LEFT_MIN) / (self.Y_LEFT_MAX - self.Y_LEFT_MIN)
            return pt + ch - frac * ch

        def y_right(val):
            frac = (val - self.Y_RIGHT_MIN) / (self.Y_RIGHT_MAX - self.Y_RIGHT_MIN)
            return pt + ch - frac * ch

        # ── mřížka ────────────────────────────────────────────────────────────
        font_ax = ("Courier New", 8)

        # vodorovné čáry – levá osa (každých 10 °C)
        for val in range(self.Y_LEFT_MIN, self.Y_LEFT_MAX + 1, 10):
            y = y_left(val)
            self.create_line(pl, y, pl+cw, y, fill=GRID_CLR, dash=(2, 4))
            self.create_text(pl - 4, y, text=str(val), fill=MUTED,
                             font=font_ax, anchor="e")

        # pravá osa (každých 25 °C)
        for val in range(self.Y_RIGHT_MIN, self.Y_RIGHT_MAX + 1, 25):
            y = y_right(val)
            self.create_text(pl + cw + 4, y, text=str(val), fill=ACCENT_S,
                             font=font_ax, anchor="w")

        # svislé čáry – každou hodinu
        for h_ago in range(0, 9):
            ts = now - h_ago * 3600
            x  = x_pos(ts)
            if pl <= x <= pl + cw:
                self.create_line(x, pt, x, pt+ch, fill=GRID_CLR, dash=(2, 4))
                label = time.strftime("%-H:%M", time.localtime(ts))
                self.create_text(x, pt+ch+4, text=label, fill=MUTED,
                                 font=font_ax, anchor="n")

        # ── rámeček grafu ─────────────────────────────────────────────────────
        self.create_rectangle(pl, pt, pl+cw, pt+ch, outline=BORDER, width=1)

        # popisky os
        self.create_text(pl - 40, pt + ch//2, text="°C", fill=MUTED,
                         font=("Courier New", 9), angle=90, anchor="center")
        self.create_text(pl + cw + 40, pt + ch//2,
                         text="°C spaliny", fill=ACCENT_S,
                         font=("Courier New", 9), angle=90, anchor="center")

        # legenda
        lx = pl + 8
        for ch_name, label in [("A0","Kotel"),("A1","AKU"),("A2","Spaliny")]:
            clr = (ACCENT_SW if ch_name == "A2" and self._warn_spaliny
                   else self._colors[ch_name])
            self.create_line(lx, pt+8, lx+18, pt+8, fill=clr, width=2)
            self.create_text(lx+22, pt+8, text=label, fill=clr,
                             font=font_ax, anchor="w")
            lx += 70

        # ── křivky ────────────────────────────────────────────────────────────
        for ch_name, deq in self._hist.items():
            pts = [(ts, t) for ts, t in deq if ts >= t_from]
            if len(pts) < 2:
                continue
            clr = (ACCENT_SW if ch_name == "A2" and self._warn_spaliny
                   else self._colors[ch_name])
            y_fn = y_right if ch_name == "A2" else y_left
            coords = []
            for ts, t in pts:
                coords += [x_pos(ts), y_fn(t)]
            self.create_line(coords, fill=clr, width=2, smooth=True)


# ══════════════════════════════════════════════════════════════════════════════
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("🔥 Kotel Monitor")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(560, 420)

        self._temps       = {"A0": None, "A1": None, "A2": None}
        self._alert_shown = False
        self._running     = True
        self._last_record = 0.0   # timestamp posledního záznamu do grafu

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

        # Tři panely s gaugy
        panels = tk.Frame(self, bg=BG)
        panels.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        panels.columnconfigure(0, weight=1)
        panels.columnconfigure(1, weight=1)
        panels.columnconfigure(2, weight=1)
        panels.rowconfigure(0, weight=1)

        channels = [("A0","KOTEL",ACCENT_K), ("A1","AKU NÁDRŽ",ACCENT_A),
                    ("A2","SPALINY",ACCENT_S)]
        self._gauges    = {}
        self._volt_lbls = {}

        for col, (ch, name, color) in enumerate(channels):
            panel = tk.Frame(panels, bg=PANEL_BG,
                             highlightbackground=BORDER, highlightthickness=1)
            panel.grid(row=0, column=col, sticky="nsew", padx=6, pady=4)
            panel.columnconfigure(0, weight=1)
            panel.rowconfigure(1, weight=1)

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

        # ── Historický graf ───────────────────────────────────────────────────
        chart_frame = tk.Frame(self, bg=PANEL_BG,
                               highlightbackground=BORDER, highlightthickness=1)
        chart_frame.pack(fill="x", padx=12, pady=(0, 6))
        tk.Label(chart_frame, text="HISTORIE TEPLOT  (8 h)",
                 bg=PANEL_BG, fg=MUTED,
                 font=("Courier New", 9), anchor="w").pack(
                 fill="x", padx=8, pady=(4, 0))
        self._chart = HistoryChart(chart_frame, height=180)
        self._chart.pack(fill="x", padx=6, pady=(2, 6))

        # ── Log ───────────────────────────────────────────────────────────────
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

        # Záznam do grafu každých RECORD_INTERVAL sekund
        now = time.time()
        if now - self._last_record >= RECORD_INTERVAL:
            self._last_record = now
            self._chart.add_point(self._temps.copy(), spaliny_warn)

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
