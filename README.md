<p align="center">
  <img src="assets/raf-banner.svg" alt="RAF MULTIMEDIA" width="880"/>
</p>

<h1 align="center">RAF STREAM</h1>
<p align="center">
  <b>DJI Mini 3 live HDMI bridge on a &#36;15 Android TV box.</b><br/>
  RC-N1 &rarr; USB gadget &rarr; FiberHome HG680P &rarr; hardware decode &rarr; HDMI.
</p>

---

**Free version — limited feature set.**

## What it does

A FiberHome HG680P set-top box (Amlogic S905X, Armbian) impersonates an
Android Open Accessory host toward the DJI RC-N1, relays the
DUML/RCLink/LogicLink session, taps the Mini 3 video stream, decodes in
hardware (meson-vdec), and drives HDMI via KMS/DRM.

- **RC plugged** = auto-switch to LIVE at the next keyframe
- **RC unplugged** = back to standby splash, no menu needed
- **DJI Fly (phone)** works concurrently via the MITM path
- Optional RTSP mirror on `:8554`

Not in this release: web UI, playlist upload, hotspot config page —
the bridge only.

## Docs that matter

| File | Contents |
|---|---|
| `RESEARCH.md` | protocol findings, captures, packet anatomy |
| `BLUEPRINT.md` | component contracts, state machines, failure matrix |
| `REGRESSIONS.md` | every production bug → root cause → fix → smoke check |
| `AGENTS.md` | two-path deploy layout, verification evidence table |
| `stb/` | install_services.sh, units, watchdog, smoke_test.sh |

## Requirements

- FiberHome HG680P (or another Amlogic S905X box) running Armbian from
  [ophub/amlogic-s9xxx-armbian](https://github.com/ophub/amlogic-s9xxx-armbian) —
  tested image:
  [`Armbian_26.11.0_amlogic_s905x_bookworm_6.12.112_server_2026.10.15.img.gz`](https://github.com/ophub/amlogic-s9xxx-armbian/releases/download/Armbian_bookworm_arm64_server_2026.10/Armbian_26.11.0_amlogic_s905x_bookworm_6.12.112_server_2026.10.15.img.gz).
  That kernel ships `raw_gadget.ko`, so the USB gadget path needs no
  out-of-tree build
- DJI RC-N1 controller + Mini 3
- SSH access to the box

## Wiring

HG680P has two USB ports with different roles — this matters:

- **USB-2** (the flashing/OTG port) = device mode, `dwc2` → **plug the
  RC-N1 here**: USB-A male ↔ USB-C male cable to the RC's **top** port
  (the cradle/phone port)
- **USB-1** = host-only (`xhci`) — cannot act as a gadget; the RC will
  never enumerate there

## Install (Python stack — one command)

```bash
./deploy.sh root@<stb-ip>
```

Pushes the `stb/` bundle, runs `install_services.sh` remotely (apt
deps → gadget/udev → files → units → enable), then the on-box smoke
test. `SKIP_INSTALL=1` / `SKIP_SMOKE=1` split the stages. If a
conflicting `dji-display-rs` service is already active on the target,
the installer skips itself — the units `Conflict=` each other by design.

That's the whole flow — install is idempotent, so re-running it is the
upgrade path too.

## License

MIT for the code in this repository ([`LICENSE`](LICENSE)). Bundled
DJI-Link components under `stb/` are Apache-2.0 — see [`NOTICE`](NOTICE)
and [`licenses/Apache-2.0.txt`](licenses/Apache-2.0.txt). DJI, Mini 3 and
RC-N1 are trademarks of SZ DJI Technology Co., Ltd.; this project is
independent and unaffiliated.

---

<p align="center">
  <b>&copy; 2026 RAF MULTIMEDIA</b><br/>
  <sub>Built for the field — test on the ground, propellers off.</sub>
</p>
