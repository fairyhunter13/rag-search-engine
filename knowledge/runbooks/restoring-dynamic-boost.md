---
type: Runbook
resource: knowledge/constraints/this-host-cannot-produce-an-admissible-latency-number.md
title: Restoring Dynamic Boost, and the two files the driver package does not install
description: nvidia-powerd ships as a bare binary on noble with neither its systemd unit nor its D-Bus policy in a place either daemon looks; assembling both raises the enforced limit from 80 W to 130 W, which is worth about +10 W of real draw and not a TGP unlock.
tags: [gpu, power, thermal, measurement, ubuntu]
status: stable
generated: { by: claude/opus-5, at: 2026-08-19T14:45:00Z }
sources:
  - id: lp-2111825
    resource: https://bugs.launchpad.net/ubuntu/+source/nvidia-graphics-drivers-570/+bug/2111825
    title: "LP #2111825 - nvidia-powerd unit missing on noble"
    author: team:ubuntu
  - id: lp-2144603
    resource: https://bugs.launchpad.net/ubuntu/+source/nvidia-graphics-drivers-595/+bug/2144603
    title: "LP #2144603 - the same omission on the 595 series"
    author: team:ubuntu
  - id: debian-1118399
    resource: https://bugs.debian.org/cgi-bin/bugreport.cgi?bug=1118399
    title: "Debian #1118399 - the D-Bus policy the daemon looks for"
    author: team:debian
---

# Symptom

The GPU holds a fraction of its rated SM clock, the live `Clocks Event Reasons` block reads
**SW Power Cap: Active**, and both thermal slowdowns read Not Active — so it is not heat.
`enforced.power.limit` sits at **80 W** while `power.max_limit` reads **175 W**.

`/proc/driver/nvidia/gpus/<dom:bus:dev.fn>/power` reports `Notebook Dynamic Boost: Supported`, but
`systemctl status nvidia-powerd` answers *"could not be found"*.

# Cause — an Ubuntu packaging gap, fixed upstream but not on noble

`nvidia-kernel-common-595` on **noble installs the daemon and nothing it needs to run**:

```
$ dpkg -L nvidia-kernel-common-595 | grep -Ei 'dbus|powerd'
/usr/bin/nvidia-powerd
/usr/share/doc/nvidia-kernel-common-595/nvidia-powerd.service
```

The unit is under `/usr/share/doc/`, where systemd never looks, and there is **no D-Bus policy in
the package at all** — so once the unit is placed the daemon starts and immediately fails:

```
Error requesting D-Bus name (... not allowed to own the service "nvidia.powerd.server" ...)
```

