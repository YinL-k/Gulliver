"""Render the last screen of an asciinema v2 cast as a static terminal SVG.

Usage: python docs/cast2svg.py demo.cast docs/demo.svg   (needs: pip install pyte)
"""

import html
import json
import re
import sys

import pyte

COLORS = {"default": "#d4d4d4", "black": "#1e1e1e", "red": "#f44747", "green": "#6a9955", "brown": "#d7ba7d",
          "yellow": "#d7ba7d", "blue": "#569cd6", "magenta": "#c586c0", "cyan": "#4ec9b0", "white": "#d4d4d4"}
CW, CH, PAD, BAR = 8.4, 18, 16, 30


def render(src: str, out: str) -> None:
    with open(src, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    head = json.loads(lines[0])
    cols, rows = head["width"], head["height"]
    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)
    for line in lines[1:]:
        _, kind, data = json.loads(line)
        if kind == "o":
            stream.feed(data)
    width, height = int(cols * CW + 2 * PAD), int(rows * CH + 2 * PAD + BAR)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}" role="img">',
             f'<rect width="{width}" height="{height}" rx="8" fill="#1e1e1e"/>',
             '<circle cx="20" cy="15" r="6" fill="#ff5f58"/><circle cx="40" cy="15" r="6" fill="#ffbd2e"/>'
             '<circle cx="60" cy="15" r="6" fill="#18c132"/>',
             '<g font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace" font-size="14">']
    for y in range(rows):
        row = screen.buffer[y]
        x = 0
        while x < cols:
            fg, bold = row[x].fg, row[x].bold
            j, text = x, ""
            while j < cols and row[j].fg == fg and row[j].bold == bold:
                text += row[j].data
                j += 1
            color = COLORS.get(fg, "#" + fg if len(fg) == 6 else "#d4d4d4")
            weight = ' font-weight="bold"' if bold else ""
            # one <text> per run of words, placed on the character grid, so spacing survives any renderer
            for m in re.finditer(r"\S+(?: \S+)*", text):
                parts.append(f'<text x="{PAD + (x + m.start()) * CW:.1f}" y="{BAR + PAD + (y + 0.8) * CH:.1f}" '
                             f'fill="{color}"{weight}>{html.escape(m.group())}</text>')
            x = j
    parts += ["</g>", "</svg>"]
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))


if __name__ == "__main__":
    render(sys.argv[1], sys.argv[2])
