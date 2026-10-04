# MT5 headless in the trader container

All of this runs inside the `trader` Incus container (`docs/server-setup.md`), not on the host.

There is no GUI access. MT5 is a Windows GUI program, so it still needs *a* display: it draws on a virtual one (Xvfb :99) that nobody looks at. Login and the EA come from a start config, and the EA hands its data over as files, so no clicking is ever needed. Everything you see goes through Telegram (`docs/telegram.md`).

```
trader container
├─ mt5-xvfb       Xvfb :99 (virtual screen, never viewed)
├─ mt5-wm         openbox (tiny; keeps MT5 windows and dialogs sane)
├─ mt5-terminal   wine terminal64.exe /portable /config:Config\startup.ini
│                   └ BarPusher EA ── JSON files ──▶ /var/spool/signal-mt5
└─ signal-service reads the spool every 2 s, uvicorn 127.0.0.1:8000, Telegram bot (long polling)
```

## Install
Run `deploy/deploy.sh` first (it creates the user `signal`), then:
1. `cp deploy/mt5/startup.ini.example deploy/mt5/startup.ini` and fill in `Login`, `Password`, `Server` (FTMO free trial first). Do this yourself on the server; don't paste the password into any chat.
2. Optional: `cp deploy/mt5/BarPusher.set.example deploy/mt5/BarPusher.set` to change EA inputs. It holds no secret.
3. `sudo deploy/mt5/install.sh` — read it first. It:
   - installs WineHQ stable **10.0** (pinned with `apt-mark hold`), Xvfb, openbox, ImageMagick. Not Wine 11: with it the MT5 installer and terminal stop with "A debugger has been found running in your system". Before any Wine upgrade, snapshot and test that MT5 still starts.
   - disables Wine's Mono/Gecko install prompts (`WINEDLLOVERRIDES=mscoree,mshtml=`): on a display nobody watches they block forever
   - creates user `mt5`, starts the virtual display, sets Wine to Windows 10
   - installs MT5 silently (`mt5setup.exe /auto`), then runs FTMO's installer once only to take its server list (`Config/servers.dat`); the generic installer does not know `FTMO-Demo`, so login would never start
   - creates the spool folder `/var/spool/signal-mt5` (owner `mt5`, group `signal`, mode 2770) and links `MQL5\Files\signal` to it
   - copies the EA, compiles it headless with MetaEditor, installs `startup.ini` (mode 600) and the preset
   - enables `mt5-terminal` and shreds the plaintext `startup.ini` in `deploy/mt5/`

## Quirks this setup works around (MT5 build 6235, Wine 10.0)
- **`/portable`** on terminal and MetaEditor: otherwise MT5 keeps its data in AppData and ignores the folders `install.sh` fills.
- **Relative paths** in `/config:` and `/compile:`: Wine quotes arguments that contain spaces and MT5 then reads the closing quote as part of the path (or MetaEditor exits silently).
- **`Config`, capital C**: the folder the installer creates. Linux is case-sensitive, so a second `config` would be a different folder.
- **No WebRequest**: the allow-list for `WebRequest` can only be entered in Tools > Options (stored encrypted), and every start with a start config resets it. Hence the file spool (`docs/protocol.md` §0).
- **Duplicate charts**: `[StartUp]` opens a new chart on every start and MT5 also restores the old one. BarPusher closes older GER40 M5 charts when it starts, so only one instance runs.
- **Graceful stop**: `systemctl stop mt5-terminal` first asks MT5 to close (`taskkill` without `/f`), so it saves profile and history, then waits for Wine.
- **MT5's own MCP server** (Tools > Options > MCP, "Enable internal server", 127.0.0.1:22346) lets AI tools control the terminal, including trading. It was switched off by hand in the GUI. After a reinstall, check that `ss -tlnp` shows nothing on 22346.

## What startup.ini does
- `[Common]` logs in to the FTMO account.
- `[Experts]` turns **algorithmic trading off** (`Enabled=0`, `AllowLiveTrading=0`). BarPusher keeps running; it only writes files. MT5 applies this on every start. If a heartbeat ever reports algo trading as on, Telegram shows a red alert.
- `[StartUp]` opens a GER40 M5 chart and attaches BarPusher with `BarPusher.set`.

So every `systemctl restart mt5-terminal` comes back logged in with the EA running.

## When something looks wrong
- Telegram reports it first (no heartbeat, disconnected, stale data, algo trading on).
- `/screenshot` in Telegram shows the virtual display, e.g. a login error or an update dialog.
- Server side: `journalctl -u mt5-terminal -f`, and the MT5 logs in `/home/mt5/.mt5/drive_c/Program Files/MetaTrader 5/logs/` (terminal) and `MQL5/logs/` (EA) (UTF-16: `iconv -f UTF-16 -t UTF-8`).
- Spool: `ls /var/spool/signal-mt5` should be empty or nearly; files that failed validation are in `rejected/`.
- Emergency GUI, only if ever needed: `apt install x11vnc`, run `sudo -u mt5 x11vnc -display :99 -localhost -once`, connect through `ssh -L 5900:127.0.0.1:5900`. Remove it afterwards.

## Operations
- Wine is pinned. Before upgrading Wine, MT5 or the OS: on the host, `incus snapshot create trader before-<what>`; test outside trading hours; `incus snapshot restore` if it breaks.
- MT5 updates itself; Telegram will show if the EA stops reporting afterwards.
- Memory: MT5 under Wine uses roughly 300–600 MB; the container's 8 GiB limit leaves plenty of room.

## Security
- Nothing new is public: Xvfb doesn't listen on TCP, the API binds to 127.0.0.1, the bot uses outbound long polling, and the spool is a local folder only `mt5` and `signal` can open.
- Firewall: only SSH (keys only) from outside.
- `startup.ini` holds the FTMO password: mode 600, owner `mt5`. Anyone with root or `mt5` access can use the logged-in account, so treat SSH keys accordingly. The service user `signal` cannot read it.
