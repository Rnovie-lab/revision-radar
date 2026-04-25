"""
Revision Radar CLI.

Usage:
    python3 cli.py <old_script.pdf> <new_script.pdf> [-o output.pdf]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from revision_radar import parse_script, diff_scripts
from revision_radar.classifier import classify_all
from revision_radar.report import render_report, render_all_dept_reports


def main() -> None:
    p = argparse.ArgumentParser(description="Revision Radar — script revision analyzer")
    p.add_argument("old", help="Older script PDF (e.g. Concept draft)")
    p.add_argument("new", help="Newer script PDF (e.g. Shooting draft)")
    p.add_argument("-o", "--out", default=None,
                   help="Output PDF path (default: <new>_revision_radar.pdf)")
    p.add_argument("--threshold", type=int, default=3,
                   help="Max depts before a change moves to General-only")
    p.add_argument("--dept", default=None,
                   help="Filter scene changes to one dept code (e.g. PROPS). "
                        "Generates a single targeted dept report.")
    p.add_argument("--all-depts", action="store_true",
                   help="Generate one targeted PDF per active department, "
                        "named <output_stem>_<DEPT>.pdf, for distribution.")
    args = p.parse_args()

    old = parse_script(args.old)
    new = parse_script(args.new)

    print(f"Parsed: {old.title} — {old.draft_label} ({old.draft_date}) "
          f"{len(old.scenes)} scenes")
    print(f"Parsed: {new.title} — {new.draft_label} ({new.draft_date}) "
          f"{len(new.scenes)} scenes")

    changes = diff_scripts(old, new)
    classify_all(changes)
    print(f"Diff produced {len(changes)} changes")

    out = args.out
    if out is None:
        out = Path(args.new).with_suffix("").as_posix() + "_revision_radar.pdf"

    # Always generate the master report
    out_path = render_report(old, new, changes, out,
                             general_only_threshold=args.threshold,
                             dept_filter=args.dept)
    print(f"Master report: {out_path}")

    # Optionally generate one PDF per active department
    if args.all_depts:
        out_stem = Path(out).stem.replace("_revision_radar", "")
        out_dir  = Path(out).parent
        dept_files = render_all_dept_reports(
            old, new, changes, out_dir, out_stem,
            general_only_threshold=args.threshold,
        )
        print(f"Dept reports ({len(dept_files)}):")
        for f in dept_files:
            print(f"  {f.name}")


if __name__ == "__main__":
    main()
