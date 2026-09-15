from config import Config, Section, Surface
from formatter import PanelFormatter, _fmt_perc, _net_fmt, _normalize_separators, _val_cell, group_rows_into_blocks
from mono_render import global_width_of
from nl80211 import Rate as WifiRate
from render_model import Ident, Separator
from sensors import HardwareInfo, Readings, DiskUsage, BatterySys, BatteryPeriph
import traces


def _bare_hw(**overrides) -> HardwareInfo:
    """A HardwareInfo with no optional hardware present — the bare-VM baseline
    (only cpu_count/net/disk_io set). Override any field to add hw back."""
    base = dict(
        cpu_temp_path=None, cpu_freq_path=None, hd_temp_paths={}, fan_paths={},
        battery_sys_ids=[], has_nvidia=False, intel_gpu_freq_path=None, intel_gpu_pci=None,
        net_device="enp0s3", disk_io_device="sda2", cpu_count=2,
        cpu_turbo_supported=False, has_backlight=False, has_wifi=False,
    )
    base.update(overrides)
    return HardwareInfo(**base)


# ── _val_cell ─────────────────────────────────────────────────────────────────

def test_val_cell_no_class_is_plain_val():
    cell = _val_cell("12%")
    assert cell.text == "12%"
    assert cell.css_class == "val"
    assert cell.align == "right"


def test_val_cell_with_class_appends_it():
    cell = _val_cell("90%", "crit")
    assert cell.css_class == "val crit"


# ── _fmt_perc ────────────────────────────────────────────────────────────────

def test_fmt_perc_panel_caps_at_100_without_percent_sign():
    assert _fmt_perc(100, tooltip=False) == "100"


def test_fmt_perc_tooltip_always_has_percent_sign():
    assert _fmt_perc(100, tooltip=True) == "100%"


def test_fmt_perc_below_100_has_percent_sign_either_way():
    assert _fmt_perc(42, tooltip=False) == "42%"
    assert _fmt_perc(42, tooltip=True) == "42%"


# ── _net_fmt ─────────────────────────────────────────────────────────────────

def test_net_fmt_zero():
    assert _net_fmt(0) == "0"


def test_net_fmt_kilobits():
    assert _net_fmt(500_000) == "500K"


def test_net_fmt_megabits():
    assert _net_fmt(2_500_000) == "2M"


# ── PanelFormatter static label helpers ──────────────────────────────────────

def test_disk_label_root_mount():
    assert PanelFormatter._disk_label("/") == "Root"


def test_disk_label_strips_mnt_prefix():
    assert PanelFormatter._disk_label("/mnt/data") == "Data"


def test_disk_label_basename_for_run_media():
    assert PanelFormatter._disk_label("/run/media/user/Backup") == "Backup"


def test_middle_ellipsis_short_string_unchanged():
    assert PanelFormatter._middle_ellipsis("MyWifi", 16) == "MyWifi"


def test_middle_ellipsis_keeps_head_and_tail():
    assert PanelFormatter._middle_ellipsis("abcdefgh", 6) == "abc…gh"


def test_middle_ellipsis_never_exceeds_budget():
    for n in range(1, 20):
        assert len(PanelFormatter._middle_ellipsis("FRITZ!Box 7590 Guest", n)) <= n


def test_middle_ellipsis_bounds_ssid_to_max():
    long_ssid = "FRITZ!Box 7590 Guest Network"
    assert len(PanelFormatter._middle_ellipsis(long_ssid, PanelFormatter._SSID_MAX)) == PanelFormatter._SSID_MAX


def test_net_device_ip_truncates_long_interface():
    fmt = PanelFormatter(Config(), _bare_hw())
    r = Readings(net_device="br-1a2b3c4d5e6f7g", ip_address="10.0.0.1")
    text = fmt._net_device_ip(r, tooltip=True)[0][1].text
    capped = PanelFormatter._middle_ellipsis("br-1a2b3c4d5e6f7g", PanelFormatter._NETDEV_MAX)
    assert capped in text and "…" in capped


