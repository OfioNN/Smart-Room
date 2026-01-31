import sys
import time
import threading
import queue
from collections import deque
import os
import csv

import serial
import serial.tools.list_ports

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

# ---------------------------
# KONFIGURACJA ESTETYCZNA
# ---------------------------
THEME = {
    "bg_root":      "#121212",
    "bg_panel":     "#1E1E1E",
    "bg_input":     "#2D2D2D",
    "fg_primary":   "#E0E0E0",
    "fg_secondary": "#A0A0A0",
    "accent":       "#007ACC",
    "accent_hover": "#0098FF",
    "success":      "#4CAF50",
    "warning":      "#FFC107",
    "danger":       "#FF5252",
    "chart_bg":     "#1E1E1E",
    "chart_plot":   "#1E1E1E",
}

FONT_UI = ("Segoe UI", 9)
FONT_BOLD = ("Segoe UI", 9, "bold")
FONT_DATA = ("Segoe UI", 20, "bold")
FONT_MONO = ("Consolas", 9)

# ---------------------------
# PARSER DANYCH
# ---------------------------
def parse_data_line(line: str):
    line = line.strip()
    if not line.startswith("DATA,"):
        return None

    payload = line[5:]
    parts = payload.split(",")

    data = {}
    for p in parts:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        data[k.strip()] = v.strip()

    out = {}
    try: out["t"] = float(data.get("t", "nan"))
    except: out["t"] = float("nan")

    try: out["h"] = float(data.get("h", "nan"))
    except: out["h"] = float("nan")

    try: out["ldr"] = int(float(data.get("ldr", "nan")))
    except: out["ldr"] = -1

    out["time"] = data.get("time", "--:--:--")
    out["mode"] = data.get("mode", "UNK")

    try: out["led"] = int(data.get("led", "0"))
    except: out["led"] = 0

    try: out["buzz"] = int(data.get("buzz", "0"))
    except: out["buzz"] = 0
    
    try: out["interval"] = int(data.get("int", "0"))
    except: out["interval"] = 0

    try: out["nst"] = int(data.get("nst", "-1"))
    except: out["nst"] = -1
    
    try: out["nend"] = int(data.get("nend", "-1"))
    except: out["nend"] = -1

    return out

# ---------------------------
# SERIAL READER
# ---------------------------
class SerialReader(threading.Thread):
    def __init__(self, ser: serial.Serial, out_queue: queue.Queue):
        super().__init__(daemon=True)
        self.ser = ser
        self.out_queue = out_queue
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                line = self.ser.readline().decode("utf-8", errors="replace")
                if line:
                    self.out_queue.put(line)
            except Exception:
                break

