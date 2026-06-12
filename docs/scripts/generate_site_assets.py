#!/usr/bin/env python3
"""Generate bitmap assets for the GitHub Pages site."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
ASSETS = DOCS / "assets"
SNAPSHOT = ROOT / "tests" / "snapshots" / "snapshot-agents.txt"

BG = (4, 9, 8)
INK = (224, 238, 225)
MUTED = (123, 144, 130)
GREEN = (92, 255, 126)
CYAN = (61, 224, 238)
AMBER = (255, 192, 76)
RED = (255, 87, 91)
PANEL = (12, 22, 19)


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


MONO = "/usr/share/fonts/TTF/JetBrainsMono-Regular.ttf"
MONO_BOLD = "/usr/share/fonts/TTF/JetBrainsMono-Bold.ttf"
DISPLAY = "/usr/share/fonts/TTF/FantasqueSansMNerdFontMono-Bold.ttf"


def draw_scanlines(draw: ImageDraw.ImageDraw, width: int, height: int, alpha: int = 34) -> None:
    for y in range(0, height, 6):
        draw.line((0, y, width, y), fill=(255, 255, 255, alpha), width=1)


def highlight_color(text: str) -> tuple[int, int, int]:
    if "DONE" in text:
        return GREEN
    if "RUN" in text or "Live" in text:
        return CYAN
    if "PEND" in text or "Prompt" in text:
        return AMBER
    if "FAIL" in text or "CNCL" in text:
        return RED
    if "Agent Workflows" in text or "● agents" in text:
        return (245, 255, 232)
    return INK


def wrap_text_to_width(draw: ImageDraw.ImageDraw, text: str, text_font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Wrap words into lines that fit within the requested pixel width."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=text_font) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
    if current:
        lines.append(current)
    return lines


def make_tui_asset() -> None:
    snapshot = SNAPSHOT.read_text(encoding="utf-8").splitlines()
    width, height = 1900, 1160
    image = Image.new("RGB", (width, height), BG)
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for x in range(-220, width + 220, 90):
        draw.line((x, 0, x + 420, height), fill=(34, 70, 54, 46), width=2)
    for y in range(90, height, 92):
        draw.line((0, y, width, y), fill=(43, 106, 71, 36), width=1)

    frame = (98, 86, width - 98, height - 92)
    draw.rounded_rectangle(frame, radius=34, fill=PANEL + (238,), outline=GREEN + (180,), width=3)
    draw.rectangle((frame[0], frame[1], frame[2], frame[1] + 72), fill=(18, 31, 27, 255))
    for i, color in enumerate((RED, AMBER, GREEN)):
        x = frame[0] + 34 + i * 34
        draw.ellipse((x, frame[1] + 25, x + 15, frame[1] + 40), fill=color + (235,))
    draw.text((frame[0] + 142, frame[1] + 23), "workflow tui / agents / live output", fill=(176, 214, 182), font=font(MONO_BOLD, 24))
    draw.text((frame[2] - 320, frame[1] + 23), "stateful multi-agent control", fill=CYAN, font=font(MONO_BOLD, 24))

    text_font = font(MONO, 20)
    bold_font = font(MONO_BOLD, 20)
    x0, y0 = frame[0] + 34, frame[1] + 102
    line_h = 30
    for index, line in enumerate(snapshot[:31]):
        y = y0 + index * line_h
        if y > frame[3] - 36:
            break
        color = highlight_color(line)
        active = "▸" in line or "● agents" in line
        draw.text((x0, y), line, fill=color + (255,), font=bold_font if active else text_font)

    for x in (frame[0] + 505, frame[0] + 1035, frame[2] - 420):
        draw.line((x, frame[1] + 90, x, frame[3] - 26), fill=CYAN + (60,), width=1)

    glow = overlay.filter(ImageFilter.GaussianBlur(14))
    image = Image.alpha_composite(image.convert("RGBA"), glow)
    image = Image.alpha_composite(image, overlay)
    scan = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw_scanlines(ImageDraw.Draw(scan), width, height)
    image = Image.alpha_composite(image, scan).convert("RGB")
    image.save(ASSETS / "workflow-tui.png", quality=94)


def make_social_card() -> None:
    width, height = 1600, 900
    image = Image.new("RGB", (width, height), BG)
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for y in range(0, height, 72):
        draw.line((0, y, width, y + 240), fill=(58, 255, 126, 44), width=2)
    for x in range(70, width, 110):
        draw.line((x, 0, x - 280, height), fill=(61, 224, 238, 34), width=1)
    draw.rectangle((92, 92, width - 92, height - 92), outline=GREEN + (210,), width=4)
    draw.rectangle((122, 122, width - 122, height - 122), outline=CYAN + (90,), width=2)

    display = font(DISPLAY, 122)
    mono = font(MONO_BOLD, 37)
    badge_font = font(MONO_BOLD, 29)
    draw.text((168, 208), "Codex", fill=INK + (255,), font=display)
    draw.text((168, 333), "Workflow", fill=GREEN + (255,), font=display)
    tagline = "stateful multi-agent coding runs, live TUI, ccc/OpenCode workers"
    for line_index, line in enumerate(wrap_text_to_width(draw, tagline, mono, width - 348)):
        draw.text((174, 516 + line_index * 48), line, fill=(186, 208, 185, 255), font=mono)
    badge_width = 205
    for idx, label in enumerate(("runs", "phases", "agents", "events", "artifacts")):
        x = 178 + idx * 230
        draw.rounded_rectangle((x, 666, x + badge_width, 724), radius=9, fill=(16, 29, 24, 255), outline=(70, 160, 100, 160), width=2)
        draw.text((x + 24, 681), label, fill=(GREEN if label == "agents" else MUTED) + (255,), font=badge_font)

    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    image.save(ASSETS / "social-card.png", quality=94)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    make_tui_asset()
    make_social_card()
    print("generated docs/assets/workflow-tui.png and docs/assets/social-card.png")


if __name__ == "__main__":
    main()