def test_string_row_caps_net_device_leaves_ip_raw():
    fmt = PanelFormatter(Config(), _bare_hw())
    assert "…" in fmt._string_row("net_device", "br-1a2b3c4d5e6f7g", tooltip=True)[0][1].text
    assert fmt._string_row("ip_address", "255.255.255.255", tooltip=True)[0][1].text == "255.255.255.255"


def _canonical_guard_cfg():
    cfg = Config()
    cfg.pages.order = []   # exclude the fixed processes-page fold — test the main page's maxed width alone
    cfg.tooltip = Surface(sections=[Section(key="io", title="IO",
        items=["net_device_ip", "wifi_ssid_signal", "load_avg", "uptime", "cpu_freq", "cpu_temp"])])
    return cfg


def test_canonical_width_exceeds_short_content():
    from formatter import group_rows_into_blocks
    from mono_render import global_width_of
    fmt = PanelFormatter(_canonical_guard_cfg(), _bare_hw(net_device="wlan0", has_wifi=True,
                                                          cpu_temp_path="/x", cpu_count=8))
    r = Readings(net_device="wlan0", ip_address="10.0.0.1", wifi_ssid="Home", wifi_signal=50,
                 load_avg=(0.1, 0.2, 0.3), uptime=60, cpu_freq=800, cpu_temp=40)
    actual = global_width_of(group_rows_into_blocks(fmt._build_entries(r, tooltip=True)), 0)
    assert fmt.canonical_width(r) > actual   # maxes IPv4/load/uptime/freq/temp → wider than idle content


def _guard_full_hw() -> HardwareInfo:
    """Every piece of hardware present, so every tooltip item's gate passes and
    gets tested."""
    return HardwareInfo(
        cpu_temp_path="/x", cpu_freq_path="/x",
        hd_temp_paths={"nvme0": "/x", "sda": "/x"}, fan_paths={"1": "/x", "2": "/x"},
        battery_sys_ids=["/BAT0"], has_nvidia=True, intel_gpu_freq_path="/x", intel_gpu_pci="0000:00:02.0",
        net_device="wlan0", disk_io_device="nvme0n1", cpu_count=8,
        cpu_turbo_supported=True, has_backlight=True, has_wifi=True, wifi_antennas=2,
        battery_mouse_id="/m", battery_kbd_id="/k",
        disk_smart_drives={"nvme0": ("/d0", "nvme", False), "sda": ("/d1", "ata", True)},
    )


