"""컬러 영상을 하치와레 스타일의 ASCII 애니메이션으로 변환한다.

    python render.py input.mp4 ascii.mp4
    python render.py --episode-11 source.mp4 master.mp4
"""

import argparse
import json
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from ascii_grid import COLS, FPS, RAMP, ROWS, TOP, W, glyph_atlas, pixels, write_frames, write_mp4

FUR, HAIR, BLUSH, WOOD, LEAF, PAPER = range(6)
LOOK = {
    FUR:   ((227, 228, 229), 0.30, True),
    HAIR:  ((105, 174, 213), 0.17, True),
    BLUSH: ((224, 146, 173), 0.24, True),
    WOOD:  ((207, 151,  93), 0.18, True),
    LEAF:  ((137, 184, 130), 0.08, False),
}
SUB = 3
ASPECT = W / TOP
LIGHT = np.array([-0.45, -0.55, 0.70])
STILL, EASE, MARGIN = 6.0, 0.25, 0.18


def video_size(path):
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
           "stream=width,height,sample_aspect_ratio:stream_side_data=rotation", "-of", "json", str(path)]
    streams = json.loads(subprocess.check_output(cmd))["streams"]
    if not streams:
        raise ValueError("영상 트랙이 없습니다.")
    stream = streams[0]
    w, h = stream["width"], stream["height"]
    ratio = stream.get("sample_aspect_ratio", "")
    if ":" in ratio:
        num, den = map(int, ratio.split(":"))
        if num > 0 and den > 0:
            w = w * num / den
    rotation = next((d["rotation"] for d in stream.get("side_data_list", []) if "rotation" in d), 0)
    if round(rotation / 90) % 2:
        w, h = h, w
    scale = min(1, 1280 / max(w, h))
    return max(1, round(w * scale)), max(1, round(h * scale))


def frames(path, size):
    w, h = size
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:v:0",
           "-vf", f"fps={FPS},scale={w}:{h}:flags=area", "-pix_fmt", "rgb24",
           "-f", "rawvideo", "pipe:1"]
    frame_bytes = w * h * 3
    with subprocess.Popen(cmd, stdout=subprocess.PIPE) as process:
        try:
            while buf := process.stdout.read(frame_bytes):
                if len(buf) != frame_bytes:
                    raise ValueError("입력 영상의 마지막 프레임을 끝까지 읽지 못했습니다.")
                yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
        finally:
            process.stdout.close()
        if process.wait():
            raise RuntimeError("FFmpeg 디코딩에 실패했습니다.")


def classify(rgb):
    c = rgb.astype(np.int16)
    r, g, b = c[..., 0], c[..., 1], c[..., 2]
    lum, chroma = c.mean(-1), c.max(-1) - c.min(-1)
    k = np.full(r.shape, PAPER, dtype=np.int8)
    k[(g - r > 8) & (g - b > 8)] = LEAF
    k[(r - b > 35) & (r > g + 8) & (lum < 215)] = WOOD
    k[(b - r > 25) & (b > 110)] = HAIR
    k[(r - g > 22) & (r > 190) & (b > g - 6)] = BLUSH
    k[(lum > 222) & (chroma <= 3)] = FUR
    k[(lum < 100) | ((lum < 140) & (chroma < 35))] = -1
    return k


def fur_or_paper(rgb, k, *, warm_fur=False):
    c = rgb.astype(float)
    lum, chroma = c.mean(-1), c.max(-1) - c.min(-1)
    white = (lum > 205) & (chroma <= 12) & np.isin(k, (FUR, PAPER))
    core = ndimage.binary_erosion(white, iterations=2)
    lab, n = ndimage.label(core)
    if not n:
        return k
    ids = np.arange(1, n + 1)
    if warm_fur:
        # 한국 방영본은 털이 따뜻한 흰색이고, 종이 배경은 조금 어둡거나 푸르다.
        r, g, b = c.transpose(2, 0, 1)
        paint = (r >= 246) & (g >= 244) & (r > g) & (np.abs(g - b) <= 2)
        fur = ndimage.mean(paint, lab, ids) > 0.40
    near = ndimage.distance_transform_edt(lab == 0, return_distances=False, return_indices=True)
    lab = np.where(white, lab[near[0], near[1]], 0)
    if not warm_fur:
        fur = ndimage.mean(chroma, lab, ids) <= 2.6
    size = np.bincount(lab.ravel(), minlength=n + 1)[1:]
    border = np.zeros(n + 1, bool)
    border[np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])] = True
    fur |= ~border[1:] & (size < 0.15 * white.size)
    k = k.copy()
    k[white] = np.where(np.concatenate([[False], fur])[lab[white]], FUR, PAPER)
    return k


