"""出陣ベルのプロフィールバナーを描く（680x240）。

模様は作り物ではなく、実際に生成した 45→0 の音声そのものの波形。
1秒間隔で46回読み上げているので、等間隔のパルス列になる。

Discordはバナーの左下にアイコンを重ねて表示するため、
文字はすべて上側の安全な帯に置く。
"""
import math
import os
import sys
import wave

from PIL import Image, ImageDraw, ImageFont

SRC = sys.argv[1] if len(sys.argv) > 1 else "voice/countdown_45-0_sp3_x1.2.wav"
OUT = sys.argv[2] if len(sys.argv) > 2 else "banner.png"

W, H = 680, 240
SS = 4

GROUND_TOP = (31, 37, 46)
GROUND_BOTTOM = (15, 18, 23)
BRASS_DIM = (128, 95, 38)       # 波形の左端（まだ時間がある）
BRASS_BRIGHT = (244, 203, 122)  # 右端（0が近い）
LINE = (54, 46, 27)
LABEL = (176, 141, 74)

MARGIN_X = 26
BAR_PITCH = 3           # 最終解像度でのバー間隔
BAR_WIDTH = 2
BASELINE_Y = 136        # アイコンの重なりを避け、やや上に置く
MAX_HALF = 62

START, END = 45, 0      # 音声が読み上げる範囲
LABEL_AT = (30, 20, 10, 0)


def vertical_gradient(size, top, bottom):
    strip = Image.new("RGB", (1, size[1]))
    pixels = strip.load()
    for y in range(size[1]):
        t = y / (size[1] - 1)
        pixels[0, y] = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom))
    return strip.resize(size)


def envelope(path, buckets):
    """音声を buckets 個に分け、各区間の最大振幅を 0..1 で返す。"""
    with wave.open(path, "rb") as reader:
        frames = reader.getnframes()
        width = reader.getsampwidth()
        channels = reader.getnchannels()
        raw = reader.readframes(frames)

    step = width * channels
    peaks = []
    for i in range(buckets):
        lo = (frames * i // buckets) * step
        hi = (frames * (i + 1) // buckets) * step
        peak = 0
        # 全サンプルを見る必要はない。区間内を間引いて最大値を拾う
        for p in range(lo, hi, step * max(1, (hi - lo) // step // 400 or 1)):
            value = int.from_bytes(raw[p:p + 2], "little", signed=True)
            if abs(value) > peak:
                peak = abs(value)
        peaks.append(peak)

    top = max(peaks) or 1
    # そのままだと小さい音が潰れるので、少し持ち上げる
    return [(p / top) ** 0.72 for p in peaks]


def mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def load_font(size):
    for name in ("consolab.ttf", "consola.ttf", "segoeuib.ttf"):
        path = os.path.join("C:\\Windows\\Fonts", name)
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def main():
    canvas = (W * SS, H * SS)
    image = vertical_gradient(canvas, GROUND_TOP, GROUND_BOTTOM)
    draw = ImageDraw.Draw(image)

    span = (W - MARGIN_X * 2) * SS
    left = MARGIN_X * SS
    base = BASELINE_Y * SS

    # 基準線
    draw.rectangle([left, base - SS // 2, left + span, base + SS // 2], fill=LINE)

    bars = span // (BAR_PITCH * SS)
    levels = envelope(SRC, bars)

    for i, level in enumerate(levels):
        t = i / max(1, bars - 1)
        x = left + i * BAR_PITCH * SS
        half = max(SS, level * MAX_HALF * SS)
        draw.rounded_rectangle(
            [x, base - half, x + BAR_WIDTH * SS, base + half],
            radius=BAR_WIDTH * SS / 2,
            fill=mix(BRASS_DIM, BRASS_BRIGHT, t),
        )

    # 目盛り。読み上げる数字の位置に合わせて置く
    font = load_font(15 * SS)
    total = START - END
    for n in LABEL_AT:
        t = (START - n) / (total + 1)     # 音声は最後の数字のあとに余韻がある
        x = left + span * t + BAR_WIDTH * SS / 2
        label = str(n)
        box = draw.textbbox((0, 0), label, font=font)
        draw.text(
            (x - (box[2] - box[0]) / 2, 30 * SS),
            label,
            font=font,
            fill=LABEL,
        )
        # ラベルと波形をつなぐ細いティック
        draw.rectangle(
            [x - SS // 2, 52 * SS, x + SS // 2, 62 * SS],
            fill=LINE,
        )

    image = image.resize((W, H), Image.LANCZOS)
    image.save(OUT, "PNG", optimize=True)
    print(f"{OUT}  {W}x{H}  {os.path.getsize(OUT) / 1024:.0f} KB  （バー {bars} 本）")


if __name__ == "__main__":
    main()
