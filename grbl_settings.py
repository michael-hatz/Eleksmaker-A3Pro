#!/usr/bin/env python3
"""GRBL-Einstellungen ($$) sichern, wiederherstellen und vergleichen.

  python3 grbl_settings.py backup  meine_settings.txt
  python3 grbl_settings.py restore grbl_settings_a3pro.txt
  python3 grbl_settings.py diff    grbl_settings_a3pro.txt

Option: --port /dev/ttyUSB1  (Standard: /dev/ttyUSB0)
"""
import argparse
import re
import sys
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


def read_settings(s):
    s.write(b"$$\n")
    lines = []
    while True:
        line = s.readline().decode(errors="replace").strip()
        if not line:
            raise RuntimeError("Timeout beim Lesen von $$")
        if line == "ok":
            break
        if line.startswith("$"):
            lines.append(line.split(" ")[0])  # "$100=80.000 (x, step/mm)" -> "$100=80.000"
    return lines


def load_file(path):
    with open(path) as f:
        return [l.strip() for l in f if l.strip().startswith("$")]


def diff(current, wanted):
    cur = dict(l.split("=", 1) for l in current)
    differences = []
    for l in wanted:
        key, val = l.split("=", 1)
        if key not in cur:
            differences.append(f"{key}: fehlt auf dem Gerät (Datei: {val})")
        elif float(cur[key]) != float(val):
            differences.append(f"{key}: Gerät={cur[key]}  Datei={val}")
    return differences


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["backup", "restore", "diff"])
    ap.add_argument("file")
    ap.add_argument("--port", default="/dev/ttyUSB0")
    args = ap.parse_args()

    s = connect(args.port)
    try:
        if args.action == "backup":
            settings = read_settings(s)
            with open(args.file, "w") as f:
                f.write("\n".join(settings) + "\n")
            print(f"{len(settings)} Einstellungen nach {args.file} gesichert.")
        elif args.action == "restore":
            for line in load_file(args.file):
                s.write((line + "\n").encode())
                reply = s.readline().decode(errors="replace").strip()
                if reply != "ok":
                    print(f"{line} -> {reply}", file=sys.stderr)
                time.sleep(0.05)  # EEPROM-Schreibzugriff abwarten
            rest = diff(read_settings(s), load_file(args.file))
            print("Alle Einstellungen übernommen." if not rest else "Abweichungen:\n  " + "\n  ".join(rest))
            sys.exit(1 if rest else 0)
        else:
            rest = diff(read_settings(s), load_file(args.file))
            print("Gerät stimmt mit Datei überein." if not rest else "\n".join(rest))
            sys.exit(1 if rest else 0)
    finally:
        s.close()


if __name__ == "__main__":
    main()
