"""
Department classifier for Revision Radar changes.

Given a Change object, decide which crew departments are impacted. We use a
rules-first approach:

  1. Structural rules (by change kind): a slug change hits Locations, etc.
  2. Keyword lookup over the combined old+new text: "blood" → Makeup/Hair;
     "ambulance" → Transportation; "crane up" → Camera; etc.

Each department has a short code and a display color (used by the PDF
renderer). text_color is normally white; Stunts uses black-on-white.
The classifier is deterministic and runs offline — no API calls.

Departments (Ross v3 — 12 consolidated departments):
    Production / Locations, Camera, Grip / Electric, Art / Set Dec,
    Props, Makeup / Hair, Costumes, Transportation,
    Casting / Extras, SPFX, VFX, Stunts.

Explicitly excluded: Script/Continuity, Color, Music, Editorial,
Sound Post, Technical Advisors, Production Sound.

Camera: tagged ONLY for explicit camera-specific language (crane, steadicam,
special POV, video monitor, etc.) — NOT for generic slug or action changes.

Grip/Electric: tagged for INT/EXT flips and time-of-day changes (major
lighting resets), and explicit practical-lighting keywords. NOT tagged simply
because a location changed.

Stunts: tagged only for unambiguously dangerous physical action or explicit
stunt-coordinator references.

Dialogue changes are suppressed unless keywords find a production implication
(new prop, extra, element) — those are flagged dialogue_flag="tbd".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .differ import Change


# ---------------------------------------------------------------------------
# Department catalog
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Dept:
    code: str           # stable internal ID (used in dict keys)
    name: str           # full display name (legend)
    color: str          # hex background color for chip
    order: int          # display order in report
    text_color: str = "#FFFFFF"   # chip text color (override for STUNT)
    label: str = ""     # short chip label; defaults to code if empty


DEPARTMENTS: list[Dept] = [
    Dept("PRODLOC", "Production / Locations", "#1B2E6B", 10,  label="PROD/LOC"),
    Dept("CAM",     "Camera",                 "#C62828", 20),
    Dept("GRIPEL",  "Grip / Electric",        "#4E342E", 30,  label="GRIP/ELEC"),
    Dept("ART",     "Art / Set Dec",           "#B8860B", 40,  label="ART/SET"),
    Dept("PROPS",   "Props",                  "#BF5E00", 50),
    Dept("MUHAIR",  "Makeup / Hair",          "#F06292", 60,  label="MU/HAIR"),
    Dept("COST",    "Costume",                 "#6A1B9A", 70,  label="COSTUME"),
    Dept("TRANS",   "Transportation",         "#37474F", 80,  label="TRANSPO"),
    Dept("CASTEX",  "Casting / Background",   "#0277BD", 90,  label="CAST/BG"),
    Dept("SPFX",    "SPFX",                   "#546E7A", 100),
    Dept("VFX",     "VFX",                    "#388E3C", 110),
    Dept("STUNT",   "Stunts",                 "#FFFFFF", 120, text_color="#000000"),
]

BY_CODE = {d.code: d for d in DEPARTMENTS}


# ---------------------------------------------------------------------------
# Keyword dictionaries — extensible.
#
# Keys are department codes; values are regex patterns (lowercased match).
# Patterns are matched against the combined old+new text for any change.
# ---------------------------------------------------------------------------

KEYWORDS: dict[str, list[str]] = {
    # Camera: ONLY explicit camera gear/moves/POV references.
    "CAM": [
        r"\b(crane(?: up| down| shot)?|jib(?: arm)?|steadicam|technocrane|"
        r"dolly(?: in| out| move)?|camera car|"
        r"handheld|oner|one-?r|long take|"
        r"pov|point of view|subjective shot|"
        r"drone(?: shot)?|aerial(?: shot| view)?|"
        r"video monitor|on(?:-| )the monitor|on(?:-| )screen (?:via|from) camera|"
        r"security camera|cctv|surveillance(?: camera)?|"
        r"close on|tight on|push in|pull back|rack focus|"
        r"slow(?:-| )mo(?:tion)?|phantom|overcrank|undercrank|"
        r"insert shot of|over the shoulder shot|go[-\s]?pro|lipstick cam|"
        r"time lapse|timelapse|split diopter)\b",
    ],

    "PROPS": [
        r"\b(surgical marker|sharpie|clipboard|iv bag|ipad|iphone|phone|"
        r"syringe|gauze|bandage|stethoscope|chart|folder|notebook|"
        r"wallet|bills|cash|check|envelope|letter|note|key|keys|"
        r"gun|weapon|knife|flowers|bouquet|juicer|juice|cup|cups|"
        r"coffee|jar|bottle|book|books|laptop|computer|tablet|"
        r"cigarette|lighter|matches|mug|tray|mayo stand|rapid rhino|"
        r"bair hugger|hearing aid|wig|cart|console|marker|pen|pencil|"
        r"remote|tv remote|binder|box|bag|purse|card|cards|magazine|"
        r"newspaper|glasses|sunglasses|watch|ring|jewelry|drink|beer|"
        r"wine|glass|gift|package|badge|id card|scanner|"
        r"powder|reishi powder|produce|carrots|loquats|fruit|snack|food|"
        r"tea|coffee cup|water bottle|thermometer)\b",
    ],

    "COST": [
        r"\b(shirt|pants|dress|suit|tie|scrubs|coat|jacket|uniform|"
        r"hat|cap|helmet|shoes|boots|heels|sweater|skirt|blouse|"
        r"gown|bra|sock|socks|glove|gloves|scarf|apron|bathrobe|"
        r"robe|wardrobe|outfit|clothes|clothing|attire|stained|"
        r"torn|ripped|bloody clothes|wet clothes|covered in|wearing|dressed|"
        r"changes into|puts on|takes off|strips|naked|shirtless|"
        r"street clothes|lab coat|pj|pajama)\b",
    ],

    "MUHAIR": [
        r"\b(blood|bloody|bleeding|nose ?bleed|wound|injury|injured|"
        r"bruise|bruised|scar|scars|scarred|cut(?:s)?|scratch|"
        r"burn|burns|burned|stitches|tattoo|makeup|lipstick|"
        r"mascara|hair(?:cut| color| dye)?|beard|mustache|facial hair|sweat|"
        r"sweaty|tears|crying|cries|dirt|dirty|mud|muddy|"
        r"bandage|prosthetic)\b",
    ],

    "SPFX": [
        r"\b(explod(?:e|es|ing|ed)|explosion|fire(?: gag)?|flames|smoke|spark(?:s)?|"
        r"flash(?: pot)?|bang|rain(?: effect)?|snow(?: effect)?|wind(?: machine)?|"
        r"fog(?: machine)?|lightning|electrical arc|"
        r"water(?: effect| gag)|flood|wet floor|steam|"
        r"blood (?:squib|spray|spatter)|gunfire effect|"
        r"shatter(?:s|ed|ing)?|breakaway|breakaway glass)\b",
    ],

    "VFX": [
        r"\b(screen (?:insert|content|display)|iphone screen|ipad screen|"
        r"computer screen|instagram|text message|text on screen|"
        r"monitor display|hologram|green screen|cgi|composite|"
        r"practical screen|graphic overlay|logo|title card|"
        r"image on screen|camera feed|video(?: playback)?|"
        r"projection|vfx|visual effect)\b",
    ],

    # Stunts: only clear physical danger or explicit stunt references.
    "STUNT": [
        r"\b(stunt(?: coordinator| double| performer| rig| work|s)?|"
        r"wire(?: work| rig| flying)?|"
        r"fall(?:s|ing)? (?:from|off|over a|down (?:a flight of )?stairs?)|"
        r"vehicle (?:chase|crash|stunt|hit)|"
        r"fight (?:choreograph|scene|sequence|coordinator)|"
        r"throw(?:n|s)? (?:through|against|into|across|over)|"
        r"hit by (?:a |the )?(?:car|truck|bus|vehicle)|"
        r"body double|crash pad|safety mat|airbag)\b",
    ],

    "TRANS": [
        r"\b(car|cars|truck|trucks|van|ambulance|ambulances|"
        r"bus|taxi|uber|lyft|motorcycle|bike|bicycle|driving|drives|"
        r"drove|pulls up|pulls in|pulls away|drives off|parks|parked|"
        r"vehicle|vehicles|nd cars?|picture (?:car|vehicle)|"
        r"backs up|backing up|honking|engine|gas station)\b",
    ],

    "CASTEX": [
        r"\b(background|bg|bg (?:actors?|extras?)|extras?|"
        r"crowd|passersby|patients|visitors|nurses|doctors|"
        r"cafeteria (?:folks|staff|workers)|hospital staff|"
        r"(?:several|group of|some) (?:people|nurses|doctors|patients))\b",
    ],

    "ART": [
        # Set dressing, set dec, and art department elements (merged)
        r"\b(set dressing|set dec|set piece|blinds|curtains|drapes|"
        r"posters?|paintings?|artwork|pictures? on wall|wall(?: decor|paper)?|"
        r"furniture|couch|sofa|chair|table|desk|shelves|bookshelf|"
        r"lamp|lighting fixture|rug|carpet|plants?|flower(?: arrangement)?|vase|"
        r"vending machine|dry erase board|bulletin board|whiteboard|chalkboard|"
        r"sheets off bed|hospital bed|gurney|exam table|"
        r"trash|trash can|garbage|signage|sign)\b",
    ],

    # Grip/Electric: explicit lighting-rig or practical-lighting references.
    "GRIPEL": [
        r"\b(practical|practicals?|lighting rig|c-?stand|"
        r"lights? (?:come |go )?on|lights? (?:go |turn )?off|"
        r"lights? flicker|dim(?:s|med|ming)?|blackout|"
        r"power (?:out|cut|failure)|generator|"
        r"strobe(?: light)?|hazard (?:light|flash)|"
        r"dark(?:ness)?|pitch black|no (?:light|power))\b",
    ],
}


# ---------------------------------------------------------------------------
# Structural rules (by change.kind)
# ---------------------------------------------------------------------------

def _base_departments_for_kind(kind: str) -> list[str]:
    """Departments that are ALWAYS impacted by a given kind of change."""
    if kind == "draft_label_changed":
        return ["PRODLOC"]
    if kind in ("cast_set", "cast_replaced", "cast_removed"):
        return ["CASTEX", "PRODLOC"]
    if kind == "scene_added":
        # New scene: production/locations and art build it.
        return ["PRODLOC", "ART"]
    if kind == "scene_cut":
        return ["PRODLOC"]
    if kind == "slug_changed":
        # Location/time change: Production/Locations and Art/Set Dec care.
        # Camera and Grip/Electric only added via _slug_specific_departments
        # (INT/EXT flip or time-of-day change).
        return ["PRODLOC", "ART"]
    if kind.startswith("dialogue_"):
        # Suppressed unless keywords fire; handled by classify_all.
        return []
    if kind.startswith("parenthetical_"):
        # Performance direction only; suppressed unless keywords fire.
        return []
    if kind.startswith("action_"):
        # Action block changes: keyword-driven only (Camera NOT default).
        return []
    if kind == "character_added":
        return ["CASTEX", "COST", "MUHAIR"]
    if kind == "character_cut":
        return ["CASTEX"]
    return []


def _slug_specific_departments(change: Change) -> list[str]:
    """Refine slug_changed: add Camera & Grip/Electric only for INT/EXT flip
    or time-of-day changes — not for every location rename."""
    old, new = change.old_text, change.new_text
    tags: list[str] = []

    # INT ↔ EXT flip: big lighting/grip reset and camera setup change
    int_ext_changed = False
    for prefix_a, prefix_b in (("INT.", "EXT."), ("EXT.", "INT.")):
        if old.startswith(prefix_a) and new.startswith(prefix_b):
            int_ext_changed = True
            break
    if int_ext_changed:
        tags += ["GRIPEL", "CAM", "PRODLOC", "SPFX"]

    # Time of day change (DAY→NIGHT, etc.) — major lighting reset
    TODS = ("DAY", "NIGHT", "MORNING", "EVENING", "DUSK", "DAWN",
            "CONTINUOUS", "LATER", "MOMENTS LATER")
    def _tod(text: str) -> str:
        m = re.search(r" - (%s)\b" % "|".join(TODS), text)
        return m.group(1) if m else ""
    if _tod(old) != _tod(new) and _tod(old) and _tod(new):
        tags += ["GRIPEL"]   # Grip/Electric care; Camera doesn't necessarily

    return tags


def _keyword_departments(change: Change) -> list[str]:
    """Match the combined old+new text against keyword dictionaries."""
    text = " ".join(filter(None, [change.old_text, change.new_text])).lower()
    if not text:
        return []
    tags: list[str] = []
    for code, patterns in KEYWORDS.items():
        for p in patterns:
            if re.search(p, text):
                tags.append(code)
                break  # one hit per dept is enough
    return tags


def _character_specific_departments(change: Change) -> list[str]:
    """A named BG/Extras count change hits Casting/Extras."""
    if change.kind == "character_added" and change.new_text:
        name = change.new_text.upper()
        if "EXTRAS" in name or "CROWD" in name or "BG" in name:
            return ["CASTEX"]
    return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_change(change: Change) -> list[str]:
    """Return an ordered, de-duplicated list of department codes for a change."""
    codes: list[str] = []

    # 1. Base rules by kind
    codes.extend(_base_departments_for_kind(change.kind))

    # 2. Slug-specific refinements (INT/EXT flip, time-of-day)
    if change.kind == "slug_changed":
        codes.extend(_slug_specific_departments(change))

    # 3. Keyword dictionary over old+new text
    codes.extend(_keyword_departments(change))

    # 4. Character-specific
    codes.extend(_character_specific_departments(change))

    # De-dupe while preserving display order
    seen: set[str] = set()
    ordered: list[tuple[int, str]] = []
    for c in codes:
        if c in seen or c not in BY_CODE:
            continue
        seen.add(c)
        ordered.append((BY_CODE[c].order, c))
    ordered.sort()
    return [c for _, c in ordered]


def classify_all(changes: list[Change]) -> None:
    """Populate `departments` and `dialogue_flag` on every Change in-place.

    Dialogue and parenthetical changes are handled specially:
    - If keyword detection finds a production implication → dialogue_flag="tbd",
      departments = those keyword depts.
    - If no keywords fire → dialogue_flag="pure", departments=[].
      Pure dialogue changes are filtered out in the renderer.
    """
    for ch in changes:
        ch.departments = classify_change(ch)
        if ch.kind.startswith("dialogue_") or ch.kind.startswith("parenthetical_"):
            # Base rule returns [] for these; departments here = keyword hits only.
            if ch.departments:
                ch.dialogue_flag = "tbd"
            else:
                ch.dialogue_flag = "pure"