def _guard_readings(hi: bool) -> Readings:
    """Two readings sharing the hardware-fixed / identity fields canonical keeps
    real (interface, SSID, RAM/disk totals, the instance sets), differing only in
    the VOLATILE fields _maxed_readings is meant to max. hi=True is every such
    field at its bound; hi=False the minimum. Multi-instance seeded rows
    (hd_temp/fan/disk_smart) are kept present and equal — they come from hw."""
    DISK_TOTAL = 999
    return Readings(
        cpu_usage=100 if hi else 0, cpu_temp=100 if hi else 0,
        cpu_freq=9999.0 if hi else 400.0, cpu_turbo=True,
        uptime=(999 * 86400 + 23 * 3600 + 59 * 60) if hi else 1,
        load_avg=(8.0, 8.0, 8.0) if hi else (0.0, 0.0, 0.0),
        top_process=[("proc", 99 if hi else 0)],
        mem_usage=100 if hi else 0, mem_used_gb=64 if hi else 0, mem_total_gb=64, swap_usage=100 if hi else 0,
        net_up_bps=999_000_000 if hi else 0, net_down_bps=999_000_000 if hi else 0,
        net_device="wlan0", ip_address="255.255.255.255" if hi else "1.1.1.1",
        wifi_ssid="Home", wifi_signal=100 if hi else 0,
        wifi_chains=[-100, -100] if hi else [-1, -1],
        wifi_tx=WifiRate(9999.9, 13, 8) if hi else WifiRate(1.0, 0, 1),
        wifi_rx=WifiRate(9999.9, 13, 8) if hi else WifiRate(1.0),
        disk_read_bps=999_000_000 if hi else 0, disk_write_bps=999_000_000 if hi else 0,
        disk_usage={"/mnt/DataStore": DiskUsage(100 if hi else 0, DISK_TOTAL if hi else 0, DISK_TOTAL)},
        disk_smart={"nvme0": True, "sda": True},
        hd_temps={"nvme0": 100, "sda": 100}, fan_speeds={"1": 9999, "2": 9999},
        battery_sys=[BatterySys(id="/BAT0", perc="100%" if hi else "0%",
                                rate=99 if hi else 0, state="discharging", limit=80)],
        battery_mouse=BatteryPeriph("Mouse", "100%" if hi else "0%"),
        battery_kbd=BatteryPeriph("Kbd", "100%" if hi else "0%"),
        gpu_temp=100 if hi else 0, gpu_usage=100 if hi else 0, gpu_mem=100 if hi else 0,
        gpu_dec=100 if hi else 0, gpu_fan=100 if hi else 0,
        gpu_intel_freq=9999 if hi else 0, gpu_intel_usage=100 if hi else 0, gpu_intel_dec_usage=100 if hi else 0,
        screen_brightness=100 if hi else 0, system_updates=999 if hi else 0, server_ok=True,
    )


def _tooltip_tokens():
    """Every metric[:form] token eligible for the tooltip surface — the same
    enumeration `pirostats list-items` uses. So the guard below auto-covers any
    item added later, no hand-maintained list."""
    from metrics import METRICS, item_surfaces
    from forms import Form, Surface as FormSurface
    toks = []
    for metric, m in METRICS.items():
        forms = [None] if m.intrinsic_shape is not None else list(m.forms)
        for form in forms:
            if item_surfaces(metric, form) & FormSurface.TOOLTIP:
                toks.append(metric if form in (None, Form.VALUE) else f"{metric}:{form.value}")
    return toks


def test_canonical_width_covers_every_tooltip_item_guard():
    """Registry-driven guard: for EVERY tooltip item, rendered alone so its row is
    the whole width, canonical computed from an all-minimal reading must cover a
    render of an all-WIDE reading. A width-driving field added to an item but
    forgotten in _maxed_readings makes canonical(minimal) fall short here — a red
    test instead of a silent tooltip resize in production. Auto-covers new items."""
    hw = _guard_full_hw()
    lo, hi = _guard_readings(hi=False), _guard_readings(hi=True)
    failures = []
    for token in _tooltip_tokens():
        cfg = Config()
        cfg.pages.order = []                     # no fixed processes-page fold
        cfg.tooltip = Surface(sections=[Section(key="s", title="S", items=[token])])
        fmt = PanelFormatter(cfg, hw)
        wide = global_width_of(group_rows_into_blocks(fmt._build_entries(hi, tooltip=True)), 0)
        if wide == 0:
            continue                                       # item renders nothing with this fixture — skip
        canon = fmt.canonical_width(lo)
        if canon < wide:
            failures.append(f"{token}: canonical {canon} < wide render {wide}")
    assert not failures, "unmaxed width-driving field(s) in _maxed_readings:\n  " + "\n  ".join(failures)


def test_hd_label_strips_trailing_index():
    assert PanelFormatter._hd_label("nvme0") == "Nvme"


def test_hd_label_no_trailing_index():
    assert PanelFormatter._hd_label("sda") == "Sda"


def test_hd_label_nvme_namespace_block_device():
    assert PanelFormatter._hd_label("nvme0n1") == "Nvme"


# ── cpu_usage/mem_usage bar+history decoupling ───────────────────────────────
# (regression coverage for an old bug: bar/history used to vanish silently if a
# toggle disagreed with the row builder's own suppression. traces.bar_html/
# traces.spark_html are now the single source of truth, shared by the
# standalone cpu_usage:bar/cpu_usage:spark rows and the combined cpu_usage:bar_spark row.)

