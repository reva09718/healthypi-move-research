"""Run: python pc_tools/research_app.py -- S2/S3/S4 laptop research recorder."""
from collections import deque
from datetime import datetime, timezone
import math
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

import hpi_ble as H
from research_capture import Capture
from research_storage import SessionStore

try:  # optional AWS storage; the recorder works without it
    import cloud_storage as CLOUD
except ImportError:
    CLOUD = None

ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "data" / "research_sessions.db"
LABELS = {key: sig.label for key, sig in H.WAVEFORMS_BY_KEY.items()}
LABELS.update(hr="Heart rate (bpm)", temp="Skin temperature (C)",
              spo2="SpO2 (%)", battery="Battery (%)")


class ResearchApp:
    def __init__(self, root):
        self.root = root
        self.capture = None
        self.closing = False
        self.session_id = None
        self.uploader = None
        self.cloud_msgs = queue.SimpleQueue()
        self.buffers = {key: deque(maxlen=6000) for key in LABELS}
        self.latest = {}
        self.last_received = {}
        self.segments = {}
        self.root.title("HealthyPi Move - Research recorder")
        self.root.geometry("1180x850")
        self.root.minsize(900, 720)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.status = tk.StringVar(value="Ready. Disconnect the phone app before connecting.")
        self.mode = tk.BooleanVar(value=False)
        self.duration = tk.StringVar(value="30")
        self.note = tk.StringVar()
        self.selected_plot = tk.StringVar(value="ppg_wrist")
        self.count_text = tk.StringVar(value="No samples recorded")
        self.vars = {key: tk.BooleanVar(value=key in ("ppg_wrist", "hr", "temp", "spo2", "battery"))
                     for key in LABELS}
        self.metrics = {key: tk.StringVar(value="--") for key in LABELS}

        body = ttk.Frame(root, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="HealthyPi Move research recording", font=("Segoe UI", 19, "bold")).pack(anchor="w")
        ttk.Label(body, text="Firmware 3.1.0: buttons control laptop saving. Start sensor measurements on the watch.",
                  wraplength=1050).pack(anchor="w", pady=(3, 10))
        settings = ttk.LabelFrame(body, text="Signals and session", padding=10)
        settings.pack(fill="x")
        self.checks = []
        for i, (key, label) in enumerate(LABELS.items()):
            check = ttk.Checkbutton(settings, text=label, variable=self.vars[key])
            check.grid(row=i // 4, column=i % 4, sticky="w", padx=8, pady=3)
            self.checks.append(check)
        ttk.Label(settings, text="Raw accelerometer/gyro are unavailable on this live interface.").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=5)
        options = ttk.Frame(settings)
        options.grid(row=3, column=0, columnspan=4, sticky="ew")
        ttk.Label(options, text="Duration (seconds; 0 = until Stop):").pack(side="left")
        self.duration_entry = ttk.Entry(options, textvariable=self.duration, width=8)
        self.duration_entry.pack(side="left", padx=6)
        self.demo_check = ttk.Checkbutton(options, text="DEMO - synthetic data", variable=self.mode)
        self.demo_check.pack(side="left", padx=14)
        self.auto_cloud = tk.BooleanVar(value=False)
        self.cloud_check = ttk.Checkbutton(options, text="Upload to AWS while recording (hybrid)",
                                           variable=self.auto_cloud,
                                           state="normal" if CLOUD else "disabled")
        self.cloud_check.pack(side="left", padx=14)
        ttk.Label(options, text="Note:").pack(side="left")
        self.note_entry = ttk.Entry(options, textvariable=self.note, width=28)
        self.note_entry.pack(side="left", padx=6)

        controls = ttk.Frame(body)
        controls.pack(fill="x", pady=10)
        self.start_button = ttk.Button(controls, text="Start recording on laptop", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Stop and save", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Label(controls, textvariable=self.status, wraplength=700).pack(side="left", padx=10)

        self.tabs = ttk.Notebook(body)
        self.tabs.pack(fill="both", expand=True)
        live = ttk.Frame(self.tabs, padding=8)
        library = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(live, text="Live signals")
        self.tabs.add(library, text="Saved sessions")
        readings = ttk.Frame(live)
        readings.pack(fill="x")
        for i, key in enumerate(LABELS):
            frame = ttk.Frame(readings, padding=4)
            frame.grid(row=i // 4, column=i % 4, sticky="w", padx=10)
            ttk.Label(frame, text=LABELS[key]).pack(anchor="w")
            ttk.Label(frame, textvariable=self.metrics[key], font=("Segoe UI", 12, "bold")).pack(anchor="w")
        plotbar = ttk.Frame(live)
        plotbar.pack(fill="x", pady=5)
        ttk.Label(plotbar, text="Plot signal:").pack(side="left")
        self.plot_choice = ttk.Combobox(plotbar, textvariable=self.selected_plot,
                                       values=list(LABELS), state="readonly", width=16)
        self.plot_choice.pack(side="left", padx=8)
        ttk.Label(plotbar, text="Latest 12 seconds. Raw values; no diagnostic interpretation.").pack(side="left")
        self.figure = Figure(figsize=(10, 3.3), dpi=100, layout="constrained")
        self.ax = self.figure.add_subplot(111)
        self.line, = self.ax.plot([], [], color="#087f8c", linewidth=1)
        self.canvas = FigureCanvasTkAgg(self.figure, master=live)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        ttk.Label(live, textvariable=self.count_text, wraplength=1050).pack(anchor="w")
        ttk.Label(live, text="Timing: laptop packet arrival + estimated nominal sample spacing; not device timestamps.",
                  wraplength=1050).pack(anchor="w", pady=4)

        ttk.Label(library, text="Sample values and raw packets are encrypted using your Windows account (DPAPI).\n"
                  "Session metadata remains readable. Explicit CSV exports are plaintext.\n"
                  "AWS upload is off unless you tick the hybrid option or press Upload; the local copy is always kept.").pack(anchor="w", pady=8)
        self.sessions = ttk.Treeview(library, columns=("time", "status", "source"), show="headings", height=10)
        for key, label in [("time", "Started (local time)"), ("status", "Ending"), ("source", "Source")]:
            self.sessions.heading(key, text=label)
            self.sessions.column(key, width=220)
        self.sessions.pack(fill="both", expand=True)
        buttons = ttk.Frame(library)
        buttons.pack(fill="x", pady=10)
        self.refresh_button = ttk.Button(buttons, text="Refresh sessions", command=self.refresh)
        self.refresh_button.pack(side="left")
        self.export_button = ttk.Button(buttons, text="Export selected session as plaintext CSV", command=self.export)
        self.export_button.pack(side="left", padx=8)
        self.upload_button = ttk.Button(buttons, text="Upload selected session to AWS", command=self.upload_selected,
                                        state="normal" if CLOUD else "disabled")
        self.upload_button.pack(side="left", padx=8)
        self.verify_button = ttk.Button(buttons, text="Verify selected in AWS", command=self.verify_selected,
                                        state="normal" if CLOUD else "disabled")
        self.verify_button.pack(side="left")
        ttk.Label(library, text=str(DATABASE), wraplength=1000).pack(anchor="w")

        self.messages = tk.Text(body, height=4, state="disabled", wrap="word")
        self.messages.pack(fill="x", pady=(8, 0))
        self.log("Nothing is uploaded unless AWS upload is requested. Existing recordings.db is left unchanged.")
        self.refresh()
        root.after(200, self.poll)

    def log(self, message):
        self.messages.configure(state="normal")
        self.messages.insert("end", datetime.now().strftime("%H:%M:%S ") + message + "\n")
        # Keep UI history bounded; all readings are stored independently.
        if int(self.messages.index("end-1c").split(".")[0]) > 100:
            self.messages.delete("1.0", "20.0")
        self.messages.see("end")
        self.messages.configure(state="disabled")

    def busy(self, active):
        state = "disabled" if active else "normal"
        for widget in self.checks + [self.start_button, self.duration_entry, self.note_entry,
                                     self.demo_check, self.cloud_check, self.export_button,
                                     self.refresh_button]:
            widget.configure(state=state)
        self.stop_button.configure(state="normal" if active else "disabled")

    def start(self):
        try:
            seconds = float(self.duration.get())
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError("Duration must be zero or a positive number.")
            selected = [key for key in LABELS if self.vars[key].get()]
            if not selected:
                raise ValueError("Select at least one signal.")
            self.capture = Capture(DATABASE, selected, seconds, self.note.get(), self.mode.get())
            self.session_id = None
            self.latest.clear()
            self.last_received.clear()
            self.segments.clear()
            for key in LABELS:
                self.buffers[key].clear()
                self.metrics[key].set("--")
            self.busy(True)
            self.status.set("DEMO starting..." if self.mode.get() else "Connecting...")
            self.capture.start()
            self.log("Start requested. " + ("SYNTHETIC DEMO." if self.mode.get() else
                     "Sensors must be started on the watch; selected channels may not operate simultaneously."))
        except Exception as exc:
            self.capture = None
            self.busy(False)
            messagebox.showerror("Cannot start", str(exc))

    def stop(self):
        if self.capture:
            self.capture.stop()
            self.stop_button.configure(state="disabled")
            self.status.set("Stopping and saving; connection setup may take a few seconds...")

    def refresh(self):
        try:
            store = SessionStore(DATABASE)
            try:
                rows = store.sessions()
            finally:
                store.close()
            self.sessions.delete(*self.sessions.get_children())
            for sid, started, status, source in rows:
                self.sessions.insert("", "end", iid=sid, values=(
                    datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S"), status, source))
            if self.session_id and self.sessions.exists(self.session_id):
                self.sessions.selection_set(self.session_id)
        except Exception as exc:
            self.log("Storage: " + str(exc))

    def export(self):
        chosen = self.sessions.selection()
        if not chosen:
            messagebox.showinfo("Select a session", "Select a saved session first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=chosen[0] + ".csv",
                                          filetypes=[("Plaintext CSV", "*.csv")])
        if not path:
            return
        try:
            store = SessionStore(DATABASE)
            try:
                store.export_csv(chosen[0], path)
                event_path = str(Path(path).with_name(Path(path).stem + "_events.csv"))
                store.export_events_csv(chosen[0], event_path)
            finally:
                store.close()
            self.log("CSV exported: " + path + "; connection events: " + event_path)
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))

    # ---- AWS (optional). Network work runs in daemon threads; the local database stays the master copy.
    def cloud_sync_factory(self):
        if CLOUD is None:
            raise RuntimeError("Install cloud dependencies: python -m pip install -r pc_tools/requirements-cloud.txt")
        cfg = CLOUD.CloudConfig.load()  # raises CloudConfigError with a readable message
        from research_storage import WindowsCipher
        return cfg, lambda: CLOUD.CloudSync.from_config(cfg, DATABASE, WindowsCipher())

    def start_hybrid(self, sid):
        try:
            _, make = self.cloud_sync_factory()
        except Exception as exc:
            self.log("AWS upload not started: %s. Recording continues locally." % exc)
            return
        self.uploader = CLOUD.HybridUploader(make, sid, notify=self.cloud_msgs.put).start()
        self.log("Hybrid mode: saved locally first, uploaded to AWS in the background.")

    def _cloud_job(self, label, job):
        def run():
            try:
                self.cloud_msgs.put(job())
            except Exception as exc:
                self.cloud_msgs.put("%s failed: %s" % (label, exc))
        threading.Thread(target=run, name="hpi-cloud-" + label, daemon=True).start()

    def upload_selected(self):
        chosen = self.sessions.selection()
        if not chosen:
            messagebox.showinfo("Select a session", "Select a saved session first.")
            return
        sid = chosen[0]
        try:
            _, make = self.cloud_sync_factory()
        except Exception as exc:
            messagebox.showerror("AWS not configured", str(exc))
            return
        final = not (self.capture and sid == self.session_id)  # also finalise sessions left 'recording' by a crash

        def job():
            sync = make()
            try:
                r = sync.sync_session(sid, final=final)
            finally:
                sync.close()
            return ("AWS upload complete: " + sid) if r["complete"] else \
                   ("AWS upload incomplete (%s); press Upload again to resume." % (r["error"] or "session still recording"))
        self.log("Uploading " + sid + " ...")
        self._cloud_job("upload", job)

    def verify_selected(self):
        chosen = self.sessions.selection()
        if not chosen:
            messagebox.showinfo("Select a session", "Select a saved session first.")
            return
        sid = chosen[0]
        try:
            cfg, _ = self.cloud_sync_factory()
        except Exception as exc:
            messagebox.showerror("AWS not configured", str(exc))
            return

        def job():
            report = CLOUD.CloudStore(cfg).verify_session(sid)
            return ("AWS verify OK: %d packets in %d chunks" % (report["packets"], report["chunks"])) if report["ok"] \
                   else "AWS verify FAILED: " + "; ".join(report["problems"])
        self.log("Verifying " + sid + " in AWS (downloads and decrypts it) ...")
        self._cloud_job("verify", job)

    def update_packet(self, packet):
        key = packet["signal"]
        self.last_received[key] = packet["recv_utc"]
        segment = packet.get("connection_segment", 0)
        if key in self.segments and self.segments[key] != segment:
            self.buffers[key].append((packet["estimated_first_s"], float("nan")))
        self.segments[key] = segment
        rate = packet["nominal_hz"]
        values = packet["values"]
        for i, value in enumerate(values):
            y = value.get("spo2") if isinstance(value, dict) else value
            if not isinstance(y, (int, float)) or not math.isfinite(y):
                continue
            t = packet["estimated_first_s"] + (i / rate if rate else 0)
            self.buffers[key].append((t, y))
        latest = values[-1]
        if isinstance(latest, dict):
            latest = latest.get("spo2")
        self.metrics[key].set("--" if latest is None else
                              ("%.2f" % latest if isinstance(latest, float) else str(latest)))

    def poll(self):
        cap = self.capture
        if cap:
            # Drain bounded plot queue; no BLE or SQLite work on Tk's UI thread.
            for _ in range(1000):
                try:
                    self.update_packet(cap.plot_packets.get_nowait())
                except queue.Empty:
                    break
            while True:
                try:
                    kind, value = cap.events.get_nowait()
                except queue.Empty:
                    break
                if kind == "session":
                    self.session_id = value
                    self.log("Session " + value)
                    if self.auto_cloud.get():
                        self.start_hybrid(value)
                elif kind == "status":
                    self.status.set(value)
                elif kind == "notice":
                    self.log(value)
                elif kind == "counts":
                    self.count_text.set("Samples received: " + ", ".join(
                        "%s: %d" % (key, value.get(key, 0)) for key in cap.selected))
                elif kind == "error":
                    self.status.set("Capture failed; inspect message below.")
                    self.log("ERROR: " + value)
                elif kind == "saved":
                    self.log("Saved (%s): %s. Invalid packets: %s" %
                             (value["status"], value["counts"], value["malformed"]))
                    absent = [key for key in cap.selected if not value["counts"].get(key)]
                    if absent:
                        self.log("No readings received for: " + ", ".join(absent))
                    self.count_text.set("Saved samples: " + str(value["counts"]))
                    if self.uploader:
                        self.uploader.finish()  # background thread uploads the rest; local copy is already safe
                        self.uploader = None
                        self.log("Finishing AWS upload in the background...")
                    self.status.set("Saved: " + value["status"] +
                                    (" (contains interruptions)" if "gaps" in value["status"] else
                                     " (partial recording)" if value["status"] in ("disconnected", "error") else ""))
                elif kind == "done":
                    self.busy(False)
                    self.refresh()
                    # Thread can still be returning; wait for actual termination.
            if cap.thread is not None and not cap.thread.is_alive():
                self.capture = None
        while True:
            try:
                self.log(self.cloud_msgs.get_nowait())
            except queue.Empty:
                break
        self.redraw()
        if self.closing and self.capture is None:
            self.root.destroy()
            return
        self.root.after(200, self.poll)

    def redraw(self):
        key = self.selected_plot.get()
        data = list(self.buffers[key])
        if data:
            end = data[-1][0]
            data = [(t, y) for t, y in data if t >= end - 12]
            # Avoid visually connecting across a long gap in received data.
            xs, ys = [], []
            for t, y in data:
                if xs and t - xs[-1] > (0.5 if key in H.WAVEFORMS_BY_KEY else 40):
                    xs.append(float("nan")); ys.append(float("nan"))
                xs.append(t); ys.append(y)
            self.line.set_data(xs, ys)
            self.ax.set_xlim(max(-1, end - 12), max(1, end + 0.1))
            finite = [y for t, y in data if math.isfinite(y)]
            low, high = (min(finite), max(finite)) if finite else (0, 1)
            pad = max((high - low) * .1, 1)
            self.ax.set_ylim(low - pad, high + pad)
        else:
            self.line.set_data([], [])
            self.ax.set_xlim(0, 12)
            self.ax.set_ylim(0, 1)
        import time
        age = (" / last packet %.1f s ago" % max(0, time.time() - self.last_received[key])
               if key in self.last_received else " / waiting for readings")
        self.ax.set_title(("DEMO / " if self.mode.get() else "") + LABELS[key] + age)
        self.ax.set_xlabel("Estimated seconds since capture start (host timing)")
        self.ax.set_ylabel("Raw sensor counts" if key in H.WAVEFORMS_BY_KEY else LABELS[key])
        self.ax.grid(True, alpha=.2)
        self.canvas.draw_idle()

    def close(self):
        self.closing = True
        if self.capture:
            self.stop()
        else:
            self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    ResearchApp(root)
    root.mainloop()
