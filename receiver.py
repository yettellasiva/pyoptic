"""Optical transfer - RECEIVER. Reads the QR stream from the webcam and rebuilds the file."""

import base64
import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import cv2
import zxingcpp

from protocol import Assembler, format_ranges, short_hash

RESOLUTIONS = ["640x480", "1280x720", "1920x1080"]
PREVIEW_W = 640


def default_out_dir():
    d = os.path.join(os.path.expanduser("~"), "Downloads")
    return d if os.path.isdir(d) else os.getcwd()


def unique_path(folder, name):
    name = os.path.basename(name).strip() or "received.bin"
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    base, ext = os.path.splitext(name)
    path, n = os.path.join(folder, name), 1
    while os.path.exists(path):
        path = os.path.join(folder, f"{base} ({n}){ext}")
        n += 1
    return path


class ReceiverApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Optical Transfer - Receiver")
        root.geometry("1100x640")

        self.asm = Assembler()
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.thread = None
        self.preview = None
        self.photo = None
        self.error = None
        self.cam_fps = 0.0
        self.counts = {"new": 0, "dup": 0, "meta": 0, "foreign": 0, "bad": 0}
        self.started_at = None
        self.finished = False

        self.cam_var = tk.StringVar(value="0")
        self.res_var = tk.StringVar(value="1280x720")
        self.out_var = tk.StringVar(value=default_out_dir())
        self.stats_var = tk.StringVar(value="Press Start camera and point it at the sender's screen.")
        self.missing_var = tk.StringVar()
        self.expected_var = tk.StringVar()
        self.computed_var = tk.StringVar()

        self._build()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.poll()

    def _build(self):
        p = ttk.Frame(self.root, padding=12, width=380)
        p.pack(side="left", fill="y")
        p.pack_propagate(False)

        grid = ttk.Frame(p)
        grid.pack(fill="x")
        ttk.Label(grid, text="Camera index").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Spinbox(grid, textvariable=self.cam_var, from_=0, to=9, width=6).grid(row=0, column=1, sticky="w")
        ttk.Label(grid, text="Resolution").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Combobox(grid, textvariable=self.res_var, values=RESOLUTIONS, width=10, state="readonly"
                     ).grid(row=1, column=1, sticky="w")

        ttk.Label(p, text="Save to folder").pack(anchor="w", pady=(8, 0))
        row = ttk.Frame(p)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.out_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="…", width=3, command=self.browse).pack(side="left", padx=(4, 0))

        btns = ttk.Frame(p)
        btns.pack(fill="x", pady=10)
        self.cam_btn = ttk.Button(btns, text="Start camera", command=self.toggle_camera)
        self.cam_btn.pack(side="left", fill="x", expand=True)
        ttk.Button(btns, text="Reset transfer", command=self.reset).pack(side="left", fill="x", expand=True,
                                                                         padx=(4, 0))

        self.progress = ttk.Progressbar(p, maximum=1, value=0)
        self.progress.pack(fill="x")
        ttk.Label(p, textvariable=self.stats_var, justify="left", wraplength=350).pack(anchor="w", pady=6)

        ttk.Label(p, text="Missing chunks (type into sender's 'Resend only'):").pack(anchor="w")
        ttk.Entry(p, textvariable=self.missing_var, state="readonly").pack(fill="x", pady=(0, 10))

        ttk.Label(p, text="SHA-256 announced by sender").pack(anchor="w")
        ttk.Entry(p, textvariable=self.expected_var, state="readonly").pack(fill="x")
        ttk.Label(p, text="SHA-256 of received file").pack(anchor="w", pady=(6, 0))
        ttk.Entry(p, textvariable=self.computed_var, state="readonly").pack(fill="x")
        self.short_lbl = ttk.Label(p, text="", font=("Consolas", 14, "bold"))
        self.short_lbl.pack(anchor="w", pady=(4, 0))
        self.result_lbl = tk.Label(p, text="", font=("Segoe UI", 16, "bold"))
        self.result_lbl.pack(anchor="w", pady=6)

        self.view = tk.Label(self.root, bg="black")
        self.view.pack(side="left", fill="both", expand=True)

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.out_var.get())
        if d:
            self.out_var.set(d)

    # ---------- camera thread ----------
    def toggle_camera(self):
        if self.thread and self.thread.is_alive():
            self.stop_evt.set()
            self.cam_btn.config(text="Start camera")
            return
        try:
            cam = int(self.cam_var.get())
            w, h = (int(x) for x in self.res_var.get().split("x"))
        except ValueError:
            messagebox.showerror("Receiver", "Invalid camera index or resolution.")
            return
        self.error = None
        self.stop_evt.clear()
        self.thread = threading.Thread(target=self.worker, args=(cam, w, h), daemon=True)
        self.thread.start()
        self.cam_btn.config(text="Stop camera")
        if self.started_at is None:
            self.started_at = time.time()

    def worker(self, cam, w, h):
        cap = cv2.VideoCapture(cam, cv2.CAP_DSHOW)
        if not cap.isOpened():
            self.error = f"Cannot open camera {cam}."
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS, 30)
        last = time.perf_counter()
        try:
            while not self.stop_evt.is_set():
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.01)
                    continue
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                codes = zxingcpp.read_barcodes(gray, formats=zxingcpp.BarcodeFormat.QRCode,
                                               try_rotate=False, try_invert=False)
                with self.lock:
                    for c in codes:
                        status = self.asm.add(c.bytes)
                        self.counts[status] += 1
                        pos = c.position
                        pts = [(pos.top_left.x, pos.top_left.y), (pos.top_right.x, pos.top_right.y),
                               (pos.bottom_right.x, pos.bottom_right.y), (pos.bottom_left.x, pos.bottom_left.y)]
                        color = (0, 200, 0) if status in ("new", "dup", "meta") else (0, 0, 255)
                        for i in range(4):
                            cv2.line(frame, pts[i], pts[(i + 1) % 4], color, 4)
                    now = time.perf_counter()
                    self.cam_fps = 0.9 * self.cam_fps + 0.1 / max(now - last, 1e-3)
                    last = now
                    scale = PREVIEW_W / frame.shape[1]
                    self.preview = cv2.resize(frame, None, fx=scale, fy=scale)
                    if self.asm.complete():
                        break
        finally:
            cap.release()

    # ---------- GUI loop ----------
    def poll(self):
        with self.lock:
            preview, self.preview = self.preview, None
            total = self.asm.total
            got = len(self.asm.chunks)
            meta = self.asm.meta
            missing = self.asm.missing() if total else []
            counts = dict(self.counts)
            complete = self.asm.complete()

        if preview is not None:
            ok, png = cv2.imencode(".png", preview)
            if ok:
                self.photo = tk.PhotoImage(data=base64.b64encode(png.tobytes()))
                self.view.config(image=self.photo)

        if self.error:
            messagebox.showerror("Receiver", self.error)
            self.error = None
            self.cam_btn.config(text="Start camera")

        if total:
            self.progress.config(maximum=total, value=got)
            elapsed = time.time() - self.started_at if self.started_at else 0
            rate = got / elapsed if elapsed > 0 else 0
            eta = (total - got) / rate if rate > 0 else 0
            name = meta["name"] if meta else "(waiting for file info…)"
            size = f"{meta['size']:,} bytes" if meta else ""
            self.stats_var.set(
                f"{name}  {size}\nChunks: {got:,} / {total:,}  ({100 * got / total:.1f} %)\n"
                f"Camera fps: {self.cam_fps:.1f}   new: {counts['new']:,}  dup: {counts['dup']:,}  "
                f"other file: {counts['foreign']}\n"
                f"Elapsed: {elapsed / 60:.1f} min   remaining ≈ {eta / 60:.1f} min")
            self.missing_var.set(format_ranges(missing)[:2000])
            if meta:
                self.expected_var.set(meta.get("sha256", ""))

        if complete and not self.finished:
            self.finished = True
            self.stop_evt.set()
            self.cam_btn.config(text="Start camera")
            self.finish()

        self.root.after(60, self.poll)

    def finish(self):
        with self.lock:
            data, sha, ok = self.asm.assemble()
            name = self.asm.meta.get("name", "received.bin")
        self.computed_var.set(sha)
        self.short_lbl.config(text=short_hash(sha))
        if not ok:
            self.result_lbl.config(text="✗ HASH MISMATCH - file not saved", fg="red")
            return
        try:
            os.makedirs(self.out_var.get(), exist_ok=True)
            path = unique_path(self.out_var.get(), name)
            with open(path, "wb") as f:
                f.write(data)
        except OSError as e:
            self.result_lbl.config(text=f"✓ Verified, but save failed:\n{e}", fg="red")
            return
        self.result_lbl.config(text="✓ VERIFIED - hashes match", fg="green")
        self.stats_var.set(self.stats_var.get() + f"\n\nSaved to:\n{path}")

    def reset(self):
        with self.lock:
            self.asm.reset()
            self.counts = dict.fromkeys(self.counts, 0)
        self.finished = False
        self.started_at = time.time() if self.thread and self.thread.is_alive() else None
        self.progress.config(value=0)
        for v in (self.missing_var, self.expected_var, self.computed_var):
            v.set("")
        self.stats_var.set("Reset. Waiting for frames…")
        self.short_lbl.config(text="")
        self.result_lbl.config(text="")

    def on_close(self):
        self.stop_evt.set()
        self.root.after(150, self.root.destroy)


def main():
    root = tk.Tk()
    ReceiverApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
