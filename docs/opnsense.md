# OPNsense installation

This guide installs `airvpn-picker` on a running OPNsense host so it
can be invoked from the GUI cron plugin every 30 minutes (or any
schedule you prefer). It assumes:

- You already have an AirVPN WireGuard tunnel configured on OPNsense
  (e.g. `wg2`) with a single peer.
- You know that peer's public key.
- You have shell access to OPNsense as `root` (via SSH or the
  hardware console).

The whole flow takes about five minutes.

## 1. Prerequisites

Verify Python 3.11 is available — OPNsense 25.x ships with it but the
binary is version-pinned (no generic `python3` symlink):

```sh
ls /usr/local/bin/python3.11
```

If that file does not exist, you are on an older OPNsense release;
upgrade before continuing or install `python311` from `pkg`.

You also need the `wg` userspace tool, which is part of any working
WireGuard installation on OPNsense:

```sh
which wg
```

## 2. Install the picker

You have two options. Pick whichever is easier for your workflow.

### Option A: install from a release tarball (recommended)

Download the latest release tarball from
[GitHub Releases](https://github.com/alfredodeza/airvpn-wireguard-picker/releases),
verify its checksum, and run the bundled installer:

```sh
cd /tmp
fetch https://github.com/alfredodeza/airvpn-wireguard-picker/releases/download/vX.Y.Z/airvpn-wireguard-picker-vX.Y.Z.tar.gz
fetch https://github.com/alfredodeza/airvpn-wireguard-picker/releases/download/vX.Y.Z/airvpn-wireguard-picker-vX.Y.Z.tar.gz.sha256
sha256 -c airvpn-wireguard-picker-vX.Y.Z.tar.gz.sha256 airvpn-wireguard-picker-vX.Y.Z.tar.gz
tar xzf airvpn-wireguard-picker-vX.Y.Z.tar.gz
cd airvpn-wireguard-picker-vX.Y.Z
./contrib/install-opnsense.sh ./airvpn-picker.pyz '<your-peer-pubkey>'
```

The installer:

1. Copies `airvpn-picker.pyz` to `/usr/local/bin/airvpn-picker` (mode 0755).
2. Installs the configd action file with your peer pubkey substituted in.
3. Runs `service configd restart` so the action becomes available.
4. Creates `/var/log/airvpn-picker.log` and the parent dir for the state file.
5. Prints the next manual step (the GUI cron entry).

### Option B: install manually

If you prefer to do it by hand, the steps are:

```sh
# 1. Drop the script somewhere on PATH
install -m 0755 airvpn-picker.pyz /usr/local/bin/airvpn-picker

# 2. Install the configd action file. Edit the [run] command line
#    to substitute your real peer pubkey for REPLACE_WITH_PUBKEY.
cp contrib/actions_airvpnpicker.conf \
   /usr/local/opnsense/service/conf/actions.d/actions_airvpnpicker.conf
vi /usr/local/opnsense/service/conf/actions.d/actions_airvpnpicker.conf

# 3. Reload configd so the new action is registered
service configd restart

# 4. Make sure the log/state directories exist
touch /var/log/airvpn-picker.log
mkdir -p /var/db
```

## 3. Test it manually

The picker runs as root (required for `wg set`). The configd action
runs commands as root automatically, so this is the right test:

```sh
configctl airvpnpicker run
```

You should see one JSON line appended to the log file:

```sh
tail -n 5 /var/log/airvpn-picker.log
```

If the action prints `Action not found`, configd has not picked up
the new action file. Re-run `service configd restart` and try again.

You can also call the binary directly with `--dry-run` to inspect a
decision without actually changing the tunnel:

```sh
/usr/local/bin/airvpn-picker \
  --interface wg2 \
  --peer-pubkey '<your-peer-pubkey>' \
  --dry-run \
  --log-level DEBUG
```

This is the safest way to test new flag combinations.

## 4. Verify the tunnel actually changed

```sh
wg show wg2 endpoints
```

Compare the IP shown to the `winner.ip` field in the most recent log
entry. They should match. If they do, the tunnel is now pointed at
the picker's chosen server, and existing TCP flows on the tunnel
survived the swap because the interface was never restarted.

## 5. Schedule it from the GUI

Once the manual run works:

1. Log in to the OPNsense web UI.
2. Navigate to **System → Settings → Cron**.
3. Click the **+** button to add a new cron job.
4. Fill in:
   - **Enabled**: ✓
   - **Minutes**: `*/30`
   - **Hours**: `*`
   - **Days of the month**: `*`
   - **Months**: `*`
   - **Days of the week**: `*`
   - **Command**: select **AirVPN Picker: pick fastest server**
     (this is the `description:` field from the configd action file)
   - **Description**: `Pick fastest AirVPN server every 30 min`
5. Save and apply.

The picker will now run every 30 minutes. Most runs will be no-ops
because of the hysteresis threshold; that is the intended behavior.

## 6. Observe

```sh
# follow the log live
tail -f /var/log/airvpn-picker.log

# inspect the last decision in human-readable form
cat /var/db/airvpn-picker.json

# what endpoint is the kernel actually using right now?
wg show wg2 endpoints
```

A typical week of output should show many `noop` lines for
`already-on-winner` or `below-hysteresis`, with the occasional
`switch` line when a meaningfully better server appears.

## 7. Troubleshooting

### "Action not found" from configctl

Configd has not registered the action file. Check:

```sh
ls -la /usr/local/opnsense/service/conf/actions.d/actions_airvpnpicker.conf
service configd restart
configctl airvpnpicker run
```

If it still does not appear, look at `/var/log/configd.log` for parse
errors in the action file (most commonly a typo in `command:` or a
stray blank line inside the section).

### "Unable to access interface: No such file or directory"

The picker is trying to talk to `wg2` but the interface does not
exist. Either:

- Edit the `actions_airvpnpicker.conf` `command:` line to point at the
  correct interface (`wg0`, `wg1`, etc.) and reload configd.
- Or fix the underlying WireGuard config so `wg2` exists.

### Picker logs `current-unhealthy` and switches every cycle

This is *not* hysteresis flapping. It means the IP your tunnel is
pointed at is no longer in any healthy candidate's IP set — usually
because:

- The server you were connected to went into `warning` or `error`.
- `--max-load` is set very tight (e.g. 30) and your current server
  drifted above it.
- AirVPN rotated server IPs and the old one is no longer advertised.

The picker is doing exactly the right thing in all of those cases.
The "every cycle" cadence ends as soon as the picker gets to a
healthy server.

### Picker logs `below-hysteresis` forever and never switches

This is *also* working as intended. It means your current server is
healthy and the best alternative is only marginally better. If you
want the picker to be more aggressive, lower `--hysteresis-pp`. Be
aware that values below ~10 will cause noticeable flapping.

### How do I force a switch right now?

```sh
configctl airvpnpicker run
```

…with a temporarily lower `--hysteresis-pp` baked into the action
file, or simply edit the action file to point at a known-good
endpoint manually with `wg set` and re-run.

## 8. Uninstall

```sh
# remove the cron job from the GUI first, then:
rm -f /usr/local/bin/airvpn-picker
rm -f /usr/local/opnsense/service/conf/actions.d/actions_airvpnpicker.conf
service configd restart

# optionally clean up state and logs
rm -f /var/log/airvpn-picker.log /var/db/airvpn-picker.json
```