def _fmt():
    return PanelFormatter(Config(), hw=None)


def test_std_never_attaches_bar_or_history_for_cpu_usage():
    fmt = _fmt()
    rows = fmt._render_item("cpu_usage", Readings(cpu_usage=73), tooltip=True)
    assert len(rows) == 1
    assert len(rows[0]) == 2  # plain [label, val], never a spanning bar/hist row


def test_bar_html_for_empty_when_value_missing():
    fmt = _fmt()
    fmt._cfg.bar_tooltip.width = 6
    assert traces.bar_html(fmt, None, (50, 70), tooltip=True) == ""


def test_bar_html_for_empty_when_width_zero():
    fmt = _fmt()
    fmt._cfg.bar_tooltip.width = 0
    assert traces.bar_html(fmt, 50, (50, 70), tooltip=True) == ""


def test_spark_html_for_empty_when_history_missing():
    fmt = _fmt()
    assert traces.spark_html(fmt, None, "cpu_spark", tooltip=True) == ""


def test_bar_spark_row_empty_when_only_bar_available():
    """The exact failure mode from the review finding: width=0 disables the
    bar but history is still available — must not produce a half-built row."""
    fmt = _fmt()
    fmt._cfg.bar_tooltip.width = 0
    rows = traces.bar_spark_row(fmt, "cpu", 50, (50, 70), [10, 20], "cpu_spark", tooltip=True)
    assert rows == []


def test_bar_spark_row_renders_when_both_available():
    fmt = _fmt()
    fmt._cfg.bar_tooltip.width = 6
    rows = traces.bar_spark_row(fmt, "cpu", 50, (50, 70), [10, 20], "cpu_spark", tooltip=True)
    assert len(rows) == 1
    assert len(rows[0]) == 4  # label, bar, label, spark


def test_bar_row_and_spark_row_agree_with_bar_spark_row():
    """cpu_usage:bar/cpu_usage:spark (standalone) and cpu_usage:bar_spark (combined) must
    never disagree about whether cpu_usage currently has a bar/history."""
    fmt = _fmt()
    fmt._cfg.bar_tooltip.width = 6
    assert traces.bar_row(fmt, 50, (50, 70), tooltip=True, ident=Ident("cpu_usage", "bar")) != []
    assert traces.spark_row(fmt, [10, 20], "cpu_spark", tooltip=True, ident=Ident("cpu_usage", "spark")) != []
    assert traces.bar_spark_row(fmt, "cpu", 50, (50, 70), [10, 20], "cpu_spark", tooltip=True) != []


# ── Hardware gate (_available) ────────────────────────────────────────────────

def _titles(entries):
    """Section titles among built entries (single 'title'-role cell rows)."""
    out = []
    for e in entries:
        if isinstance(e, list) and len(e) == 1 and e[0].css_class == "title":
            out.append(e[0].text)
    return out


def test_available_hw_bound_items_off_on_bare_machine():
    fmt = PanelFormatter(Config(), _bare_hw())
    r = Readings(swap_usage=None)
    for name in ("cpu_temp", "cpu_turbo", "fan_speed", "battery_sys", "battery_mouse",
                 "battery_kbd", "screen_brightness", "gpu_nvidia_temp", "gpu_intel_usage",
                 "wifi_ssid_signal", "swap_usage"):
        assert fmt._available(name, r) is False, name


def test_available_unbound_items_always_on():
    fmt = PanelFormatter(Config(), _bare_hw())
    r = Readings()
    for name in ("cpu_usage", "mem_usage", "uptime", "load_avg", "top_process",
                 "disk_usage", "cpu_usage:spark_value"):
        assert fmt._available(name, r) is True, name


