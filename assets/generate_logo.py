#!/usr/bin/env python3
"""Generate ``assets/sift.gif`` — the animated ASCII wordmark in the README.

Renders a figlet "sift" and animates a left-to-right terminal reveal with a
blinking block cursor on a GitHub-dark background. Pure Pillow + pyfiglet —
no ffmpeg/VHS. Regenerate with:

    pip install pyfiglet pillow && python assets/generate_logo.py
"""
from __future__ import annotations

import os

from pyfiglet import Figlet
from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sift.gif")

BG = (13, 17, 23)        # GitHub dark
FG = (63, 185, 80)       # GitHub green
CURSOR = (88, 220, 110)
FONT_SIZE = 26
PAD = 28
FRAME_MS = 55
REVEAL_STEP = 2          # columns revealed per frame
BLINKS = 3
HOLD_MS = 1500           # pause on the full logo before looping


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in (
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/SFNSMono.ttf",
        "/Library/Fonts/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def ascii_art(text: str) -> list[str]:
    for name in ("ansi_shadow", "slant", "standard"):
        try:
            art = Figlet(font=name).renderText(text)
        except Exception:
            continue
        lines = art.split("\n")
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            w = max(len(line) for line in lines)
            return [line.ljust(w) for line in lines]
    return [text]


def main() -> None:
    lines = ascii_art("sift")
    cols = len(lines[0])
    rows = len(lines)
    font = load_font(FONT_SIZE)
    cw = font.getlength("M")
    line_h = int(FONT_SIZE * 1.18)
    W = int(PAD * 2 + cw * cols)
    H = int(PAD * 2 + line_h * rows)

    def frame(reveal: int, cursor: bool) -> Image.Image:
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        for r, line in enumerate(lines):
            d.text((PAD, PAD + r * line_h), line[:reveal], font=font, fill=FG)
        if cursor and reveal <= cols:
            x = PAD + cw * reveal
            d.rectangle([x, PAD, x + cw * 0.85, PAD + line_h * rows - 5], fill=CURSOR)
        return img

    frames, durs = [], []
    for c in range(0, cols + 1, REVEAL_STEP):          # wipe-reveal
        frames.append(frame(c, True)); durs.append(FRAME_MS)
    for _ in range(BLINKS):                              # blink on the full logo
        frames.append(frame(cols, True)); durs.append(380)
        frames.append(frame(cols, False)); durs.append(380)
    frames.append(frame(cols, False)); durs.append(HOLD_MS)

    frames[0].save(OUT, save_all=True, append_images=frames[1:],
                   duration=durs, loop=0, optimize=True, disposal=2)
    print(f"wrote {OUT}  {W}x{H}  {len(frames)} frames  {os.path.getsize(OUT) // 1024} KB")


if __name__ == "__main__":
    main()
