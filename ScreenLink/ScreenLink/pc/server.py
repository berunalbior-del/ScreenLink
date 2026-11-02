"""ScreenLink PC server: streams the screen (H.264, low latency) to the tablet
and injects the tablet's multi-touch back into Windows."""
import ctypes, os, queue, re, socket, struct, subprocess, sys, threading, time
import tkinter as tk
from tkinter import ttk
from ctypes import wintypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

PORT, BEACON_PORT, NOWIN = 5600, 5602, 0x08000000
user32 = ctypes.windll.user32
SW, SH = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
FFMPEG = os.path.join(BASE, "ffmpeg.exe")
if not os.path.exists(FFMPEG):
    FFMPEG = "ffmpeg"
START_RE = re.compile(b"\x00\x00\x01")
LAST_GOOD = {}

# ---------------- multi-touch injection (Windows Touch Injection API) -------------
class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
class POINTER_INFO(ctypes.Structure):
    _fields_ = [("pointerType", wintypes.DWORD), ("pointerId", ctypes.c_uint32), ("frameId", ctypes.c_uint32),
                ("pointerFlags", wintypes.DWORD), ("sourceDevice", wintypes.HANDLE), ("hwndTarget", wintypes.HWND),
                ("ptPixelLocation", POINT), ("ptHimetricLocation", POINT), ("ptPixelLocationRaw", POINT),
                ("ptHimetricLocationRaw", POINT), ("dwTime", wintypes.DWORD), ("historyCount", ctypes.c_uint32),
                ("InputData", ctypes.c_int32), ("dwKeyStates", wintypes.DWORD), ("PerformanceCount", ctypes.c_uint64),
                ("ButtonChangeType", ctypes.c_int)]
class POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = [("pointerInfo", POINTER_INFO), ("touchFlags", wintypes.DWORD), ("touchMask", wintypes.DWORD),
                ("rcContact", RECT), ("rcContactRaw", RECT), ("orientation", ctypes.c_uint32), ("pressure", ctypes.c_uint32)]

F_NEW, F_INRANGE, F_INCONTACT = 0x1, 0x2, 0x4
F_DOWN, F_UPDATE, F_UP = 0x10000, 0x20000, 0x40000
_touch_ok = False

def init_touch():
    global _touch_ok
    _touch_ok = bool(user32.InitializeTouchInjection(10, 2))  # up to 10 fingers
    return _touch_ok

def inject(action, changed, pts):
    """action D/M/U/C; changed = pointer id that went down/up; pts = [(id, x01, y01)]"""
    if not _touch_ok or not pts:
        return
    arr = (POINTER_TOUCH_INFO * len(pts))()
    for i, (pid, x, y) in enumerate(pts):
        px = int(min(max(x, 0.0), 1.0) * (SW - 1))
        py = int(min(max(y, 0.0), 1.0) * (SH - 1))
        c = arr[i]
        c.pointerInfo.pointerType = 2  # PT_TOUCH
        c.pointerInfo.pointerId = pid
        c.pointerInfo.ptPixelLocation.x = px
        c.pointerInfo.ptPixelLocation.y = py
        if action == "C" or (action == "U" and pid == changed):
            flags = F_UP
        elif action == "D" and pid == changed:
            flags = F_DOWN | F_INRANGE | F_INCONTACT | F_NEW
        else:
            flags = F_UPDATE | F_INRANGE | F_INCONTACT
        c.pointerInfo.pointerFlags = flags
        c.touchMask = 7
        c.orientation = 90
        c.pressure = 512
        c.rcContact = RECT(px - 2, py - 2, px + 2, py + 2)
    user32.InjectTouchInput(len(pts), arr)

