"""
Video Stats - how much footage do you have per game?

Pick a folder (e.g. your ShadowPlay "Videos" folder). Every sub-folder is treated
as one game, and the app shows for each: number of videos, average length,
total minutes and total hours.

Run:  python video_stats.py
Needs only Python 3 - no extra packages.

Why it is fast
  * The length is read from the MP4/MOV header (a few dozen bytes per file),
    the video itself is never read.
  * Files are checked in parallel.
  * Results are remembered, so the next scan only looks at new/changed files.
Other formats (mkv, avi, ...) are measured with ffprobe if it is installed.
"""
import ctypes
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, ttk
from tkinter import font as tkfont

# ----------------------------------------------------------------------------
# Scanning
# ----------------------------------------------------------------------------
MP4_EXT = {".mp4", ".m4v", ".mov"}
VIDEO_EXT = MP4_EXT | {".mkv", ".avi", ".webm", ".flv", ".wmv"}
FFPROBE = shutil.which("ffprobe")
WORKERS = min(32, (os.cpu_count() or 4) * 4)

# "Rocket League 2023.05.12 - 21.33.44.01" -> "Rocket League"
DATE_RE = re.compile(r"[\s_\-]*\d{4}[.\-_]\d{2}[.\-_]\d{2}.*$")

CACHE_FILE = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "VideoStats", "cache.json")

_BOX = struct.Struct(">I4s")


def mp4_duration(path):
    """Length in seconds, read from the 'mvhd' box. None if it can't be found."""
    with open(path, "rb", buffering=0) as f:
        end = os.fstat(f.fileno()).st_size
        pos = 0
        while pos + 8 <= end:                       # walk the top-level boxes
            f.seek(pos)
            hdr = f.read(16)
            if len(hdr) < 8:
                return None
            size, kind = _BOX.unpack_from(hdr)
            header = 8
            if size == 1:                           # 64-bit box size
                if len(hdr) < 16:
                    return None
                size = struct.unpack_from(">Q", hdr, 8)[0]
                header = 16
            elif size == 0:                         # box runs to end of file
                size = end - pos
            if size < header:
                return None
            if kind == b"moov":
                return _mvhd(f, pos + header, min(pos + size, end))
            pos += size                             # skips 'mdat' without reading it
    return None


def _mvhd(f, pos, end):
    while pos + 8 <= end:
        f.seek(pos)
        buf = f.read(48)                            # box header + the fields we need
        if len(buf) < 8:
            return None
        size, kind = _BOX.unpack_from(buf)
        if kind == b"mvhd":
            if len(buf) >= 40 and buf[8] == 1:      # version 1: 64-bit duration
                scale, dur = struct.unpack_from(">IQ", buf, 28)
            elif len(buf) >= 28:
                scale, dur = struct.unpack_from(">II", buf, 20)
                if dur == 0xFFFFFFFF:
                    return None
            else:
                return None
            return dur / scale if scale and dur else None
        if size < 8:
            return None
        pos += size
    return None


def ffprobe_duration(path):
    if not FFPROBE:
        return None
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return float(out.stdout.strip()) or None
    except Exception:
        return None


def get_duration(path):
    dur = None
    if os.path.splitext(path)[1].lower() in MP4_EXT:
        try:
            dur = mp4_duration(path)
        except Exception:
            dur = None
    return dur or ffprobe_duration(path)


def find_videos(root):
    """Yield (path, size, mtime_ns, game) for every video under root.

    The game is the first-level folder name. Videos lying loose in root are
    grouped by their title with the date removed.
    """
    stack = [(root, None)]
    while stack:
        folder, game = stack.pop()
        try:
            with os.scandir(folder) as entries:
                for e in entries:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append((e.path, game or e.name))
                            continue
                        stem, ext = os.path.splitext(e.name)
                        if ext.lower() not in VIDEO_EXT:
                            continue
                        st = e.stat()               # free on Windows (comes with the listing)
                        yield (e.path, st.st_size, st.st_mtime_ns,
                               game or DATE_RE.sub("", stem).strip() or "(unnamed)")
                    except OSError:
                        pass
        except OSError:
            pass


def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data["files"] if data.get("v") == 1 else {}
    except Exception:
        return {}