def test_available_present_hw_turns_item_on():
    from pathlib import Path
    fmt = PanelFormatter(Config(), _bare_hw(
        cpu_temp_path=Path("/x"), has_nvidia=True, has_wifi=True, fan_paths={"1": Path("/y")}))
    r = Readings()
    assert fmt._available("cpu_temp", r)
    assert fmt._available("gpu_nvidia_temp", r)
    assert fmt._available("wifi_ssid_signal", r)
    assert fmt._available("fan_speed", r)


def test_available_battery_periph_via_bolt_config():
    cfg = Config()
    cfg.battery.kbd_bolt = 1
    fmt = PanelFormatter(cfg, _bare_hw())
    assert fmt._available("battery_kbd", Readings()) is True
    assert fmt._available("battery_mouse", Readings()) is False


# ── Section collapse ──────────────────────────────────────────────────────────

def _surface_cfg():
    cfg = Config()
    sections = [
        Section(key="live", title="Live", items=["cpu_usage", "mem_usage"]),
        Section(key="thermal", title="Thermal", items=["cpu_temp", "fan_speed"]),
        Section(key="batteries", title="Batteries", items=["battery_sys"]),
        Section(key="load", title="Load", items=["uptime", "load_avg"]),
    ]
    cfg.tooltip = Surface(sections=sections)
    cfg.panel = Surface(sections=sections)
    return cfg


def test_empty_section_drops_title_and_separator():
    fmt = PanelFormatter(_surface_cfg(), _bare_hw())  # no thermal/battery hw
    r = Readings(cpu_usage=10, mem_usage=20, uptime=60, load_avg=(0.1, 0.2, 0.3))
    entries = fmt._build_entries(r, tooltip=True)
    titles = _titles(entries)
    assert titles == ["Live", "Load"]              # Thermal/Batteries collapsed
    # exactly one separator (between the two rendered sections), none leading
    seps = [e for e in entries if isinstance(e, Separator)]
    assert len(seps) == 1
    assert not isinstance(entries[0], Separator)


def test_first_section_has_no_leading_separator():
    fmt = PanelFormatter(_surface_cfg(), _bare_hw())
    r = Readings(cpu_usage=10, mem_usage=20, uptime=60, load_avg=(0.1, 0.2, 0.3))
    entries = fmt._build_entries(r, tooltip=True)
    assert not isinstance(entries[0], Separator)
    assert entries[0][0].css_class == "title"  # Live title first


def test_panel_has_no_title_rows_and_no_separators():
    fmt = PanelFormatter(_surface_cfg(), _bare_hw())
    r = Readings(cpu_usage=10, mem_usage=20, uptime=60, load_avg=(0.1, 0.2, 0.3))
    entries = fmt._build_entries(r, tooltip=False)
    assert _titles(entries) == []                  # panel never renders titles
    # panel is a continuous strip: sections drive ordering only, no separators
    assert not any(isinstance(e, Separator) for e in entries)


# ── Disk rows hidden without real data ────────────────────────────────────────

def test_hd_temp_row_empty_without_temp():
    # a disk present in hw but with no temperature reading yields no row at all
    # (the per() source filters it out), not a '--' placeholder
    fmt = PanelFormatter(Config(), _bare_hw(hd_temp_paths={"sda": "/sys/sda"}))
    assert fmt._render_item("hd_temp", Readings(hd_temps={}), tooltip=True) == []


# ── top_process: no padding ───────────────────────────────────────────────────

def test_top_process_no_padding_to_fixed_count():
    fmt = PanelFormatter(Config(), _bare_hw())
    r = Readings(top_process=[("plasmashell", 8), ("kwin_wayland", 5)])
    rows = fmt._top_process(r, tooltip=True)
    assert len(rows) == 2  # only processes with measurable CPU, no '--' third row


# ── disk_smart (name + SMART status, 2 drives per row) ───────────────────────

def _hw_disks(labels):
    drives = {l: ("/drive", "nvme", False) for l in labels}
    return _bare_hw(disk_smart_drives=drives)