# ---------------- video pipeline -----------------------------------------------------
def build_cmd(enc, cap, W, H, fps, mbps):
    br = f"{int(mbps * 1000)}k"
    buf = f"{max(int(mbps * 1000 / fps * 2), 200)}k"
    g = str(fps * 2)
    if cap == "dd":  # Desktop Duplication (fastest)
        src = ["-f", "lavfi", "-i", f"ddagrab=framerate={fps}"]
        vf = f"hwdownload,format=bgra,scale={W}:{H}:flags=fast_bilinear,format=yuv420p"
    else:            # GDI fallback
        src = ["-f", "gdigrab", "-framerate", str(fps), "-i", "desktop"]
        vf = f"scale={W}:{H}:flags=fast_bilinear,format=yuv420p"
    encs = {
        "nvenc": ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll", "-zerolatency", "1", "-delay", "0",
                  "-rc", "cbr", "-b:v", br, "-maxrate", br, "-bufsize", buf, "-bf", "0", "-g", g, "-profile:v", "baseline"],
        "amf": ["-c:v", "h264_amf", "-usage", "ultralowlatency", "-quality", "speed", "-rc", "cbr", "-b:v", br, "-g", g],
        "qsv": ["-c:v", "h264_qsv", "-preset", "veryfast", "-look_ahead", "0", "-b:v", br, "-bf", "0", "-g", g],
        "x264": ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "baseline",
                 "-b:v", br, "-maxrate", br, "-bufsize", buf, "-g", g],
    }
    return ([FFMPEG, "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay"] + src +
            ["-an", "-vf", vf] + encs[enc] + ["-f", "h264", "-flush_packets", "1", "pipe:1"])

def nal_packets(nals):
    return b"".join(struct.pack(">I", len(n)) + n for n in nals if n)

def run_stream(p, conn, stop):
    q = queue.Queue()
    def reader():
        while True:
            try:
                d = p.stdout.read(65536)
            except Exception:
                d = b""
            q.put(d)
            if not d:
                return
    threading.Thread(target=reader, daemon=True).start()
    buf, sent, t0 = b"", False, time.time()
    while not stop.is_set():
        try:
            chunk = q.get(timeout=0.003)
        except queue.Empty:
            # idle: the last NAL is complete (no frame data pending) -> send it now, no waiting for next frame
            pos = [m.start() for m in START_RE.finditer(buf)]
            if pos:
                conn.sendall(nal_packets([buf[pos[-1] + 3:]]))
                sent, buf = True, b""
            if not sent and time.time() - t0 > 4:
                return False
            continue
        if not chunk:
            return sent
        buf += chunk
        pos = [m.start() for m in START_RE.finditer(buf)]
        if len(pos) < 2:
            continue
        nals = [buf[pos[i] + 3:pos[i + 1]].rstrip(b"\x00") for i in range(len(pos) - 1)]
        buf = buf[pos[-1]:]
        conn.sendall(nal_packets(nals))
        sent = True
    return sent

def pump(conn, W, H, fps, mbps, pref, stop, log):
    order = ["nvenc", "amf", "qsv", "x264"] if pref == "auto" else [pref]
    combos = [(e, c) for e in order for c in ("dd", "gdi")]
    if LAST_GOOD.get("combo") in combos:
        combos.remove(LAST_GOOD["combo"])
        combos.insert(0, LAST_GOOD["combo"])
    for enc, cap in combos:
        if stop.is_set():
            return
        log(f"Trying encoder={enc} capture={cap}")
        p = subprocess.Popen(build_cmd(enc, cap, W, H, fps, mbps), stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, creationflags=NOWIN, bufsize=0)
        try:
            ok = run_stream(p, conn, stop)
        except OSError:
            ok = True  # client closed the socket
            stop.set()
        finally:
            p.kill()
        if ok:
            LAST_GOOD["combo"] = (enc, cap)
            return
    log("No working encoder/capture found. Is ffmpeg.exe next to the app?")