def save_cache(files):
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"v": 1, "files": files}, f, separators=(",", ":"))
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


# ----------------------------------------------------------------------------
# Look
# ----------------------------------------------------------------------------
BG, CARD, ROW_ALT, ROW_HOVER, LINE = "#0f1020", "#1a1b33", "#1f2040", "#2a2c5a", "#2a2b4d"
TEXT, MUTED = "#eceaff", "#9b99c4"
ACCENT, ACCENT_HI, CYAN, PINK, GOLD, GREEN = (
    "#7c5cff", "#9a80ff", "#00d4ff", "#ff4fa3", "#ffd166", "#7cf5b0")
PALETTE = [CYAN, ACCENT, PINK, GOLD, GREEN, "#ff8a5c", "#5cc8ff", "#c77dff"]

# key, heading, width (None = shares the remaining space)
COLUMNS = [("game", "GAME", None), ("count", "VIDEOS", 80), ("avg", "AVG LENGTH", 115),
           ("minutes", "TOTAL MINUTES", 135), ("hours", "TOTAL HOURS", 115),
           ("share", "SHARE OF FOOTAGE", None)]


def fmt_time(sec):
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Video Stats")
        self.configure(bg=BG)
        self.scale = self.winfo_fpixels("1i") / 96          # 1.0 at 100% Windows scaling
        px = self.px
        self.geometry("%dx%d" % (min(px(1000), self.winfo_screenwidth() - 80),
                                 min(px(660), self.winfo_screenheight() - 120)))
        self.minsize(px(860), px(480))

        fam = "Segoe UI" if "Segoe UI" in tkfont.families() else tkfont.nametofont("TkDefaultFont").actual("family")
        self.f_title = tkfont.Font(family=fam, size=22, weight="bold")
        self.f_big = tkfont.Font(family=fam, size=20, weight="bold")
        self.f_body = tkfont.Font(family=fam, size=11)
        self.f_bold = tkfont.Font(family=fam, size=11, weight="bold")
        self.f_small = tkfont.Font(family=fam, size=9, weight="bold")
        self.row_h = self.f_body.metrics("linespace") + px(20)

        self.q = queue.Queue()
        self.stop = threading.Event()
        self.scanning = False
        self.rows = []                       # (game, videos, seconds)
        self.sort_key, self.sort_desc = "minutes", True
        self.empty_msg = "Press Browse and pick your Videos folder"

        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(50, self.poll)
        self.after(10, self._dark_titlebar)

    def px(self, n):
        return int(round(n * self.scale))

    # ---- widgets ---------------------------------------------------------
    def _build(self):
        px = self.px
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="both", expand=True, padx=px(22), pady=px(18))

        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="Video", bg=BG, fg=TEXT, font=self.f_title).pack(side="left")
        tk.Label(head, text="Stats", bg=BG, fg=ACCENT, font=self.f_title).pack(side="left")
        tk.Label(head, text="how much footage you have per game", bg=BG, fg=MUTED,
                 font=self.f_body).pack(side="left", padx=px(14), pady=(px(8), 0))

        bar = tk.Frame(outer, bg=CARD)
        bar.pack(fill="x", pady=(px(14), 0))
        self.path_lbl = tk.Label(bar, text="No folder selected", bg=CARD, fg=MUTED,
                                 font=self.f_body, anchor="w")
        self.path_lbl.pack(side="left", fill="x", expand=True, padx=px(14), pady=px(10))
        self.btn = tk.Label(bar, text="Browse…", bg=ACCENT, fg="white", font=self.f_bold,
                            padx=px(22), pady=px(8), cursor="hand2")
        self.btn.pack(side="right", padx=px(6), pady=px(6))
        self.btn.bind("<Button-1>", lambda e: self.browse())
        self.btn.bind("<Enter>", lambda e: self.scanning or self.btn.config(bg=ACCENT_HI))
        self.btn.bind("<Leave>", lambda e: self.scanning or self.btn.config(bg=ACCENT))

        cards = tk.Frame(outer, bg=BG)
        cards.pack(fill="x", pady=(px(14), 0))
        self.cards = {}
        for i, (key, caption, color) in enumerate([
                ("games", "GAMES", CYAN), ("videos", "VIDEOS", GREEN),
                ("total", "TOTAL FOOTAGE", GOLD), ("avg", "AVG LENGTH", PINK)]):
            cards.columnconfigure(i, weight=1, uniform="card")
            card = tk.Frame(cards, bg=CARD)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else px(12), 0))
            tk.Frame(card, bg=color, height=px(3)).pack(fill="x")
            value = tk.Label(card, text="–", bg=CARD, fg=color, font=self.f_big, anchor="w")
            value.pack(fill="x", padx=px(14), pady=(px(8), 0))
            cap = tk.Label(card, text=caption, bg=CARD, fg=MUTED, font=self.f_small, anchor="w")
            cap.pack(fill="x", padx=px(14), pady=(0, px(10)))
            self.cards[key] = (value, cap, caption)

        bottom = tk.Frame(outer, bg=BG)
        bottom.pack(side="bottom", fill="x")
        self.progress = tk.Canvas(bottom, height=px(4), bg=LINE, highlightthickness=0)
        self.progress.pack(fill="x", pady=(px(12), px(8)))
        self.status = tk.Label(bottom, text="Ready.", bg=BG, fg=MUTED, font=self.f_body, anchor="w")
        self.status.pack(fill="x")

        table = tk.Frame(outer, bg=CARD)
        table.pack(fill="both", expand=True, pady=(px(14), 0))
        table.columnconfigure(0, weight=1)
        table.rowconfigure(1, weight=1)
        self.head = tk.Canvas(table, height=self.row_h, bg=CARD, highlightthickness=0, cursor="hand2")
        self.head.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.body = tk.Canvas(table, bg=CARD, highlightthickness=0, yscrollincrement=self.row_h)
        self.body.grid(row=1, column=0, sticky="nsew")

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Vertical.TScrollbar", background="#3a3c6e", troughcolor=CARD,
                        bordercolor=CARD, lightcolor="#3a3c6e", darkcolor="#3a3c6e",
                        arrowcolor=MUTED, relief="flat")
        style.map("Vertical.TScrollbar", background=[("active", ACCENT)])
        self.vsb = ttk.Scrollbar(table, orient="vertical", command=self.body.yview)
        self.vsb.grid(row=1, column=1, sticky="ns")
        self.body.configure(yscrollcommand=self.vsb.set)

        self.body.bind("<Configure>", lambda e: self.draw())
        self.body.bind("<Motion>", self._hover)
        self.body.bind("<Leave>", lambda e: self.body.itemconfigure("hover", state="hidden"))
        self.head.bind("<Button-1>", self._head_click)
        self.bind_all("<MouseWheel>", lambda e: self._wheel(-1 if e.delta > 0 else 1))
        if sys.platform.startswith("linux"):
            self.bind_all("<Button-4>", lambda e: self._wheel(-1))
            self.bind_all("<Button-5>", lambda e: self._wheel(1))

    def _dark_titlebar(self):
        try:                                             # Windows 10/11 only
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            on = ctypes.c_int(1)
            for attr in (20, 19):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), 4)
        except Exception:
            pass

    # ---- table -----------------------------------------------------------
    def _columns(self, width):
        """{key: (left, right)} in pixels."""
        free = max(width - sum(self.px(w) for _, _, w in COLUMNS if w), self.px(260))
        flex = {"game": int(free * 0.55), "share": free - int(free * 0.55)}
        x, out = 0, {}
        for key, _, w in COLUMNS:
            w = self.px(w) if w else flex[key]
            out[key] = (x, x + w)
            x += w
        return out

    def _sorted_rows(self):
        key = {"game": lambda r: r[0].lower(), "count": lambda r: r[1],
               "avg": lambda r: r[2] / r[1]}.get(self.sort_key, lambda r: r[2])
        return sorted(self.rows, key=key, reverse=self.sort_desc)

    def _fit(self, text, font, width):
        if font.measure(text) <= width:
            return text
        while text and font.measure(text + "…") > width:
            text = text[:-1]
        return text + "…"

    def draw_head(self):
        c, pad = self.head, self.px(14)
        c.delete("all")
        y = self.row_h / 2
        for key, title, _ in COLUMNS:
            x0, x1 = self._columns(self.body.winfo_width())[key]
            active = key == self.sort_key
            if active:
                title += "  ↓" if self.sort_desc else "  ↑"
            left = key in ("game", "share")
            c.create_text(x0 + pad if left else x1 - pad, y, text=title, anchor="w" if left else "e",
                          fill=TEXT if active else MUTED, font=self.f_small)
        c.create_line(0, self.row_h - 1, c.winfo_width(), self.row_h - 1, fill=LINE)

    def draw(self):
        c, px, rh = self.body, self.px, self.row_h
        c.delete("all")
        w, h = max(c.winfo_width(), 1), c.winfo_height()
        self.draw_head()
        rows = self._sorted_rows()
        if not rows:
            c.create_text(w / 2, h / 2, text=self.empty_msg, fill=MUTED, font=self.f_body)
            c.configure(scrollregion=(0, 0, w, h))
            self.vsb.grid_remove()
            return

        cols, pad = self._columns(w), px(14)
        total = sum(r[2] for r in rows) or 1
        longest = max(r[2] for r in rows) or 1
        by_time = sorted(self.rows, key=lambda r: -r[2])
        color = {r[0]: PALETTE[i % len(PALETTE)] for i, r in enumerate(by_time)}

        for i in range(1, len(rows), 2):
            c.create_rectangle(0, i * rh, w, (i + 1) * rh, fill=ROW_ALT, width=0)
        c.create_rectangle(0, 0, 0, 0, fill=ROW_HOVER, width=0, state="hidden", tags="hover")

        dot = px(5)
        pct_w = self.f_body.measure("100.0%") + px(10)
        for i, (game, count, sec) in enumerate(rows):
            y = i * rh + rh / 2
            col = color[game]
            x0, x1 = cols["game"]
            c.create_oval(x0 + pad, y - dot, x0 + pad + 2 * dot, y + dot, fill=col, width=0)
            name_x = x0 + pad + 2 * dot + px(10)
            c.create_text(name_x, y, anchor="w", fill=TEXT, font=self.f_bold,
                          text=self._fit(game, self.f_bold, x1 - name_x - pad))
            for key, text, fill, font in (
                    ("count", f"{count:,}", GREEN, self.f_body),
                    ("avg", fmt_time(sec / count), TEXT, self.f_body),
                    ("minutes", f"{sec / 60:,.1f}", GOLD, self.f_bold),
                    ("hours", f"{sec / 3600:,.2f}", PINK, self.f_body)):
                c.create_text(cols[key][1] - pad, y, anchor="e", text=text, fill=fill, font=font)
            x0, x1 = cols["share"]
            b0, b1 = x0 + pad + px(3), x1 - pad - pct_w
            c.create_line(b0, y, b1, y, fill=LINE, width=px(6), capstyle="round")
            c.create_line(b0, y, b0 + (b1 - b0) * sec / longest, y, fill=col, width=px(6), capstyle="round")
            c.create_text(x1 - pad, y, anchor="e", text=f"{sec / total * 100:.1f}%", fill=MUTED, font=self.f_body)

        content = len(rows) * rh
        c.configure(scrollregion=(0, 0, w, max(content, h)))
        if content > h:
            self.vsb.grid()
        else:
            self.vsb.grid_remove()
            c.yview_moveto(0)

    def _hover(self, e):
        i = int(self.body.canvasy(e.y) // self.row_h)
        if 0 <= i < len(self.rows):
            self.body.coords("hover", 0, i * self.row_h, self.body.winfo_width(), (i + 1) * self.row_h)
            self.body.itemconfigure("hover", state="normal")
        else:
            self.body.itemconfigure("hover", state="hidden")

    def _wheel(self, direction):
        if len(self.rows) * self.row_h > self.body.winfo_height():
            self.body.yview_scroll(direction, "units")

    def _head_click(self, e):
        for key, (x0, x1) in self._columns(self.body.winfo_width()).items():
            if x0 <= e.x < x1:
                if key == self.sort_key:
                    self.sort_desc = not self.sort_desc
                else:
                    self.sort_key, self.sort_desc = key, key != "game"
                self.draw()
                return

    def show(self, rows):
        self.rows = [r for r in rows if r[1]]
        videos = sum(r[1] for r in self.rows)
        sec = sum(r[2] for r in self.rows)
        self.cards["games"][0].config(text=f"{len(self.rows):,}" if self.rows else "–")
        self.cards["videos"][0].config(text=f"{videos:,}" if videos else "–")
        self.cards["total"][0].config(text=f"{sec / 3600:,.1f} h" if videos else "–")
        self.cards["total"][1].config(
            text=f"TOTAL  ·  {sec / 60:,.0f} MIN" if videos else "TOTAL FOOTAGE")
        self.cards["avg"][0].config(text=fmt_time(sec / videos) if videos else "–")
        self.draw()

    def set_progress(self, frac):
        self.progress.delete("all")
        w = self.progress.winfo_width()
        if frac > 0:
            self.progress.create_rectangle(0, 0, w * min(frac, 1), self.px(4), fill=CYAN, width=0)

    # ---- scanning --------------------------------------------------------
    def browse(self):
        if self.scanning:
            return
        folder = filedialog.askdirectory(title="Select your Videos folder")
        if folder:
            self.start_scan(folder)

    def start_scan(self, folder):
        folder = os.path.normpath(folder)
        self.scanning = True
        self.btn.config(bg="#4a4a6a", cursor="arrow")
        self.path_lbl.config(text=folder, fg=TEXT)
        self.empty_msg = "Scanning…"
        self.show([])
        self.set_progress(0)
        self.status.config(text="Looking for videos…", fg=CYAN)
        threading.Thread(target=self.scan, args=(folder,), daemon=True).start()

    def _duration_job(self, item):
        return None if self.stop.is_set() else get_duration(item[0])

    def scan(self, root):
        started = time.perf_counter()
        cache = load_cache()
        stats, keep, todo = {}, {}, []
        failed = 0

        def add(game, dur):
            s = stats.setdefault(game, [0, 0.0])
            s[0] += 1
            s[1] += dur

        def post(kind, *extra):
            self.q.put((kind, [(g, c, s) for g, (c, s) in stats.items()]) + extra)

        for item in find_videos(root):
            path, size, mtime, game = item
            hit = cache.get(path)
            if hit and hit[0] == size and hit[1] == mtime:     # unchanged since last scan
                keep[path] = hit
                add(game, hit[2])
            else:
                todo.append(item)
        cached, total = len(keep), len(keep) + len(todo)
        post("progress", cached, total)

        pool = ThreadPoolExecutor(WORKERS)
        try:
            last = time.perf_counter()
            for n, (item, dur) in enumerate(zip(todo, pool.map(self._duration_job, todo)), 1):
                if self.stop.is_set():
                    return
                path, size, mtime, game = item
                if dur:
                    keep[path] = [size, mtime, dur]
                    add(game, dur)
                else:
                    failed += 1
                if time.perf_counter() - last > 0.2:
                    last = time.perf_counter()
                    post("progress", cached + n, total)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        prefix = root.rstrip(os.sep) + os.sep                  # forget files that are gone
        cache = {p: v for p, v in cache.items() if not p.startswith(prefix)}
        cache.update(keep)
        save_cache(cache)
        post("done", total, cached, failed, time.perf_counter() - started)

    def poll(self):
        try:
            while True:
                kind, rows, *rest = self.q.get_nowait()
                if kind == "progress":
                    done, total = rest
                    self.show(rows)
                    self.set_progress(done / total if total else 0)
                    self.status.config(text=f"Scanning…  {done:,} / {total:,} videos")
                else:
                    self.finish(rows, *rest)
        except queue.Empty:
            pass
        self.after(50, self.poll)

    def finish(self, rows, total, cached, failed, seconds):
        self.empty_msg = "No videos found in that folder"
        self.show(rows)
        self.set_progress(1 if total else 0)
        msg = f"Done  ·  {total - failed:,} videos in {seconds:.1f} s"
        if cached:
            msg += f"  ·  {cached:,} remembered from last scan"
        if failed:
            msg += f"  ·  {failed:,} couldn't be read"
            if not FFPROBE:
                msg += " (install ffmpeg to read non-MP4 files)"
        self.status.config(text=msg, fg=GOLD if failed else GREEN)
        self.scanning = False
        self.btn.config(bg=ACCENT, cursor="hand2")

    def on_close(self):
        self.stop.set()
        self.destroy()


def main():
    try:                                    # crisp text on scaled Windows displays
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App().mainloop()


if __name__ == "__main__":
    main()
