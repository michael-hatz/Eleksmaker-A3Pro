"""Simulierter GRBL-0.9i-Controller für Tests (ohne Hardware).

Bildet nach: 128-Byte-Empfangspuffer (Überlauf wird registriert und Bytes
verworfen wie bei echter Hardware), 18-Block-Planer, der Zeilen langsam
abarbeitet, `?`-Status, `!`/`~`/Strg-X, Banner nach Reset. Fehler lassen
sich gezielt einbauen.
"""
import re
import threading
import time


class FakeGrbl:
    RX = 127

    def __init__(self, block_time=0.002, banner=True, corrupt=(), drop_ok=(), planner=18):
        self.lock = threading.Lock()
        self.rx = bytearray()
        self.tx = bytearray()
        self.overflow = 0
        self.block_time = block_time
        self.planner = []
        self.planner_size = planner
        self.received = []          # vollständig empfangene Zeilen (ohne Leerzeilen)
        self.executed = []
        self.corrupt = set(corrupt)  # Nummern (0-basiert, nur G-Zeilen) -> einmal "error"
        self.drop_ok = set(drop_ok)  # Nummern -> "ok" geht verloren
        self.gcount = 0
        self.state = "Idle"
        self.hold = False
        self.pos = [0.0, 0.0]
        self.last_exec = time.monotonic()
        self.closed = False
        self.line = bytearray()
        if banner:
            self.tx += b"\r\nGrbl 0.9i ['$' for help]\r\n"

    # --- pyserial-Schnittstelle
    @property
    def in_waiting(self):
        self._tick()
        return len(self.tx)

    def write(self, data):
        with self.lock:
            for b in data:
                c = bytes([b])
                if c == b"?":
                    st = "Hold" if self.hold else ("Run" if self.planner else "Idle")
                    self.tx += f"<{st},MPos:{self.pos[0]:.3f},{self.pos[1]:.3f},0.000,WPos:0,0,0>\r\n".encode()
                elif c == b"!":
                    self.hold = True
                elif c == b"~":
                    self.hold = False
                elif c == b"\x18":
                    self.rx.clear()
                    self.planner.clear()
                    self.line.clear()
                    self.hold = False
                    self.tx += b"\r\nGrbl 0.9i ['$' for help]\r\n"
                elif len(self.rx) >= self.RX:
                    self.overflow += 1     # Byte geht verloren
                else:
                    self.rx += c
        return len(data)

    def read(self, n=1):
        self._tick()
        time.sleep(0.0005)
        with self.lock:
            out = bytes(self.tx[:n])
            del self.tx[:n]
        return out

    def close(self):
        self.closed = True

    # --- Simulation
    def _tick(self):
        with self.lock:
            now = time.monotonic()
            # Planer abarbeiten
            while self.planner and not self.hold and now - self.last_exec >= self.block_time:
                self.last_exec += self.block_time
                done = self.planner.pop(0)
                self.executed.append(done)
                m = re.search(r"X([-\d.]+)Y([-\d.]+)", done)
                if m:
                    self.pos = [float(m.group(1)), float(m.group(2))]
            if not self.planner:
                self.last_exec = now
            # Zeilen aus dem Empfangspuffer holen, solange der Planer Platz hat
            while b"\n" in self.rx and len(self.planner) < self.planner_size:
                i = self.rx.index(b"\n")
                line = self.rx[:i].decode().strip()
                del self.rx[:i + 1]
                if not line:
                    self.tx += b"ok\r\n"
                    continue
                self.received.append(line)
                if line == "$$":
                    self.tx += b"$110=500.000 (x max rate, mm/min)\r\n$120=100.000 (x accel, mm/sec^2)\r\nok\r\n"
                    continue
                k = self.gcount
                self.gcount += 1
                if k in self.corrupt:
                    self.corrupt.discard(k)
                    self.tx += b"error:Invalid gcode ID:24\r\n"
                    continue
                if line.startswith(("M3", "M5", "G4")):
                    # Synchronisiert: wartet, bis der Planer leer ist (vereinfacht: sofort ausführen)
                    self.executed.extend(self.planner)
                    self.planner.clear()
                    self.executed.append(line)
                else:
                    self.planner.append(line)
                if k in self.drop_ok:
                    continue
                self.tx += b"ok\r\n"