# ---------------------------
# GŁÓWNA KLASA GUI
# ---------------------------
class SmartRoomGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Smart Room - Panel Sterowania")
        self.root.geometry("1150x760")
        self.root.configure(bg=THEME["bg_root"])
        
        self._init_styles()
        self._init_variables()
        self._build_layout()
        self._refresh_ports()

        # Timery
        self.root.after(100, self._poll_queue)
        self.root.after(500, self._update_plot)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _init_variables(self):
        self.ser = None
        self.reader = None
        self.q = queue.Queue()
        self.connection_start_time = 0
        self.STABILIZATION_DELAY = 2.0 

        # Dane wykresowe
        self.max_points = 100
        self.ts = deque(maxlen=self.max_points)
        self.temp = deque(maxlen=self.max_points)
        self.hum = deque(maxlen=self.max_points)
        self.ldr = deque(maxlen=self.max_points)

        # CSV
        self.csv_enabled = False
        self.csv_file = None
        self.csv_writer = None
        self.csv_header = ["pc_timestamp", "device_time", "temp_c", "hum_pct", "ldr", "led", "buzz", "mode", "interval", "night_start", "night_end"]

        # Zmienne UI
        self.var_port = tk.StringVar()
        self.var_status_csv = tk.StringVar(value="Status: Oczekiwanie")
        
        # Zmienne sensorów
        self.var_t = tk.StringVar(value="--")
        self.var_h = tk.StringVar(value="--")
        self.var_ldr = tk.StringVar(value="--")
        self.var_time = tk.StringVar(value="--:--:--")
        self.var_mode = tk.StringVar(value="---")
        self.var_led = tk.StringVar(value="OFF")
        self.var_buzz = tk.StringVar(value="OFF")
        self.var_interval = tk.StringVar(value="--")
        self.var_nst = tk.StringVar(value="--")
        self.var_nend = tk.StringVar(value="--")

    def _init_styles(self):
        style = ttk.Style()
        style.theme_use('clam')

        # Konfiguracja ogólna
        style.configure(".", background=THEME["bg_root"], foreground=THEME["fg_primary"], font=FONT_UI)
        
        # Style przycisków
        style.configure("TButton", 
                        background=THEME["bg_input"], 
                        foreground=THEME["fg_primary"], 
                        borderwidth=0, 
                        focuscolor=THEME["accent"],
                        padding=6)
        style.map("TButton", 
                  background=[("active", THEME["accent"]), ("disabled", "#333333")], 
                  foreground=[("active", "white"), ("disabled", "#555555")])

        style.configure("Accent.TButton", background=THEME["accent"], foreground="white", font=FONT_BOLD)
        style.map("Accent.TButton", background=[("active", THEME["accent_hover"])])

        style.configure("Danger.TButton", background=THEME["danger"], foreground="white")
        style.map("Danger.TButton", background=[("active", "#FF8888")])

        # Style paneli i ramek
        style.configure("Card.TFrame", background=THEME["bg_panel"], relief="flat")
        style.configure("TLabelframe", background=THEME["bg_panel"], bordercolor="#444444")
        style.configure("TLabelframe.Label", background=THEME["bg_panel"], foreground=THEME["accent"], font=FONT_BOLD)

        # Inne widgety
        style.configure("TCombobox", fieldbackground=THEME["bg_input"], background=THEME["bg_input"], foreground=THEME["fg_primary"], arrowcolor="white", borderwidth=0)
        style.map("TCombobox", fieldbackground=[("readonly", THEME["bg_input"])], selectbackground=[("readonly", THEME["accent"])])

        style.configure("Vertical.TScrollbar", background=THEME["bg_input"], troughcolor=THEME["bg_root"], borderwidth=0, arrowcolor="white")

    def _build_layout(self):
        # --- TOP BAR ---
        top_bar = tk.Frame(self.root, bg=THEME["bg_panel"], height=50, padx=10, pady=8)
        top_bar.pack(side=tk.TOP, fill=tk.X)

        # Wybór portu
        tk.Label(top_bar, text="PORT:", bg=THEME["bg_panel"], fg=THEME["fg_secondary"], font=FONT_BOLD).pack(side=tk.LEFT, padx=(0, 5))
        
        self.combo_ports = ttk.Combobox(top_bar, textvariable=self.var_port, width=54, state="readonly")
        self.combo_ports.pack(side=tk.LEFT)
        
        ttk.Button(top_bar, text="⟳", width=3, command=self._refresh_ports).pack(side=tk.LEFT, padx=2)
        
        tk.Frame(top_bar, width=15, bg=THEME["bg_panel"]).pack(side=tk.LEFT) 

        self.btn_connect = ttk.Button(top_bar, text="POŁĄCZ", style="Accent.TButton", command=self.connect)
        self.btn_connect.pack(side=tk.LEFT, padx=2)
        self.btn_disconnect = ttk.Button(top_bar, text="ROZŁĄCZ", state=tk.DISABLED, command=self.disconnect)
        self.btn_disconnect.pack(side=tk.LEFT, padx=2)

        # Sekcja CSV
        tk.Label(top_bar, textvariable=self.var_status_csv, bg=THEME["bg_panel"], fg=THEME["fg_secondary"]).pack(side=tk.RIGHT, padx=10)
        self.btn_csv_stop = ttk.Button(top_bar, text="STOP ■", style="Danger.TButton", state=tk.DISABLED, command=self.csv_stop)
        self.btn_csv_stop.pack(side=tk.RIGHT, padx=2)
        self.btn_csv_start = ttk.Button(top_bar, text="REC ●", state=tk.DISABLED, command=self.csv_start)
        self.btn_csv_start.pack(side=tk.RIGHT, padx=2)

        # --- GŁÓWNY PODZIAŁ ---
        self.paned_main = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        self.paned_main.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # --- LEWY PANEL ---
        self.left_panel = tk.Frame(self.paned_main, bg=THEME["bg_root"], width=320)
        self.left_panel.pack_propagate(False) 
        self.paned_main.add(self.left_panel, weight=0)

        self._build_left_content()

        # --- PRAWY PANEL ---
        right_split = ttk.PanedWindow(self.paned_main, orient=tk.VERTICAL)
        self.paned_main.add(right_split, weight=3)

        graph_frame = ttk.Frame(right_split, style="Card.TFrame")
        right_split.add(graph_frame, weight=4) 
        self._build_graph(graph_frame)

        log_frame = ttk.LabelFrame(right_split, text="TERMINAL UART")
        right_split.add(log_frame, weight=1) 
        self._build_log(log_frame)

    def _build_left_content(self):
        # 1. STATUS
        status_frame = ttk.LabelFrame(self.left_panel, text="STATUS SENSORÓW", padding=10)
        status_frame.pack(fill=tk.X, pady=(0, 10))

        def make_tile(parent, title, var, unit, color, r, c, colspan=1):
            f = tk.Frame(parent, bg=THEME["bg_panel"], highlightbackground=color, highlightthickness=1)
            f.grid(row=r, column=c, sticky="nsew", padx=4, pady=4, columnspan=colspan)
            tk.Label(f, text=title, bg=THEME["bg_panel"], fg=THEME["fg_secondary"], font=("Segoe UI", 7)).pack(anchor="w", padx=4, pady=(2,0))
            tk.Label(f, textvariable=var, bg=THEME["bg_panel"], fg=color, font=FONT_DATA).pack(anchor="center")
            tk.Label(f, text=unit, bg=THEME["bg_panel"], fg=THEME["fg_secondary"], font=("Segoe UI", 7)).pack(anchor="e", padx=4, pady=(0,2))

        status_grid = tk.Frame(status_frame, bg=THEME["bg_panel"])
        status_grid.pack(fill=tk.X)
        status_grid.columnconfigure(0, weight=1)
        status_grid.columnconfigure(1, weight=1)

        make_tile(status_grid, "TEMPERATURA", self.var_t, "°C", THEME["danger"], 0, 0)
        make_tile(status_grid, "WILGOTNOŚĆ", self.var_h, "%", THEME["success"], 0, 1)
        make_tile(status_grid, "JASNOŚĆ (LDR)", self.var_ldr, "raw", THEME["warning"], 1, 0, colspan=2)

        info_tbl = tk.Frame(status_frame, bg=THEME["bg_panel"], pady=5)
        info_tbl.pack(fill=tk.X, pady=(5,0))
        
        def info_row(label, var):
            r = tk.Frame(info_tbl, bg=THEME["bg_panel"])
            r.pack(fill=tk.X, pady=1)
            tk.Label(r, text=label, bg=THEME["bg_panel"], fg=THEME["fg_secondary"], width=12, anchor="w").pack(side=tk.LEFT)
            tk.Label(r, textvariable=var, bg=THEME["bg_panel"], fg="white", font=FONT_BOLD).pack(side=tk.LEFT)
        
        info_row("Czas:", self.var_time)
        info_row("Tryb:", self.var_mode)
        info_row("LED:", self.var_led)
        info_row("Buzzer:", self.var_buzz)

        # 2. STEROWANIE
        ctrl_frame = ttk.LabelFrame(self.left_panel, text="STEROWANIE", padding=10)
        ctrl_frame.pack(fill=tk.X, pady=(0, 10))

        tk.Label(ctrl_frame, text="Tryb pracy systemu:", bg=THEME["bg_panel"], fg=THEME["fg_secondary"], font=("Segoe UI", 8)).pack(anchor="w")
        f_mode = tk.Frame(ctrl_frame, bg=THEME["bg_panel"])
        f_mode.pack(fill=tk.X, pady=(2, 8))
        self.btn_auto = ttk.Button(f_mode, text="AUTO", command=lambda: self.send_cmd("MA"), state=tk.DISABLED)
        self.btn_auto.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0,2))
        self.btn_manual = ttk.Button(f_mode, text="MANUAL", command=lambda: self.send_cmd("ML"), state=tk.DISABLED)
        self.btn_manual.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(2,0))

        tk.Label(ctrl_frame, text="Oświetlenie (Manual):", bg=THEME["bg_panel"], fg=THEME["fg_secondary"], font=("Segoe UI", 8)).pack(anchor="w")
        f_led = tk.Frame(ctrl_frame, bg=THEME["bg_panel"])
        f_led.pack(fill=tk.X, pady=2)
        self.btn_led_on = ttk.Button(f_led, text="WŁĄCZ", command=lambda: self.send_cmd("LO"), state=tk.DISABLED)
        self.btn_led_on.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0,2))
        self.btn_led_off = ttk.Button(f_led, text="WYŁĄCZ", command=lambda: self.send_cmd("LOF"), state=tk.DISABLED)
        self.btn_led_off.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(2,0))

        tk.Frame(self.left_panel, bg=THEME["bg_root"]).pack(fill=tk.BOTH, expand=True)

        # 3. KONFIGURACJA
        conf_frame = ttk.LabelFrame(self.left_panel, text="KONFIGURACJA", padding=10)
        conf_frame.pack(fill=tk.X, pady=(0, 0))

        tk.Label(conf_frame, text="Interwał odczytu:", bg=THEME["bg_panel"], fg=THEME["fg_secondary"], font=("Segoe UI", 8)).pack(anchor="w")
        self.combo_int = ttk.Combobox(conf_frame, values=["1000 ms", "2500 ms", "5000 ms", "10000 ms"], state="disabled")
        self.combo_int.pack(fill=tk.X, pady=(2, 10))
        self.combo_int.set("1000 ms")
        self.combo_int.bind("<<ComboboxSelected>>", self._on_interval_change)

        tk.Label(conf_frame, text="Tryb nocny (Godziny):", bg=THEME["bg_panel"], fg=THEME["fg_secondary"], font=("Segoe UI", 8)).pack(anchor="w")
        
        night_grid = tk.Frame(conf_frame, bg=THEME["bg_panel"])
        night_grid.pack(fill=tk.X, pady=2)
        
        self.night_controls = night_grid 
        tk.Label(night_grid, text="Start:", bg=THEME["bg_panel"], fg="white", width=6, anchor="w").grid(row=0, column=0)
        self.btn_ns_d = ttk.Button(night_grid, text="-", width=3, command=lambda: self.send_cmd("SND"), state=tk.DISABLED)
        self.btn_ns_d.grid(row=0, column=1, padx=2)
        tk.Label(night_grid, textvariable=self.var_nst, bg=THEME["bg_panel"], fg=THEME["accent"], font=FONT_BOLD, width=4).grid(row=0, column=2)
        self.btn_ns_i = ttk.Button(night_grid, text="+", width=3, command=lambda: self.send_cmd("SNI"), state=tk.DISABLED)
        self.btn_ns_i.grid(row=0, column=3, padx=2)

        tk.Label(night_grid, text="Koniec:", bg=THEME["bg_panel"], fg="white", width=6, anchor="w").grid(row=1, column=0, pady=5)
        self.btn_ne_d = ttk.Button(night_grid, text="-", width=3, command=lambda: self.send_cmd("SD"), state=tk.DISABLED)
        self.btn_ne_d.grid(row=1, column=1, padx=2, pady=5)
        tk.Label(night_grid, textvariable=self.var_nend, bg=THEME["bg_panel"], fg=THEME["accent"], font=FONT_BOLD, width=4).grid(row=1, column=2, pady=5)
        self.btn_ne_i = ttk.Button(night_grid, text="+", width=3, command=lambda: self.send_cmd("SI"), state=tk.DISABLED)
        self.btn_ne_i.grid(row=1, column=3, padx=2, pady=5)

    def _build_graph(self, parent):
        self.fig = Figure(dpi=100, facecolor=THEME["chart_bg"])
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor(THEME["chart_plot"])
        
        # Oś
        self.ax.tick_params(colors='#888888', which='both')
        self.ax.spines['bottom'].set_color('#444444')
        self.ax.spines['left'].set_color('#444444')
        self.ax.spines['top'].set_visible(False)
        self.ax.spines['right'].set_visible(False)
        
        self.ax.set_xlabel("Próbki", color='#888888')
        self.ax.set_ylabel("Wartość", color='#888888')
        self.ax.grid(True, color="#333333", linestyle='--', linewidth=0.5)

        self.line_t, = self.ax.plot([], [], label="Temp [C]", color=THEME["danger"], linewidth=1.5)
        self.line_h, = self.ax.plot([], [], label="Wilg [%]", color=THEME["success"], linewidth=1.5)
        self.line_l, = self.ax.plot([], [], label="LDR", color=THEME["warning"], linewidth=1.5, alpha=0.7)

        legend = self.ax.legend(loc="upper right", facecolor=THEME["bg_panel"], edgecolor="#444444")
        for text in legend.get_texts(): text.set_color("#CCCCCC")

        self.canvas = FigureCanvasTkAgg(self.fig, master=parent)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def _build_log(self, parent):
        self.log_text = tk.Text(parent, height=5, bg="#111111", fg=THEME["success"], 
                                insertbackground="white", font=FONT_MONO, 
                                bd=0, highlightthickness=0, state="disabled")
        
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.log_text.yview, style="Vertical.TScrollbar")
        self.log_text.configure(yscrollcommand=scrollbar.set)
        
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)

    # ---------------------------
    # LOGIKA APLIKACJI
    # ---------------------------
    def _toggle_controls(self, state):
        btns = [self.btn_auto, self.btn_manual, self.btn_led_on, self.btn_led_off,
                self.btn_ns_d, self.btn_ns_i, self.btn_ne_d, self.btn_ne_i]
        
        for btn in btns:
            btn.configure(state=state)
        
        self.combo_int.configure(state="readonly" if state == tk.NORMAL else "disabled")
        
        if state == tk.DISABLED:
            self.btn_csv_start.configure(state=tk.DISABLED)
            self.btn_csv_stop.configure(state=tk.DISABLED)
        else:
            self.btn_csv_start.configure(state=tk.DISABLED if self.csv_enabled else tk.NORMAL)
            self.btn_csv_stop.configure(state=tk.NORMAL if self.csv_enabled else tk.DISABLED)

    def _log(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, msg)
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def connect(self):
        port = self.var_port.get().split(" - ")[0]
        if not port:
            messagebox.showwarning("Info", "Wybierz port COM.")
            return

        try:
            self.ser = serial.Serial(port, 115200, timeout=1)
            self.reader = SerialReader(self.ser, self.q)
            self.reader.start()
            self.connection_start_time = time.time()
            
            self.btn_connect.configure(state=tk.DISABLED)
            self.btn_disconnect.configure(state=tk.NORMAL)
            self.combo_ports.configure(state="disabled")
            self._toggle_controls(tk.NORMAL)
            self._log(f">> POŁĄCZONO Z {port}\n")
        except Exception as e:
            messagebox.showerror("Błąd", str(e))

    def disconnect(self):
        self.csv_stop()
        if self.reader: self.reader.stop()
        if self.ser: 
            try: self.ser.close()
            except: pass
        
        self.btn_connect.configure(state=tk.NORMAL)
        self.btn_disconnect.configure(state=tk.DISABLED)
        self.combo_ports.configure(state="readonly")
        self._toggle_controls(tk.DISABLED)
        self._log(">> ROZŁĄCZONO\n")
        self.ser = None

    def send_cmd(self, cmd):
        if self.ser and self.ser.is_open:
            try:
                self.ser.write((cmd + "\n").encode())
                self._log(f"TX: {cmd}\n")
            except:
                self._log("ERR: TX Fail\n")

    def _on_interval_change(self, event):
        mapping = {"1000 ms": "I", "2500 ms": "I2", "5000 ms": "I5", "10000 ms": "I1"}
        val = self.combo_int.get()
        if val in mapping: self.send_cmd(mapping[val])

    def _refresh_ports(self):
        ports = serial.tools.list_ports.comports()
        vals = [f"{p.device} - {p.description}" for p in ports]
        self.combo_ports['values'] = vals
        if vals: self.combo_ports.current(0)

    # ---------------------------
    # OBSŁUGA CSV
    # ---------------------------
    def csv_start(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path: return
        try:
            exists = os.path.exists(path) and os.path.getsize(path) > 0
            self.csv_file = open(path, "a", newline="", encoding="utf-8")
            self.csv_writer = csv.writer(self.csv_file)
            if not exists: self.csv_writer.writerow(self.csv_header)
            
            self.csv_enabled = True
            self.var_status_csv.set(f"NAGRYWANIE: {os.path.basename(path)}")
            self.btn_csv_start.configure(state=tk.DISABLED)
            self.btn_csv_stop.configure(state=tk.NORMAL)
        except Exception as e:
            messagebox.showerror("Błąd pliku", str(e))

    def csv_stop(self):
        if self.csv_file:
            self.csv_file.close()
            self.csv_file = None
        self.csv_enabled = False
        self.var_status_csv.set("Status: Oczekiwanie")
        
        if self.ser:
            self.btn_csv_start.configure(state=tk.NORMAL)
            self.btn_csv_stop.configure(state=tk.DISABLED)

    # ---------------------------
    # PĘTLA DANYCH
    # ---------------------------
    def _poll_queue(self):
        while not self.q.empty():
            line = self.q.get()
            if time.time() - self.connection_start_time < self.STABILIZATION_DELAY: continue
            
            if not line.startswith("DATA"):
                self._log(line if line.endswith('\n') else line+'\n')
            
            data = parse_data_line(line)
            if data:
                self._update_data_ui(data)
                self._update_csv(data)
                self._update_chart_data(data)
                
        self.root.after(50, self._poll_queue)

    def _update_data_ui(self, d):
        def safef(v, fmt="{:.1f}"): return fmt.format(v) if v == v else "--"
        
        self.var_t.set(safef(d['t']))
        self.var_h.set(safef(d['h'], "{:.0f}"))
        self.var_ldr.set(str(d['ldr']))
        self.var_time.set(d['time'])
        self.var_mode.set(d['mode'])
        self.var_led.set("ON" if d['led'] else "OFF")
        self.var_buzz.set("ON" if d['buzz'] else "OFF")
        
        if d['nst'] != -1: self.var_nst.set(f"{d['nst']:02d}")
        if d['nend'] != -1: self.var_nend.set(f"{d['nend']:02d}")

    def _update_csv(self, d):
        if self.csv_enabled and self.csv_writer:
            row = [time.strftime("%Y-%m-%d %H:%M:%S"), d.get("time"), d.get("t"), d.get("h"), 
                   d.get("ldr"), d.get("led"), d.get("buzz"), d.get("mode"), d.get("interval"), 
                   d.get("nst"), d.get("nend")]
            self.csv_writer.writerow(row)
            self.csv_file.flush()

    def _update_chart_data(self, d):
        self.ts.append(len(self.ts))
        self.temp.append(d['t'])
        self.hum.append(d['h'])
        self.ldr.append(d['ldr'])

    def _update_plot(self):
        if len(self.ts) > 1:
            x = list(range(len(self.ts)))
            self.line_t.set_data(x, list(self.temp))
            self.line_h.set_data(x, list(self.hum))
            self.line_l.set_data(x, list(self.ldr))
            self.ax.relim()
            self.ax.autoscale_view()
            self.canvas.draw_idle()
        self.root.after(500, self._update_plot)

    def on_close(self):
        self.disconnect()
        self.root.destroy()

if __name__ == "__main__":
    app = tk.Tk()
    gui = SmartRoomGUI(app)
    app.mainloop()