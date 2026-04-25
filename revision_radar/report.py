"""
PDF report renderer for Revision Radar.

Layout:
    1. Header block — episode title, old/new draft labels, change summary.
    2. Revision pages callout — if the new draft's revision history tells us
       which pages changed, surface that.
    3. General Changes section — every change, in script order, each with
       department color chips.
    4. Department sections — one per department that has at least one change
       impacting <=3 departments total (per Ross's "general-only if >3" rule).
       Cross-cutting changes are intentionally NOT repeated in every
       department; they live in the General section only.

Design aim: scannable headline document. Avoid dense prose; prefer small
visual chips, short lines, generous whitespace.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table,
    TableStyle, KeepTogether, PageBreak, Image,
)

from .classifier import BY_CODE, DEPARTMENTS
from .differ import Change
from .parser import Script


# ---------------------------------------------------------------------------
# Shared styles
# ---------------------------------------------------------------------------

_styles = getSampleStyleSheet()

TITLE = ParagraphStyle(
    name="RRTitle", parent=_styles["Title"],
    fontName="Helvetica-Bold", fontSize=20, leading=24, spaceAfter=4,
    textColor=colors.HexColor("#1a1a1a"),
)
SUBTITLE = ParagraphStyle(
    name="RRSub", parent=_styles["Normal"],
    fontName="Helvetica", fontSize=11, leading=14, spaceAfter=6,
    textColor=colors.HexColor("#4a4a4a"),
)
H2 = ParagraphStyle(
    name="RRH2", parent=_styles["Heading2"],
    fontName="Helvetica-Bold", fontSize=14, leading=18, spaceBefore=14,
    spaceAfter=6, textColor=colors.HexColor("#1a1a1a"),
)
BODY = ParagraphStyle(
    name="RRBody", parent=_styles["Normal"],
    fontName="Helvetica", fontSize=10, leading=13, spaceAfter=2,
)
CHANGE_LINE = ParagraphStyle(
    name="RRLine", parent=BODY,
    leftIndent=0, spaceAfter=1, leading=12, fontSize=9.5,
)
SCENE_HEADER = ParagraphStyle(
    name="RRScene", parent=BODY,
    fontName="Helvetica-Bold", fontSize=10, leading=13,
    textColor=colors.HexColor("#333"), spaceBefore=6, spaceAfter=2,
)
META = ParagraphStyle(
    name="RRMeta", parent=BODY,
    fontSize=8.5, textColor=colors.HexColor("#666"),
)
CHIP = ParagraphStyle(
    name="RRChip", parent=BODY,
    fontName="Helvetica-Bold", fontSize=7, leading=9,
    textColor=colors.white, alignment=TA_LEFT,
)
DEPT_HEADER = ParagraphStyle(
    name="RRDept", parent=_styles["Heading2"],
    fontName="Helvetica-Bold", fontSize=13, leading=16,
    textColor=colors.white, spaceBefore=0, spaceAfter=0, leftIndent=0,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chip_flowable(dept_code: str) -> Table:
    """A small colored chip with the department's short code."""
    dept = BY_CODE[dept_code]
    txt_color = colors.HexColor(dept.text_color)
    chip_style = ParagraphStyle(
        name=f"Chip_{dept_code}", parent=CHIP,
        textColor=txt_color,
    )
    chip_label = dept.label or dept.code
    t = Table(
        [[Paragraph(f"<b>{chip_label}</b>", chip_style)]],
        colWidths=[0.58 * inch], rowHeights=[0.17 * inch],
    )
    border_cmds = []
    if dept.color == "#FFFFFF":
        # Stunts: white chip needs a visible border
        border_cmds = [("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#333333"))]
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(dept.color)),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ] + border_cmds))
    return t


