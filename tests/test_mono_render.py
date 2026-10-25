import re

from render_model import Cell, Separator, group_rows_into_blocks
from mono_render import _visible_width, render_blocks_monospace


def _label(text):
    return Cell(text=text, css_class="label")


def _val(text, cls="val"):
    return Cell(text=text, css_class=cls, align="right")


def _line_widths(html):
    """Visible width of each <div> line (the rendered rows), tags stripped."""
    return [_visible_width(m) for m in re.findall(r"<div>(.*?)</div>", html, re.S)]


# ── _visible_width ───────────────────────────────────────────────────────────

def test_visible_width_strips_tags_and_decodes_entities():
    assert _visible_width('<span class="val crit">12%</span>') == 3
    assert _visible_width("a&nbsp;b") == 3           # nbsp counts as one column
    assert _visible_width("<b>x</b>&nbsp;&nbsp;") == 3


# ── no data tables ───────────────────────────────────────────────────────────

def test_plain_blocks_emit_no_table():
    blocks = group_rows_into_blocks([
        [_label("Cpu:"), _val("9%")],
        [_label("Mem:"), _val("22%")],
    ])
    html = render_blocks_monospace(blocks)
    assert "<table" not in html
    assert "<div>" in html


# ── shared right edge across blocks ──────────────────────────────────────────

def test_values_share_a_global_right_edge():
    """Every value-bearing row is padded to the same total width, so the
    right edges line up (the monospace stand-in for width="100%")."""
    blocks = group_rows_into_blocks([
        [_label("A:"), _val("1%")],
        [_label("LongerLabel:"), _val("100%")],
    ])
    widths = _line_widths(render_blocks_monospace(blocks))
    assert len(set(widths)) == 1   # all lines the same total width
    # and that width fits the widest natural line (label + value)
    assert widths[0] >= len("LongerLabel:") + len("100%")


def test_value_sits_at_the_right_edge():
    blocks = group_rows_into_blocks([[_label("A:"), _val("9%")]])
    html = render_blocks_monospace(blocks)
    inner = re.search(r"<div>(.*?)</div>", html, re.S).group(1)
    # the value span is the last thing on the line (flush right)
    assert inner.rstrip().endswith("</span>")
    assert re.search(r'>9%</span>\s*$', inner)


# ── two-pair rows (net_speed / disk_io) ──────────────────────────────────────

def test_two_pair_row_splits_into_two_halves():
    row = [_label("Up:"), _val("9K"), _label("Down:"), _val("1K")]
    blocks = group_rows_into_blocks([row])
    html = render_blocks_monospace(blocks)
    assert "<table" not in html
    for token in ("Up:", "9K", "Down:", "1K"):
        assert token in html


# ── separators ───────────────────────────────────────────────────────────────

def test_separator_small_emits_rule_div():
    blocks = group_rows_into_blocks([[_label("A:"), _val("1")],
                                     Separator(size="small"),
                                     [_label("B:"), _val("2")]])
    html = render_blocks_monospace(blocks)
    assert 'class="separator-rule-small"' in html
    assert "separator-rule-big" not in html


def test_separator_big_emits_rule_div():
    blocks = group_rows_into_blocks([[_label("A:"), _val("1")],
                                     Separator(size="big"),
                                     [_label("B:"), _val("2")]])
    html = render_blocks_monospace(blocks)
    assert 'class="separator-rule-big"' in html


def test_no_rule_without_explicit_separator():
    blocks = group_rows_into_blocks([
        [Cell(text="spark", css_class="aux")],
        [_label("A:"), _val("1")],
    ])
    html = render_blocks_monospace(blocks)
    assert "separator-rule" not in html


# ── titles left-aligned ───────────────────────────────────────────────────────

def test_title_is_left_aligned():
    blocks = group_rows_into_blocks([
        [Cell(text="Title", css_class="title")],
        [_label("LongerLabel:"), _val("100%")],
    ])
    html = render_blocks_monospace(blocks)
    title_div = re.search(r"<div>((?:(?!</div>).)*Title.*?)</div>", html, re.S).group(1)
    # left-aligned: the title span leads the div, no centering .gap padding
    # before it
    assert title_div.startswith('<span class="title">Title</span>')
    assert '<span class="gap">&nbsp;' not in title_div


def test_title_is_one_row_with_no_rule_element():
    blocks = group_rows_into_blocks([
        [Cell(text="Title", css_class="title")],
        [_label("LongerLabel:"), _val("100%")],
    ])
    html = render_blocks_monospace(blocks)
    # the underline is the title's text-decoration (style sheet), so there is no
    # separate full-width element that could follow the frame instead of the text
    assert "title-rule" not in html
    assert html.count("<div") == 2
    # the data row stays the widest: the value right-aligns to its edge
    assert '<span class="val">100%</span></div>' in html
