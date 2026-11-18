"""하치와레 ASCII와 실제 소스를 함께 보여 주는 16:9 QHD 영상.

    python hachiware_code_qhd.py
    python hachiware_code_qhd.py hachiware_full.mp4 another_cut.mp4

같은 폴더의 hachiware.py와 ascii_grid.py를 읽는다. NumPy, Pillow, FFmpeg가
필요하다. 기본 오디오는 입력 영상에 담긴 한국어판 노래와 대사, 효과음을 그대로 사용한다.
별도의 녹음으로 교체하려면 --voice 녹음.wav 를 지정한다.
"""

import argparse
import ast
import builtins
import io
import json
import keyword
import math
import os
import subprocess
import tempfile
import tokenize
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


HERE = Path(__file__).resolve().parent
W, H, FPS, FRAMES = 2560, 1440, 30, 2701
STAGE_W = 1376
EDIT_W = W - STAGE_W
BODY_X, BODY_Y, BODY_W, BODY_H = 27, 240, EDIT_W-54, 980
TEXT_X, ROW_H, FOCUS_Y = 103, 60, 459
PANEL_BG, INK, MUTED = "#151619", "#e1e3ea", "#a5a8b6"
LILAC, ROSE = "#a7a1d8", "#dd8eb4"
TYPE = dict(plain="#dce0e7", keyword="#d4a9c9", call="#b4b9e8", number="#dfb5ad",
            string="#acc9b0", comment="#9caeaa", constant="#c9bbdf", op="#b4bcc9")
MONOS = ("consola.ttf", "DejaVuSansMono.ttf")
KOREAN = ("NotoSansKR-VF.ttf", "NotoSansCJK-Regular.ttc", "malgun.ttf")


@dataclass(frozen=True)
class Cue:
    frame: int
    lines: tuple
    file: str = "hachiware.py"


def lines(first, last):
    return tuple(range(first, last + 1))


# The frame numbers follow the episode's hard cuts, the '끝' caption and the
# changes of credit card. Each entry points to the source that creates that view.
CUES = (
    Cue(0, lines(303, 305)), Cue(90, lines(20, 24)),
    Cue(293, lines(77, 79)), Cue(369, (76,)),
    Cue(422, (368, 370)), Cue(505, (96, 97)),
    Cue(571, lines(179, 182)), Cue(849, (248,)),
    Cue(918, lines(155, 160)), Cue(1134, lines(205, 207)),
    Cue(1231, lines(200, 204)), Cue(1400, lines(37, 43), "ascii_grid.py"),
    Cue(1606, (256,)), Cue(1700, lines(347, 349)),
    Cue(1802, lines(279, 282)), Cue(1937, lines(267, 270)),
    Cue(2045, lines(295, 298)), Cue(2166, lines(315, 318)),
    Cue(2380, lines(319, 324)), Cue(2571, lines(64, 66), "ascii_grid.py"),
)
STARTS = tuple(c.frame for c in CUES)


@lru_cache(maxsize=None)
def face(family, size):
    directories = (Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
                   Path("/mnt/c/Windows/Fonts"), Path.home() / ".local/share/fonts/win-ko",
                   Path("/usr/share/fonts/opentype/noto"), Path("/usr/share/fonts/truetype/noto"),
                   Path("/usr/share/fonts/truetype/dejavu"))
    for name in MONOS if family == "mono" else KOREAN:
        for path in [d / name for d in directories] + [name]:
            try:
                font = ImageFont.truetype(str(path), size)
            except OSError:
                continue
            if "-VF" in name:
                font.set_variation_by_axes([450])
            return font
    raise RuntimeError("Noto 한글 글꼴과 고정폭 글꼴이 필요합니다.")


def label(draw, xy, value, font, color, anchor="lm"):
    draw.text(xy, value, font=font, fill=color, anchor=anchor)


