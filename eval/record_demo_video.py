# eval/record_demo_video.py
"""把一条已有 trace 收成一段 mp4。只读文件，不跑任务，不连编辑器。

画面上同时有三样：任务行里的验收句、test 行的裁判摘要、trace 文件名。
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRACE = ROOT / "data" / "traces" / "demo-c-patrol-20261016-124807.jsonl"
REPORTS = Path(__file__).resolve().parent / "reports"
FONT_PATH = Path(r"C:\Windows\Fonts\msyh.ttc")
WIDTH = 1280
HEIGHT = 720
FPS = 8
SECONDS = 12


def _load(path: Path) -> tuple[str, str]:
    query = ""
    summary = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("node") == "task" and row.get("query"):
            query = str(row["query"])
        if row.get("node") == "test" and row.get("judge_kind") == "automation" and row.get("summary"):
            summary = str(row["summary"])
    if not query or not summary:
        raise SystemExit(f"trace missing acceptance query or automation summary: {path}")
    return query, summary


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    ascii_run = ""
    for char in text:
        if char.isascii() and not char.isspace():
            ascii_run += char
            continue
        if ascii_run:
            tokens.append(ascii_run)
            ascii_run = ""
        tokens.append(char)
    if ascii_run:
        tokens.append(ascii_run)
    return tokens


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for token in _tokens(text):
        piece = token
        if draw.textlength(piece, font=font) > width:
            piece = ""
            for char in token:
                trial = piece + char
                if piece and draw.textlength(trial, font=font) > width:
                    if current:
                        lines.append(current)
                        current = ""
                    lines.append(piece)
                    piece = char
                else:
                    piece = trial
        glued = current + piece
        if not current or draw.textlength(glued, font=font) <= width:
            current = glued
            continue
        if piece.strip("，。、；：,. ") == "":
            current = glued
            continue
        lines.append(current)
        current = piece.lstrip() if piece[:1].isspace() else piece
    if current:
        lines.append(current)
    return lines


def _frame(filename: str, query: str, summary: str, progress: float) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#16181d")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.truetype(str(FONT_PATH), 22, index=0)
    name_font = ImageFont.truetype(str(FONT_PATH), 34, index=0)
    body_font = ImageFont.truetype(str(FONT_PATH), 26, index=0)
    summary_font = ImageFont.truetype(str(FONT_PATH), 28, index=0)
    margin = 64
    text_width = WIDTH - margin * 2
    y = 48

    def block(label: str, body: str, font: ImageFont.FreeTypeFont, fill: str) -> None:
        nonlocal y
        draw.text((margin, y), label, font=title_font, fill="#8ab4f8")
        y += 36
        for line in _wrap(draw, body, font, text_width):
            draw.text((margin, y), line, font=font, fill=fill)
            y += font.size + 10
        y += 28

    block("trace 文件名", filename, name_font, "#f4f6f8")
    block("验收句", query, body_font, "#e8eaed")
    draw.text((margin, y), "裁判摘要", font=title_font, fill="#8ab4f8")
    y += 36
    summary_lines = _wrap(draw, summary, summary_font, text_width - 36)
    box_height = 24 + len(summary_lines) * (summary_font.size + 10)
    draw.rounded_rectangle((margin, y, WIDTH - margin, y + box_height), radius=12, fill="#1e3326")
    line_y = y + 12
    for line in summary_lines:
        draw.text((margin + 18, line_y), line, font=summary_font, fill="#b6f0c2")
        line_y += summary_font.size + 10

    bar_y = HEIGHT - 28
    draw.rectangle((margin, bar_y, WIDTH - margin, bar_y + 8), fill="#2c313a")
    filled = margin + int((WIDTH - margin * 2) * progress)
    draw.rectangle((margin, bar_y, filled, bar_y + 8), fill="#8ab4f8")
    return image


def _write(path: Path, filename: str, query: str, summary: str) -> None:
    import imageio_ffmpeg

    path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{WIDTH}x{HEIGHT}",
        "-r", str(FPS),
        "-i", "-",
        "-an",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(path),
    ]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    count = FPS * SECONDS
    try:
        for index in range(count):
            frame = _frame(filename, query, summary, (index + 1) / count)
            proc.stdin.write(frame.tobytes())
    finally:
        proc.stdin.close()
    stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
    code = proc.wait()
    if code != 0:
        raise SystemExit(stderr[-800:] or f"ffmpeg exit {code}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", type=Path, default=DEFAULT_TRACE)
    args = parser.parse_args()
    trace = args.trace if args.trace.is_absolute() else ROOT / args.trace
    if not trace.is_file():
        print(f"trace not found: {trace}")
        return 1
    if not FONT_PATH.is_file():
        print(f"font not found: {FONT_PATH}")
        return 1
    query, summary = _load(trace)
    dest = REPORTS / f"{trace.stem}.mp4"
    _write(dest, trace.name, query, summary)
    sys.stdout.reconfigure(encoding="utf-8")
    print(dest)
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
