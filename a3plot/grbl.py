"""Zuverlässiges Streamen zu GRBL 0.9i (grbl-servo).

Warum verschluckt GRBL Befehle?
-------------------------------
GRBL hat einen Empfangspuffer von 128 Byte (127 nutzbar). Schickt der Rechner
mehr unbestätigte Zeichen, als hineinpassen, gehen Bytes verloren – ohne
Fehlermeldung. Typische Ursachen: falsches Mitzählen beim
„Character Counting“, Befehle, bevor GRBL nach dem Reset bereit ist, oder
gestörte Übertragung.

Gegenmaßnahmen hier:
* Nach dem Öffnen auf das Banner warten (der Nano startet neu).
* Modus "safe" (Standard): immer nur **eine** Zeile unterwegs. Jede Zeile
  wird erst nach `ok` weitergeschickt. Der GRBL-Planer (18 Blöcke) bleibt
  trotzdem voll, weil `ok` sofort kommt, solange im Planer Platz ist.
* Modus "count": Zeichen zählen, aber nur bis `budget` Byte (Standard 64 von
  127) – also reichlich Reserve.
* Bei `error:` wird die Zeile (im Modus "safe") automatisch erneut gesendet.
  Alle Plot-Koordinaten sind absolut (G90), Wiederholen ist also harmlos.
* Watchdog: steht die Maschine still (Idle), aber eine Zeile ist seit
  Sekunden unbestätigt, gilt sie als verloren und wird erneut gesendet.
* Ein unerwartetes Banner während des Plots (Spannungseinbruch → Reset)
  bricht den Plot ab, statt blind weiterzusenden.
"""
from __future__ import annotations

import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass

RX_BUFFER = 127
STATUS_RE = re.compile(r"<(\w+)(?:,MPos:([-\d.]+),([-\d.]+),([-\d.]+))?(?:,WPos:([-\d.]+),([-\d.]+),([-\d.]+))?")


@dataclass
class Inflight:
    text: str
    nbytes: int
    src: str            # "job" | "cmd"
    idx: int            # Index im Job (oder -1)
    sent_at: float