The same source package on later Ubuntu installs both correctly (`nvidia-dbus.conf` →
`usr/share/dbus-1/system.d`, the unit → `lib/systemd/system`, with `dh_installsystemd`). It is a
noble-only omission, tracked as [LP #2111825](https://bugs.launchpad.net/ubuntu/+source/nvidia-graphics-drivers-570/+bug/2111825)
(RTX 5080 Laptop, 80 W on Ubuntu against 170 W on Fedora, **New**),
[LP #2144603](https://bugs.launchpad.net/ubuntu/+source/nvidia-graphics-drivers-595/+bug/2144603)
(**New**) and [Debian #1118399](https://bugs.debian.org/cgi-bin/bugreport.cgi?bug=1118399)
(**open**). NVIDIA's own forum position: Ubuntu enables `nvidia-powerd` from 25.04.

If `nvidia-driver-595` (non-open) is installed, the policy already exists at
`/usr/share/doc/nvidia-driver-595/nvidia-dbus.conf` — **check before writing your own.**

# Fix

The policy is the upstream 595 file verbatim. Do **not** paste a copy from a pre-2022 post: those
carried bare `send_requested_reply`/`receive_requested_reply` allows under `context="default"`, a
system-wide D-Bus hole fixed in [CVE-2022-31608](https://forums.developer.nvidia.com/t/nvidia-dbus-conf-lead-to-high-security-concerns/215303).

```bash
sudo cp /usr/share/doc/nvidia-kernel-common-595/nvidia-powerd.service /etc/systemd/system/
# the noble doc copy omits this; without it a clean "unsupported" exit is logged
# as a failure and Restart=on-abort churns
sudo sed -i '/^Restart=on-abort/i SuccessExitStatus=1' /etc/systemd/system/nvidia-powerd.service

sudo tee /usr/share/dbus-1/system.d/nvidia-dbus.conf >/dev/null <<'EOF'
<busconfig>
  <type>system</type>
  <policy user="root">
    <allow own="nvidia.powerd.server"/>
  </policy>
  <policy context="default">
    <allow send_destination="nvidia.powerd.server"/>
  </policy>
</busconfig>
EOF
sudo chmod 644 /usr/share/dbus-1/system.d/nvidia-dbus.conf

python3 -c "import xml.dom.minidom;xml.dom.minidom.parse('/usr/share/dbus-1/system.d/nvidia-dbus.conf')"
sudo systemctl reload dbus
systemctl is-active dbus systemd-logind NetworkManager

sudo systemctl daemon-reload && sudo systemctl enable --now nvidia-powerd
```

**Validate the XML and reload before you reboot.** A malformed or zero-byte `nvidia-dbus.conf`
makes dbus-broker refuse to start, taking `systemd-logind` and NetworkManager with it — an
unbootable system ([nixpkgs #545966](https://github.com/NixOS/nixpkgs/issues/545966), on this exact
driver branch). The `is-active` line above is the cheap proof it is safe to reboot.

`/usr/share/dbus-1/system.d/` is the correct directory. `man dbus-daemon` marks `/etc/dbus-1/system.d`
deprecated and reserved for the administrator; upstream's README still says `/etc`, and is legacy.

`nvidia-powerd` shells out to **`lscpu`**, so `util-linux` must be on its PATH.

# Verify — and `power.limit` is not the field

```bash
journalctl -u nvidia-powerd -b            # want: "DBus Connection is established"
nvidia-smi --query-gpu=power.draw,enforced.power.limit,power.default_limit,\
clocks_event_reasons.sw_power_cap --format=csv -l 1
```

**`enforced.power.limit` is the field that moves.** Measured here: **80 W → 130 W**, and
`sw_power_cap` went `Active` → `Not Active`.

`power.limit` is the *software-requested* ceiling, per `man nvidia-smi`. It reads `[N/A]` on a mobile
GPU because no software-settable limit exists on the board — the same reason `nvidia-smi -pl` is
refused. **It reads `[N/A]` before and after a successful fix, so it is not a symptom and not a
check.** A verification built on it reports failure on a working system.

# What to expect, which is less than the headline

Dynamic Boost shifts budget between CPU and GPU. It is worth roughly **+5 to +25 W**, not a TGP
unlock: measured draw here went from 75–80 W to 83–90 W, about +10 W. The 80 W → 175 W gap is the
**platform power mode**, not powerd — and this machine cannot reach it, because
`/sys/firmware/acpi/platform_profile` does not exist, which is also why `power-profiles-daemon`
reports `PlatformDriver: placeholder` and has no channel to the dGPU at all.

If powerd runs and nothing moves, the remaining gates are SBIOS-side and not user-fixable: the NPCF
ACPI device may never bind ([open-gpu-kernel-modules #1162](https://github.com/NVIDIA/open-gpu-kernel-modules/issues/1162),
open), or the SBIOS may veto outright (`"Client (presumably SBIOS) has requested to disable Dynamic
Boost DC controller"`). Record that as the result.

**`nvidia-smi -pl` is not an alternative.** Laptop GPUs answer *"Changing power management limit is
not supported"*. The knob is absent, not root-gated. Do not retry it.

# What it does not fix

**It does not make this host admissible for latency.** At 130 W the card reaches 87 °C, at which
point `SW Power Cap` goes Not Active and **`SW Thermal Slowdown` goes Active**. The budget was the
binding constraint at 80 W; heat is the binding constraint at 130 W.

`msi-wmi-platform` is read-only telemetry (hwmon channels `0444`, no write callback in mainline), so
the fan curve is EC firmware and not tunable from Linux. The noise is the chassis behaving as
reviewed, not a symptom of this.

Recall verdicts taken under the 80 W cap **remain valid** — all arms were capped equally and
recall@k is not a timing metric. Any arm-to-arm *timing* comparison that straddles the change is
void: record which side of it each run fell on.
