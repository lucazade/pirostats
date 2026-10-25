"""
Table-free monospace serializer for the tooltip and the vertical panel.

Qt Quick's *live* RichText engine re-runs full <table> column-balancing on
every content change, super-linear in table size (measured — see the
pirostats project memory). It bites hardest on the tooltip: while the popup
is open and refreshing every poll, that cost dominated at ~20% plasmashell
CPU. The vertical panel pays a much smaller version of the same tax every
poll, 24/7 (fewer, smaller tables), so it's routed here too. The horizontal
panel is already table-free (inline spans in render_model).

Because both use a monospace font (a Nerd Font Mono), the exact same column
alignment the tables produced can be computed once in Python with &nbsp;
padding instead of by Qt's layout engine every frame.

Visual contract:
  - labels / left cells: left-aligned, padded to per-block column widths;
  - value cells: right-aligned to a single shared right edge across the WHOLE
    block list, so every block's right edge lines up with all the others;
  - net_speed/disk_io paired rows split into two ~50% halves; spanning rows
    (bars/sparks) stay left; titles are left-aligned;
  - separators reuse render_model's helper (_separator_rule_html, a plain
    <div>). No <table> is emitted anywhere here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from render_model import (
    Block,
    Cell,
    Row,
    _separator_rule_html,
    cell_inner,
    visible_width as _visible_width,
)


def _pad(n: int) -> str:
    """n non-breaking spaces, wrapped in a .gap span so structural inter-cell
    and inter-column padding is role-tagged like every real cell: invisible in
    normal rendering (no .gap rule paints it), but targetable by the inspection
    overlay to reveal the gaps between coloured cells. Regular spaces would be
    collapsed by the rich text engine, destroying the alignment, hence &nbsp;.
    The wrapping is transparent to the width math: _visible_width strips the
    tag and counts the &nbsp; entities just the same."""
    return f'<span class="gap">{"&nbsp;" * n}</span>' if n > 0 else ""


def _cell_width(cell: Cell) -> int:
    """On-screen monospace footprint of a whole cell: real content plus its
    structural pad_left/pad_right. The padding takes columns in the layout but
    is kept out of `text`, so it must be added back here for the column math.
    layout_width, when set, replaces the content's visible width — the cell is
    drawn at a different font-size and occupies that many full-size columns in
    pixels regardless of its character count (small-font bar, see Cell)."""
    content = cell.layout_width if cell.layout_width is not None else _visible_width(cell.text)
    return cell.pad_left + content + cell.pad_right


def _span(cell: Cell) -> str:
    """Wrap a cell's content (with pad_left/pad_right materialized via
    cell_inner) in its CSS class span (color/role). The text may already
    contain its own nested spans (bar/spark segments) — fine."""
    inner = cell_inner(cell)
    return f'<span class="{cell.css_class}">{inner}</span>' if cell.css_class else inner


# ── Row layout plan (pass 1) ─────────────────────────────────────────────────

@dataclass
class _Plan:
    kind: str          # "left" | "rightval" | "centermid" | "twopair"
    natural_width: int
    html: str = ""     # for "left": the full pre-rendered content
    left_html: str = ""    # for "rightval"/"centermid": label (left) column(s)
    left_width: int = 0
    val_html: str = ""     # for "rightval"/"centermid": the right-aligned value cell
    val_width: int = 0
    cells: Optional[Row] = None  # for "twopair": the 4 cells (label,val,label,val)
    mid_html: str = ""     # for "centermid": the middle cell to center
    mid_width: int = 0
    val_col_width: int = 0  # for "centermid": block-wide value width (right zone)


def _is_two_pair(row: Row) -> bool:
    """net_speed / disk_io: two right-aligned label/value pairs sharing one
    row (Up/Down, Read/Write), each pair in its own half with the value
    right-aligned at the half's edge. Detected by the alternating left/right
    alignment (cells 1 and 3 are the right-aligned values) — distinct from the
    other 4-cell rows (the bar+spark/braille combos) whose 2nd/4th cells are
    left-aligned bars/sparks, not values."""
    return len(row) == 4 and row[1].align == "right" and row[3].align == "right"


def _col_widths(block: Block) -> list[int]:
    ncol = max(len(row) for row in block.rows)
    widths = [0] * ncol
    for row in block.rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _cell_width(cell))
    return widths


def _render_cols(cells: list[Cell], widths: list[int]) -> tuple[str, int]:
    """Render cells each padded to its column width, honoring per-cell
    alignment (right cells padded on the left, others on the right). Columns
    are flush — any gap between them lives in the cell text itself."""
    out = []
    for cell, w in zip(cells, widths):
        pad = w - _cell_width(cell)
        if cell.align == "right":
            out.append(_pad(pad) + _span(cell))
        else:
            out.append(_span(cell) + _pad(pad))
    return "".join(out), sum(widths)


def _plan_row(row: Row, widths: list[int], val_col_width: int = 0) -> _Plan:
    if len(row) == 3 and row[2].align == "right" and row[1].align == "center":
        # disk_usage: label | used/total | percent. The middle cell is centered
        # in the gap between the label column and the value's right zone (its
        # block-wide column width, so a lone "100%" doesn't shift the others).
        lbl_html, lbl_w = _render_cols(row[:1], widths[:1])
        mid_w, val_w = _cell_width(row[1]), _cell_width(row[2])
        return _Plan(
            kind="centermid", natural_width=lbl_w + mid_w + max(val_w, val_col_width),
            left_html=lbl_html, left_width=lbl_w,
            mid_html=_span(row[1]), mid_width=mid_w,
            val_html=_span(row[2]), val_width=val_w, val_col_width=val_col_width,
        )

    if len(row) == 1:
        cell = row[0]
        # A lone cell (e.g. a section title) renders left-aligned —
        # alignment lives here, not in CSS (text-align has no effect on the
        # already-laid-out span).
        return _Plan(kind="left", natural_width=_cell_width(cell), html=_span(cell))

    if _is_two_pair(row):
        return _Plan(
            kind="twopair",
            natural_width=sum(_cell_width(c) for c in row),
            cells=row,
        )

    last = row[-1]
    if last.align == "right":
        left_html, left_w = _render_cols(row[:-1], widths[:-1])
        val_w = _cell_width(last)
        return _Plan(
            kind="rightval", natural_width=left_w + val_w,
            left_html=left_html, left_width=left_w, val_html=_span(last), val_width=val_w,
        )

    full_html, full_w = _render_cols(row, widths)
    return _Plan(kind="left", natural_width=full_w, html=full_html)


# ── Emit (pass 2) ────────────────────────────────────────────────────────────

def _emit(plan: _Plan, global_width: int) -> str:
    if plan.kind == "twopair":
        # Split the full width in two halves, one label/value pair each, value
        # right-aligned at its half's edge.
        a_lbl, a_val, b_lbl, b_val = plan.cells
        half = global_width // 2
        pad1 = half - _cell_width(a_lbl) - _cell_width(a_val)
        pad2 = (global_width - half) - _cell_width(b_lbl) - _cell_width(b_val)
        return (
            f"<div>{_span(a_lbl)}{_pad(pad1)}{_span(a_val)}"
            f"{_span(b_lbl)}{_pad(pad2)}{_span(b_val)}</div>"
        )
    if plan.kind == "rightval":
        mid = global_width - plan.left_width - plan.val_width
        return f"<div>{plan.left_html}{_pad(mid)}{plan.val_html}</div>"
    if plan.kind == "centermid":
        # Region between the label column and the value's right zone; center the
        # middle block in it, then pad out to the value's true right edge.
        region = global_width - plan.val_col_width - plan.left_width
        left   = (region - plan.mid_width) // 2
        after  = global_width - plan.val_width - plan.left_width - left - plan.mid_width
        return (f"<div>{plan.left_html}{_pad(left)}{plan.mid_html}"
                f"{_pad(after)}{plan.val_html}</div>")
    return f"<div>{plan.html}</div>"


def global_width_of(blocks: list[Block], min_width: int = 0) -> int:
    """The shared monospace column width render_blocks_monospace lays out to —
    the widest natural row, floored by min_width. Exposed so a caller can center
    a footer (the pager) within the same width the body was padded to, anchored
    to the content instead of the (resize-laggy) tooltip box."""
    width = min_width
    for block in blocks:
        widths = _col_widths(block)
        val_col_width = max((_cell_width(row[-1]) for row in block.rows
                             if len(row) >= 2 and row[-1].align == "right"), default=0)
        for row in block.rows:
            width = max(width, _plan_row(row, widths, val_col_width).natural_width)
    return width


def render_blocks_monospace(blocks: list[Block], min_width: int = 0) -> str:
    # Pass 1: lay out every row, recording the widest natural line — that
    # width becomes the shared right edge all value cells align to.
    laid_out: list[tuple[Optional[str], list[_Plan]]] = []
    global_width = min_width  # floor: keep the surface from shrinking below this
    for block in blocks:
        widths = _col_widths(block)
        # Block-wide width of the right-aligned value column: centermid rows
        # center against it so values of different widths keep the slashes aligned.
        val_col_width = max((_cell_width(row[-1]) for row in block.rows
                             if len(row) >= 2 and row[-1].align == "right"), default=0)
        plans = [_plan_row(row, widths, val_col_width) for row in block.rows]
        for p in plans:
            global_width = max(global_width, p.natural_width)
        laid_out.append((block.separator_size, plans))

    # Pass 2: serialize, right-aligning values to global_width.
    parts: list[str] = []
    for separator_size, plans in laid_out:
        if separator_size is not None:
            parts.append(_separator_rule_html(separator_size))
        parts.extend(_emit(p, global_width) for p in plans)
    return "".join(parts)
