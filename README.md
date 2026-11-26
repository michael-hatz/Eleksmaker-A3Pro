# EleksMaker A3 Pro als Pen-Plotter unter Linux

Diese Anleitung zeigt, wie man den Lasergravierer **EleksMaker A3 Pro** zum **Stiftplotter** umbaut. Statt des Lasers hebt und senkt ein kleiner RC-Servo den Stift. Gesteuert wird der Plotter von **Linux** aus, ganz ohne LaserGRBL, BenBox oder EleksCAM.

Dieses Repo dokumentiert, was an einem realen Gerät funktioniert hat:

- [wo der Servo angeschlossen wird](#1-servo-anschließen),
- [warum die Original-Firmware den Servo zittern lässt und welche Firmware das behebt](#2-firmware-warum-die-original-firmware-nicht-reicht),
- [wie man den Stift per G-Code ansteuert](#4-stift-ansteuern),
- [Python-Testprogramme](#5-die-python-programme) für Servo, Achsen und GRBL-Einstellungen,
- [A3 Plot](#6-a3-plot-svg-dateien-plotten), ein Programm mit Oberfläche zum Plotten von SVG-Dateien.

> Getestet mit: EleksMaker A3 Pro, Controller **EleksMana SE v3.2** (Arduino Nano/ATmega328P, USB-Seriell-Wandler CH340), SG90-Servo, Ubuntu-Linux, Python 3.

---

## Inhalt

1. [Servo anschließen](#1-servo-anschließen)
2. [Firmware: warum die Original-Firmware nicht reicht](#2-firmware-warum-die-original-firmware-nicht-reicht)
3. [Firmware flashen (grbl-servo)](#3-firmware-flashen-grbl-servo)
4. [Stift ansteuern](#4-stift-ansteuern)
5. [Die Python-Programme](#5-die-python-programme)
6. [A3 Plot: SVG-Dateien plotten](#6-a3-plot-svg-dateien-plotten)
7. [GRBL-Einstellungen des A3 Pro](#7-grbl-einstellungen-des-a3-pro)
8. [Fehlersuche](#8-fehlersuche)
9. [Quellen & Lizenz](#9-quellen--lizenz)

---

## 1. Servo anschließen

Die Mana SE hat einen dreipoligen Anschluss mit der Beschriftung **„Servo“**. Dort wird der Servo eingesteckt. **Nicht** an den Laser-Stecker anschließen!

| Pin auf der Platine | Funktion | Servo-Ader (übliche Farben) |
|---|---|---|
| **S** | Signal (PWM, Arduino-Pin **D11**) | orange / gelb / weiß |
| **+** | +5 V | rot |
| **−** | GND | braun / schwarz |

```
   Mana SE "Servo"           SG90
   ┌───┬───┬───┐
   │ S │ + │ − │  ──────►  orange  rot  braun
   └───┴───┴───┘
```

Ein paar wichtige Hinweise:

- **Laser abstecken.** Der Servo-Port und der Laser-Stecker teilen sich dasselbe Signal (D11). Jeder Befehl, der den Servo bewegt, würde sonst auch den Laser einschalten.
- **Der Laser-Stecker führt 12 V.** Dort angeschlossen wäre der Servo sofort kaputt. Deshalb nur den Servo-Port benutzen.
- **Vorher messen.** Die Reihenfolge der Pins kann sich zwischen Platinen-Revisionen unterscheiden. Darum vor dem Einstecken mit dem Multimeter prüfen: Zwischen **+** und **−** müssen **ca. 5 V** anliegen, keine 12 V.
- **Das 12-V-Netzteil muss eingeschaltet sein.** Hängt das Board nur am USB, antwortet GRBL zwar ganz normal mit `ok`, aber **weder die Achsen noch der Servo bewegen sich**. Das war bei uns der Grund, warum der erste Test „nichts gemacht“ hat.
- Ein SG90 reicht völlig aus. Größere Servos besser über ein eigenes 5-V-Netzteil versorgen, dessen GND mit dem GND des Boards verbunden ist.

---

## 2. Firmware: warum die Original-Firmware nicht reicht

Ab Werk läuft auf dem A3 Pro **GRBL 0.9i** (Build 20150620). Abfragen kann man das mit `$I`.

Ein RC-Servo braucht alle **20 ms (50 Hz)** einen Puls von **1–2 ms** Länge. Die Pulslänge bestimmt den Winkel. GRBL erzeugt auf D11 dagegen ein Laser-PWM mit rund **1 kHz**. Mit `M3 Sxxx` ändert sich zwar das Tastverhältnis, aber die Frequenz bleibt falsch.

Mit der Original-Firmware gibt es deshalb nur zwei Ergebnisse:

- Der Servo **zittert** oder **bewegt sich unzuverlässig**. Für sauberes Plotten taugt das nicht.
- Je nach Servo bewegt er sich auch gar nicht oder fährt an einen Anschlag.

**Die Lösung ist [robottini/grbl-servo](https://github.com/robottini/grbl-servo).** Das ist ebenfalls GRBL 0.9i. Es ändert nur Timer 2 so, dass D11 ein **61-Hz-Servosignal** mit Pulsen von ca. 0,96–2,05 ms ausgibt. Weil die Version gleich bleibt, ändert sich nichts an der Pin-Belegung. Beim Wechsel von 0.9 auf 1.1 ist das anders, dort sind D11 und D12 teilweise vertauscht. Danach läuft der Servo **ruhig und ohne Zittern**.

---

## 3. Firmware flashen (grbl-servo)

### Voraussetzungen

```bash
sudo apt install git avrdude gcc-avr avr-libc python3-serial
# Der Benutzer muss in der Gruppe dialout sein (für /dev/ttyUSB0):
sudo usermod -aG dialout $USER   # danach neu anmelden
```

### Schritt 1: Backup der Original-Firmware und der Einstellungen

```bash
mkdir -p backup
avrdude -c arduino -b 57600 -P /dev/ttyUSB0 -p atmega328p \
  -U flash:r:backup/original_flash.hex:i \
  -U eeprom:r:backup/original_eeprom.hex:i
python3 grbl_settings.py backup backup/original_settings.txt
```

> Der Nano auf der Mana SE hat den **alten Bootloader**. Deshalb flasht man mit **57600 Baud**. Meldet avrdude `not in sync`, `-b 115200` ausprobieren.

### Schritt 2: Bauen und flashen

Entweder fertig gebaut:

```bash
avrdude -c arduino -b 57600 -P /dev/ttyUSB0 -p atmega328p -U flash:w:firmware/grbl-servo.hex:i
```

Oder selbst bauen. Das Skript lädt grbl-servo im getesteten Stand herunter, spielt eine kleine Korrektur ein ([firmware/grbl-servo-fixes.patch](firmware/grbl-servo-fixes.patch)) und kompiliert:

```bash
./firmware/build.sh          # nur bauen -> firmware/build/grbl-servo.hex
./firmware/build.sh flash    # bauen und flashen
```

Die beigelegte [firmware/grbl-servo.hex](firmware/grbl-servo.hex) wurde genau so gebaut (avr-gcc 7.3.0). Ihr SHA-256 lautet
`43654378451f79a0badacbc1af93a4463bd51d09c66a8b4cf053009d590ae86c`.

### Schritt 3: Einstellungen wiederherstellen (wichtig!)

Nach dem Flashen standen bei uns **alle `$$`-Werte auf GRBL-Standard**, unter anderem 250 statt 80 Schritte/mm. Mit diesen Werten fährt die Maschine dreimal so weit wie verlangt. Deshalb die Einstellungen sofort zurückspielen:

```bash
python3 grbl_settings.py restore backup/original_settings.txt
# oder die Werte aus diesem Repo:
python3 grbl_settings.py restore grbl_settings_a3pro.txt
```

Das Skript liest die Werte danach wieder aus und vergleicht sie mit der Datei.

### Zurück zur Original-Firmware

```bash
avrdude -c arduino -b 57600 -P /dev/ttyUSB0 -p atmega328p -U flash:w:backup/original_flash.hex:i
python3 grbl_settings.py restore backup/original_settings.txt
```

---

## 4. Stift ansteuern

Mit grbl-servo steuert man den Stift über den „Spindel“-Befehl:

| Befehl | Wirkung |
|---|---|
| `M3 S0` … `M3 S90` | Servo-Winkel (0 ≈ 0,96 ms Puls, 90 ≈ 2,05 ms) |
| `M5` | Servo auf Position 0 |

> **Achtung:** Laut README von grbl-servo reicht der Bereich bis `S255`. In der `config.h` steht aber `SPINDLE_MAX_RPM 90`. Der **tatsächlich nutzbare Bereich ist deshalb S0–S90**. Größere Werte werden auf 90 begrenzt.

**Auflösung:** Timer 2 läuft mit 64 µs pro Takt. Der ganze Bereich S0–S90 umfasst deshalb nur **18 Stellungen**, nämlich die Timer-Werte 15 bis 32. Die Formel lautet `Timer = floor(S × 17/90 + 15)`. Erst etwa **5–6 S-Einheiten ergeben eine neue Stellung**. Eine Stufe entspricht bei einem SG90 rund **10°**. Der Servo bewegt sich deshalb sichtbar **in Schritten**, und kleine Änderungen wie S25 → S27 bewirken oft gar nichts. Für einen Stifthub reicht das trotzdem aus. Unsere Werte S25 und S30 liegen genau eine Stufe auseinander (Timer 19 und 20).

Wer feinere Abstufungen braucht, kann in `spindle_control.c` die Werte `RC_SERVO_SHORT` und `RC_SERVO_LONG` anpassen. Mehr Auflösung gibt es mit einem 8-Bit-Timer bei 61 Hz allerdings nicht.

### Werte an unserem Gerät

| Zustand | Befehl |
|---|---|
| **Stift oben** | `M3 S25` |
| **Stift unten** | `M3 S30` |

Diese Werte hängen davon ab, wie der Servohebel und der Stifthalter montiert sind. **Für jedes Gerät selbst ermitteln**, am einfachsten mit [`pen_calibrate.py`](#pen_calibratepy--stift-werte-finden).

### Beispiel-G-Code: ein Quadrat mit 20 mm Seitenlänge

```gcode
G21            ; Millimeter
G90            ; absolute Koordinaten
M3 S25         ; Stift hoch
G0 X10 Y10     ; zum Startpunkt fahren
M3 S30         ; Stift runter
G4 P0.15       ; 150 ms warten, bis der Stift aufliegt
G1 X30 Y10 F500
G1 X30 Y30
G1 X10 Y30
G1 X10 Y10
M3 S25         ; Stift hoch
G4 P0.15
G0 X0 Y0
M5
```

Hinweise:
- `M3` wartet in grbl-servo, bis alle vorherigen Bewegungen fertig sind. Der Stift wird also nicht mitten in einer Fahrt angehoben oder abgesenkt.
- Ein kurzes `G4 P0.1`–`G4 P0.2` nach dem Absenken verhindert, dass die erste Linie zu spät beginnt.
- Es gibt **kein Homing**, weil keine Endschalter verbaut sind. Die Position beim Einschalten bzw. beim Reset ist `0,0`. Vor dem Plotten den Stift also von Hand in die gewünschte Ecke schieben, solange die Motoren noch nicht bestromt sind.

---

## 5. Die Python-Programme

Die Testprogramme in diesem Abschnitt brauchen nur `pyserial` (`sudo apt install python3-serial`). Alle Programme nehmen `/dev/ttyUSB0` als Standard-Port.

### `pen_calibrate.py`: Stift-Werte finden

Interaktiv: Man tippt einen Wert ein, der Servo fährt hin, und man schaut, wo der Stift steht.

```bash
python3 pen_calibrate.py            # oder: python3 pen_calibrate.py /dev/ttyUSB1
```

| Eingabe + Enter | Wirkung |
|---|---|
| `0` … `90` | Servo auf diesen Wert |
| `+` / `-` | S-Wert um 5 erhöhen / verringern (≈ 1 Servo-Stellung) |
| `++` / `--` | S-Wert um 1 erhöhen / verringern (bewegt den Servo oft gar nicht, siehe [Auflösung](#4-stift-ansteuern)) |
| `q` (oder Strg+C) | beenden, sendet `M5` |

Gesucht sind zwei Werte:
- **Stift oben:** sicher vom Papier weg, aber nicht höher als nötig. Ein kurzer Weg macht das Plotten schneller.
- **Stift unten:** Der Stift liegt sauber auf, ohne dass der Servo brummt.

### `machine_test.py`: Funktionstest von Servo und Achsen

```bash
python3 machine_test.py                     # Servo + Achsen
python3 machine_test.py --servo             # nur Servo
python3 machine_test.py --xy                # nur Achsen
python3 machine_test.py --up 25 --down 30 --dist 20 --feed 800
```

- **Servo-Test:** fährt S0 bis S90 in 15er-Stufen ab (je 2 s) und hebt und senkt danach den Stift fünfmal.
- **Achsen-Test:** Stift hoch, dann relativ **10 mm +X, zurück, 10 mm +Y, zurück**. Nach jeder Bewegung wird die GRBL-Position ausgegeben.

Jede Zeile wird mit Uhrzeit protokolliert. So lässt sich vergleichen, was man an der Maschine gesehen hat.

### `grbl_settings.py`: `$$`-Einstellungen sichern und wiederherstellen

```bash
python3 grbl_settings.py backup  meine.txt                   # Gerät -> Datei
python3 grbl_settings.py restore grbl_settings_a3pro.txt     # Datei -> Gerät (mit Kontrolle)
python3 grbl_settings.py diff    grbl_settings_a3pro.txt     # nur vergleichen
```

### Hinweis zur seriellen Verbindung

Beim Öffnen des Ports startet der Nano normalerweise neu und meldet sich mit `Grbl 0.9i ['$' for help]`. Ein Neustart setzt Stift, Modus und Position zurück. Nach einem avrdude-Lauf passiert dieser Neustart manchmal nicht mehr, weil das HUPCL-Flag des Ports zurückgesetzt ist. Die Skripte prüfen die Verbindung deshalb mit `$I` und melden gegebenenfalls `(ohne Neustart)`. In diesem Fall gilt der vorherige Zustand der Maschine weiter.

---

## 6. A3 Plot: SVG-Dateien plotten

`a3plot_gui.py` ist ein Programm mit Oberfläche (Qt) zum Plotten von SVG-Dateien. Man legt die Zeichnung auf einem A4-Blatt zurecht, stellt die Geschwindigkeit ein und verfolgt den Plot: Wie lange dauert er voraussichtlich, wie lange läuft er schon, wie lange dauert er noch, wann ist er fertig?

### Installation (Ubuntu)

```bash
sudo apt install python3-pyqt6 python3-numpy python3-scipy python3-serial
python3 a3plot_gui.py                # oder: python3 a3plot_gui.py zeichnung.svg
./install_desktop.sh                 # optional: Starter „A3 Plot“ im Anwendungsmenü
```

### Ablauf

1. **SVG öffnen.** Unterstützt werden Pfade (mit allen Kurven und Bögen), Linien, Rechtecke, Kreise, Ellipsen, Polygone, Gruppen mit Transformationen und Inkscape-Ebenen. Jede Ebene lässt sich einzeln an- und abwählen, zum Beispiel für mehrere Stiftfarben nacheinander. Die Position bleibt dabei gleich. Text muss vorher in Inkscape in Pfade umgewandelt werden.
2. **Positionieren.** Die Zeichnung mit der Maus auf dem Blatt verschieben und an den Ecken größer oder kleiner ziehen. Das Seitenverhältnis bleibt dabei erhalten. Mit gedrückter Umschalttaste wird nicht auf 0,5 mm gerundet. Alternativ X/Y, Breite/Höhe oder Prozent eintippen. *Einpassen* füllt das Blatt bis zum Rand, *↻ 90°* dreht die Zeichnung. Mausrad = Zoom, rechte Maustaste = Ansicht verschieben, Doppelklick = ganzes Blatt.
3. **Stift einstellen.** Zeichengeschwindigkeit, Leerfahrt-Geschwindigkeit, Servo-Werte oben/unten und Wartezeiten nach dem Absenken und Anheben. Die geschätzte Dauer wird sofort neu berechnet.
4. **Verbinden** und den Stift über die Blattecke am Nullpunkt bringen (roter Punkt in der Ansicht, Standard: unten links). Am einfachsten vor dem Verbinden von Hand hinschieben, denn beim Verbinden startet der Controller neu und die aktuelle Position wird zu 0/0. Oder mit den Pfeiltasten hinfahren und **Papierecke = hier** drücken. Das Blatt muss nicht gerade liegen: Danach zur benachbarten Ecke an der **kurzen** Blattseite fahren (bei A3 hoch mit Nullpunkt unten links also unten rechts; **Zur 2. Ecke** fährt ungefähr hin, den Rest mit den Pfeiltasten) und **2. Ecke = hier** drücken. A3 Plot berechnet daraus die Schräglage und dreht den G-Code passend, die Drehung steht unter den Maschinen-Knöpfen. Wer die erste Ecke neu setzt, muss auch die zweite neu setzen. Weicht der gemessene Abstand mehr als 5 mm von der Seitenlänge ab oder ist das Blatt um mehr als 20° gedreht, fragt A3 Plot nach. **Rahmen abfahren** zeigt mit gehobenem Stift, wo die Zeichnung landet.
5. **Plot starten.** Rechts unten stehen geschätzte Gesamtdauer, vergangene Zeit, verbleibende Zeit und die voraussichtliche Uhrzeit des Endes. Die verbleibende Zeit wird nach der ersten Minute an das tatsächliche Tempo angepasst. Bereits gezeichnete Linien werden in der Ansicht dunkelblau, die aktuelle Stiftposition ist rot markiert. Nach dem Plot steht die tatsächliche Dauer neben der Schätzung. Jeder Plot wird in `~/.local/share/a3plot/history.csv` protokolliert.

| Knopf | Wirkung |
|---|---|
| **Pause** | hält nach dem aktuellen Strich an, **Stift oben**. Danach kann man z. B. den Stift tauschen. |
| **Sofort halt** | GRBL-Feed-Hold: bremst sofort, der Stift bleibt aber unten (gibt einen Punkt) |
| **Weiter** | setzt nach Pause oder Halt fort |
| **Abbrechen** | hält an, setzt GRBL zurück (Position bleibt erhalten) und hebt den Stift |

### Richtung der Achsen prüfen

Beim ersten Mal unter *Einstellungen → Maschine und Übertragung* prüfen, ob die Annahmen stimmen: Nullpunkt unten links, +X nach rechts, +Y nach hinten. Mit den Pfeiltasten unter „Maschine“ testen: Der Stift muss in Pfeilrichtung fahren (↑ = vom Betrachter weg). Fährt er falsch herum, die Richtung in den Einstellungen umstellen.

### Keine verschluckten Befehle mehr

GRBL hat einen Empfangspuffer von 128 Byte. Schickt das Programm mehr unbestätigte Zeichen, als hineinpassen, gehen Bytes **ohne Fehlermeldung** verloren. Das ist die übliche Ursache für „verschluckte“ Befehle. A3 Plot schützt sich mehrfach dagegen:

- **Übertragung „Sicher“ (Standard):** Es ist immer nur **eine** Zeile unterwegs. Die nächste geht erst raus, wenn GRBL `ok` gemeldet hat. Der Puffer kann so nie überlaufen. Der Bewegungsplaner von GRBL (18 Blöcke) bleibt trotzdem gefüllt, weil `ok` sofort kommt, solange darin Platz ist.
- Alternativ **Zeichen zählen** mit einstellbarer Obergrenze (Standard 64 von 127 Byte, also reichlich Reserve).
- Nach dem Verbinden wartet das Programm, bis sich GRBL nach dem Neustart meldet. Erst dann geht der erste Befehl raus.
- Lehnt GRBL eine Zeile mit `error` ab (z. B. weil sie verstümmelt ankam), wird sie im sicheren Modus **automatisch erneut gesendet**. Alle Koordinaten sind absolut (`G90`), deshalb ist das Wiederholen harmlos.
- **Watchdog:** Steht die Maschine still, obwohl eine Zeile seit Sekunden unbestätigt ist, gilt die Zeile als verloren und wird erneut gesendet.
- Startet der Controller mitten im Plot neu (z. B. bei einem Spannungseinbruch), bricht das Programm ab, statt blind weiterzusenden.

Wie viele Zeilen wiederholt wurden, steht unter „Zeilen“ und im Protokoll.

### Tipp: Motoren zwischen den Bewegungen bestromt lassen

Mit `$1=25` schaltet GRBL die Motoren 25 ms nach jeder Bewegung ab. Beim Stiftwechsel (`M3`) steht die Maschine kurz still und könnte sich dabei minimal verschieben. Mit `$1=255` bleiben die Motoren dauerhaft bestromt (im Protokollfeld eingeben). Die Motoren werden dann etwas wärmer, und der Schlitten lässt sich nicht mehr von Hand schieben.

### Tests

```bash
python3 -m pytest tests
```

Die Tests laufen gegen einen simulierten GRBL-Controller (`tests/fake_grbl.py`) mit 128-Byte-Empfangspuffer. Geprüft wird unter anderem, dass der Puffer nie überläuft, dass jede Zeile genau einmal und in der richtigen Reihenfolge ausgeführt wird und dass fehlerhafte Zeilen und verlorene `ok` erkannt werden.

---

## 7. GRBL-Einstellungen des A3 Pro

Die Werte unseres Geräts, wie sie ab Werk eingestellt waren, stehen in [grbl_settings_a3pro.txt](grbl_settings_a3pro.txt). Die wichtigsten:

| Einstellung | Wert | Bedeutung |
|---|---|---|
| `$2` | `1` | Step-Signal der X-Achse invertiert |
| `$100`, `$101` | `80.000` | Schritte/mm für X und Y |
| `$110`, `$111` | `500.000` | max. Geschwindigkeit X/Y in mm/min |
| `$120`, `$121` | `100.000` | Beschleunigung X/Y in mm/s² |
| `$20`, `$21`, `$22` | `0` | keine Soft-/Hard-Limits, kein Homing (keine Endschalter) |
| `$130`, `$131` | `200.000` | Verfahrweg laut GRBL. Die Mechanik schafft mehr, da Soft-Limits aus sind, hat der Wert keine Wirkung |

---

## 8. Fehlersuche

| Symptom | Ursache / Lösung |
|---|---|
| GRBL antwortet mit `ok`, aber nichts bewegt sich | 12-V-Netzteil ist aus oder nicht eingesteckt |
| Servo zittert und fährt nicht sauber in Position | Original-Firmware mit ~1 kHz PWM, siehe [Abschnitt 2](#2-firmware-warum-die-original-firmware-nicht-reicht) |
| Servo bewegt sich gar nicht | Servo steckt nicht am Servo-Port oder ist verdreht eingesteckt. Mit dem Multimeter 5 V zwischen + und − prüfen |
| Achsen fahren nach dem Flashen dreimal zu weit | EEPROM wurde zurückgesetzt, Einstellungen mit `grbl_settings.py restore` zurückspielen |
| `Permission denied: /dev/ttyUSB0` | Benutzer ist nicht in der Gruppe `dialout` |
| avrdude: `not in sync` | anderen Bootloader-Baudwert probieren (`-b 57600` ↔ `-b 115200`). Kein anderes Programm darf den Port gerade benutzen |
| Plot zeichnet spiegelverkehrt oder fährt in die falsche Richtung | in A3 Plot unter *Einstellungen → Maschine und Übertragung* Nullpunkt-Ecke und Achsrichtungen anpassen (siehe [Abschnitt 6](#6-a3-plot-svg-dateien-plotten)) |
| `/dev/ttyUSB0` fehlt | `lsusb` sollte `1a86:7523 QinHeng Electronics CH340` zeigen. Ein anderes USB-Kabel probieren, manche Kabel können nur laden |

---

## 9. Quellen & Lizenz

**Quellen und Hintergrund**
- [robottini/grbl-servo](https://github.com/robottini/grbl-servo): GRBL 0.9i mit RC-Servo auf D11
- [grbl/grbl](https://github.com/grbl/grbl): Original-GRBL
- [jandelgado/eleksmaker_a3](https://github.com/jandelgado/eleksmaker_a3): Notizen zum EleksMaker A3, Laser an D11
- [matsab.de: EleksLaser A3 Pro](https://matsab.de/de/elekslaser.html): Mana SE, GRBL, Flashen unter Linux/macOS
- [EleksMaker-Forum: „grbl 1.1g with servo for Mana SE“](https://forum.eleksmaker.com/topic/2510/grbl-1-1g-with-servo-for-mana-se)

**Lizenz**
- Die Python-Skripte und diese Dokumentation stehen unter der [MIT-Lizenz](LICENSE).
- `firmware/grbl-servo.hex` und `firmware/grbl-servo-fixes.patch` sind aus GRBL bzw. grbl-servo abgeleitet und stehen wie diese unter der **GPLv3**. Den Quellcode gibt es unter [robottini/grbl-servo](https://github.com/robottini/grbl-servo) (Commit `1e7a63a`) zusammen mit dem Patch in diesem Repo.

**Nutzung auf eigene Gefahr.** Wenn ein Laser montiert ist, gilt: Jeder `M3`-Befehl kann ihn einschalten.