def test_disk_smart_packs_two_drives_per_row():
    fmt = PanelFormatter(Config(), _hw_disks(["nvme0n1", "sda", "sdb", "sdc"]))
    r = Readings(disk_smart={"nvme0n1": True, "sda": True, "sdb": True, "sdc": False})
    rows = fmt._disk_smart_pair(r, tooltip=True)
    assert len(rows) == 2          # 4 drives / 2 per row
    assert all(len(row) == 4 for row in rows)  # two label/val pairs each


def test_disk_smart_odd_count_uses_blank_filler():
    fmt = PanelFormatter(Config(), _hw_disks(["nvme0n1", "sda", "sdb"]))
    r = Readings(disk_smart={"nvme0n1": True, "sda": True, "sdb": True})
    rows = fmt._disk_smart_pair(r, tooltip=True)
    assert len(rows) == 2
    assert all(len(row) == 4 for row in rows)  # last row padded to keep the shape
    # filler half of the last row is blank (label + value empty)
    assert rows[1][2].text == "" and rows[1][3].text == ""


def test_disk_smart_single_disk_is_full_width_row():
    fmt = PanelFormatter(Config(), _hw_disks(["nvme0n1"]))
    rows = fmt._disk_smart_pair(Readings(disk_smart={"nvme0n1": True}), tooltip=True)
    assert len(rows) == 1
    assert len(rows[0]) == 2          # plain [label, value], status at right edge


def test_disk_smart_single_result_among_many_is_full_width():
    fmt = PanelFormatter(Config(), _hw_disks(["nvme0n1", "sda"]))
    r = Readings(disk_smart={"nvme0n1": True, "sda": None})
    rows = fmt._disk_smart_pair(r, tooltip=True)
    assert len(rows) == 1             # only the one with a result
    assert len(rows[0]) == 2          # full-width, not a 2-pair filler row


def test_disk_smart_empty_when_no_results():
    fmt = PanelFormatter(Config(), _hw_disks(["sda"]))
    assert fmt._disk_smart_pair(Readings(disk_smart={"sda": None}), tooltip=True) == []


# ── hd_temp:pair (disk temps in pairs, same grid as disk_smart:pair) ─────────

def _hw_hd_temps(labels):
    return _bare_hw(hd_temp_paths={l: "/sys/" + l for l in labels})


def test_hd_temp_pair_packs_two_drives_per_row():
    fmt = PanelFormatter(Config(), _hw_hd_temps(["nvme0n1", "sda", "sdb", "sdc"]))
    r = Readings(hd_temps={"nvme0n1": 40, "sda": 45, "sdb": 50, "sdc": 55})
    rows = fmt._hd_temp_pair(r, tooltip=True)
    assert len(rows) == 2          # 4 drives / 2 per row
    assert all(len(row) == 4 for row in rows)  # two label/val pairs each


def test_hd_temp_pair_odd_count_uses_blank_filler():
    fmt = PanelFormatter(Config(), _hw_hd_temps(["nvme0n1", "sda", "sdb"]))
    r = Readings(hd_temps={"nvme0n1": 40, "sda": 45, "sdb": 50})
    rows = fmt._hd_temp_pair(r, tooltip=True)
    assert len(rows) == 2
    assert all(len(row) == 4 for row in rows)
    assert rows[1][2].text == "" and rows[1][3].text == ""


def test_hd_temp_pair_single_disk_is_full_width_row():
    fmt = PanelFormatter(Config(), _hw_hd_temps(["nvme0n1"]))
    rows = fmt._hd_temp_pair(Readings(hd_temps={"nvme0n1": 42}), tooltip=True)
    assert len(rows) == 1
    assert len(rows[0]) == 2          # plain [label, value], temp at right edge


def test_hd_temp_pair_skips_disks_without_temp():
    fmt = PanelFormatter(Config(), _hw_hd_temps(["nvme0n1", "sda"]))
    r = Readings(hd_temps={"nvme0n1": 42})   # sda has no reading
    rows = fmt._hd_temp_pair(r, tooltip=True)
    assert len(rows) == 1             # only the disk with a temperature
    assert len(rows[0]) == 2          # full-width, not a 2-pair filler row


