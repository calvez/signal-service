# Server setup — bare-metal host + Incus container

Target: Hetzner bare metal in Finland (Intel i7-7700, 4 cores / 8 threads, 64 GB RAM (62 GiB usable), 3 NVMe drives (2 × 512 GB + 1 × 1 TB) in software RAID: `/` is a RAID5 `md2` of ~888 GB, `/boot` RAID1, swap RAID1), host OS **Ubuntu 26.04** (already installed). The host stays minimal; everything for this project lives in one Incus container called `trader`. Inside it, the layout from the rest of the docs applies unchanged (MT5 and the service talk over 127.0.0.1).

```
Host: Ubuntu 26.04, SSH + Incus only, firewall: SSH in, nothing else
└─ incus container "trader" (Ubuntu 24.04 — deliberately, see §4)
   ├─ Xvfb + openbox + MT5 under Wine      (deploy/mt5/)
   ├─ signal-service on 127.0.0.1:8000     (this repo)
   └─ outbound only: FTMO MT5 server, OpenRouter, Telegram
```

All steps below are run by Lorant (or by Claude Code on the host, with Lorant's go-ahead for each block).

## 1. Host OS
Already done: Ubuntu 26.04, installed and updated. Nothing in this project depends on the host's Ubuntu version; the container pins its own (§4).

## 2. Harden the host
```bash
apt update && apt full-upgrade -y
apt install -y chrony unattended-upgrades ufw
timedatectl set-timezone UTC                       # bar timestamps depend on a correct clock
# SSH: keys only
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/; s/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
systemctl reload ssh
ufw default deny incoming && ufw default allow outgoing
ufw allow OpenSSH && ufw enable
```
Check you can still log in with your key from a second terminal before closing the first one.

> ufw and Incus: Incus manages its own nftables rules for `incusbr0`. If containers lose internet after enabling ufw, allow forwarding for the bridge: `ufw route allow in on incusbr0` and `ufw route allow out on incusbr0`, then `ufw reload`.

## 3. Incus with a btrfs pool
The disks are fully used by the OS install (RAID5 ext4 root), so the pool is a btrfs **loop file**. That's fine at this load and still gives instant snapshots. RAID5 protects against one drive failing but is not a backup; §6 still applies.
```bash
apt install -y incus
incus admin init
#   clustering: no
#   new storage pool: yes, backend btrfs, create a loop device, size 200GiB
#   new bridge incusbr0 with NAT: yes; IPv6: your choice
#   make Incus available over the network: NO
```
Snapshots are the main reason for the container: snapshot before every Wine or MT5 change, roll back in seconds.

## 4. The `trader` container
```bash
# 24.04 on purpose: WineHQ packages and deploy/mt5/install.sh target noble, and the stack uses Python 3.12.
incus launch images:ubuntu/24.04 trader
incus config set trader limits.cpu=4 limits.memory=8GiB
incus config set trader boot.autostart=true
incus config set trader snapshots.schedule=@daily snapshots.expiry=14d snapshots.pattern="daily-%d"
# Ubuntu 26.04 (kernel 7.0) AppArmor otherwise blocks signals between processes inside the
# container: systemd there could not stop Wine/MT5 ("Failed to kill control group: Permission
# denied"). This allows signals only between processes of this container. Restart to apply.
incus config set trader raw.apparmor='signal (send) peer="incus-trader_**",'
incus restart trader
incus exec trader -- bash -c 'apt update && apt full-upgrade -y && timedatectl set-timezone UTC || true'
```
- No proxy devices, no inbound ports: nothing in the container is reachable from outside.
- Copy the repo in: `incus file push -r signal-service trader/root/` (or `tar … | incus exec trader -- tar -x …`)
- Work inside: `incus exec trader -- bash`, then install Claude Code there and run it in `/root/signal-service`.
- MT5 under Wine and Xvfb run fine in an unprivileged container with the AppArmor rule above.
- Inside the container, `rsync` hangs on exit on this host; `deploy/deploy.sh` copies with `tar`.
- Processes started with `incus exec` cannot be killed from another `incus exec` session (same AppArmor mediation). Start long jobs with `systemd-run` inside the container so `systemctl stop` works, or kill them from the host (container UID + 1000000).

## 5. Snapshot routine
```bash
incus snapshot create trader before-wine-upgrade     # before any Wine/MT5/system upgrade
incus snapshot list trader
incus snapshot restore trader before-wine-upgrade    # rollback (stops/starts the container)
```
Do this before every change that could break MT5, and never during a session window.

## 6. Off-server backups (RAID5 survives one drive failure, not deletion, corruption or a lost server)
What matters most is the signal log in SQLite (weeks of evidence), then the container itself.
- **Nightly**: `deploy/backup.sh` (run in the container by a systemd timer) makes a consistent SQLite copy with `.backup` and pushes it with **restic** to a Hetzner Storage Box over SFTP. Encrypted; restic password in the container's `/root/.restic-env`, mode 600.
- **Weekly**: on the host, `incus export trader /var/backups/trader-$(date +%F).tar.gz --instance-only`, then copy it to the Storage Box and keep the last 4.
- **Test a restore** once, in a scratch container, before relying on it.

## 7. Checklist before the first session
- [ ] Host: key-only SSH, ufw active, only 22/tcp open (`ss -tlnp` shows nothing else public)
- [ ] `trader` container autostarts after a host reboot, MT5 comes back logged in, Telegram reports recovery
- [ ] Daily snapshots appear in `incus snapshot list trader`
- [ ] Nightly restic backup ran and a test restore worked
- [ ] Clock: `chronyc tracking` on the host shows sync
