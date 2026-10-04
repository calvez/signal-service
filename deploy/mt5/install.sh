#!/usr/bin/env bash
# Headless MetaTrader 5 on Ubuntu 24.04 (noble): Wine + Xvfb, no VNC, no GUI access.
# MT5 still needs a display to run, so it draws on a virtual one (Xvfb :99) nobody looks at.
# Visibility comes from Telegram: status, alerts and /screenshot of the virtual display.
#
# Run as root, once, from the repo, AFTER deploy/deploy.sh (it creates the user "signal"):
#   sudo deploy/mt5/install.sh
# Read it first. Before running, create next to this script:
#   startup.ini     (from startup.ini.example — FTMO login, password, server; never commit it)
# Optional: BarPusher.set (EA inputs); without it BarPusher.set.example is used. It holds no secret.
set -euo pipefail

MT5_USER=mt5
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PREFIX="/home/$MT5_USER/.mt5"
MT5_DIR="$PREFIX/drive_c/Program Files/MetaTrader 5"
# WINEDLLOVERRIDES: without it Wine opens a "install Mono/Gecko?" dialog on the display nobody
# watches and wineboot waits for it forever. MT5 needs neither.
as_mt5() { sudo -u "$MT5_USER" env DISPLAY=:99 WINEPREFIX="$PREFIX" WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml=" "$@"; }

SPOOL=/var/spool/signal-mt5   # the EA writes its files here, the service reads them (app/spool.py)
[ -f "$HERE/startup.ini" ] || { echo "Missing $HERE/startup.ini — copy it from startup.ini.example and fill it in."; exit 1; }
PRESET="$HERE/BarPusher.set"; [ -f "$PRESET" ] || PRESET="$HERE/BarPusher.set.example"

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

echo "== FTMO server list =="
# The generic installer does not know the FTMO servers (login would never start). FTMO's own
# installer (linked from ftmo.com) ships them in Config/servers.dat: install it once, keep that
# file, remove the rest.
FTMO_DIR="$PREFIX/drive_c/Program Files/FTMO Global Markets MT5 Terminal"
as_mt5 wget -qO "/home/$MT5_USER/ftmo5setup.exe" \
  https://download.mql5.com/cdn/web/ftmo.global.markets/mt5/ftmo5setup.exe
as_mt5 wine "/home/$MT5_USER/ftmo5setup.exe" /auto || true
for i in $(seq 1 60); do [ -f "$FTMO_DIR/Config/servers.dat" ] && break; sleep 5; done
[ -f "$FTMO_DIR/Config/servers.dat" ] || { echo "FTMO installer did not produce servers.dat"; exit 1; }
sleep 20
as_mt5 wineserver -k || true
install -d -o "$MT5_USER" -g "$MT5_USER" "$MT5_DIR/Config"
install -o "$MT5_USER" -g "$MT5_USER" -m 644 "$FTMO_DIR/Config/servers.dat" "$MT5_DIR/Config/servers.dat"
rm -rf "$FTMO_DIR"

echo "== spool folder (EA -> service) =="
getent group signal >/dev/null || groupadd --system signal
# mt5 writes, group signal (the service) reads and deletes; setgid keeps new files in group signal.
install -d -o "$MT5_USER" -g signal -m 2770 "$SPOOL"

echo "== EA, settings, start config =="
# MQL5/ only appears after the terminal's first start, so create the folders we need.
# "Config" with a capital C: that is what the installer uses (Linux is case-sensitive).
install -d -o "$MT5_USER" -g "$MT5_USER" "$MT5_DIR/MQL5" "$MT5_DIR/MQL5/Experts" "$MT5_DIR/MQL5/Presets" "$MT5_DIR/MQL5/Files" "$MT5_DIR/Config"
ln -sfn "$SPOOL" "$MT5_DIR/MQL5/Files/signal"
chown -h "$MT5_USER:$MT5_USER" "$MT5_DIR/MQL5/Files/signal"
install -o "$MT5_USER" -g "$MT5_USER" -m 644 "$REPO/mt5/BarPusher.mq5" "$MT5_DIR/MQL5/Experts/BarPusher.mq5"
install -o "$MT5_USER" -g "$MT5_USER" -m 644 "$PRESET" "$MT5_DIR/MQL5/Presets/BarPusher.set"
install -o "$MT5_USER" -g "$MT5_USER" -m 600 "$HERE/startup.ini" "$MT5_DIR/Config/startup.ini"
# Compile headless; MetaEditor writes a log next to the source. It only works with /portable
# (data folder = the install folder, not AppData), run from the MT5 folder with a RELATIVE path:
# an absolute path with spaces makes MetaEditor exit silently.
(cd "$MT5_DIR" && as_mt5 wine MetaEditor64.exe /portable '/compile:MQL5\Experts\BarPusher.mq5' /log) || true
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

# The plaintext copy here is no longer needed (the installed one is mode 600, owner mt5).
shred -u "$HERE/startup.ini"

cat <<EOF

Done. MT5 is starting headless.
 - Logs:        journalctl -u mt5-terminal -f
 - Screenshot:  sudo -u $MT5_USER import -display :99 -window root /tmp/mt5.png
 - Within ~2 minutes files appear in $SPOOL and the service reports the first heartbeat.
EOF
