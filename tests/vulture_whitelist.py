"""Names vulture can't see are used — fed to it as an extra source file by
tests/test_deadcode.py, never imported or run.

Vulture is a static reader: a name reached only through `getattr(obj, name)`,
where `name` is a string built or passed at runtime, looks unused to it. Every
entry below is such a case, and each says which code does the dynamic lookup.
Mentioning a name here marks it live, so keep the list tight: a name that stops
being reachable must show up as dead code, not hide in here forever.

The form is vulture's own (`--make-whitelist`): a bare name marks a variable or
attribute; `_.attr` marks an attribute. This file is not valid to execute — the
names resolve to nothing, hence the blanket F821 waiver. Only vulture parses it.
"""
# ruff: noqa: F821  (undefined names are the point: vulture only ever reads them)

# ── config.ThresholdConfig ────────────────────────────────────────────────────
# Thresholds are looked up by the item's own name: `getattr(cfg.thresholds,
# hist_name)` in traces.spark_html, and items._thr(name)'s `getattr(cfg
# .thresholds, name)` via the registry's `_thr("gpu_nvidia_mem_usage")` etc.
# Most fields are also named literally somewhere; these never are.
cpu_spark
mem_spark
gpu_nvidia_mem_usage
gpu_amd_mem_usage

# ── config.SensorOverrides ────────────────────────────────────────────────────
# The manual hwmon specs are read by index in a loop:
# `getattr(ovr, f"hd{i}_temp")` (sensors._read_hd_temps) and
# `getattr(ovr, f"fan{i}_speed")` (sensors._read_fan_speeds).
fan1_speed
fan2_speed
fan3_speed
fan4_speed
hd1_temp
hd2_temp
hd3_temp
hd4_temp

# ── sensors.Readings ──────────────────────────────────────────────────────────
# Reading fields the registry names as strings, not attributes:
# `value("gpu_mem", ...)` / `value("screen_brightness", ...)` become
# `getattr(r, attr)` in items.value(). The other Readings fields survive only
# because something else happens to mention them literally.
gpu_mem
screen_brightness
_.gpu_mem
_.screen_brightness
# Same for the AMD readings: the registry names them as strings in
# `value("gpu_amd_mem_usage", ...)`, `freq_value("gpu_amd_freq")` and
# `gpu_rpm_value("gpu_amd_fan_speed")`, and formatter._maxed_readings assigns
# them for the canonical width.
gpu_amd_mem_usage
gpu_amd_fan_speed
gpu_amd_freq
_.gpu_amd_mem_usage
_.gpu_amd_fan_speed
_.gpu_amd_freq

# ── daemon._cleanup ───────────────────────────────────────────────────────────
# The signal-handler signature Python calls it with; unused by name, required
# by position.
signum
frame