def _change_row(change: Change) -> Table:
    """A row for a change: [ scene# | description | dept chips ]."""
    scene_label = change.scene_number or "—"
    desc = _describe_rich(change)

    # Wrap chips into rows of 3 so longer codes (PRODLOC, CASTEX) don't overflow.
    CHIPS_PER_ROW = 3
    chip_codes = change.departments[:9]  # hard cap visible chips at 9
    chip_rows: list[list] = []
    for i in range(0, len(chip_codes), CHIPS_PER_ROW):
        row_codes = chip_codes[i:i + CHIPS_PER_ROW]
        row = [_chip_flowable(c) for c in row_codes]
        while len(row) < CHIPS_PER_ROW:
            row.append("")
        chip_rows.append(row)

    if chip_rows:
        chip_grid = Table(chip_rows,
                          colWidths=[0.60 * inch] * CHIPS_PER_ROW,
                          rowHeights=[0.20 * inch] * len(chip_rows))
        chip_grid.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
    else:
        chip_grid = Paragraph("", META)

    t = Table(
        [[Paragraph(f"<b>{scene_label}</b>", CHANGE_LINE),
          desc,
          chip_grid]],
        colWidths=[0.45 * inch, 4.6 * inch, 2.3 * inch],
    )
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e6e6e6")),
    ]))
    return t


