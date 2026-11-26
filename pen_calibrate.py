#!/usr/bin/env python3
"""Interaktiv Servo-Werte für Stift oben/unten finden (grbl-servo, M3 S0..S90)."""
import re
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"


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


def send(s, cmd):
    s.write((cmd + "\n").encode())
    return s.readline().decode(errors="replace").strip()


def main():
    s = connect(PORT)
    value = 45
    print("Zahl 0–90 + Enter: Servo auf diesen Wert")
    print("'+' / '-' + Enter: S um 5 ändern (≈ 1 Servo-Stellung), '++' / '--': S um 1")
    print("Hinweis: S0..90 ergibt nur 18 Stellungen – S-Änderungen < 5 bewegen oft nichts.")
    print("'q' + Enter: beenden (M5)")
    print(f"M3 S{value} -> {send(s, f'M3 S{value}')}")
    try:
        while True:
            inp = input(f"[S{value}] > ").strip()
            if inp == "q":
                break
            step = {"+": 5, "-": -5, "++": 1, "--": -1}.get(inp)
            if step is not None:
                value += step
            elif inp.isdigit():
                value = int(inp)
            else:
                print("?")
                continue
            value = max(0, min(90, value))
            print(f"M3 S{value} -> {send(s, f'M3 S{value}')}")
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        print(f"M5 -> {send(s, 'M5')}")
        s.close()


if __name__ == "__main__":
    main()