def test_hd_temp_pair_empty_when_no_temps():
    fmt = PanelFormatter(Config(), _hw_hd_temps(["sda"]))
    assert fmt._hd_temp_pair(Readings(hd_temps={}), tooltip=True) == []


# ── fan_speed:pair (fan RPM in pairs, same grid as hd_temp:pair) ─────────────

def _hw_fans(keys):
    return _bare_hw(fan_paths={k: "/sys/fan" + k for k in keys})


def test_fan_speed_pair_two_fans_one_row():
    # the dev box has 2 fans -> a single row with one pair of fans
    fmt = PanelFormatter(Config(), _hw_fans(["1", "2"]))
    r = Readings(fan_speeds={"1": 1397, "2": 571})
    rows = fmt._fan_speed_pair(r, tooltip=True)
    assert len(rows) == 1
    assert len(rows[0]) == 4          # two label/val pairs on one row


def test_fan_speed_pair_odd_count_uses_blank_filler():
    fmt = PanelFormatter(Config(), _hw_fans(["1", "2", "3"]))
    r = Readings(fan_speeds={"1": 1000, "2": 1100, "3": 1200})
    rows = fmt._fan_speed_pair(r, tooltip=True)
    assert len(rows) == 2
    assert all(len(row) == 4 for row in rows)
    assert rows[1][2].text == "" and rows[1][3].text == ""


def test_fan_speed_pair_single_fan_is_full_width_row():
    fmt = PanelFormatter(Config(), _hw_fans(["1"]))
    rows = fmt._fan_speed_pair(Readings(fan_speeds={"1": 900}), tooltip=True)
    assert len(rows) == 1
    assert len(rows[0]) == 2          # plain [label, value], rpm at right edge


def test_fan_speed_pair_skips_fans_without_reading():
    fmt = PanelFormatter(Config(), _hw_fans(["1", "2"]))
    r = Readings(fan_speeds={"1": 900})   # fan 2 has no reading
    rows = fmt._fan_speed_pair(r, tooltip=True)
    assert len(rows) == 1
    assert len(rows[0]) == 2          # full-width, not a 2-pair filler row


def test_fan_speed_pair_empty_when_no_readings():
    fmt = PanelFormatter(Config(), _hw_fans(["1"]))
    assert fmt._fan_speed_pair(Readings(fan_speeds={}), tooltip=True) == []


def test_disk_smart_empty_when_smart_disabled():
    cfg = Config()
    cfg.disks.smart = False
    fmt = PanelFormatter(cfg, _hw_disks(["sda"]))
    assert fmt._disk_smart_pair(Readings(disk_smart={"sda": True}), tooltip=True) == []


# ── _normalize_separators ────────────────────────────────────────────────────
# A separator may only sit between two real rows; edge/stranded ones are dropped
# and consecutive ones collapse to the largest. This is what lets a separator
# declared at a section edge become the gap between two concatenated sections.

def _row(name):  # minimal stand-in for a real row
    return [_val_cell(name)]

def test_normalize_keeps_separator_between_two_rows():
    a, b = _row("a"), _row("b")
    out = _normalize_separators([a, Separator(size="small"), b])
    assert out == [a, Separator(size="small"), b]

def test_normalize_drops_leading_and_trailing_separators():
    a = _row("a")
    out = _normalize_separators([Separator(size="small"), a, Separator(size="big")])
    assert out == [a]

def test_normalize_collapses_consecutive_keeping_largest():
    a, b = _row("a"), _row("b")
    out = _normalize_separators([a, Separator(size="small"), Separator(size="big"), b])
    assert out == [a, Separator(size="big"), b]

