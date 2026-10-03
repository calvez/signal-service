# MT5 headless in the trader container

All of this runs inside the `trader` Incus container (`docs/server-setup.md`), not on the host.

There is no GUI access. MT5 is a Windows GUI program, so it still needs *a* display: it draws on a virtual one (Xvfb :99) that nobody looks at. Login, WebRequest permission and the EA are set from a start config, so no clicking is ever needed. Everything you see goes through Telegram (`docs/telegram.md`).

```
trader container
├─ mt5-xvfb       Xvfb :99 (virtual screen, never viewed)
├─ mt5-wm         openbox (tiny; keeps MT5 windows and dialogs sane)
├─ mt5-terminal   wine terminal64.exe /config:startup.ini ── BarPusher ──▶ 127.0.0.1:8000
└─ signal-service uvicorn 127.0.0.1:8000 + Telegram bot (long polling)
```

## Install
1. `cp deploy/mt5/startup.ini.example deploy/mt5/startup.ini` and fill in `Login`, `Password`, `Server` (FTMO free trial first). Do this yourself on the server; don't paste the password into any chat.
2. `cp deploy/mt5/BarPusher.set.example deploy/mt5/BarPusher.set` and set `IngestToken` to the value in `.env`.
3. `sudo deploy/mt5/install.sh` — read it first. It:
   - installs WineHQ stable (pinned with `apt-mark hold`), Xvfb, openbox, ImageMagick
   - creates user `mt5`, starts the virtual display
   - installs MT5 silently (`mt5setup.exe /auto`)
   - copies the EA, compiles it headless with MetaEditor, installs `startup.ini` (mode 600) and the preset
   - enables `mt5-terminal` and shreds the plaintext copies in `deploy/mt5/`

FTMO's branded MT5 installer works too; the generic one is fine as long as `Server=` is the FTMO server shown in your FTMO client area.

## What startup.ini does
- `[Common]` logs in to the FTMO account.
- `[Experts]` enables EAs and allows WebRequest to `http://127.0.0.1:8000`.
- `[StartUp]` opens a GER40 M5 chart and attaches BarPusher with `BarPusher.set`.

So every `systemctl restart mt5-terminal` comes back logged in with the EA running.

## When something looks wrong
- Telegram reports it first (no heartbeat, disconnected, stale data).
- `/screenshot` in Telegram shows the virtual display, e.g. a login error or an update dialog.
- Server side: `journalctl -u mt5-terminal -f`, and the MT5 logs in `/home/mt5/.mt5/drive_c/Program Files/MetaTrader 5/logs/` and `MQL5/Logs/` (UTF-16: `iconv -f UTF-16 -t UTF-8`).
- Emergency GUI, only if ever needed: `apt install x11vnc`, run `sudo -u mt5 x11vnc -display :99 -localhost -once`, connect through `ssh -L 5900:127.0.0.1:5900`. Remove it afterwards.

## Things to verify on first start
- The EA's first POST arrives. If WebRequest refuses `127.0.0.1:8000`, try `127.0.0.1 signal.local` in `/etc/hosts` with `http://signal.local:8000` in both `startup.ini` and `BarPusher.set`, or nginx on `127.0.0.1:80`.
- Symbol names match FTMO's (`GER40.cash` etc.); the EA prints a warning in the Experts log if not.
- After a restart, the EA reattaches by itself.

## Operations
- Wine is pinned. Before upgrading Wine, MT5 or the OS: on the host, `incus snapshot create trader before-<what>`; test outside trading hours; `incus snapshot restore` if it breaks.
- MT5 updates itself; Telegram will show if the EA stops reporting afterwards.
- Memory: MT5 under Wine uses roughly 300–600 MB; the container's 8 GiB limit leaves plenty of room.

## Security
- Nothing new is public: Xvfb doesn't listen on TCP, the API binds to 127.0.0.1, the bot uses outbound long polling.
- Firewall: only SSH (keys only) from outside.
- `startup.ini` holds the FTMO password: mode 600, owner `mt5`. Anyone with root or `mt5` access can use the logged-in account, so treat SSH keys accordingly.