def token_style(tok, line):
    if tok.type == tokenize.NAME:
        if keyword.iskeyword(tok.string):
            return "keyword"
        if tok.string.isupper():
            return "constant"
        if line[tok.end[1]:].lstrip().startswith("(") or hasattr(builtins, tok.string):
            return "call"
    return {tokenize.NUMBER: "number", tokenize.STRING: "string", tokenize.COMMENT: "comment",
            tokenize.OP: "op"}.get(tok.type, "plain")


class Source:
    def __init__(self, path):
        source = path.read_text(encoding="utf-8")
        self.text = [""] + source.splitlines()
        self.styles = [["plain"] * len(line) for line in self.text]
        self.defs = [n for n in ast.parse(source).body if isinstance(n, (ast.FunctionDef, ast.Assign))]
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            a, b = tok.start[0], tok.end[0]
            if not (1 <= a <= b < len(self.text)):
                continue
            color = token_style(tok, self.text[a])
            if color == "plain":
                continue
            for n in range(a, b+1):
                lo = tok.start[1] if n == a else 0
                hi = tok.end[1] if n == b else len(self.text[n])
                self.styles[n][lo:hi] = [color] * (hi-lo)

    def scope(self, number):
        for node in self.defs:
            if node.lineno <= number <= node.end_lineno:
                if isinstance(node, ast.FunctionDef):
                    return node.name + "()"
                return ast.unparse(node.targets[0])
        return ""


def cells(char):
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def wrapped(line, columns):
    """Yield source column ranges and visual indents, without truncating code."""
    start, indent = 0, 0
    continuation = min(12, len(line) - len(line.lstrip()) + 4)
    while start < len(line):
        end = start
        used = indent
        last_break = None
        while end < len(line) and used + cells(line[end]) <= columns:
            used += cells(line[end])
            if (line[end].isspace() or line[end] in ",([{") and used > columns * .52:
                last_break = end+1
            end += 1
        if end < len(line) and last_break is not None:
            end = last_break
        yield start, end, indent
        start, indent = end, continuation
    if not line:
        yield 0, 0, 0


