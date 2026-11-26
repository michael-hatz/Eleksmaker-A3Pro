#!/usr/bin/env bash
# Baut grbl-servo (GRBL 0.9i mit RC-Servo auf D11) für den Arduino Nano (ATmega328P)
# und flasht es optional.
#
#   ./build.sh            # nur bauen  -> build/grbl-servo.hex
#   ./build.sh flash      # bauen und auf /dev/ttyUSB0 flashen
#   PORT=/dev/ttyUSB1 ./build.sh flash
#
# Voraussetzungen (Debian/Ubuntu): sudo apt install git avrdude gcc-avr avr-libc
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/grbl-servo"
BUILD="$HERE/build"
PORT="${PORT:-/dev/ttyUSB0}"
COMMIT=1e7a63ab05cbcf73fd34c5285316af5db591b828   # getesteter Stand von robottini/grbl-servo

if [ ! -d "$SRC" ]; then
  git clone https://github.com/robottini/grbl-servo "$SRC"
fi
git -C "$SRC" checkout -q "$COMMIT"
git -C "$SRC" checkout -q -- .
git -C "$SRC" apply "$HERE/grbl-servo-fixes.patch"

mkdir -p "$BUILD"
avr-gcc -Wall -Wno-int-in-bool-context -Os -DF_CPU=16000000L -mmcu=atmega328p \
  -ffunction-sections -fdata-sections -Wl,--gc-sections \
  -I"$SRC" "$SRC"/*.c -o "$BUILD/grbl-servo.elf" -lm
avr-objcopy -j .text -j .data -O ihex "$BUILD/grbl-servo.elf" "$BUILD/grbl-servo.hex"
avr-size --mcu=atmega328p -C "$BUILD/grbl-servo.elf"
echo "OK: $BUILD/grbl-servo.hex"

if [ "${1:-}" = "flash" ]; then
  # Der Nano der Mana SE hat den alten Bootloader -> 57600 Baud.
  # Falls "not in sync": -b 115200 versuchen.
  avrdude -c arduino -b 57600 -P "$PORT" -p atmega328p -U flash:w:"$BUILD/grbl-servo.hex":i
  echo
  echo "WICHTIG: Das EEPROM wurde beim Flashen auf GRBL-Standardwerte zurückgesetzt."
  echo "Einstellungen wiederherstellen:  python3 grbl_settings.py restore grbl_settings_a3pro.txt"
fi
