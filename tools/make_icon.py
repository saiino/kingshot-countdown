"""出陣ベルのアイコンを描く。

構成: カウントダウンのリング（真鍮）の中にベル（クリーム）。
Discordはアイコンを円形に切り抜き、メンバー一覧では32px程度で表示されるので、
細部を詰め込まず、太い形と高いコントラストで作る。

4倍で描いてから縮小し、輪郭を滑らかにする。
"""
import math
import os
import sys

from PIL import Image, ImageDraw

OUT = sys.argv[1] if len(sys.argv) > 1 else "icon.png"

FINAL = 1024
SS = 4            # スーパーサンプリング倍率
S = FINAL * SS

# マニュアルと揃えた配色
GROUND_TOP = (31, 37, 46)
GROUND_BOTTOM = (16, 19, 24)
TRACK = (58, 48, 26)        # リングの下地（残り時間の溝）
BRASS = (232, 185, 92)      # 進行中のアーク
BRASS_DEEP = (176, 130, 48)
CREAM = (244, 228, 195)     # ベル本体
CREAM_SHADE = (206, 182, 138)

cx = cy = S / 2


def vertical_gradient(size, top, bottom):
    """上から下へのグラデーションを1枚作る。"""
    image = Image.new("RGB", (1, size))
    pixels = image.load()
    for y in range(size):
        t = y / (size - 1)
        pixels[0, y] = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom))
    return image.resize((size, size))


CROWN = 0.34      # 頭頂部の幅（最大幅に対する比）
FLARE = 0.30      # 裾の張り出し


def bell_profile(t):
    """0（頭頂）〜1（裾）に対する半幅を、最大幅を1として返す。

    鐘は「細い冠 → ゆるやかに広がる胴 → 最後で一気に開く口」という形。
    t**1.5 でゆっくり広げ、t**12 の項で最後だけ強く張り出させる。
    ここを sin のように早く広げると、鐘ではなく円錐（料理のフタ）に見える。
    """
    w = (CROWN + (1 - CROWN) * t ** 1.5) * (1 + FLARE * t ** 12)
    return w / ((1.0) * (1 + FLARE))     # t=1 で 1 になるよう正規化


def bell_outline(top_y, height, half_width):
    """ベルの輪郭を点列で返す。"""
    steps = 400
    right = [
        (cx + half_width * bell_profile(i / steps), top_y + height * (i / steps))
        for i in range(steps + 1)
    ]

    # 頭頂部の丸み。冠の幅ぶんのドームをかぶせる
    cap_r = right[0][0] - cx
    cap = [
        (cx + cap_r * math.cos(math.pi + math.pi * i / 72),
         top_y + cap_r * math.sin(math.pi + math.pi * i / 72) * 0.95)
        for i in range(72 + 1)
    ]

    left = [(2 * cx - x, y) for x, y in reversed(right)]
    return cap + right + left


def draw_ring(draw):
    """外周のカウントダウンリング。上から時計回りに3/4だけ点灯させる。"""
    outer = S * 0.405
    width = S * 0.058
    box = [cx - outer, cy - outer, cx + outer, cy + outer]

    draw.arc(box, 0, 360, fill=TRACK, width=round(width))
    draw.arc(box, -90, 180, fill=BRASS, width=round(width))

    # アークの始点に丸いキャップを置き、切りっぱなしに見せない
    r = width / 2
    draw.ellipse([cx - r, cy - outer - r, cx + r, cy - outer + r], fill=BRASS)


def draw_bell(draw):
    top_y = cy - S * 0.170
    height = S * 0.275
    half_width = S * 0.173
    rim_y = top_y + height

    # 吊り手のループ
    loop_r = S * 0.046
    loop_w = S * 0.028
    loop_cy = top_y - loop_r * 0.62
    draw.ellipse(
        [cx - loop_r, loop_cy - loop_r, cx + loop_r, loop_cy + loop_r],
        outline=CREAM,
        width=round(loop_w),
    )

    draw.polygon(bell_outline(top_y, height, half_width), fill=CREAM)

    # 口の帯。本体の最大幅とちょうど揃えないと、裾が羽のように飛び出す
    rim_half = half_width * 1.02
    rim_h = S * 0.046
    draw.rounded_rectangle(
        [cx - rim_half, rim_y - rim_h * 0.55, cx + rim_half, rim_y + rim_h * 0.45],
        radius=rim_h * 0.5,
        fill=CREAM,
    )

    # 舌（クラッパー）
    clap_r = S * 0.038
    clap_cy = rim_y + rim_h * 0.45 + clap_r * 0.85
    draw.ellipse(
        [cx - clap_r, clap_cy - clap_r, cx + clap_r, clap_cy + clap_r],
        fill=BRASS,
    )


def main():
    image = vertical_gradient(S, GROUND_TOP, GROUND_BOTTOM)
    draw = ImageDraw.Draw(image)

    draw_ring(draw)
    draw_bell(draw)

    image = image.resize((FINAL, FINAL), Image.LANCZOS)
    image.save(OUT, "PNG", optimize=True)
    print(f"{OUT}  {image.size[0]}x{image.size[1]}  {os.path.getsize(OUT) / 1024:.0f} KB")


if __name__ == "__main__":
    main()
