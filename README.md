# signal-service

Phase-1 market observer: MT5 pushes closed bars → Python computes Brooks features → an LLM reads the context → validated alerts go to Telegram. **No trading in phase 1.**

## Files
- `CLAUDE.md` — project guide and non-negotiables (Claude Code reads this first)
- `docs/protocol.md` — HTTP payloads, LLM response schema, validation rules, alert format
- `docs/tasks-phase1.md` — ordered build tasks T0–T10
- `config.example.yaml` — symbols, sessions, rules, LLM settings
- `prompts/market_read_v1.md` — first version of the market-read prompt
- `mt5/BarPusher.mq5` — the EA (data only, no order code)
- `docs/telegram.md` — the bot: alerts, statuses, commands, risk and ops warnings (the only UI)
- `deploy/mt5/` — headless MT5: install script, start config, EA preset, systemd units
- `docs/mt5-linux.md` — MT5-on-Linux setup and operations
- `docs/server-setup.md` — bare-metal host, Incus `trader` container, snapshots, off-server backups
- `deploy/backup/` — nightly restic backup script and systemd timer

## Getting started
1. Host and container: follow `docs/server-setup.md` §2–4 (host is Ubuntu 26.04: hardening, Incus with a btrfs pool, container `trader` on Ubuntu 24.04).
2. `incus file push -r signal-service trader/root/` and `incus exec trader -- bash`.
3. Inside the container: install Claude Code, `cd /root/signal-service && claude`.
4. Tell it: *"Read CLAUDE.md and the docs, then do T0 and T1. Stop after T1 and report."*
5. Review, then continue task by task. Tasks marked **ASK** need your answer.

## MetaTrader 5 in the container
Headless MT5 under Wine on a virtual display; no GUI, no VNC. Fill in `deploy/mt5/startup.ini` and `BarPusher.set` from the examples, then `sudo deploy/mt5/install.sh` (details in `docs/mt5-linux.md`). Everything you see afterwards comes through Telegram.

Start with the FTMO free trial account, not the paid challenge.