def shot_box(histogram, w, h):
    # 각 x좌표의 빈도로 3·97 백분위수를 계산한다.
    cumulative = histogram.cumsum()
    lo, hi = 0, w
    if cumulative[-1]:
        ranks = (cumulative[-1] - 1) * np.array([0.03, 0.97])
        lower = np.searchsorted(cumulative, np.floor(ranks), side="right")
        upper = np.searchsorted(cumulative, np.ceil(ranks), side="right")
        lo, hi = (lower + (upper - lower) * (ranks % 1)) * w / len(histogram)
    width = np.clip((hi - lo) * 1.08, w * 0.49, w * 0.80)
    centre = np.clip((lo + hi) / 2, width / 2, w - width / 2)
    height = min(h, width / ASPECT)
    return centre - width / 2, (h - height) / 2, centre + width / 2, (h + height) / 2


def plan_shots(path, size):
    # 축소한 영상에서 장면 전환을 찾고 장면별 x 분포를 누적한다.
    shots = []
    histogram = np.zeros(192, dtype=np.int64)
    prev = None
    count = 0
    for count, frame in enumerate(frames(path, (192, 108)), start=1):
        if prev is not None and np.abs(frame.astype(np.int16) - prev).mean() > 35:
            shots.append((count - 1, shot_box(histogram, *size)))
            histogram.fill(0)
        histogram += np.isin(classify(frame), (HAIR, BLUSH, -1)).sum(axis=0)
        prev = frame
    if not count:
        raise ValueError("입력 영상에 읽을 프레임이 없습니다.")
    shots.append((count, shot_box(histogram, *size)))
    return shots


def depth(solid, steps=6):
    # 경계를 한 번에 한 칸씩 상하좌우로 넓힌다.
    edge = ~solid
    d = np.zeros(solid.shape)
    for _ in range(steps):
        edge = ndimage.binary_dilation(edge)
        d += ~edge
    return d / steps