class Excerpt:
    def __init__(self, source, cue, duration):
        font = face("mono", 35)
        step = font.getlength("M")
        columns = int((BODY_W - TEXT_X - 19) / step)
        positions, rows = {}, []
        for n in range(max(1, cue.lines[0]-13), min(len(source.text), cue.lines[-1]+14)):
            first = len(rows)
            rows.extend((n, a, b, indent) for a, b, indent in wrapped(source.text[n], columns))
            positions[n] = (first * ROW_H, (len(rows)-first) * ROW_H)
        self.sheet = Image.new("RGBA", (BODY_W, len(rows) * ROW_H))
        d = ImageDraw.Draw(self.sheet)
        for index, (n, start, end, indent) in enumerate(rows):
            y = index * ROW_H + ROW_H//2
            if start == 0:
                label(d, (72, y), str(n), face("mono", 25), "#969ba8", "rm")
            else:
                d.line((65, y, 72, y), fill="#6d7383", width=2)
            pos, x = start, TEXT_X + step*indent
            while pos < end:
                wide = cells(source.text[n][pos]) == 2
                style = source.styles[n][pos]
                stop = pos+1
                while (stop < end and source.styles[n][stop] == style
                       and cells(source.text[n][stop]) == (2 if wide else 1)):
                    stop += 1
                part = source.text[n][pos:stop]
                if wide:
                    for char in part:
                        label(d, (round(x), y), char, face("sans", 29), TYPE[style])
                        x += 2*step
                else:
                    label(d, (round(x), y), part, font, TYPE[style])
                    x += len(part)*step
                pos = stop
        self.positions, self.cue = positions, cue
        self.scope = source.scope(cue.lines[0])
        weights = [1 + .42*(positions[n][1]/ROW_H-1) for n in cue.lines]
        self.stops = np.rint(np.r_[0, np.cumsum(weights)] / sum(weights) * duration).astype(int)

    def window(self, step, elapsed):
        current = self.cue.lines[step]
        previous = self.cue.lines[max(0, step-1)]
        travel = min(12, max(1, (self.stops[step+1] - self.stops[step]) // 3))
        t = min(1., elapsed/travel)
        # Gentle deceleration with a small settling movement at the new baseline.
        motion = 1-(1-t)**3*(1-1.6*t)
        y0, h0 = self.positions[previous]
        y1, h1 = self.positions[current]
        centre = (1-motion)*(y0+h0/2) + motion*(y1+h1/2)
        top = round(centre-FOCUS_Y)
        band = max(ROW_H, round((1-motion)*h0 + motion*h1)-5)
        a, b = round(FOCUS_Y-band/2), round(FOCUS_Y+band/2)
        body = Image.new("RGB", (BODY_W, BODY_H), PANEL_BG)
        d = ImageDraw.Draw(body)
        d.rounded_rectangle((6, a, BODY_W-6, b), radius=12, fill="#2a2b36")
        d.rounded_rectangle((7, a+6, 12, b-6), radius=3, fill=LILAC)
        visible = self.sheet.crop((0, top, BODY_W, top+BODY_H))
        body.paste(visible, (0, 0), visible)
        label(d, (72, y1+ROW_H//2-top), str(current), face("mono", 25), "#e1d7f6", "rm")
        # A short pop in the gutter marks the arrival of each line.
        if elapsed < 12:
            radius = round(7*math.sin(elapsed/12*math.pi)**1.4)
            if radius:
                cx, cy = 23, FOCUS_Y
                d.polygon([(cx,cy-radius), (cx+2,cy-2), (cx+radius,cy),
                           (cx+2,cy+2), (cx,cy+radius), (cx-2,cy+2),
                           (cx-radius,cy), (cx-2,cy-2)], fill=ROSE)
        body.paste(PANEL_BG, (0, 0, BODY_W, BODY_H), edge_fade())
        return body


@lru_cache(maxsize=1)
def edge_fade():
    y = np.arange(BODY_H)
    distance = np.minimum(y, BODY_H-1-y)
    alpha = np.rint(252*(1-np.clip(distance/123, 0, 1))**1.65).astype(np.uint8)
    return Image.fromarray(alpha[:, None]).resize((BODY_W, BODY_H))


class Editor:
    def __init__(self):
        self.sources = {name: Source(HERE / name) for name in {c.file for c in CUES}}
        self.index, self.last_view = None, None

    def set_cue(self, index):
        cue = CUES[index]
        end = STARTS[index+1] if index+1 < len(CUES) else FRAMES
        self.excerpt = Excerpt(self.sources[cue.file], cue, end-cue.frame)
        self.page = Image.new("RGB", (EDIT_W, H), PANEL_BG)
        d = ImageDraw.Draw(self.page)
        # A compact source-file tab. The film has no title bar or player UI.
        x, y = BODY_X+TEXT_X, 173
        mono = face("mono", 32)
        tab_width = round(mono.getlength(cue.file)) + 48
        d.rounded_rectangle((x+3, y-26+4, x+tab_width+3, y+27+4), radius=12, fill="#494559")
        d.rounded_rectangle((x, y-26, x+tab_width, y+27), radius=12,
                            fill="#202127", outline="#66647b", width=2)
        label(d, (x+24, y), cue.file, mono, INK)
        label(d, (x+tab_width+26, y), "/", face("mono", 29), "#818293")
        label(d, (x+tab_width+67, y), self.excerpt.scope, face("mono", 29), MUTED)
        self.index, self.last_view = index, None

    def frame(self, number):
        index = bisect_right(STARTS, number)-1
        if self.index != index:
            self.set_cue(index)
        local = number-STARTS[index]
        step = bisect_right(self.excerpt.stops, local)-1
        key = (step, min(12, local-int(self.excerpt.stops[step])))
        if key != self.last_view:
            self.page.paste(self.excerpt.window(*key), (BODY_X, BODY_Y))
            self.last_view = key
        return self.page


def streams(path):
    cmd = ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)]
    first = {}
    for stream in json.loads(subprocess.check_output(cmd))["streams"]:
        first.setdefault(stream["codec_type"], stream)
    return first


def render(movie, output, voice=None):
    if output.exists():
        raise FileExistsError(f"출력 파일이 이미 있습니다: {output}")
    original = streams(movie)
    video = original["video"]
    if (video["width"], video["height"], video["avg_frame_rate"], int(video["nb_frames"])) != (1080, 1232, "30/1", FRAMES):
        raise ValueError("이 장면표에는 1080×1232, 30 fps, 2701프레임인 하치와레 완성본이 필요합니다.")
    inputs = ["-i", str(movie)]
    if voice is None:
        if "audio" not in original:
            raise ValueError("입력 영상에 원본 오디오 트랙이 없습니다.")
        audio = ["-map", "1:a:0", "-c:a", "copy"]
        expected_seconds = float(original["audio"]["duration"])
    else:
        inputs += ["-i", str(voice)]
        voice_filter = ("loudnorm=I=-16:TP=-1.5:LRA=8,aresample=48000,apad,"
                        f"atrim=end_sample={FRAMES*48000//FPS},asetpts=N/SR/TB")
        audio = ["-map", "2:a:0", "-af", voice_filter, "-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
        expected_seconds = FRAMES/FPS
    editor = Editor()
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        staged = Path(temp) / "film.mp4"
        # Only black matte is trimmed. A uniform scale preserves the character's
        # proportions. Compose the source and the native-QHD type in YUV 4:2:0.
        graph = (f"[0:v]scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,setsar=1[code];"
                 f"[1:v]crop=1080:1080:0:76,scale=1340:1340:flags=lanczos,"
                 f"setsar=1,pad={STAGE_W}:{H}:18:50:black[film];"
                 f"[film][code]hstack=inputs=2:shortest=1,setsar=1[v]")
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-filter_complex_threads", "4",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{EDIT_W}x{H}", "-r", str(FPS), "-i", "pipe:0",
               *inputs, "-filter_complex", graph, "-map", "[v]", *audio,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "15", "-threads:v", "8", "-pix_fmt", "yuv420p",
               "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
               "-metadata:s:a:0", "language=kor",
               "-movflags", "+faststart", str(staged)]
        with subprocess.Popen(cmd, stdin=subprocess.PIPE) as writer:
            try:
                cached_page = cached_pixels = None
                for number in range(FRAMES):
                    page = editor.frame(number)
                    key = (editor.index, editor.last_view)
                    if cached_page != key:
                        cached_pixels = page.tobytes()
                        cached_page = key
                    writer.stdin.write(cached_pixels)
                    if number % 300 == 0:
                        print(f"{number:4d} / {FRAMES}", flush=True)
            finally:
                writer.stdin.close()
            if writer.wait():
                raise RuntimeError("FFmpeg가 QHD 영상을 저장하지 못했습니다.")
        final = streams(staged)
        frames = int(final["video"]["nb_frames"])
        seconds = float(final["audio"]["duration"])
        if frames != FRAMES or abs(seconds-expected_seconds) > 1/int(final["audio"]["sample_rate"]):
            raise RuntimeError(f"길이가 다릅니다. 영상 {frames}프레임, 오디오 {seconds:.6f}초")
        os.link(staged, output)
    print(output, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("movie", type=Path, nargs="?", default=HERE / "hachiware_full.mp4")
    parser.add_argument("output", type=Path, nargs="?", default=HERE / "hachiware_code_qhd.mp4")
    parser.add_argument("--voice", type=Path, help="직접 녹음한 해설 트랙")
    args = parser.parse_args()
    render(args.movie, args.output, args.voice)