def test_normalize_section_edge_separator_becomes_inter_section_gap():
    # cpumem ends with a separator, thermal follows: the trailing separator is
    # the gap between the two sections, not silently dropped.
    cpu, mem, temp = _row("cpu"), _row("mem"), _row("temp")
    out = _normalize_separators([cpu, mem, Separator(size="small"), temp])
    assert out == [cpu, mem, Separator(size="small"), temp]


# ── wifi link rows ────────────────────────────────────────────────────────────

def _wifi_fmt(glyphs: bool = True) -> PanelFormatter:
    cfg = Config()
    cfg.panel.glyphs = glyphs
    return PanelFormatter(cfg, _bare_hw(net_device="wlan0", has_wifi=True))


def test_wifi_rate_tooltip_puts_mcs_nss_in_the_middle_column():
    row = _wifi_fmt()._wifi_rate(WifiRate(1152.8, 5, 2), "wifi_tx", tooltip=True)
    assert [c.text for c in row[1:]] == ["MCS 5 NSS 2", "1152.8 Mbit/s"]


def test_wifi_rate_panel_shows_whole_mbit_only():
    assert _wifi_fmt()._wifi_rate(WifiRate(1152.8, 5, 2), "wifi_rx", tooltip=False)[-1].text == "1153"


def test_wifi_rate_legacy_shows_the_bitrate_alone():
    fmt = _wifi_fmt()
    assert fmt._wifi_rate(WifiRate(54.0), "wifi_tx", tooltip=False)[-1].text == "54"
    assert fmt._wifi_rate(WifiRate(54.0), "wifi_tx", tooltip=True)[1].text == ""


def test_wifi_rate_missing_link_is_placeholder():
    fmt = _wifi_fmt()
    assert fmt._wifi_rate(None, "wifi_tx", tooltip=True)[-1].text == "--"
    assert fmt._wifi_rate(None, "wifi_tx", tooltip=False)[-1].text == "--"


def test_wifi_ant_reads_its_chain_in_dbm_with_threshold_class():
    fmt, r = _wifi_fmt(), Readings(wifi_chains=[-57, -85])
    assert fmt._wifi_ant(r, 0, tooltip=True)[-1].text == "-57 dBm"
    assert "good" in fmt._wifi_ant(r, 0, tooltip=True)[-1].css_class
    assert "crit" in fmt._wifi_ant(r, 1, tooltip=False)[-1].css_class
    assert fmt._wifi_ant(Readings(wifi_chains=[-57]), 1, tooltip=True)[-1].text == "--"


def test_wifi_ants_joins_every_antenna_colored_on_its_own():
    fmt = _wifi_fmt()
    text = fmt._wifi_ants(Readings(wifi_chains=[-55, -65, -75]), tooltip=True)[-1].text
    assert text == ('<span class="good">-55</span> / <span class="warn">-65</span> / '
                    '<span class="crit">-75</span> dBm')
    assert fmt._wifi_ants(Readings(), tooltip=True)[-1].text == "--"


def test_wifi_rows_drop_the_glyph_in_a_glyphless_panel():
    fmt = _wifi_fmt(glyphs=False)
    assert len(fmt._wifi_ant(Readings(wifi_chains=[-57]), 0, tooltip=False)) == 1
    assert len(fmt._wifi_rate(WifiRate(100.0, 3, 1), "wifi_tx", tooltip=False)) == 1


def test_wifi_ant2_gate_follows_the_radio_or_the_live_chains():
    one = PanelFormatter(Config(), _bare_hw(has_wifi=True, wifi_antennas=1))
    two = PanelFormatter(Config(), _bare_hw(has_wifi=True, wifi_antennas=2))
    unknown = PanelFormatter(Config(), _bare_hw(has_wifi=True))
    assert not one._available("wifi_ant2", Readings(wifi_chains=[-50]))
    assert two._available("wifi_ant2", Readings())
    assert unknown._available("wifi_ant2", Readings(wifi_chains=[-50, -52]))
    assert not unknown._available("wifi_ant2", Readings())