class GrblStreamer:
    """Besitzt den seriellen Port in einem eigenen Thread.

    `on_event(name, data)` wird aus dem Worker-Thread aufgerufen. Ereignisse:
      log (kind, text)        kind: tx, rx, info, warn, error
      connected (version)     disconnected (reason)
      status (state, x, y)    setting (key, value)
      job_acked (idx)         job_sent (idx)
      job_state (state)       running, soft_paused, holding, finished, aborted, failed
      job_problem (text)      repeats (n)
      alarm (text)
    """

    def __init__(self, port_factory, on_event, mode="safe", budget=64,
                 poll_interval=0.25, idle_timeout=3.0, banner_timeout=3.0, max_retries=3):
        self.port_factory = port_factory
        self.on_event = on_event
        self.mode = mode
        self.budget = max(16, min(int(budget), RX_BUFFER))
        self.poll_interval = poll_interval
        self.idle_timeout = idle_timeout
        self.banner_timeout = banner_timeout
        self.max_retries = max_retries
        self.requests = queue.Queue()
        self.thread = None
        self.running = False
        # Zustand (nur im Worker-Thread verändert)
        self.port = None
        self.inflight: deque[Inflight] = deque()
        self.manual = deque()
        self.job = None              # Liste von Zeilen
        self.job_next = 0
        self.job_active = False
        self.job_gate = False        # Senden angehalten (weiche Pause / Fehler)
        self.pause_points = frozenset()
        self.pause_requested = False
        self.resume_prefix = []
        self.retries = {}
        self.repeats = 0
        self.state = "Unknown"
        self.mpos = (0.0, 0.0)
        self.last_status_at = 0.0
        self.last_rx_at = 0.0
        self.aborting = None         # None oder dict mit Phase
        self.rxbuf = b""
        self.version = ""
        self.held = False            # wir haben "!" geschickt

    # ------------------------------------------------------------------ API
    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, name="grbl", daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)

    def command(self, line):
        self.requests.put(("cmd", line))

    def realtime(self, ch: bytes):
        self.requests.put(("rt", ch))

    def start_job(self, lines, pause_points=()):
        self.requests.put(("job", (list(lines), frozenset(pause_points))))

    def pause_soft(self):
        """Nach dem aktuellen Strich anhalten (Stift oben)."""
        self.requests.put(("pause", None))

    def hold(self):
        """Sofort anhalten (Feed Hold). Der Stift bleibt, wo er ist."""
        self.requests.put(("hold", None))

    def resume(self, prefix=()):
        self.requests.put(("resume", list(prefix)))

    def abort(self):
        self.requests.put(("abort", None))

    def skip_failed(self):
        """Nach einem Fehler im Zählmodus: fehlerhafte Zeile übergehen und weitermachen."""
        self.requests.put(("resume", []))

    # ------------------------------------------------------------- Worker
    def _emit(self, name, *data):
        try:
            self.on_event(name, data[0] if len(data) == 1 else data)
        except Exception:   # pragma: no cover – GUI-Fehler dürfen den Port nicht killen
            pass

    def _log(self, kind, text):
        self._emit("log", (kind, text))

    def _write(self, data: bytes):
        self.port.write(data)

    def _run(self):
        try:
            self.port = self.port_factory()
        except Exception as e:
            self._emit("disconnected", f"Port lässt sich nicht öffnen: {e}")
            self.running = False
            return
        try:
            if not self._handshake():
                self._emit("disconnected", "GRBL antwortet nicht.")
                return
            last_poll = 0.0
            while self.running:
                self._process_requests()
                now = time.monotonic()
                if now - last_poll >= self.poll_interval:
                    self._write(b"?")
                    last_poll = now
                self._fill()
                self._read()
                self._watchdog()
                self._abort_step()
        except Exception as e:
            self._emit("disconnected", f"Verbindung verloren: {e}")
            return
        finally:
            try:
                self.port.close()
            except Exception:
                pass
        self._emit("disconnected", "Getrennt.")

    def _handshake(self):
        deadline = time.monotonic() + self.banner_timeout
        banner = False
        while time.monotonic() < deadline and not banner:
            for line in self._read_lines():
                if line.startswith("Grbl"):
                    banner = True
                    self.version = line
                    self._log("rx", line)
        self.rxbuf = b""
        # Verbindungsprobe; ohne Neustart (HUPCL aus) ist das die einzige Antwort
        self._write(b"\n")
        ok = self._wait_ok(2.0)
        if not ok:
            return False
        if not banner:
            self._log("info", "Kein Banner – Controller wurde nicht neu gestartet, alter Zustand gilt weiter.")
        self._emit("connected", (self.version or "Grbl ?", banner))
        self.command("$$")
        self.command("$#")
        return True

    def _wait_ok(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in self._read_lines():
                if line == "ok":
                    return True
                if line.startswith("Grbl"):
                    self.version = line
        return False

    def _read_lines(self):
        n = getattr(self.port, "in_waiting", 0)
        data = self.port.read(n or 1)
        if data:
            self.rxbuf += data
        out = []
        while b"\n" in self.rxbuf:
            raw, self.rxbuf = self.rxbuf.split(b"\n", 1)
            line = raw.decode(errors="replace").strip()
            if line:
                out.append(line)
        return out

    # ------------------------------------------------------- Anfragen
    def _process_requests(self):
        while True:
            try:
                kind, arg = self.requests.get_nowait()
            except queue.Empty:
                return
            if kind == "cmd":
                self.manual.append(arg)
            elif kind == "rt":
                self._write(arg)
                self._log("tx", {b"!": "! (Halt)", b"~": "~ (Weiter)", b"\x18": "Strg-X (Reset)"}.get(arg, repr(arg)))
            elif kind == "job":
                lines, points = arg
                if self.job_active:
                    self._log("warn", "Es läuft bereits ein Plot.")
                    continue
                self.job, self.pause_points = lines, points
                self.job_next, self.job_active, self.job_gate = 0, True, False
                self.pause_requested = False
                self.retries, self.repeats = {}, 0
                self._emit("job_state", "running")
            elif kind == "pause":
                if self.job_active:
                    self.pause_requested = True
                    self._log("info", "Pause nach dem aktuellen Strich …")
            elif kind == "hold":
                self.held = True
                self._write(b"!")
                self._log("tx", "! (Halt)")
                if self.job_active:
                    self._emit("job_state", "holding")
            elif kind == "resume":
                if self.held or self.state == "Hold":
                    self.held = False
                    self._write(b"~")
                    self._log("tx", "~ (Weiter)")
                if self.job_active:
                    for l in reversed(arg):
                        self.manual.appendleft(l)
                    self.job_gate = False
                    self.pause_requested = False
                    self._emit("job_state", "running")
            elif kind == "abort":
                self._start_abort()

    # ------------------------------------------------------- Senden
    def _inflight_bytes(self):
        return sum(e.nbytes for e in self.inflight)

    def _can_send(self, nbytes):
        if self.aborting:
            return False
        if self.mode == "safe":
            return not self.inflight
        return self._inflight_bytes() + nbytes <= self.budget

    def _fill(self):
        while True:
            if self.manual:
                text, src, idx = self.manual[0], "cmd", -1
            elif self.job_active and not self.job_gate and self.job_next < len(self.job):
                idx = self.job_next
                text, src = self.job[idx], "job"
            else:
                return
            data = (text + "\n").encode("ascii", errors="replace")
            if len(data) > RX_BUFFER:
                self._log("error", f"Zeile zu lang, übersprungen: {text[:40]}…")
                if src == "cmd":
                    self.manual.popleft()
                else:
                    self.job_next += 1
                continue
            if not self._can_send(len(data)):
                return
            self._write(data)
            self.inflight.append(Inflight(text, len(data), src, idx, time.monotonic()))
            if src == "cmd":
                self.manual.popleft()
                self._log("tx", text)
            else:
                self.job_next += 1
                self._emit("job_sent", idx)
                if self.pause_requested and idx in self.pause_points:
                    self.job_gate = True
                    self.pause_requested = False
                    self._emit("job_state", "soft_paused")

    # ------------------------------------------------------- Empfangen
    def _read(self):
        for line in self._read_lines():
            self.last_rx_at = time.monotonic()
            self._handle(line)

    def _handle(self, line):
        if line == "ok":
            self._ack(None)
        elif line.startswith("error"):
            self._ack(line)
        elif line.startswith("<"):
            self._status(line)
        elif line.startswith("Grbl"):
            self._reset_seen(line)
        elif line.startswith("ALARM"):
            self._log("error", line)
            self._emit("alarm", line)
            if self.job_active and not self.aborting:
                self._fail_job(f"GRBL meldet {line}. Plot abgebrochen.")
        elif re.match(r"\[(G5[4-9]|G28|G30|G92|TLO|PRB):", line):
            self._offset(line)
        elif line.startswith("$") and "=" in line:
            m = re.match(r"\$(\d+)=([-\d.]+)", line)
            if m:
                self._emit("setting", (int(m.group(1)), float(m.group(2))))
        else:
            self._log("rx", line)

    def _ack(self, err):
        if not self.inflight:
            self._log("warn", f"Unerwartete Antwort: {err or 'ok'}")
            return
        e = self.inflight.popleft()
        if e.src == "cmd":
            if err:
                self._log("error", f"{e.text} -> {err}")
            elif e.text not in ("$$", "$#"):
                self._log("rx", f"{e.text} -> ok")
            self._check_finished()
            return
        if err is None:
            self._emit("job_acked", e.idx)
            self._check_finished()
            return
        # Fehler bei einer Plot-Zeile
        n = self.retries.get(e.idx, 0) + 1
        self.retries[e.idx] = n
        self._log("warn", f"Zeile {e.idx + 1} „{e.text}“ -> {err}")
        if self.mode == "safe" and n <= self.max_retries and not self.inflight:
            self.job_next = e.idx        # genau diese Zeile noch einmal
            self.repeats += 1
            self._emit("repeats", self.repeats)
            self._log("info", f"Zeile {e.idx + 1} wird erneut gesendet ({n}/{self.max_retries}).")
            return
        self._write(b"!")
        self.held = True
        self.job_gate = True
        self._emit("job_state", "holding")
        self._emit("job_problem", f"GRBL hat Zeile {e.idx + 1} („{e.text}“) abgelehnt: {err}.\n"
                                  "Fortsetzen überspringt die Zeile, Abbrechen beendet den Plot.")

    def _check_finished(self):
        if (self.job_active and self.job_next >= len(self.job) and not self.inflight
                and not any(True for _ in self.manual)):
            self.job_active = False
            self._emit("job_state", "finished")

    def _offset(self, line):
        m = re.match(r"\[(G54|G92):([-\d.]+),([-\d.]+)", line)
        if m and (abs(float(m.group(2))) > 1e-6 or abs(float(m.group(3))) > 1e-6):
            self._log("warn", f"{line}: Koordinaten-Versatz {m.group(1)} ist nicht null. Positionen "
                              "und G-Code passen dann nicht zusammen – in der Konsole "
                              + ("„G10 L2 P1 X0 Y0“" if m.group(1) == "G54" else "„G92.1“")
                              + " senden.")

    def _status(self, line):
        m = STATUS_RE.match(line)
        if not m:
            return
        self.state = m.group(1)
        # G-Code-Koordinaten sind Werkstückkoordinaten (WPos). Meldet GRBL nur MPos
        # ($10=1), gilt MPos = WPos – beim Verbinden wird geprüft, dass G54/G92 null sind.
        if m.group(5) is not None:
            self.mpos = (float(m.group(5)), float(m.group(6)))
        elif m.group(2) is not None:
            self.mpos = (float(m.group(2)), float(m.group(3)))
        self.last_status_at = time.monotonic()
        self._emit("status", (self.state, self.mpos[0], self.mpos[1]))

    def _reset_seen(self, line):
        self._log("rx", line)
        self.inflight.clear()
        self.held = False
        self.version = line
        if self.aborting:
            self.aborting["reset_seen"] = True
            return
        if self.job_active:
            self._fail_job("Der Controller hat unerwartet neu gestartet (Spannungseinbruch, "
                           "Servo zieht zu viel Strom?). Die Position ist verloren – Plot abgebrochen.")
        else:
            self._log("warn", "Controller wurde neu gestartet.")
        self._emit("controller_reset", None)

    def _fail_job(self, text):
        self.job_active = False
        self.job_gate = False
        self.manual.clear()
        self._emit("job_problem", text)
        self._emit("job_state", "failed")

    # ------------------------------------------------------- Watchdog
    def _watchdog(self):
        if not self.inflight or self.aborting:
            return
        now = time.monotonic()
        oldest = self.inflight[0]
        if (self.state == "Idle" and self.last_status_at > oldest.sent_at + 0.3
                and now - oldest.sent_at > self.idle_timeout
                and now - self.last_rx_at < 2 * self.poll_interval + 1.0  # GRBL lebt (Status kommt)
                ):
            lost = [e for e in self.inflight if e.text not in ("$$", "$#")]
            self.inflight.clear()
            if not lost:
                return
            self._log("warn", "Keine Bestätigung für: " + ", ".join(e.text for e in lost)
                      + " – wird erneut gesendet.")
            job_ids = [e.idx for e in lost if e.src == "job"]
            for e in reversed([e for e in lost if e.src == "cmd"]):
                self.manual.appendleft(e.text)
            if job_ids:
                self.job_next = min(job_ids)
                self.repeats += len(job_ids)
                self._emit("repeats", self.repeats)

    # ------------------------------------------------------- Abbrechen
    def _start_abort(self):
        self.manual.clear()
        was_job = self.job_active
        self.job_active = False
        self.job_gate = False
        self.pause_requested = False
        self.aborting = {"phase": "hold", "t": time.monotonic(), "last": None, "stable": 0,
                         "was_job": was_job, "reset_seen": False}
        self._write(b"!")
        self._log("tx", "! (Halt)")

    def _abort_step(self):
        a = self.aborting
        if not a:
            return
        now = time.monotonic()
        if a["phase"] == "hold":
            # warten, bis die Maschine wirklich steht (Hold abgeschlossen) – sonst
            # löst der Reset einen Alarm aus und die Position ist weg.
            if self.last_status_at > a["t"]:
                if a["last"] == self.mpos and self.state in ("Hold", "Idle"):
                    a["stable"] += 1
                else:
                    a["stable"] = 0
                a["last"] = self.mpos
                a["t"] = self.last_status_at
            if a["stable"] >= 2 or now - a.get("t0", now) > 10:
                self.inflight.clear()
                self._write(b"\x18")
                self._log("tx", "Strg-X (Reset)")
                a["phase"] = "reset"
                a["t_reset"] = now
            a.setdefault("t0", now)
        elif a["phase"] == "reset":
            if a["reset_seen"] or now - a["t_reset"] > 3:
                self.inflight.clear()
                self.rxbuf = b""
                self.aborting = None
                if a["was_job"]:
                    self._emit("job_state", "aborted")
                self._log("info", "Angehalten. Position bleibt erhalten.")
