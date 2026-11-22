"""Optical transfer - SENDER. Shows a file as a looping stream of QR codes.

Keys: Space = start/pause, F11 = fullscreen, Esc = leave fullscreen.
"""

import base64
import io
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import segno

from protocol import OVERHEAD, FileEncoder, parse_ranges, short_hash

ECC_LEVELS = ["L", "M", "Q", "H"]


class SenderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Optical Transfer - Sender")
        root.geometry("1280x820")

        self.encoder = None
        self.enc_key = None
        self.version = None
        self.seq = []
        self.pos = 0
        self.passes = 0
        self.running = False
        self.job = None
        self.fullscreen = False
        self.photo = None
        self.fps_measured = 0.0
        self._last_tick = None

        self.path_var = tk.StringVar()
        self.chunk_var = tk.StringVar(value="800")
        self.ecc_var = tk.StringVar(value="M")
        self.fps_var = tk.StringVar(value="8")
        self.only_var = tk.StringVar()
        self.sha_var = tk.StringVar()
        self.info_var = tk.StringVar(value="Choose a file.")
        self.status_var = tk.StringVar()

        self._build_panel()
        self.canvas = tk.Canvas(root, bg="white", highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)

        root.bind("<F11>", lambda e: self.toggle_fullscreen())
        root.bind("<Escape>", lambda e: self.toggle_fullscreen(False))
        root.bind("<space>", lambda e: self.toggle_run())

    # ---------- UI ----------
    def _build_panel(self):
        p = self.panel = ttk.Frame(self.root, padding=12, width=340)
        p.pack(side="left", fill="y")
        p.pack_propagate(False)

        ttk.Label(p, text="File").pack(anchor="w")
        row = ttk.Frame(p)
        row.pack(fill="x", pady=(0, 8))
        ttk.Entry(row, textvariable=self.path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", command=self.browse).pack(side="left", padx=(4, 0))

        grid = ttk.Frame(p)
        grid.pack(fill="x")
        ttk.Label(grid, text="Bytes per QR").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Spinbox(grid, textvariable=self.chunk_var, from_=100, to=2900, increment=100, width=8
                    ).grid(row=0, column=1, sticky="w")
        ttk.Label(grid, text="Error correction").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Combobox(grid, textvariable=self.ecc_var, values=ECC_LEVELS, width=6, state="readonly"
                     ).grid(row=1, column=1, sticky="w")
        ttk.Label(grid, text="Frames / second").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Spinbox(grid, textvariable=self.fps_var, from_=1, to=30, increment=1, width=8
                    ).grid(row=2, column=1, sticky="w")

        ttk.Label(p, text="Resend only chunks (optional, e.g. 12-40, 77)").pack(anchor="w", pady=(10, 0))
        ttk.Entry(p, textvariable=self.only_var).pack(fill="x")

        btns = ttk.Frame(p)
        btns.pack(fill="x", pady=10)
        self.run_btn = ttk.Button(btns, text="Start (Space)", command=self.toggle_run)
        self.run_btn.pack(side="left", fill="x", expand=True)
        ttk.Button(btns, text="Fullscreen (F11)", command=self.toggle_fullscreen
                   ).pack(side="left", fill="x", expand=True, padx=(4, 0))

        ttk.Label(p, textvariable=self.info_var, justify="left", wraplength=310).pack(anchor="w", pady=(4, 8))

        ttk.Label(p, text="SHA-256 of file (compare with receiver)").pack(anchor="w")
        ttk.Entry(p, textvariable=self.sha_var, state="readonly").pack(fill="x")
        self.short_lbl = ttk.Label(p, text="", font=("Consolas", 14, "bold"))
        self.short_lbl.pack(anchor="w", pady=(2, 10))

        ttk.Label(p, textvariable=self.status_var, justify="left", wraplength=310).pack(anchor="w")

        tips = ("Tips: start with 800 bytes / M / 8 fps. If the receiver struggles, lower bytes per QR "
                "or fps; if it's reliable, raise them. Turn screen brightness up, avoid glare.")
        ttk.Label(p, text=tips, wraplength=310, foreground="gray").pack(side="bottom", anchor="w")

    def browse(self):
        path = filedialog.askopenfilename(title="File to send")
        if path:
            self.path_var.set(path)
            self.prepare(show_errors=True)

    def toggle_fullscreen(self, state=None):
        self.fullscreen = (not self.fullscreen) if state is None else state
        self.root.attributes("-fullscreen", self.fullscreen)
        if self.fullscreen:
            self.panel.pack_forget()
        else:
            self.panel.pack(side="left", fill="y", before=self.canvas)

    # ---------- logic ----------
    def _settings(self):
        chunk = int(self.chunk_var.get())
        fps = float(self.fps_var.get())
        ecc = self.ecc_var.get()
        if not 16 <= chunk <= 2953 - OVERHEAD:
            raise ValueError(f"Bytes per QR must be between 16 and {2953 - OVERHEAD}.")
        if not 0.5 <= fps <= 60:
            raise ValueError("Frames / second must be between 0.5 and 60.")
        return chunk, fps, ecc

    def prepare(self, show_errors=False) -> bool:
        """(Re)build the encoder and frame sequence from the current settings."""
        try:
            chunk, fps, ecc = self._settings()
            path = self.path_var.get()
            if not path:
                raise ValueError("Choose a file first.")
            key = (path, chunk)
            if key != self.enc_key:
                self.encoder = FileEncoder(path, chunk)
                self.enc_key = key
                self.pos = self.passes = 0
            # fixed QR version so every frame has the same size on screen
            full = self.encoder.data_packet(0) if self.encoder.total > 1 else self.encoder.meta_packet()
            try:
                self.version = segno.make(b"\0" * max(len(full), OVERHEAD + chunk), error=ecc,
                                          mode="byte", boost_error=False).version
            except segno.DataOverflowError:
                raise ValueError(f"{chunk} bytes don't fit in one QR at error level {ecc}. "
                                 "Lower bytes per QR or use a lower error level.")
            only = parse_ranges(self.only_var.get()) if self.only_var.get().strip() else None
            new_seq = self.encoder.sequence(only)
            if new_seq != self.seq:
                self.seq, self.pos = new_seq, 0
        except (ValueError, OSError) as e:
            if show_errors:
                messagebox.showerror("Sender", str(e))
            return False

        enc = self.encoder
        modules = 17 + 4 * self.version
        secs = len(self.seq) / fps
        self.info_var.set(
            f"{enc.name}\n{len(enc.data):,} bytes\n"
            f"{enc.total:,} chunks, QR version {self.version} ({modules}×{modules} modules)\n"
            f"Frames per pass: {len(self.seq):,}  ≈ {secs / 60:.1f} min at {fps:g} fps")
        self.sha_var.set(enc.sha256)
        self.short_lbl.config(text=short_hash(enc.sha256))
        return True

    def toggle_run(self):
        if self.running:
            self.running = False
            if self.job:
                self.root.after_cancel(self.job)
            self.run_btn.config(text="Start (Space)")
            return
        if not self.prepare(show_errors=True):
            return
        self.running = True
        self._last_tick = None
        self.run_btn.config(text="Pause (Space)")
        self.tick()

    def tick(self):
        if not self.running:
            return
        t0 = time.perf_counter()
        if self._last_tick:
            dt = t0 - self._last_tick
            self.fps_measured = 0.9 * self.fps_measured + 0.1 * (1 / dt) if self.fps_measured else 1 / dt
        self._last_tick = t0

        kind, idx = self.seq[self.pos]
        pkt = self.encoder.meta_packet() if kind == "meta" else self.encoder.data_packet(idx)
        self.show_qr(pkt)

        label = "meta" if kind == "meta" else f"chunk {idx}"
        self.status_var.set(f"Showing {label}  (frame {self.pos + 1}/{len(self.seq)})\n"
                            f"Completed passes: {self.passes}\nActual fps: {self.fps_measured:.1f}")
        self.pos += 1
        if self.pos >= len(self.seq):
            self.pos = 0
            self.passes += 1

        _, fps, _ = self._settings()
        delay = max(1, int(1000 / fps - (time.perf_counter() - t0) * 1000))
        self.job = self.root.after(delay, self.tick)

    def show_qr(self, pkt: bytes):
        ecc = self.ecc_var.get()
        # fixed mask: skipping segno's 8-mask search makes rendering ~4x faster (payload is random-ish anyway)
        try:
            qr = segno.make(pkt, error=ecc, mode="byte", version=self.version, mask=0, boost_error=False)
        except segno.DataOverflowError:
            qr = segno.make(pkt, error=ecc, mode="byte", mask=0, boost_error=False)
        buf = io.BytesIO()
        qr.save(buf, kind="png", scale=1, border=4)
        img = tk.PhotoImage(data=base64.b64encode(buf.getvalue()))
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        scale = max(1, (min(w, h) - 30) // img.width())
        if scale > 1:
            img = img.zoom(scale)
        self.photo = img  # keep reference
        self.canvas.delete("all")
        self.canvas.create_image(w // 2, h // 2, image=img)
        if self.fullscreen and self.encoder:
            self.canvas.create_text(10, h - 10, anchor="sw", fill="#888", font=("Consolas", 11),
                                    text=f"{self.pos + 1}/{len(self.seq)}  pass {self.passes}  "
                                         f"SHA {short_hash(self.encoder.sha256)}")


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)  # crisp QR pixels on scaled displays
    except Exception:
        pass
    root = tk.Tk()
    SenderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
