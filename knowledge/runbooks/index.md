# Runbook

* [Restoring Dynamic Boost, and the two files the driver package does not install](restoring-dynamic-boost.md) - nvidia-powerd ships as a bare binary on noble with neither its systemd unit nor its D-Bus policy in a place either daemon looks; assembling both raises the enforced limit from 80 W to 130 W, which is worth about +10 W of real draw and not a TGP unlock.
* [Restoring the registry, the one file that cannot be re-derived](restoring-the-registry.md) - projects.json holds which projects are indexed, which roots claim them and which are enabled; the indexes can be rebuilt from disk and it cannot, so it is backed up on every write.
* [Running anything that touches the real GPU](running-the-live-suite.md) - Two preconditions — a clear lock and ≥10 GiB free VRAM — the one-at-a-time rule, and the switch that separates starting the daemon from starting an overnight fleet index.
