"""문자 격자를 그리고 MP4 파일로 저장한다."""

import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

CW, CH = 8, 14
COLS, ROWS = 135, 88
W, TOP = CW * COLS, CH * ROWS
FPS = 30
RAMP = ".,:;-~=+!i/lraecnosS$5%#&8B0@"
GLYPHS = " " + RAMP


def glyph_atlas():
    names = ("DejaVuSansMono-Bold.ttf", "consolab.ttf", "Menlo.ttc", "Courier New Bold.ttf")
    for name in names:
        try:
            font = ImageFont.truetype(name, 12)
            break
        except OSError:
            continue
    else:
        font = ImageFont.load_default(size=12)
    atlas = np.empty((len(GLYPHS), CH, CW), dtype=np.uint8)
    for index, char in enumerate(GLYPHS):
        tile = Image.new("L", (CW, CH))
        ImageDraw.Draw(tile).text((CW / 2, CH / 2), char, font=font, fill=255, anchor="mm")
        atlas[index] = np.asarray(tile)
    return atlas


def pixels(atlas, indices, colors):
    rows, cols = indices.shape
    _, ch, cw = atlas.shape
    alpha = atlas[indices].astype(np.uint16)
    rgb = np.clip(colors, 0, 255).astype(np.uint8)
    tiles = (alpha[..., None] * rgb[:, :, None, None, :]) // 255
    return tiles.astype(np.uint8).transpose(0, 2, 1, 3, 4).reshape(rows * ch, cols * cw, 3)


def write_mp4(output, grids):
    atlas = glyph_atlas()
    return write_frames(output, (pixels(atlas, indices, colors) for indices, colors in grids))


def write_frames(output, frames, *, audio_source=None):
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"출력 경로 {output}에 파일이 이미 있습니다.")
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        temp = Path(directory) / "render.mp4"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{TOP}", "-r", str(FPS),
               "-i", "pipe:0"]
        if audio_source is not None:
            cmd += ["-i", str(audio_source), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "copy"]
        else:
            cmd += ["-an"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temp)]
        with subprocess.Popen(cmd, stdin=subprocess.PIPE) as writer:
            try:
                count = 0
                for frame in frames:
                    writer.stdin.write(frame.tobytes())
                    count += 1
            finally:
                writer.stdin.close()
            if writer.wait():
                raise RuntimeError("FFmpeg 인코딩에 실패했습니다.")
            if not count:
                raise ValueError("출력할 프레임이 없습니다.")
        os.replace(temp, output)
    return count