# ---------------- server ---------------------------------------------------------------
class Server:
    def __init__(self, log, settings):
        self.log, self.settings = log, settings
        self.running = False
        self.stop_evt = threading.Event()

    def start(self):
        self.running = True
        init_touch() or self.log("Touch injection unavailable (touch input disabled).")
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", PORT))
        self.sock.listen(1)
        threading.Thread(target=self.accept_loop, daemon=True).start()
        threading.Thread(target=self.beacon, daemon=True).start()
        self.log(f"Waiting for tablet on port {PORT}...")

    def stop(self):
        self.running = False
        self.stop_evt.set()
        try:
            self.sock.close()
        except Exception:
            pass

    def beacon(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        msg = f"SCREENLINK|{socket.gethostname()}|{PORT}".encode()
        while self.running:
            try:
                s.sendto(msg, ("255.255.255.255", BEACON_PORT))
            except Exception:
                pass
            time.sleep(1)

    def accept_loop(self):
        while self.running:
            try:
                conn, addr = self.sock.accept()
            except OSError:
                return
            self.stop_evt.set()          # kick the previous client
            self.stop_evt = threading.Event()
            threading.Thread(target=self.handle, args=(conn, addr, self.stop_evt), daemon=True).start()

    def handle(self, conn, addr, stop):
        self.log(f"Tablet connected: {addr[0]}")
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        f = conn.makefile("rb")
        try:
            hello = f.readline().decode(errors="ignore").strip().split(",")
            game = len(hello) > 1 and hello[1] == "game"
            H, fps = (720, 60) if game else (1080, 30)
            W = int(round(SW * H / SH / 2)) * 2
            s = self.settings()
            self.log(f"Mode: {'GAME' if game else 'Mirror'}  {W}x{H}@{fps}  {s['mbps']} Mbps")
            conn.sendall(struct.pack(">HHB", W, H, fps))
            threading.Thread(target=pump, args=(conn, W, H, fps, s["mbps"] * (1.5 if game else 1.0), s["enc"], stop, self.log),
                             daemon=True).start()
            for raw in f:  # touch lines: ACTION;changedId;id,x,y;id,x,y...
                try:
                    parts = raw.decode(errors="ignore").strip().split(";")
                    pts = []
                    for t in parts[2:]:
                        a = t.split(",")
                        pts.append((int(a[0]), float(a[1]), float(a[2])))
                    inject(parts[0], int(parts[1]), pts)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            stop.set()
            try:
                conn.close()
            except Exception:
                pass
            self.log("Tablet disconnected")

# ---------------- GUI ------------------------------------------------------------------
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("ScreenLink")
        self.root.geometry("440x340")
        self.q = queue.Queue()
        self.enc = tk.StringVar(value="auto")
        self.mbps = tk.DoubleVar(value=15)
        fr = ttk.Frame(self.root, padding=10)
        fr.pack(fill="both", expand=True)
        try:
            ips = ", ".join(i for i in socket.gethostbyname_ex(socket.gethostname())[2] if not i.startswith("127."))
        except Exception:
            ips = "?"
        ttk.Label(fr, text=f"PC address: {ips}", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        row = ttk.Frame(fr); row.pack(fill="x", pady=8)
        ttk.Label(row, text="Encoder").pack(side="left")
        ttk.Combobox(row, textvariable=self.enc, values=["auto", "nvenc", "amf", "qsv", "x264"], width=8,
                     state="readonly").pack(side="left", padx=6)
        ttk.Label(row, text="Bitrate Mbps").pack(side="left", padx=(12, 0))
        ttk.Spinbox(row, from_=2, to=80, textvariable=self.mbps, width=5).pack(side="left", padx=6)
        self.btn = ttk.Button(fr, text="Start server", command=self.toggle)
        self.btn.pack(fill="x")
        self.text = tk.Text(fr, height=10, state="disabled")
        self.text.pack(fill="both", expand=True, pady=8)
        self.server = Server(self.log, lambda: {"enc": self.enc.get(), "mbps": float(self.mbps.get())})
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(150, self.poll)

    def log(self, m):
        self.q.put(m)

    def poll(self):
        while not self.q.empty():
            self.text.config(state="normal")
            self.text.insert("end", self.q.get() + "\n")
            self.text.see("end")
            self.text.config(state="disabled")
        self.root.after(150, self.poll)

    def toggle(self):
        if self.server.running:
            self.server.stop()
            self.btn.config(text="Start server")
            self.log("Stopped")
        else:
            self.server = Server(self.log, lambda: {"enc": self.enc.get(), "mbps": float(self.mbps.get())})
            self.server.start()
            self.btn.config(text="Stop server")

    def close(self):
        self.server.stop()
        self.root.destroy()
        os._exit(0)

if __name__ == "__main__":
    App().root.mainloop()