def _describe_rich(change: Change) -> Paragraph:
    """Rich description of a change for the PDF, with color highlighting."""
    kind = change.kind
    old = _esc(change.old_text)
    new = _esc(change.new_text)
    who = f" <i>{_esc(change.character)}</i>:" if change.character else ""

    def tag_add(s: str) -> str:
        return (f"<font color='#0a6e46'><b>ADD</b></font> "
                f"<font color='#0a6e46'>{s}</font>")

    def tag_cut(s: str) -> str:
        return (f"<font color='#a12020'><b>CUT</b></font> "
                f"<font color='#a12020'>{s}</font>")

    def tag_change(a: str, b: str) -> str:
        # No CHG label — the before→after format is self-evident.
        # Old text in muted grey; arrow and new text both in red.
        return (f"<font color='#888888'>{a}</font> "
                f"<font color='#a12020'>&rarr; {b}</font>")

    if kind == "draft_label_changed":
        text = f"<b>Draft:</b> {old} &rarr; {new}"
    elif kind == "cast_set":
        text = f"<b>Casting</b> – {_esc(change.character)}: <b>{new}</b>"
        if old and old.upper() != "TBD":
            text = f"<b>Casting</b> – {_esc(change.character)}: {old} &rarr; <b>{new}</b>"
    elif kind == "cast_replaced":
        text = f"<b>Casting recast</b> – {_esc(change.character)}: {old} &rarr; <b>{new}</b>"
    elif kind == "cast_removed":
        text = f"<b>Casting</b> – {_esc(change.character)} removed ({old})"
    elif kind == "scene_added":
        text = tag_add(f"<b>Scene {change.scene_number}</b> — {_esc(change.scene_slug)}")
    elif kind == "scene_cut":
        text = tag_cut(f"<b>Scene {change.scene_number}</b> — {_esc(change.scene_slug)}")
    elif kind == "slug_changed":
        text = tag_change(old, new)
    elif kind.startswith("dialogue_") or kind.startswith("parenthetical_"):
        # Only TBD dialogue reaches the renderer (pure is filtered upstream).
        snippet = _esc((change.new_text or change.old_text)[:90])
        who_label = f" <i>{_esc(change.character)}</i>" if change.character else ""
        text = (
            f"<font color='#888'><b>*Dialogue only — TBD</b></font>{who_label}"
            f" <font color='#aaa'>…{snippet}…</font>"
        )
    elif kind.startswith("action_"):
        verb = kind.split("_")[1]
        if verb == "added":
            text = "<b>Action:</b> " + tag_add(new)
        elif verb == "cut":
            text = "<b>Action:</b> " + tag_cut(old)
        else:
            text = "<b>Action:</b> " + tag_change(old, new)
    elif kind.startswith("parenthetical_"):
        verb = kind.split("_")[1]
        base = "<i>Direction:</i>" + who
        if verb == "added":
            text = base + " " + tag_add(new)
        elif verb == "cut":
            text = base + " " + tag_cut(old)
        else:
            text = base + " " + tag_change(old, new)
    elif kind == "character_added":
        text = f"<b>Character added to scene:</b> {_esc(change.new_text)}"
    elif kind == "character_cut":
        text = f"<b>Character removed from scene:</b> {_esc(change.old_text)}"
    else:
        text = _esc(change.describe())

    # Cross-impact badge for changes hitting >3 departments
    if len(change.departments) > 3:
        text += (f" <font color='#888' size='8'>"
                 f"⚑ impacts {len(change.departments)} depts</font>")
    if change.page:
        text += f" <font color='#999' size='8'>  p.{change.page}</font>"

    return Paragraph(text, CHANGE_LINE)


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _dept_banner(dept_code: str) -> Table:
    dept = BY_CODE[dept_code]
    t = Table([[Paragraph(f"&nbsp;&nbsp;{dept.name}", DEPT_HEADER)]],
              colWidths=[7.5 * inch], rowHeights=[0.32 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(dept.color)),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def _dept_legend() -> Table:
    """Legend chip grid at top of report."""
    cells = []
    for d in DEPARTMENTS:
        cells.append([_chip_flowable(d.code), Paragraph(d.name, BODY)])
    # Lay out 2 pairs per row (wider chips need the room)
    rows = []
    row = []
    for i, (chip, label) in enumerate(cells):
        row.extend([chip, label])
        if len(row) == 4:  # 2 pairs per row
            rows.append(row)
            row = []
    if row:
        while len(row) < 4:
            row.append("")
        rows.append(row)
    t = Table(rows,
              colWidths=[0.62 * inch, 2.5 * inch] * 2,
              hAlign="LEFT")
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return t


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------

def render_report(
    old: Script,
    new: Script,
    changes: list[Change],
    out_path: str | Path,
    *,
    general_only_threshold: int = 3,
    dept_filter: str | None = None,
) -> Path:
    """Render the Revision Radar report.

    Structure:
        1. Header (title, comparing, revision history, dept key)
        2. Cast Changes — character adds / drops / fills (brief)
        3. Set / Location Changes — slug and scene adds/cuts (no dept chips;
           impact is self-evident)
        4. Scene-by-Scene — all other production changes, grouped by scene,
           with dept chips. High-impact (>threshold depts) items appear here
           only; lower-impact items also appear in per-dept sections.
        5. Department Sections — one coloured band per dept that has hits.

    If ``dept_filter`` is set (a dept code like "PROPS"), sections 1–3 remain
    unchanged and section 4 is filtered to only that department's changes.
    Section 5 is omitted from filtered reports (the filter IS the dept view).
    """
    out_path = Path(out_path)
    doc = BaseDocTemplate(
        str(out_path),
        pagesize=LETTER,
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=f"Revision Radar — {new.title} {new.episode}",
        author="Revision Radar",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin,
                  doc.width, doc.height, id="main")
    doc.addPageTemplates([PageTemplate(id="all", frames=[frame],
                                       onPage=_draw_page_chrome)])

    story = []

    # Partition changes upfront.
    # Completely suppress: pure dialogue, draft label (in header), and all
    # cast-list entries (cast_set / replaced / removed = who's playing the
    # role — Casting's internal business, not a revision radar item).
    _suppress = {
        "draft_label_changed",
        "cast_set", "cast_replaced", "cast_removed",
    }
    visible = [ch for ch in changes
               if ch.dialogue_flag != "pure" and ch.kind not in _suppress]
    n_pure  = len(changes) - len(visible)

    # Cast section: only actual character adds/drops that appear in scenes.
    cast_changes  = [ch for ch in visible if ch.kind in (
        "character_added", "character_cut",
    )]
    set_changes   = [ch for ch in visible if ch.kind in (
        "slug_changed", "scene_added", "scene_cut",
    )]
    scene_changes = [ch for ch in visible if ch not in cast_changes
                     and ch not in set_changes]

    # --- Logo (aspect-ratio-safe) ---
    _logo_path = Path(__file__).parent.parent / "static" / "logo.png"
    if _logo_path.exists():
        _rdr = Image(str(_logo_path))          # load to read natural size
        _nat_w, _nat_h = _rdr.drawWidth, _rdr.drawHeight
        _target_w = 2.0 * inch
        _target_h = _target_w * (_nat_h / _nat_w) if _nat_w else _target_w
        logo = Image(str(_logo_path), width=_target_w, height=_target_h)
        logo.hAlign = "LEFT"
        story.append(logo)
        story.append(Spacer(1, 6))

    # --- Header ---
    if new.title or new.episode or new.production_number:
        meta_parts = []
        if new.title:          meta_parts.append(f"<b>{_esc(new.title)}</b>")
        if new.episode:        meta_parts.append(f"Episode {_esc(new.episode)}")
        if new.production_number: meta_parts.append(f"Production #{_esc(new.production_number)}")
        story.append(Paragraph(" &nbsp;·&nbsp; ".join(meta_parts), SUBTITLE))

    # Build "Comparing" line only when we have real label/date data
    def _draft_label(label: str, date: str) -> str:
        if label and date:
            return f"{_esc(label)} ({_esc(date)})"
        if label:
            return _esc(label)
        if date:
            return _esc(date)
        return "Unknown Draft"

    story.append(Paragraph(
        f"<b>Comparing:</b> "
        f"{_draft_label(old.draft_label, old.draft_date)} &nbsp;&rarr;&nbsp; "
        f"{_draft_label(new.draft_label, new.draft_date)}",
        SUBTITLE,
    ))

    # Summary counts
    n_cast  = len(cast_changes)
    n_set   = len(set_changes)
    n_scene = len(scene_changes)
    filtered_note = (f" &nbsp;<font color='#888' size='9'>({n_pure} dialogue-only lines suppressed)</font>"
                     if n_pure else "")
    story.append(Paragraph(
        f"<b>{len(visible)} production changes</b> &nbsp;·&nbsp; "
        f"{n_cast} cast &nbsp;·&nbsp; {n_set} set/location &nbsp;·&nbsp; "
        f"{n_scene} scene{filtered_note}",
        SUBTITLE,
    ))

    if new.revision_history:
        rh_lines = [f"<b>{color}</b> {date} — {pages}"
                    for date, color, pages in new.revision_history]
        story.append(Paragraph(
            "<b>Revision history:</b> " + " &nbsp;·&nbsp; ".join(rh_lines), META))
        story.append(Spacer(1, 4))

    story.append(Spacer(1, 8))
    story.append(Paragraph("<b>Department key</b>", BODY))
    story.append(_dept_legend())
    story.append(Spacer(1, 12))

    # =========================================================
    # SECTION 1 — Set / Location Changes
    # =========================================================
    if set_changes:
        story.append(Paragraph("Set / Location Changes", H2))
        story.append(Paragraph(
            "Slug and scene changes. Impact is self-evident — no department "
            "chips shown here.",
            META,
        ))
        story.append(Spacer(1, 3))
        for ch in set_changes:
            story.append(_set_change_row(ch))
        story.append(Spacer(1, 12))

    # =========================================================
    # SECTION 2 — Cast Changes
    # =========================================================
    if cast_changes:
        story.append(Paragraph("Cast Changes", H2))
        story.append(Spacer(1, 3))
        for ch in cast_changes:
            story.append(_change_row(ch))
        story.append(Spacer(1, 12))

    # =========================================================
    # SECTION 3 — Scene-by-Scene Production Changes
    # =========================================================
    if dept_filter:
        dept_obj = BY_CODE.get(dept_filter)
        section_title = (f"Scene Changes — {dept_obj.name}" if dept_obj
                         else f"Scene Changes — {dept_filter}")
        filtered_scene = [ch for ch in scene_changes
                          if dept_filter in ch.departments]
    else:
        section_title = "Scene Changes"
        filtered_scene = scene_changes

    if filtered_scene:
        story.append(Paragraph(section_title, H2))
        story.append(Spacer(1, 3))

        # Group by scene number
        grouped: dict[str, list[Change]] = defaultdict(list)
        order: list[str] = []
        for ch in filtered_scene:
            k = ch.scene_number or "__global__"
            if k not in grouped:
                order.append(k)
            grouped[k].append(ch)

        for k in order:
            group = grouped[k]
            slug = next((c.scene_slug for c in group if c.scene_slug), "")
            if k == "__global__":
                story.append(Paragraph("<b>Pre-script</b>", SCENE_HEADER))
            else:
                story.append(Paragraph(
                    f"<b>Scene {k}</b>"
                    + (f" — <font color='#666'>{_esc(slug)}</font>" if slug else ""),
                    SCENE_HEADER,
                ))
            for ch in group:
                story.append(_change_row(ch))

    # =========================================================
    # SECTION 4 — Per-Department Sections (full report only)
    # =========================================================
    if not dept_filter:
        story.append(PageBreak())
        story.append(Paragraph("By Department", H2))
        story.append(Spacer(1, 6))

        dept_buckets: dict[str, list[Change]] = defaultdict(list)
        for ch in scene_changes:
            for d in ch.departments:
                dept_buckets[d].append(ch)
        # Cast changes also appear under CASTEX / PRODLOC
        for ch in cast_changes:
            for d in ch.departments:
                dept_buckets[d].append(ch)

        for dept in DEPARTMENTS:
            items = dept_buckets.get(dept.code, [])
            if not items:
                continue
            block = [_dept_banner(dept.code), Spacer(1, 3)]
            for ch in items:
                block.append(_change_row(ch))
            block.append(Spacer(1, 8))
            story.append(KeepTogether(block[:2]))
            for flow in block[2:]:
                story.append(flow)

    doc.build(story)
    return out_path


def _set_change_row(change: Change) -> Table:
    """Simplified row for set/location changes — no dept chip column."""
    scene_label = change.scene_number or "—"
    desc = _describe_rich(change)
    t = Table(
        [[Paragraph(f"<b>{scene_label}</b>", CHANGE_LINE), desc]],
        colWidths=[0.45 * inch, 6.9 * inch],
    )
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e6e6e6")),
    ]))
    return t


