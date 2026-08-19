---
type: Runbook
resource: knowledge/constraints/this-host-cannot-produce-an-admissible-latency-number.md
title: Restoring Dynamic Boost, and the two files the driver package does not install
description: The laptop GPU sits at 46% of its power budget because nvidia-powerd is shipped as a bare binary with neither a systemd unit nor a D-Bus policy; assembling both raises the enforced limit from 80 W to 130 W.
tags: [gpu, power, thermal, measurement, ubuntu]
status: stable
generated: { by: claude/opus-5, at: 2026-08-19T14:30:00Z }
---

# Symptom

Every latency number on this host is a host measurement. The GPU holds ~15% of rated SM clock, and
the live `Clocks Event Reasons` block reads **SW Power Cap: Active** with both thermal slowdowns
**Not Active** — so it is not heat. `nvidia-smi -q -d POWER` shows a **Current Power Limit of 80 W**
against a **Max Power Limit of 175 W**.

`/proc/driver/nvidia/gpus/0000:01:00.0/power` reports `Notebook Dynamic Boost: Supported`, but
`systemctl status nvidia-powerd` answers *"could not be found"*. Nothing is negotiating the budget,
so the board sits on its floor.

# Cause

Ubuntu 24.04's `nvidia-kernel-common-595` installs the daemon and **nothing it needs to run**:

```
$ dpkg -L nvidia-kernel-common-595 | grep -Ei 'dbus|powerd'
/usr/bin/nvidia-powerd
/usr/share/doc/nvidia-kernel-common-595/nvidia-powerd.service
```

The systemd unit is under `/usr/share/doc/`, where systemd never looks. There is no D-Bus policy in
the package at all, so even once the unit is in place the daemon starts and immediately fails:

```
Error requesting D-Bus name (... not allowed to own the service "nvidia.powerd.server" ...)
Failed to acquire D-Bus name
```

Both files must be placed by hand. Ubuntu ships the unit enabled only from 25.04.

# Fix

```bash
sudo cp /usr/share/doc/nvidia-kernel-common-595/nvidia-powerd.service /etc/systemd/system/
sudo tee /usr/share/dbus-1/system.d/nvidia-dbus.conf >/dev/null <<'EOF'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
    <policy user="root">
        <allow own="nvidia.powerd.server"/>
        <allow send_destination="nvidia.powerd.server"/>
        <allow receive_sender="nvidia.powerd.server"/>
    </policy>
    <policy context="default">
        <deny own="nvidia.powerd.server"/>
        <deny send_destination="nvidia.powerd.server"/>
    </policy>
</busconfig>
EOF
sudo chmod 644 /usr/share/dbus-1/system.d/nvidia-dbus.conf
sudo systemctl daemon-reload && sudo systemctl reload dbus
sudo systemctl enable --now nvidia-powerd
```

The policy grants ownership to `root` only and denies everyone else, so it adds no reachable surface
beyond the daemon systemd already runs as root.

# Verify — and do not read `power.limit`

```bash
journalctl -u nvidia-powerd -n 5   # want: "DBus Connection is established"
nvidia-smi -q -d POWER | grep -E 'Current Power Limit|Max Power Limit'
```

Measured here: **80 W → 130 W**, against a 175 W maximum.

**`nvidia-smi --query-gpu=power.limit` is the wrong probe.** On this laptop it prints `[N/A]` both
before and after the fix, because that field maps to *Requested* Power Limit, which Dynamic Boost
never sets — the negotiated value only appears as **Current Power Limit** in the `-q -d POWER`
block. A check built on `--query-gpu=power.limit` reports failure on a working system.

# Two fixes that cannot work here

* **`nvidia-smi -pl`** — laptop GPUs answer *"Changing power management limit is not supported in
  current scope"*. The knob is absent, not root-gated. Do not retry it.
* **`power-profiles-daemon`** — `/sys/firmware/acpi/platform_profile` does not exist on this
  machine, which is why PPD reports `PlatformDriver: placeholder`. It has no channel to the dGPU and
  is CPU-only here.

`msi-wmi-platform` is read-only telemetry (hwmon channels `0444`, no write callback in mainline), so
the fan curve is EC firmware and not tunable from Linux either. The noise this produces is the
chassis behaving as reviewed, not a symptom of this problem.

# What it does not fix

Recall verdicts taken under the 80 W cap **remain valid** — all arms were capped equally and
recall@k is not a timing metric. Latency verdicts taken before the fix stay inadmissible, and any
arm-to-arm *timing* comparison that straddles the change is void: record which side of it each run
fell on.
