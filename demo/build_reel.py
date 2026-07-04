#!/usr/bin/env python3
"""Inline demo/reel_frames.json into demo/reel_template.html -> demo/reel.html.

Keeps the template human-editable while producing a single self-contained
HTML file (no external fetch, no network) suitable for headless recording.

Usage:
    python demo/gen_frames.py     # regenerate reel_frames.json from the engine
    python demo/build_reel.py     # inline it into demo/reel.html
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).parent
TEMPLATE = HERE / "reel_template.html"
FRAMES = HERE / "reel_frames.json"
OUT = HERE / "reel.html"


def main() -> None:
    template = TEMPLATE.read_text()
    frames_json = FRAMES.read_text().strip()
    if "__FRAMES_JSON__" not in template:
        raise SystemExit("template missing __FRAMES_JSON__ placeholder")
    out = template.replace("__FRAMES_JSON__", frames_json)
    OUT.write_text(out)
    print(f"wrote {OUT} ({len(out)} bytes)")


if __name__ == "__main__":
    main()
