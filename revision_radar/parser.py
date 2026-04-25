"""
Script parser for Revision Radar.

Parses industry-standard Final Draft / Scenechronize PDFs into structured
Scene objects with typed blocks (slug, action, character, dialogue,
parenthetical, transition).

Classification is done on the X-coordinate of the leftmost word on each
line, which is far more reliable than regex on plain text.

Typical 8.5x11 letter-size script layout (in points):
    54   -> scene number, left margin
    108  -> slug / action / transition (left-flush)
    180  -> dialogue
    208  -> parenthetical (roughly)
    252  -> character cue (centered)
    524  -> scene number, right margin

Transitions (e.g. "CUT TO:") are right-aligned, typically x0 > 400.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import pdfplumber


# X-coordinate tolerances (points). Courier 12pt is ~7pt wide per char.
X_TOL = 6

# Canonical column positions (left edge of text)
X_SCENE_NUM_LEFT = 54
X_ACTION = 108
X_DIALOGUE = 180
X_PAREN = 208
X_CHARACTER = 252
X_SCENE_NUM_RIGHT = 524

# A page is 612 wide. Anything > 400 with a colon is likely a transition.
X_TRANSITION_MIN = 400


@dataclass
class Block:
    """A typed line or group of lines within a scene."""
    kind: str  # slug | action | character | dialogue | parenthetical | transition
    text: str
    page: int
    revised: bool = False  # True if Final Draft asterisk in margin

    def __repr__(self) -> str:
        prefix = "*" if self.revised else " "
        return f"[{self.kind:13s}]{prefix} {self.text[:80]}"


@dataclass
class Scene:
    number: str           # e.g. "2", "21pt1", "A3" — kept as string
    slug: str             # full slug line, e.g. "INT. ZONE B - PATIENT ROOM 112 - DAY (D1)"
    int_ext: str = ""     # INT | EXT | INT/EXT
    location: str = ""    # ZONE B - PATIENT ROOM 112
    time_of_day: str = "" # DAY | NIGHT | etc.
    story_day: str = ""   # D1, N2, etc. (the (D1) marker)
    start_page: int = 0
    blocks: list[Block] = field(default_factory=list)
    revised: bool = False

    @property
    def characters(self) -> list[str]:
        seen: list[str] = []
        for b in self.blocks:
            if b.kind == "character":
                # Strip any (CONT'D), (V.O.), (O.S.) etc.
                name = b.text.split("(")[0].strip()
                if name and name not in seen:
                    seen.append(name)
        return seen

    @property
    def dialogue_text(self) -> str:
        return "\n".join(b.text for b in self.blocks if b.kind == "dialogue")

    @property
    def action_text(self) -> str:
        return "\n".join(b.text for b in self.blocks if b.kind == "action")

    @property
    def full_text(self) -> str:
        """Flat text for similarity comparison."""
        parts = [self.slug]
        for b in self.blocks:
            if b.kind == "character":
                parts.append(b.text.upper())
            else:
                parts.append(b.text)
        return "\n".join(parts)


@dataclass
class Script:
    title: str = ""
    episode: str = ""
    production_number: str = ""
    draft_label: str = ""     # e.g. "Concept Draft", "Shooting Draft - Blue"
    draft_date: str = ""      # e.g. "08/28/25"
    revision_history: list[tuple[str, str, str]] = field(default_factory=list)
    # (date, color, pages) tuples from the SCRIPT REVISION LIST
    cast: dict[str, str] = field(default_factory=dict)  # character -> actor
    scenes: list[Scene] = field(default_factory=list)
    source_path: str = ""


# ---------------------------------------------------------------------------
# Low-level line extraction
# ---------------------------------------------------------------------------

def _group_words_into_lines(words: list[dict]) -> list[list[dict]]:
    """Group words on the same visual line (by y-coordinate)."""
    if not words:
        return []
    words = sorted(words, key=lambda w: (round(w["top"], 0), w["x0"]))
    lines: list[list[dict]] = []
    current: list[dict] = []
    current_y: float | None = None
    for w in words:
        y = w["top"]
        if current_y is None or abs(y - current_y) < 4:
            current.append(w)
            current_y = y if current_y is None else current_y
        else:
            lines.append(sorted(current, key=lambda w: w["x0"]))
            current = [w]
            current_y = y
    if current:
        lines.append(sorted(current, key=lambda w: w["x0"]))
    return lines


def _classify_line(line: list[dict]) -> tuple[str, str, bool]:
    """Return (kind, cleaned_text, revised) for a line of words."""
    if not line:
        return ("skip", "", False)

    # Detect revision asterisk in far right margin (x > 540-ish, text == '*')
    revised = any(w["text"] == "*" and w["x0"] > 535 for w in line)

    # Remove margin elements (left scene #, right scene #, revision asterisks)
    # before classifying.
    body = [
        w for w in line
        if not (w["x0"] < 70 and _is_scene_number(w["text"]))
        and not (w["x0"] > 510 and _is_scene_number(w["text"]))
        and not (w["text"] == "*" and w["x0"] > 535)
    ]
    if not body:
        # Line was only a margin element (e.g. left scene number alone)
        return ("skip", "", revised)

    x0 = body[0]["x0"]
    text = " ".join(w["text"] for w in body).strip()
    # Fix dual-layer PDF artefact (doubled characters) on every word
    text = " ".join(_fix_doubled_chars(w) for w in text.split())

    # Running page header: typically at y < 90 — we skip those at page level
    # so classification here assumes body content.

    # Slug lines begin with INT./EXT./INT-EXT.
    if abs(x0 - X_ACTION) < X_TOL and text.split()[:1] and text.split()[0] in {
        "INT.", "EXT.", "INT/EXT.", "INT./EXT.", "I/E."
    }:
        return ("slug", text, revised)

    # Character cue: centered-ish, ALL CAPS, short
    if abs(x0 - X_CHARACTER) < 18 and _is_character_cue(text):
        return ("character", text, revised)

    # Parenthetical: indented between dialogue and character, in parens
    if abs(x0 - X_PAREN) < 14 and text.startswith("(") :
        return ("parenthetical", text, revised)

    # Dialogue
    if abs(x0 - X_DIALOGUE) < X_TOL:
        return ("dialogue", text, revised)

    # Transition: right-aligned with colon
    if x0 > X_TRANSITION_MIN and text.endswith(":"):
        return ("transition", text, revised)

    # Action is left-flush at x=108
    if abs(x0 - X_ACTION) < X_TOL:
        return ("action", text, revised)

    # Anything else — keep as action (broad catch), but tag kind so we can audit
    return ("action", text, revised)


def _is_scene_number(text: str) -> bool:
    """Scene numbers look like '2', '21pt1', 'A3', '1A', etc."""
    if not text:
        return False
    # Pure digit
    if text.isdigit():
        return True
    # Alphanumeric mix starting or ending with a digit
    has_digit = any(c.isdigit() for c in text)
    has_alpha = any(c.isalpha() for c in text)
    if has_digit and has_alpha and len(text) <= 8:
        return True
    return False


def _is_character_cue(text: str) -> bool:
    """Character cues are ALL CAPS, short, may have (CONT'D), (V.O.), etc."""
    # Strip parenthetical modifiers for the check
    core = text.split("(")[0].strip()
    if not core:
        return False
    if len(core) > 40:
        return False
    # Must be mostly uppercase letters
    letters = [c for c in core if c.isalpha()]
    if not letters:
        return False
    return all(c.isupper() for c in letters)


# ---------------------------------------------------------------------------
# Cover page / metadata extraction
# ---------------------------------------------------------------------------

def _extract_cover_metadata(pdf: pdfplumber.PDF, script: Script) -> None:
    """Pull title, draft label/date, revision list, cast from opening pages."""
    # Most Scenechronize scripts put the title + draft info on page 1,
    # cast list on page 2, and revision list on page 2 if it exists.
    for pg_idx in range(min(4, len(pdf.pages))):
        text = pdf.pages[pg_idx].extract_text() or ""
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

        # Title is usually in quotes e.g. '"Cabo"'
        for ln in lines:
            if ln.startswith('"') and ln.endswith('"') and not script.title:
                script.title = ln.strip('"')

        # Production # / Script #
        for ln in lines:
            if "Script #" in ln and not script.episode:
                script.episode = ln.split("Script #")[-1].strip()
            if "Production #" in ln and not script.production_number:
                script.production_number = ln.split("Production #")[-1].strip()

        # Draft label: the line right before the date on cover
        for i, ln in enumerate(lines):
            if ln.endswith("Draft") and not script.draft_label:
                # The next non-empty line that looks like a date
                for j in range(i + 1, min(i + 6, len(lines))):
                    candidate = lines[j]
                    if "/" in candidate and any(c.isdigit() for c in candidate):
                        script.draft_label = ln
                        script.draft_date = candidate
                        break
            # Handle "Shooting Draft" followed by colored revision lines
            # like "Blue - 9/11/25" / "White - 9/5/25"
            if ln == "Shooting Draft" and not script.draft_label:
                for j in range(i + 1, min(i + 4, len(lines))):
                    candidate = lines[j]
                    # Parse colored revision entries
                    parts = candidate.split(" - ")
                    if len(parts) == 2 and "/" in parts[1]:
                        color, date = parts[0].strip(), parts[1].strip()
                        script.revision_history.append((date, color, "Full"))
                # The latest (top) colored entry is the current draft
                if script.revision_history:
                    date, color, _ = script.revision_history[0]
                    script.draft_label = f"Shooting Draft - {color}"
                    script.draft_date = date

        # Parse SCRIPT REVISION LIST block if present
        if "SCRIPT REVISION LIST" in text:
            capture = False
            for ln in lines:
                if ln == "SCRIPT REVISION LIST":
                    capture = True
                    continue
                if not capture:
                    continue
                if ln in ("DATE COLOR PAGES", "DATE  COLOR  PAGES"):
                    continue
                # Format is typically: "09/11/25 Blue 28-28A (Scene 26)"
                parts = ln.split(None, 2)
                if len(parts) >= 3 and "/" in parts[0]:
                    date, color, pages = parts[0], parts[1], parts[2]
                    # Replace an earlier "Full" placeholder if we already
                    # inferred from cover
                    existing = next(
                        (i for i, (d, c, _) in enumerate(script.revision_history)
                         if d == date and c == color), None,
                    )
                    if existing is not None:
                        script.revision_history[existing] = (date, color, pages)
                    else:
                        script.revision_history.append((date, color, pages))
                elif script.revision_history and ln and "/" not in ln.split()[0]:
                    # Continuation line for previous entry (e.g. "31-33 (Scene 30)")
                    d, c, p = script.revision_history[-1]
                    script.revision_history[-1] = (d, c, f"{p}; {ln}")
                else:
                    # Blank line or section change → stop capturing
                    if not ln or ln.startswith('"') or ln.startswith("Shooting"):
                        capture = False

        # Cast list: "NAME.......ACTOR" pattern on page 2
        if "CAST LIST" in text:
            for ln in lines:
                if "." in ln and ln.count(".") >= 4:
                    # Split on the dot run
                    name_part, _, actor_part = ln.partition(".")
                    # Rebuild properly: split at the last dot run
                    import re
                    m = re.match(r"^([A-Z][A-Z \-\.]*?)\.{2,}(.+)$", ln)
                    if m:
                        name = m.group(1).strip()
                        actor = m.group(2).strip()
                        if name and actor and name not in script.cast:
                            script.cast[name] = actor


# ---------------------------------------------------------------------------
# Scene segmentation
# ---------------------------------------------------------------------------

def _parse_slug(slug: str) -> tuple[str, str, str, str]:
    """Parse a slug line like 'INT. ZONE B - PATIENT ROOM 112 - DAY (D1)'
    into (int_ext, location, time_of_day, story_day)."""
    int_ext = ""
    for prefix in ("INT./EXT.", "INT/EXT.", "I/E.", "INT.", "EXT."):
        if slug.startswith(prefix):
            int_ext = prefix
            rest = slug[len(prefix):].strip()
            break
    else:
        rest = slug

    # Extract story day (D1, N2, etc.) in parens at end
    story_day = ""
    if rest.endswith(")") and "(" in rest:
        open_idx = rest.rfind("(")
        paren = rest[open_idx + 1:-1].strip()
        # Story day markers are typically short: D1, N2, CONTINUOUS, etc.
        if len(paren) <= 20:
            story_day = paren
            rest = rest[:open_idx].strip()

    # Location and time-of-day split on final " - "
    # e.g. "ZONE B - PATIENT ROOM 112 - DAY"
    location = rest
    time_of_day = ""
    if " - " in rest:
        parts = rest.rsplit(" - ", 1)
        # Last segment is usually DAY/NIGHT/CONTINUOUS etc.
        last = parts[1].strip()
        if last.upper() in {"DAY", "NIGHT", "MORNING", "EVENING",
                            "CONTINUOUS", "LATER", "MOMENTS LATER",
                            "DUSK", "DAWN", "SAME", "SAME TIME"}:
            location = parts[0].strip()
            time_of_day = last

    return int_ext, location, time_of_day, story_day


def _fix_doubled_chars(text: str) -> str:
    """Fix dual-layer PDF rendering artefact where every character is doubled.

    Some PDF generators (e.g. certain Final Draft exports) render the same
    text in two stacked font layers (bold + regular) at identical coordinates.
    pdfplumber merges both layers and produces e.g. 'DDIINNAA' instead of 'DINA'.
    Detected when len is even and text[0::2] == text[1::2].
    """
    if len(text) >= 2 and len(text) % 2 == 0:
        if text[0::2] == text[1::2]:
            return text[0::2]
    return text


def _extract_running_header_metadata(pdf: pdfplumber.PDF, script: Script) -> None:
    """Fallback: extract show title and episode from the running page header.

    Some scripts (e.g. network multi-camera) don't have a standard cover page
    — the title and episode are only in the running header on each scene page.
    Header format: SHOW TITLE  "Episode Title"  Draft Label  Date  CO/page
    e.g.: SUPERSTORE  "Floor Supervisor"  Concept Draft  9/17/20 CO/2.
    """
    import re
    for page in pdf.pages[1:6]:   # skip cover, look at early scene pages
        # The header line is the topmost text, typically y < 30
        chars = page.chars
        if not chars:
            continue
        min_y = min(c["top"] for c in chars)
        header_chars = [c for c in chars if c["top"] < min_y + 4]
        if not header_chars:
            continue
        raw = "".join(c["text"] for c in header_chars)
        # Deduplicate doubled header (two font layers)
        raw = _fix_doubled_chars(raw)
        # If still doubled at word level, take first half
        if len(raw) > 4 and raw[:len(raw)//2] == raw[len(raw)//2:]:
            raw = raw[:len(raw)//2]

        # Pattern: SHOW  "Episode"  Draft Label  Date  ...
        m = re.search(r'^(.+?)\s+"([^"]+)"\s+(.+?Draft(?:[^0-9]*))\s+([\d/]+)', raw)
        if m:
            if not script.title:
                script.title = m.group(1).strip()
            if not script.episode:
                # episode title goes into title field if no other title found
                ep_title = m.group(2).strip()
                if not script.title or script.title == ep_title:
                    script.title = ep_title
                else:
                    # Show title already set — store episode title separately
                    script.title = script.title  # keep show title
            if not script.draft_label:
                script.draft_label = m.group(3).strip().rstrip()
            if not script.draft_date:
                script.draft_date = m.group(4).strip()
            break  # found what we need


def _drop_page_header(words: list[dict]) -> list[dict]:
    """Remove running header (top of page) and page number (bottom)."""
    # Running header starts near y=50-90 and is typically a single line
    # containing the title / script info / draft color / date / page id.
    if not words:
        return words
    min_y = min(w["top"] for w in words)
    # Drop words within 15 pts of the top if they span the width
    header_ys = {round(w["top"]) for w in words if w["top"] < min_y + 8}
    # Also drop the very last line if it's just a page number
    max_y = max(w["top"] for w in words)
    footer_ys = {round(w["top"]) for w in words if w["top"] > max_y - 4}

    keep = []
    for w in words:
        if round(w["top"]) in header_ys and w["top"] < 90:
            continue
        if round(w["top"]) in footer_ys and w["top"] > 720:
            continue
        keep.append(w)
    return keep


def _is_scene_header_line(line: list[dict]) -> str | None:
    """If this line is a scene slug (with a scene number in the left
    margin AND typically the matching number in the right margin),
    return the scene number. Else None.

    We accept any body text, not just INT./EXT., because some shows use
    custom slug formats like 'CHARACTER TALKING HEAD - LOCATION' for
    confessional scenes.
    """
    if not line:
        return None
    left_nums = [w for w in line if w["x0"] < 70 and _is_scene_number(w["text"])]
    right_nums = [w for w in line if w["x0"] > 510 and _is_scene_number(w["text"])]
    action_words = [w for w in line if abs(w["x0"] - X_ACTION) < X_TOL]
    if not left_nums or not action_words:
        return None
    # Require the left scene number to match the right scene number when
    # a right number exists (this is the canonical Final Draft pattern and
    # prevents false positives on lines that merely start with a number).
    if right_nums and left_nums[0]["text"] != right_nums[0]["text"]:
        return None
    # If no right margin match, be conservative: only accept INT./EXT. slugs
    # or slugs whose body clearly looks like a scene header.
    if not right_nums:
        first_word = action_words[0]["text"]
        if first_word not in {"INT.", "EXT.", "INT/EXT.", "INT./EXT.", "I/E."}:
            return None
    return left_nums[0]["text"]


def parse_script(pdf_path: str | Path) -> Script:
    """Main entry: parse a script PDF into a Script object.

    Consecutive same-kind lines at the same indentation and within a small
    vertical gap are merged into single blocks so multi-line paragraphs and
    multi-line speeches come through as one block (which is essential for a
    clean semantic diff).
    """
    pdf_path = Path(pdf_path)
    script = Script(source_path=str(pdf_path))

    # Max vertical gap (in points) between lines still considered part of
    # the same paragraph/block. Courier 12pt line height is ~12pt; paragraph
    # breaks typically introduce ~24pt spacing.
    MAX_CONTINUATION_GAP = 16

    with pdfplumber.open(pdf_path) as pdf:
        _extract_cover_metadata(pdf, script)
        # Fallback for scripts that carry title/draft info only in page headers
        if not script.title or not script.draft_label:
            _extract_running_header_metadata(pdf, script)

        current_scene: Scene | None = None
        last_block_y: float | None = None
        for page_idx, page in enumerate(pdf.pages):
            page_num = page_idx + 1
            words = page.extract_words(keep_blank_chars=False)
            words = _drop_page_header(words)
            lines = _group_words_into_lines(words)

            # Reset continuation tracker at every page boundary — scripts
            # nearly always start a fresh block on a new page.
            last_block_y = None

            for line in lines:
                line_y = min((w["top"] for w in line), default=0.0)

                scene_num = _is_scene_header_line(line)
                if scene_num is not None:
                    if current_scene is not None:
                        script.scenes.append(current_scene)
                    _, slug_text, revised = _classify_line(line)
                    int_ext, loc, tod, sday = _parse_slug(slug_text)
                    current_scene = Scene(
                        number=scene_num,
                        slug=slug_text,
                        int_ext=int_ext,
                        location=loc,
                        time_of_day=tod,
                        story_day=sday,
                        start_page=page_num,
                        revised=revised,
                    )
                    last_block_y = line_y
                    continue

                if current_scene is None:
                    continue

                kind, text, revised = _classify_line(line)
                if kind == "skip" or not text:
                    continue

                # Merge into previous block if it's a continuation of a
                # multi-line paragraph / speech.
                can_merge = (
                    current_scene.blocks
                    and current_scene.blocks[-1].kind == kind
                    and kind in ("action", "dialogue", "parenthetical")
                    and last_block_y is not None
                    and (line_y - last_block_y) < MAX_CONTINUATION_GAP
                    and current_scene.blocks[-1].page == page_num
                )
                if can_merge:
                    prev = current_scene.blocks[-1]
                    prev.text = (prev.text + " " + text).strip()
                    if revised:
                        prev.revised = True
                else:
                    current_scene.blocks.append(
                        Block(kind=kind, text=text,
                              page=page_num, revised=revised)
                    )
                if revised:
                    current_scene.revised = True
                last_block_y = line_y

        if current_scene is not None:
            script.scenes.append(current_scene)

    return script


# ---------------------------------------------------------------------------
# CLI / debug
# ---------------------------------------------------------------------------

def summarize(script: Script) -> str:
    out = []
    out.append(f"Title:           {script.title}")
    out.append(f"Episode:         {script.episode}")
    out.append(f"Production #:    {script.production_number}")
    out.append(f"Draft:           {script.draft_label}  ({script.draft_date})")
    out.append(f"Revision hist:   {script.revision_history}")
    out.append(f"Cast entries:    {len(script.cast)}")
    out.append(f"Scenes:          {len(script.scenes)}")
    if script.scenes:
        out.append("First 5 scenes:")
        for s in script.scenes[:5]:
            out.append(
                f"  #{s.number:>6s}  {s.int_ext:4s} {s.location[:40]:40s}  "
                f"{s.time_of_day:8s}  ({s.story_day})  p{s.start_page}  "
                f"blocks={len(s.blocks)}  chars={s.characters[:4]}"
            )
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    s = parse_script(sys.argv[1])
    print(summarize(s))
