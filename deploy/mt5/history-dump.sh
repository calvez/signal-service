#!/usr/bin/env bash
# One-off export of MT5 price history for backtests (mt5/HistoryDump.mq5).
# Run as root INSIDE the trader container, outside trading hours:  deploy/mt5/history-dump.sh
#
# What it does:
#   1. compiles HistoryDump into MQL5\Scripts
#   2. raises "Max bars in chart" (it caps how much history MT5 hands out)
#   3. stops the normal terminal and starts it once with a start config that logs in, runs
#      HistoryDump and shuts the terminal down again (ShutdownTerminal=1)
#   4. starts the normal terminal again and copies the CSV files to the service:
#      /opt/signal-service/data/history_csv/<symbol>_<tf>.csv
# Then import them:  cd /opt/signal-service && sudo -u signal .venv/bin/python scripts/import_history.py
set -euo pipefail

MT5_USER=mt5
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PREFIX="/home/$MT5_USER/.mt5"
MT5_DIR="$PREFIX/drive_c/Program Files/MetaTrader 5"
DEST=/opt/signal-service/data/history_csv
MAX_BARS=5000000
RUN=(-p "User=$MT5_USER" -p "WorkingDirectory=$MT5_DIR" -p Environment=DISPLAY=:99
     -p "Environment=WINEPREFIX=$PREFIX" -p Environment=WINEDEBUG=-all
     -p "Environment=WINEDLLOVERRIDES=mscoree,mshtml=")

echo "== compile HistoryDump =="
install -d -o "$MT5_USER" -g "$MT5_USER" "$MT5_DIR/MQL5/Scripts"
install -o "$MT5_USER" -g "$MT5_USER" -m 644 "$REPO/mt5/HistoryDump.mq5" "$MT5_DIR/MQL5/Scripts/HistoryDump.mq5"
systemd-run --wait --quiet -p RuntimeMaxSec=120 "${RUN[@]}" \
  /usr/bin/wine MetaEditor64.exe /portable '/compile:MQL5\Scripts\HistoryDump.mq5' /log || true
# (MetaEditor's exit code is not 0 even on success; the .ex5 file is the real check)
[ -f "$MT5_DIR/MQL5/Scripts/HistoryDump.ex5" ] || { echo "compile failed"; iconv -f UTF-16 -t UTF-8 "$MT5_DIR/MQL5/Scripts/HistoryDump.log" | tail; exit 1; }

echo "== stop the normal terminal =="
systemctl stop mt5-terminal

set_max_bars() {  # Config/common.ini is UTF-16; MT5 must not be running
python3 - "$MT5_DIR/Config/common.ini" "$1" <<'PYEOF'
import re, sys
path, value = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-16").read()
old = re.search(r"(?m)^MaxBars=(\d+)", text)
print(old.group(1) if old else "100000")
text = re.sub(r"(?m)^MaxBars=\d+", f"MaxBars={value}", text)
open(path, "w", encoding="utf-16", newline="").write(text)
PYEOF
}

echo "== raise Max bars in chart to $MAX_BARS (restored afterwards) =="
OLD_MAX_BARS=$(set_max_bars "$MAX_BARS")

echo "== one terminal run with Script=HistoryDump =="
# Same login and [Experts] as the normal start config; only [StartUp] differs.
HIST_INI="$MT5_DIR/Config/history.ini"
python3 - "$MT5_DIR/Config/startup.ini" "$HIST_INI" <<'PYEOF'
import re, sys
src, dst = sys.argv[1], sys.argv[2]
text = open(src).read()
text = re.sub(r"\[StartUp\].*", "", text, flags=re.S).rstrip() + "\n\n"
text += "[StartUp]\nScript=HistoryDump\nSymbol=GER40.cash\nPeriod=M5\nShutdownTerminal=1\n"
open(dst, "w").write(text)
PYEOF
chown "$MT5_USER:$MT5_USER" "$HIST_INI"; chmod 600 "$HIST_INI"
rm -rf "$MT5_DIR/MQL5/Files/history"
# TimeoutStopSec: a Wine helper (winedevice.exe) can outlive the terminal and hold the unit.
systemd-run --wait --quiet -p RuntimeMaxSec=1800 -p TimeoutStopSec=30 "${RUN[@]}" \
  -p "ExecStopPost=/usr/bin/wineserver -k" \
  /usr/bin/wine terminal64.exe /portable '/config:Config\history.ini' || true
shred -u "$HIST_INI"   # it holds the FTMO password
set_max_bars "$OLD_MAX_BARS" >/dev/null

echo "== start the normal terminal again =="
systemctl start mt5-terminal

echo "== copy the files to the service =="
SRC="$MT5_DIR/MQL5/Files/history"
[ -f "$SRC/done.txt" ] || { echo "HistoryDump did not finish (no done.txt). Check MQL5/logs."; exit 1; }
install -d -o signal -g signal "$DEST"
for f in "$SRC"/*.csv "$SRC/done.txt"; do install -o signal -g signal -m 644 "$f" "$DEST/"; done
cat "$DEST/done.txt"
echo "Next: cd /opt/signal-service && sudo -u signal .venv/bin/python scripts/import_history.py"
