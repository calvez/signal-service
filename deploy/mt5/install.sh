#!/usr/bin/env bash
# Headless MetaTrader 5 on Ubuntu 24.04 (noble): Wine + Xvfb, no VNC, no GUI access.
# MT5 still needs a display to run, so it draws on a virtual one (Xvfb :99) nobody looks at.
# Visibility comes from Telegram: status, alerts and /screenshot of the virtual display.
#
# Run as root, once, from the repo:  sudo deploy/mt5/install.sh
# Read it first. Before running, create these two files next to this script:
#   startup.ini     (from startup.ini.example — FTMO login, server)
#   BarPusher.set   (from BarPusher.set.example — INGEST_TOKEN from .env)
set -euo pipefail

MT5_USER=mt5
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PREFIX="/home/$MT5_USER/.mt5"
MT5_DIR="$PREFIX/drive_c/Program Files/MetaTrader 5"
# WINEDLLOVERRIDES: without it Wine opens a "install Mono/Gecko?" dialog on the display nobody
# watches and wineboot waits for it forever. MT5 needs neither.
as_mt5() { sudo -u "$MT5_USER" env DISPLAY=:99 WINEPREFIX="$PREFIX" WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml=" "$@"; }

for f in startup.ini BarPusher.set; do
  [ -f "$HERE/$f" ] || { echo "Missing $HERE/$f — copy it from $f.example and fill it in."; exit 1; }
done

echo "== packages =="
dpkg --add-architecture i386
mkdir -pm755 /etc/apt/keyrings
wget -qO /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key
wget -qNP /etc/apt/sources.list.d/ https://dl.winehq.org/wine-builds/ubuntu/dists/noble/winehq-noble.sources
apt-get update
# Wine 10.0 on purpose: with Wine 11 the MT5 installer and terminal stop with "A debugger has
# been found running in your system" (MetaQuotes' anti-debug check; known Wine 11 issue).
WINE_VERSION="10.0.0.0~noble-1"
apt-mark unhold winehq-stable wine-stable wine-stable-amd64 wine-stable-i386 >/dev/null 2>&1 || true
apt-get install -y --install-recommends --allow-downgrades \
  "winehq-stable=$WINE_VERSION" "wine-stable=$WINE_VERSION" \
  "wine-stable-amd64=$WINE_VERSION" "wine-stable-i386:i386=$WINE_VERSION"
apt-get install -y xvfb openbox imagemagick xdotool fonts-dejavu-core
# Pin Wine: an unplanned Wine upgrade is the most likely thing to break MT5.
apt-mark hold winehq-stable wine-stable wine-stable-amd64 wine-stable-i386 || true

echo "== user =="
id "$MT5_USER" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$MT5_USER"

echo "== virtual display =="
cp "$HERE"/systemd/mt5-xvfb.service "$HERE"/systemd/mt5-wm.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mt5-xvfb mt5-wm
sleep 3

echo "== Wine prefix + MT5 (silent install) =="
as_mt5 wineboot --init
as_mt5 winecfg -v win10
as_mt5 wget -qO "/home/$MT5_USER/mt5setup.exe" \
  https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe
as_mt5 wine "/home/$MT5_USER/mt5setup.exe" /auto || true
# The installer starts the terminal when done; wait for the files, then stop everything.
for i in $(seq 1 60); do [ -f "$MT5_DIR/terminal64.exe" ] && break; sleep 5; done
[ -f "$MT5_DIR/terminal64.exe" ] || { echo "MT5 did not install. Take a screenshot: sudo -u $MT5_USER import -display :99 -window root /tmp/mt5.png"; exit 1; }
sleep 20
as_mt5 wineserver -k || true

echo "== EA, settings, start config =="
# MQL5/ only appears after the terminal's first start, so create the folders we need.
install -d -o "$MT5_USER" -g "$MT5_USER" "$MT5_DIR/MQL5" "$MT5_DIR/MQL5/Experts" "$MT5_DIR/MQL5/Presets" "$MT5_DIR/config"
install -o "$MT5_USER" -g "$MT5_USER" -m 644 "$REPO/mt5/BarPusher.mq5" "$MT5_DIR/MQL5/Experts/BarPusher.mq5"
install -o "$MT5_USER" -g "$MT5_USER" -m 600 "$HERE/BarPusher.set" "$MT5_DIR/MQL5/Presets/BarPusher.set"
install -o "$MT5_USER" -g "$MT5_USER" -m 600 "$HERE/startup.ini"   "$MT5_DIR/config/startup.ini"
# Compile headless; MetaEditor writes a log next to the source.
as_mt5 wine "$MT5_DIR/MetaEditor64.exe" /compile:"C:\\Program Files\\MetaTrader 5\\MQL5\\Experts\\BarPusher.mq5" /log || true
LOG="$MT5_DIR/MQL5/Experts/BarPusher.log"
if [ -f "$MT5_DIR/MQL5/Experts/BarPusher.ex5" ]; then
  echo "BarPusher compiled."
else
  echo "Compile failed — see $LOG"; iconv -f UTF-16 -t UTF-8 "$LOG" 2>/dev/null | tail -20 || true; exit 1
fi

echo "== terminal service =="
cp "$HERE"/systemd/mt5-terminal.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mt5-terminal

# The plaintext copies here are no longer needed.
shred -u "$HERE/startup.ini" "$HERE/BarPusher.set"

cat <<EOF

Done. MT5 is starting headless.
 - Logs:        journalctl -u mt5-terminal -f
 - Screenshot:  sudo -u $MT5_USER import -display :99 -window root /tmp/mt5.png
 - Within ~2 minutes the signal service should report the first heartbeat in Telegram.
EOF
