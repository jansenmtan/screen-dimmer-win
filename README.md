# screen-dimmer-win

A Windows gamma-ramp dimmer / tint utility (brightness, colour temperature, and an
auto-brightness curve that follows sunrise/sunset). It drives the display through
`SetDeviceGammaRamp`, with a watchdog that re-applies the ramp if another app or a
display event resets it.

Requires Python >= 3.12. Run with `run.ps1` (i.e. `uv run screen-dimmer-win.py`).

---

## Notice for OLED monitor users: G-SYNC / VRR can brighten fullscreen content

**If fullscreen games look washed-out or brighter than the same app windowed, the
cause is probably your monitor's variable refresh rate — not this dimmer.**

### Symptom

- Fullscreen (independent-flip) presentation looks brighter / washed-out versus the
  same content in a window.
- Often accompanied by the brightness *fluctuating* during play.
- Reproduces even with this app fully quit and an identity gamma ramp.

### What we verified

Test setup: ASUS ROG Swift OLED PG27AQWP-W (540 Hz WOLED), NVIDIA GTX 1660,
Windows 11 build 26200, HDR off.

| Test | Result |
|---|---|
| G-SYNC on, fullscreen vs windowed (osu!lazer, The Bazaar) | brighter fullscreen; osu also flickered |
| G-SYNC off | brightness difference gone in both apps |
| G-SYNC back on (osu!lazer) | brighter fullscreen returned, plus visible brightness flicker |
| G-SYNC **off**, identical borderless dark grey (`iflip_test.exe`), monitor-verified 120 Hz vs 540 Hz | noticeably **brighter at 120 Hz**, darker at 540 Hz |
| Gamma ramp readback across the transitions | unchanged — the LUT is not being reset or bypassed |

The decisive point is the last two rows together: **the same pixels at the same gamma
ramp are displayed at different brightness depending only on the panel's refresh rate.**

### Why G-SYNC triggers it

With G-SYNC on, the monitor's refresh rate follows the application's frame delivery
instead of sitting at a fixed rate. On OLED panels, low-level gamma is calibrated for a
particular refresh rate, so a changing refresh rate changes how dark and mid-tones are
displayed. Frame-time variation then shows up as brightness flicker.

This is a display-side effect: the gamma ramp this app writes is intact and unchanged
while it happens. **No fix belongs in this app.**

The measurement above establishes *refresh-dependent output brightness*. It does not by
itself separate the panel's own gamma behaviour from a refresh-dependent conversion in
the GPU/driver path — both remain possible, though the panel is the likely source.

### Workaround

**Disable G-SYNC / Adaptive-Sync** (NVIDIA Control Panel → *Set up G-SYNC* → uncheck
*Enable G-SYNC, G-SYNC Compatible*) and run a fixed refresh rate. On the monitor above
this removed both the steady brightening and the flicker, with no objectionable tearing
noticed at 540 Hz.

If you want adaptive sync back, test it per game — the effect is not equally visible in
every application.

### If you test this yourself

- **Verify the actual refresh rate with the monitor's own OSD, not Windows.** Fullscreen
  applications can request their own display mode: during this investigation the desktop
  was set to 120 Hz while a fullscreen game kept running the monitor at 540 Hz, which
  silently invalidated an A/B comparison.
- Compare identical content at two *verified* refresh rates with G-SYNC off; that is the
  cleanest way to see whether your panel does this.
- Don't trust eyeball comparisons of white on a changing background (simultaneous
  contrast misleads easily). Locked-exposure photos compared with `photo_compare.py`
  are the reliable method used in this repo.

See `IFLIP_HANDOFF.md` for the full investigation history, tooling (`iflip_test.exe`),
and the measurement caveats.