def render_all_dept_reports(
    old: Script,
    new: Script,
    changes: list[Change],
    out_dir: str | Path,
    base_name: str,
    *,
    general_only_threshold: int = 3,
) -> list[Path]:
    """Generate one targeted PDF per department that has scene changes.

    Files are named: <base_name>_<DEPT_CODE>.pdf
    Returns the list of paths written.
    """
    from .classifier import DEPARTMENTS, BY_CODE

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Find which departments actually have scene-level changes
    visible = [ch for ch in changes if ch.dialogue_flag != "pure"
               and ch.kind not in ("draft_label_changed",)]
    scene_changes = [ch for ch in visible if ch.kind not in (
        "slug_changed", "scene_added", "scene_cut",
        "character_added", "character_cut",
        "cast_set", "cast_replaced", "cast_removed",
    )]
    active_depts = {d for ch in scene_changes for d in ch.departments}

    written: list[Path] = []
    for dept in DEPARTMENTS:
        if dept.code not in active_depts:
            continue
        out_path = out_dir / f"{base_name}_{dept.code}.pdf"
        render_report(old, new, changes, out_path,
                      general_only_threshold=general_only_threshold,
                      dept_filter=dept.code)
        written.append(out_path)
    return written


def _draw_page_chrome(canvas, doc):
    """Footer: page number + tool label."""
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#888"))
    canvas.drawRightString(LETTER[0] - 0.5 * inch, 0.35 * inch,
                           f"Page {doc.page}")
    canvas.drawString(0.5 * inch, 0.35 * inch,
                      "Revision Radar")
    canvas.restoreState()