def shade(puff):
    h = np.sqrt(np.clip(puff, 0, 1))
    gy, gx = np.gradient(h * 6)
    n = np.stack([-gx, -gy, np.ones_like(h)], -1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    lit = np.clip(n @ (LIGHT / np.linalg.norm(LIGHT)), 0, 1)
    return np.clip(0.55 + 0.25 * lit + 0.25 * h, 0, 1)


def letters(rgb, box, prev, *, warm_fur=False, density=1):
    x0, y0, x1, y1 = box
    sw = rgb.shape[1]
    cols, rows = COLS * density, ROWS * density
    gw = cols * SUB
    gh = round(rows * SUB * (y1 - y0) / ((x1 - x0) / ASPECT))
    gh = max(2 * SUB, min(rows * SUB, gh))
    nrows = gh // SUB
    full = max(gw, round(gw * sw / (x1 - x0)))
    wide = np.asarray(Image.fromarray(rgb).resize((full, gh), Image.Resampling.BOX,
                                                   box=(0, y0, sw, y1)))
    a = int(np.clip(round(x0 / sw * full), 0, full - gw))
    k = fur_or_paper(wide, classify(wide), warm_fur=warm_fur)[:nrows * SUB, a:a + gw]
    sub = wide[:nrows * SUB, a:a + gw]
    k = k.reshape(nrows, SUB, cols, SUB).transpose(0, 2, 1, 3).reshape(nrows, cols, -1)
    sub = sub.reshape(nrows, SUB, cols, SUB, 3).transpose(0, 2, 1, 3, 4).reshape(nrows, cols, -1, 3)
    cell = sub.mean(2)
    moved = np.ones(cell.shape[:2], bool) if prev is None else np.abs(cell - prev["cell"]).mean(-1) > STILL
    moved = ndimage.binary_dilation(moved, iterations=1)
    ease = np.where(moved, 1.0, EASE)
    ink = (k == -1).mean(-1)
    counts = np.stack([(k == c).sum(-1) for c in range(PAPER + 1)], -1)
    major = counts.argmax(-1)
    if prev is not None:
        ink = prev["ink"] + ease * (ink - prev["ink"])
        held = np.take_along_axis(counts, prev["major"][..., None], -1)[..., 0]
        major = np.where(~moved & (held >= 0.6 * counts.max(-1)), prev["major"], major)
    same_paint = k == major[..., None]
    samples = same_paint.sum(-1)
    total_lum = (sub.mean(-1) * same_paint).sum(-1)
    cell_lum = np.divide(total_lum, samples, out=np.full(cell.shape[:2], 180.0), where=samples > 0)
    near = ndimage.uniform_filter(ink, 3)
    lift = 0 if prev is None else np.where(prev["hole"], -MARGIN, MARGIN)
    hole = (ink > 0.5 + lift) | ((ink > 0.25 + lift) & (near < 0.35))
    solid = ~hole & (major != PAPER)
    bright = shade(depth(ndimage.binary_fill_holes(solid)))
    level = np.zeros(major.shape)
    for c, (_, floor, puffy) in LOOK.items():
        b = bright if puffy else np.full(major.shape, 0.75)
        if c == LEAF:
            b = b * np.clip((cell_lum - 120) / 110, 0.25, 1)
        level = np.where(major == c, floor + (1 - floor) * b, level)
    if prev is not None:
        same = (major == prev["major"]) & solid & prev["solid"]
        level = np.where(same, prev["level"] + ease * (level - prev["level"]), level)
    idx = 1 + np.minimum((level * len(RAMP)).astype(int), len(RAMP) - 1)
    if prev is not None:
        centre = (prev["idx"] - 0.5) / len(RAMP)
        idx = np.where(same & (np.abs(level - centre) < 1.3 / len(RAMP)), prev["idx"], idx)
    idx = np.where(major == LEAF, np.minimum(idx, 14), idx)
    state = dict(cell=cell, ink=ink, major=major, hole=hole, solid=solid, level=level, idx=idx)
    top = (rows - nrows) // 2
    visible = slice(top, top + nrows)
    chars = np.zeros((rows, cols), dtype=np.uint8)
    colors = np.zeros((rows, cols, 3), dtype=np.uint8)
    for c, (col, _, _) in LOOK.items():
        mask = solid & (major == c)
        tint = np.array(col) * (0.90 + 0.30 * level[mask, None])
        chars[visible][mask] = idx[mask]
        colors[visible][mask] = np.clip(tint, 0, 255)
    return (chars, colors), state


def render(path):
    size = video_size(path)
    shots = iter(plan_shots(path, size))
    end, box = next(shots)
    prev = None
    for index, frame in enumerate(frames(path, size)):
        if index == end:
            end, box = next(shots)
            prev = None
        grid, prev = letters(frame, box, prev)
        yield grid


# 한국어판 11화의 30 fps 출력 프레임 번호와 (가로 중심, 원본에서 사용할 폭).
# 같은 숏에서는 구도를 고정해 입이나 손이 움직여도 카메라가 따라 흔들리지 않는다.
EPISODE_SHOTS = {
    90:   (0.52, 0.64),  # 나무 그루터기 위에서 기타를 치는 하치와레
    293:  (0.54, 0.88),
    369:  (0.50, 0.90),  # 기타와 손
    422:  (0.50, 1.00),  # 왼쪽의 치이카와와 오른쪽의 하치와레
    505:  (0.40, 0.80),
    571:  (0.54, 0.65),
    849:  (0.52, 0.68),  # 박수
    918:  (0.50, 0.90),
    1020: (0.52, 0.64),
    1134: (0.52, 0.68),
    1231: (0.49, 0.84),  # 나뭇잎 아래의 두 친구
    1400: (0.54, 0.90),
    1475: (0.50, 0.95),
    1550: (0.50, 1.00),
    1606: (0.50, 1.00),  # 숲으로 디졸브하고 오른쪽에 '끝'이 들어온다.
}
CREDITS_START = 1802
WIDE_SHOTS = {422, 1231, 1606}


def small_glyphs(atlas, width, height):
    return np.stack([np.asarray(Image.fromarray(tile).resize((width, height), Image.Resampling.BOX))
                     for tile in atlas])


def print_strength(rgb):
    r, g, b = rgb.transpose(2, 0, 1)
    magenta = np.minimum((r - g - 5) / 62, (b - g + 12) / 65)
    return np.clip(magenta, 0, 1) * np.clip((38 - (r - b)) / 12, 0, 1)


def card_frame(frame, atlas, type_atlas, *, credits):
    # 가로 카드는 원래 비율로 배치한다. 밝은 종이는 검은 여백, 인쇄된 선은 밝은 문자다.
    im = Image.fromarray(frame)
    if not credits:
        margin = round(im.width * 0.08)
        im = im.crop((margin, 0, im.width - margin, im.height))
    _, ch, cw = atlas.shape
    cols = W // cw
    rows = round(W * im.height / im.width / ch)
    swatch = np.asarray(im.resize((cols, rows), Image.Resampling.BOX)).astype(np.int16)
    luma = swatch.mean(-1)
    chroma = swatch.max(-1) - swatch.min(-1)
    strength = np.clip(np.maximum((231 - luma) / 108, (chroma - 32) / 78), 0, 1)
    r, g, b = swatch.transpose(2, 0, 1)
    line = np.clip((150 - swatch.max(-1)) / 78, 0, 1)[..., None]
    color = (1 - line) * np.minimum(255, 1.20 * swatch + 12) + line * [246, 211, 190]
    if credits:
        faces = (r > 242) & (r - g >= 2) & (np.abs(b - g) < 6) & (chroma < 18)
        cream = (r > 235) & (r - g > 7) & (g > 185) & (g - b > 20)
        strength[faces | cream] = 0.96
        strength[ndimage.binary_dilation(faces | cream, iterations=2) & (luma < 96)] = 0
        # 분홍색 크레딧은 작은 글리프로 따로 그려 가는 한글 획을 살린다.
        left, top, right, bottom = cols // 16, rows // 7, cols * 15 // 16, rows * 4 // 5
        area = np.s_[top:bottom, left:right]
        printed = ndimage.binary_dilation(print_strength(swatch[area]) > 0.06)
        strength[area][printed] = 0
    else:
        # 타이틀 일러스트의 흰 털과 연필 해칭은 종이보다 따뜻한 색이다.
        figure = np.zeros_like(strength, dtype=bool)
        figure[rows // 6:rows * 7 // 10, cols * 35 // 100:cols * 74 // 100] = True
        fur = figure & (r >= g + 1) & (chroma < 28) & (luma > 115)
        strength[fur] = np.clip((luma[fur] - 100) / 150, 0.10, 1)
        color[fur] = [248, 239, 232]
        strength[ndimage.binary_dilation(fur, iterations=2) & (luma < 100)] = 0
    idx = np.where(strength > 0.055, 1 + np.rint((len(RAMP) - 1) * strength**0.72), 0)
    card = pixels(atlas, idx.astype(np.uint8), color)
    if credits:
        _, th, tw = type_atlas.shape
        x, y = left * cw, top * ch
        nx, ny = (right * cw - x) // tw, (bottom * ch - y) // th
        box = (x * im.width / W, y * im.height / len(card),
               (x + nx * tw) * im.width / W, (y + ny * th) * im.height / len(card))
        sample = np.asarray(im.resize((nx, ny), Image.Resampling.BOX, box=box)).astype(np.int16)
        opacity = print_strength(sample)**0.72
        ink_color = opacity[..., None] * [255, 166, 222]
        ink = pixels(type_atlas, np.zeros((ny, nx), np.uint8), ink_color)
        region = card[y:y + ny * th, x:x + nx * tw]
        np.maximum(region, ink, out=region)
    canvas = np.zeros((TOP, W, 3), np.uint8)
    margin = (TOP - len(card)) // 2
    canvas[margin:margin + len(card)] = card
    return canvas


def end_caption(canvas, frame, stage_rows, atlas):
    # 새하얀 말풍선이 오른쪽에서 들어온다. 안쪽만 작은 문자로 다시 그려 '끝'의 겹선을 보존한다.
    ch, cw = atlas.shape[1:]
    stage_height = stage_rows * 7
    stage_top = (ROWS * 2 - stage_rows) // 2 * 7
    x, y = W * 2 // 3, stage_height * 2 // 3
    nx, ny = (W - x) // cw, (stage_height - y) // ch
    im = Image.fromarray(frame)
    box = (x * im.width / W, y * im.height / stage_height,
           im.width, (y + ny * ch) * im.height / stage_height)
    sample = np.asarray(im.resize((nx, ny), Image.Resampling.BOX, box=box))
    lab, count = ndimage.label(sample.min(-1) >= 248)
    if not count:
        return
    areas = np.bincount(lab.ravel())[1:]
    if areas.max() < 50:
        return
    inside = ndimage.binary_fill_holes(lab == 1 + areas.argmax())
    light = np.clip((sample.mean(-1) - 65) / 170, 0, 1)
    color = light[..., None] * [250, 248, 247]
    ink = pixels(atlas, np.zeros((ny, nx), np.uint8), color)
    region = canvas[stage_top + y:stage_top + y + ny * ch, x:x + nx * cw]
    np.copyto(region, ink, where=inside.repeat(ch, 0).repeat(cw, 1)[..., None])


def episode_frames(path):
    width, height = video_size(path)
    atlas = glyph_atlas()
    card_atlas = small_glyphs(atlas, 4, 7)
    caption_atlas = small_glyphs(atlas[-1:], 2, 3)
    stamp = Image.fromarray(atlas[-1])
    stamp = np.asarray(stamp.crop(stamp.getbbox()).resize((2, 3), Image.Resampling.BOX))
    type_atlas = np.rint(stamp[None] * (255.0 / stamp.max())).astype(np.uint8)
    first_shot = min(EPISODE_SHOTS)
    for index, frame in enumerate(frames(path, (width, height))):
        if index < first_shot or index >= CREDITS_START:
            yield card_frame(frame, card_atlas, type_atlas, credits=index >= CREDITS_START)
            continue
        if index in EPISODE_SHOTS:
            centre, span = EPISODE_SHOTS[index]
            h = min(height, span * width / ASPECT)
            box = ((centre - span / 2) * width, (height - h) / 2,
                   (centre + span / 2) * width, (height + h) / 2)
            density = 2 if index in WIDE_SHOTS else 1
            prev = None
        grid, prev = letters(frame, box, prev, warm_fur=True, density=density)
        canvas = pixels(card_atlas if density == 2 else atlas, *grid)
        if index >= 1700:
            end_caption(canvas, frame, prev["cell"].shape[0], caption_atlas)
        yield canvas


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="컬러 영상을 하치와레 스타일의 ASCII 영상으로 변환합니다.")
    parser.add_argument("input", type=Path, help="ASCII로 변환할 컬러 영상")
    parser.add_argument("output", type=Path, help="저장할 MP4 파일")
    parser.add_argument("--episode-11", action="store_true", help="한국어판 11화 전체 구도와 원본 음성 사용")
    args = parser.parse_args()
    if args.episode_11:
        write_frames(args.output, episode_frames(args.input), audio_source=args.input)
    else:
        write_mp4(args.output, render(args.input))
