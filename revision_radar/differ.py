"""
Scene alignment and block-level diffing.

Given two Script objects (old, new), produce a list of Change objects that
describe what changed: scenes added/cut, slugs changed, dialogue or action
added/removed/modified, cast additions, and metadata shifts.

Changes are intentionally small and atomic so the classifier can tag each
one by department, and the renderer can display one change per row.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Literal

from .parser import Block, Scene, Script


ChangeKind = Literal[
    "scene_added", "scene_cut", "scene_moved",
    "slug_changed",
    "dialogue_added", "dialogue_cut", "dialogue_changed",
    "action_added", "action_cut", "action_changed",
    "character_added", "character_cut",
    "parenthetical_added", "parenthetical_cut", "parenthetical_changed",
    "cast_set", "cast_replaced", "cast_removed",
    "draft_label_changed",
]


@dataclass
class Change:
    kind: ChangeKind
    scene_number: str = ""         # "" for global (cast, metadata)
    scene_slug: str = ""           # for context
    page: int = 0
    old_text: str = ""
    new_text: str = ""
    character: str = ""            # for dialogue changes
    block_kind: str = ""           # dialogue | action | slug | character | ...
    departments: list[str] = field(default_factory=list)
    # "pure"  → dialogue/parenthetical with no production keywords → filtered out
    # "tbd"   → dialogue/parenthetical with keyword hit → shown as *Dialogue only TBD
    # ""      → not a dialogue/parenthetical change
    dialogue_flag: str = ""
    # Raw summary line used for the "General Changes" section
    summary: str = ""

    def describe(self) -> str:
        """Short human-readable line."""
        if self.summary:
            return self.summary
        if self.kind == "scene_added":
            return f"Scene {self.scene_number} ADDED — {self.scene_slug}"
        if self.kind == "scene_cut":
            return f"Scene {self.scene_number} CUT — {self.scene_slug}"
        if self.kind == "slug_changed":
            return (f"Scene {self.scene_number} slug changed: "
                    f"{self.old_text!r} → {self.new_text!r}")
        if self.kind == "dialogue_added":
            who = f" ({self.character})" if self.character else ""
            return f"Scene {self.scene_number}{who} dialogue added: {self.new_text!r}"
        if self.kind == "dialogue_cut":
            who = f" ({self.character})" if self.character else ""
            return f"Scene {self.scene_number}{who} dialogue cut: {self.old_text!r}"
        if self.kind == "dialogue_changed":
            who = f" ({self.character})" if self.character else ""
            return (f"Scene {self.scene_number}{who} dialogue changed: "
                    f"{self.old_text!r} → {self.new_text!r}")
        if self.kind.startswith("action_"):
            verb = self.kind.split("_")[1]
            return f"Scene {self.scene_number} action {verb}: {self.new_text or self.old_text!r}"
        if self.kind == "character_added":
            return f"Scene {self.scene_number}: {self.new_text} added"
        if self.kind == "character_cut":
            return f"Scene {self.scene_number}: {self.old_text} removed"
        if self.kind in ("parenthetical_added", "parenthetical_cut",
                         "parenthetical_changed"):
            verb = self.kind.split("_")[1]
            who = f" ({self.character})" if self.character else ""
            return f"Scene {self.scene_number}{who} parenthetical {verb}: {self.new_text or self.old_text!r}"
        if self.kind == "cast_set":
            return f"Casting: {self.character} set to {self.new_text}"
        if self.kind == "cast_replaced":
            return f"Casting: {self.character} {self.old_text} → {self.new_text}"
        if self.kind == "cast_removed":
            return f"Casting: {self.character} removed ({self.old_text})"
        if self.kind == "draft_label_changed":
            return f"Draft: {self.old_text} → {self.new_text}"
        return f"{self.kind}: {self.old_text!r} → {self.new_text!r}"


# ---------------------------------------------------------------------------
# Diffing helpers
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Collapse whitespace, unify quote marks, strip (CONT'D) markers."""
    t = text.replace("\u2019", "'").replace("\u2018", "'")
    t = t.replace("\u201c", '"').replace("\u201d", '"')
    t = t.replace("\u2014", "--").replace("\u2013", "-")
    t = re.sub(r"\(CONT'?D\)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _signature(block: Block) -> str:
    """A normalized signature for matching similar blocks across drafts."""
    return f"{block.kind}|{_normalize(block.text)}"


def _similar(a: str, b: str, threshold: float = 0.6) -> bool:
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


# ---------------------------------------------------------------------------
# Scene-level diff
# ---------------------------------------------------------------------------

def _diff_scene(old: Scene, new: Scene) -> list[Change]:
    """Produce Change objects comparing two versions of the same scene."""
    changes: list[Change] = []
    ctx = {
        "scene_number": new.number,
        "scene_slug": new.slug,
        "page": new.start_page,
    }

    # Slug change
    if _normalize(old.slug) != _normalize(new.slug):
        changes.append(Change(
            kind="slug_changed",
            old_text=old.slug, new_text=new.slug, block_kind="slug",
            **ctx,
        ))

    # Character list changes (presence of characters in scene)
    old_chars = set(old.characters)
    new_chars = set(new.characters)
    for c in new_chars - old_chars:
        changes.append(Change(kind="character_added", new_text=c,
                              block_kind="character", **ctx))
    for c in old_chars - new_chars:
        changes.append(Change(kind="character_cut", old_text=c,
                              block_kind="character", **ctx))

    # Block-level diff. We restrict to content blocks (action, dialogue,
    # parenthetical) — slugs and characters are handled above.
    def _keep(b: Block) -> bool:
        return b.kind in ("action", "dialogue", "parenthetical")

    old_blocks = [b for b in old.blocks if _keep(b)]
    new_blocks = [b for b in new.blocks if _keep(b)]

    old_sigs = [_signature(b) for b in old_blocks]
    new_sigs = [_signature(b) for b in new_blocks]

    matcher = difflib.SequenceMatcher(None, old_sigs, new_sigs, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            for b in old_blocks[i1:i2]:
                changes.append(_block_change("cut", b, None, ctx))
        elif tag == "insert":
            for b in new_blocks[j1:j2]:
                changes.append(_block_change("added", None, b, ctx))
        elif tag == "replace":
            # Pair up each old/new as a modification if they're the same kind
            # and reasonably similar; otherwise treat as delete + insert.
            olds = old_blocks[i1:i2]
            news = new_blocks[j1:j2]
            pairs, leftover_old, leftover_new = _pair_replaced(olds, news)
            for o, n in pairs:
                changes.append(_block_change("changed", o, n, ctx))
            for b in leftover_old:
                changes.append(_block_change("cut", b, None, ctx))
            for b in leftover_new:
                changes.append(_block_change("added", None, b, ctx))

    return changes


def _pair_replaced(olds: list[Block], news: list[Block]) -> tuple[
    list[tuple[Block, Block]], list[Block], list[Block]
]:
    """Greedy pairing: match each old block to the most-similar same-kind new
    block above a threshold; return pairs and leftovers."""
    pairs: list[tuple[Block, Block]] = []
    used_new: set[int] = set()
    leftover_old: list[Block] = []
    for o in olds:
        best_j = -1
        best_ratio = 0.0
        for j, n in enumerate(news):
            if j in used_new or n.kind != o.kind:
                continue
            r = difflib.SequenceMatcher(
                None, _normalize(o.text), _normalize(n.text)
            ).ratio()
            if r > best_ratio:
                best_ratio = r
                best_j = j
        if best_j >= 0 and best_ratio >= 0.4:
            pairs.append((o, news[best_j]))
            used_new.add(best_j)
        else:
            leftover_old.append(o)
    leftover_new = [n for j, n in enumerate(news) if j not in used_new]
    return pairs, leftover_old, leftover_new


def _block_change(
    verb: str, old: Block | None, new: Block | None, ctx: dict,
) -> Change:
    """Build a Change for an add/cut/modify of a single block."""
    b = new or old
    assert b is not None
    character = _infer_speaker(new or old)
    block_kind = b.kind
    # Map block kinds to Change kinds
    kind_map = {
        ("dialogue", "added"): "dialogue_added",
        ("dialogue", "cut"): "dialogue_cut",
        ("dialogue", "changed"): "dialogue_changed",
        ("action", "added"): "action_added",
        ("action", "cut"): "action_cut",
        ("action", "changed"): "action_changed",
        ("parenthetical", "added"): "parenthetical_added",
        ("parenthetical", "cut"): "parenthetical_cut",
        ("parenthetical", "changed"): "parenthetical_changed",
    }
    change_kind = kind_map.get((block_kind, verb), f"{block_kind}_{verb}")
    return Change(
        kind=change_kind,  # type: ignore[arg-type]
        block_kind=block_kind,
        old_text=old.text if old else "",
        new_text=new.text if new else "",
        character=character,
        **ctx,
    )


def _infer_speaker(block: Block | None) -> str:
    """For dialogue/parenthetical blocks we don't know the speaker from the
    block alone — the caller should enrich after. For now return empty."""
    return ""


def _attach_speakers(scene: Scene, changes: list[Change]) -> None:
    """Walk the scene and annotate dialogue/parenthetical changes with the
    most-recent CHARACTER cue that precedes them."""
    # Build a lookup: for each block (by identity in text order), the
    # character in scope at that point.
    speaker = ""
    block_to_speaker: dict[int, str] = {}
    for idx, b in enumerate(scene.blocks):
        if b.kind == "character":
            speaker = b.text.split("(")[0].strip()
        else:
            block_to_speaker[id(b)] = speaker
    # Now fix up the changes: match by text to find the block
    for ch in changes:
        if ch.block_kind not in ("dialogue", "parenthetical"):
            continue
        target = ch.new_text or ch.old_text
        for b in scene.blocks:
            if b.kind == ch.block_kind and b.text == target:
                ch.character = block_to_speaker.get(id(b), "")
                break


# ---------------------------------------------------------------------------
# Metadata + cast diff
# ---------------------------------------------------------------------------

def _diff_metadata(old: Script, new: Script) -> list[Change]:
    out: list[Change] = []
    if old.draft_label != new.draft_label or old.draft_date != new.draft_date:
        out.append(Change(
            kind="draft_label_changed",
            old_text=f"{old.draft_label} ({old.draft_date})",
            new_text=f"{new.draft_label} ({new.draft_date})",
        ))
    return out


def _diff_cast(old: Script, new: Script) -> list[Change]:
    out: list[Change] = []
    all_names = set(old.cast) | set(new.cast)
    for name in sorted(all_names):
        o = old.cast.get(name)
        n = new.cast.get(name)
        if o == n:
            continue
        if o is None:
            out.append(Change(kind="cast_set", character=name, new_text=n or ""))
        elif n is None:
            out.append(Change(kind="cast_removed", character=name, old_text=o or ""))
        else:
            # TBD → real cast counts as "set", not "replaced", which is more
            # intuitive for the report.
            if (o or "").strip().upper() == "TBD":
                out.append(Change(kind="cast_set", character=name,
                                  old_text=o, new_text=n))
            else:
                out.append(Change(kind="cast_replaced", character=name,
                                  old_text=o, new_text=n))
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def diff_scripts(old: Script, new: Script) -> list[Change]:
    """Produce the full ordered list of changes between two scripts."""
    all_changes: list[Change] = []

    # Global / metadata changes first
    all_changes.extend(_diff_metadata(old, new))
    all_changes.extend(_diff_cast(old, new))

    # Index scenes by number
    old_by_num = {s.number: s for s in old.scenes}
    new_by_num = {s.number: s for s in new.scenes}

    # Order of appearance in the new script (to keep report in script order),
    # with cut scenes threaded in at roughly where they used to be.
    seen: set[str] = set()
    for s_new in new.scenes:
        seen.add(s_new.number)
        s_old = old_by_num.get(s_new.number)
        if s_old is None:
            all_changes.append(Change(
                kind="scene_added",
                scene_number=s_new.number,
                scene_slug=s_new.slug,
                page=s_new.start_page,
            ))
            continue
        scene_changes = _diff_scene(s_old, s_new)
        _attach_speakers(s_new, scene_changes)
        all_changes.extend(scene_changes)

    for s_old in old.scenes:
        if s_old.number in seen:
            continue
        all_changes.append(Change(
            kind="scene_cut",
            scene_number=s_old.number,
            scene_slug=s_old.slug,
            page=s_old.start_page,
        ))

    return all_changes
