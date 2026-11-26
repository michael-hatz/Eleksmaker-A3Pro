#!/usr/bin/env python3
"""Funktionstest für den EleksMaker A3 Pro als Pen-Plotter (grbl-servo).

  python3 machine_test.py              # Servo-Test + X/Y-Test
  python3 machine_test.py --servo      # nur Servo
  python3 machine_test.py --xy         # nur Achsen (10 mm hin und zurück)
  python3 machine_test.py --up 25 --down 30   # Stift-Werte für den Hub-Test

Der Servo-Test fährt S0..S90 in Stufen ab und danach fünfmal Stift hoch/runter.
Der X/Y-Test fährt relativ 10 mm in +X, zurück, 10 mm in +Y, zurück – der
Stift ist dabei oben. Vorher: 12-V-Netzteil einschalten, Stift über freier Fläche.
"""
import argparse
import re
import time

import serial


def connect(port):
    """Öffnet den Port und prüft per $I, dass GRBL antwortet.

    Beim Öffnen startet der Nano meist neu (DTR) und schickt ein Banner.
    Hat der Port HUPCL nicht gesetzt (z. B. nach avrdude), gibt es keinen
    Neustart – dann gilt der vorherige Maschinenzustand weiter.
    """
    s = serial.Serial(port, 115200, timeout=0.5)
    banner, deadline = "", time.time() + 3
    while "Grbl" not in banner and time.time() < deadline:
        banner += s.read(64).decode(errors="replace")
    s.timeout = 2
    s.reset_input_buffer()
    s.write(b"$I\n")
    reply = ""
    while not reply.endswith("ok"):
        line = s.readline().decode(errors="replace").strip()
        if not line:
            raise RuntimeError(f"GRBL antwortet nicht auf {port}")
        reply += line
    version = re.search(r"\[([^:\]]*)", reply)
    print(f"GRBL {version.group(1) if version else '?'} auf {port}"
          + ("" if "Grbl" in banner else " (ohne Neustart)"))
    return s


class Grbl:
    def __init__(self, port):
        self.s = connect(port)

    def send(self, cmd):
        self.s.write((cmd + "\n").encode())
        reply = self.s.readline().decode(errors="replace").strip()
        print(f"{time.strftime('%H:%M:%S')}  {cmd:<16} -> {reply}")
        if reply.startswith(("error", "ALARM")):
            raise RuntimeError(reply)
        return reply

    def wait_idle(self):
        while True:
            self.s.write(b"?")
            time.sleep(0.2)
            status = self.s.read(self.s.in_waiting or 1).decode(errors="replace")
            if "Idle" in status:
                return status.strip()
            if "Alarm" in status:
                raise RuntimeError(status.strip())

    def close(self):
        try:
            self.send("M5")
        finally:
            self.s.close()


def servo_test(g, up, down):
    print("\n--- Servo: S0 .. S90 in 15er-Schritten (je 2 s) ---")
    for v in range(0, 91, 15):
        g.send(f"M3 S{v}")
        time.sleep(2)
    print(f"\n--- Servo: 5x Stift hoch (S{up}) / runter (S{down}) ---")
    for _ in range(5):
        g.send(f"M3 S{down}")
        time.sleep(0.6)
        g.send(f"M3 S{up}")
        time.sleep(0.6)


def xy_test(g, up, dist, feed):
    print(f"\n--- X/Y: {dist} mm hin und zurück, F{feed} ---")
    g.send(f"M3 S{up}")
    g.send("G21")  # mm
    g.send("G91")  # relativ
    try:
        for move in (f"X{dist}", f"X-{dist}", f"Y{dist}", f"Y-{dist}"):
            g.send(f"G1 {move} F{feed}")
            print("   ", g.wait_idle())
    finally:
        g.send("G90")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--servo", action="store_true", help="nur Servo testen")
    ap.add_argument("--xy", action="store_true", help="nur Achsen testen")
    ap.add_argument("--up", type=int, default=25, help="S-Wert Stift oben (Standard 25)")
    ap.add_argument("--down", type=int, default=30, help="S-Wert Stift unten (Standard 30)")
    ap.add_argument("--dist", type=float, default=10, help="Fahrweg in mm (Standard 10)")
    ap.add_argument("--feed", type=int, default=500, help="Vorschub mm/min (Standard 500)")
    args = ap.parse_args()
    both = not args.servo and not args.xy

    g = Grbl(args.port)
    try:
        if args.servo or both:
            servo_test(g, args.up, args.down)
        if args.xy or both:
            xy_test(g, args.up, args.dist, args.feed)
    finally:
        g.close()


if __name__ == "__main__":
    main()
