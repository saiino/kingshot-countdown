"""起動時のパネルを「この日時に起動した」とみなして試す。

    .venv\\Scripts\\python tools\\try_startup_panels.py "2026-09-26 19:00"          # 判定だけ（Discordには何もしない）
    .venv\\Scripts\\python tools\\try_startup_panels.py "2026-09-26 19:00" --post   # 実際に貼る
    .venv\\Scripts\\python tools\\try_startup_panels.py --goodbye                     # 貼ったパネルを「おやすみ中」にする

判定と貼り方は src/bot.py と同じ関数を使う。貼る先は .env のチャンネルID。
試すときはテスト用チャンネルのIDを書いておくこと。本物の493やJYPのIDが書いてあると、そこに貼られる。

Botを動かしたままでも使える。ただし「前回貼ったパネル」の記録（announced_panels.json）は
Botと共有しているので、Botが起動時に貼ったパネルもこれで消える。
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import bot  # noqa: E402

WEEKDAYS = "月火水木金土日"


def parse_when(text):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f'日時として読めません: {text}（例: "2026-09-26 19:00"）')


def describe(when):
    kind, ids = bot.channel_ids_for(when)
    print(f"{when:%Y-%m-%d}（{WEEKDAYS[when.weekday()]}）{when:%H:%M} に起動したとき")
    print(f"  判定  : {kind or '戦闘の夜ではない'}")
    print(f"  貼る先: {', '.join(str(i) for i in ids) or '（なし）'}")
    print(f"    .env 毎週   PANEL_CHANNEL_IDS         = {bot.PANEL_CHANNEL_IDS or '（空）'}")
    print(f"    .env 鯖戦   PANEL_CHANNELS_SERVER_WAR = {bot.PANEL_CHANNELS_SERVER_WAR or '（空）'}")
    print(f"    .env 国内戦 PANEL_CHANNELS_DOMESTIC   = {bot.PANEL_CHANNELS_DOMESTIC or '（空）'}")


async def run(when, post, goodbye):
    bell = bot.ShutsujinBell()
    # ゲートウェイには繋がず、HTTPだけで貼る。動いているBotの邪魔をしない
    await bell.login(bot.TOKEN)
    try:
        if post:
            await bell.post_startup_panels(now=when)
        if goodbye:
            await bell.say_goodbye()
    finally:
        # 終わるときに Bot 本体の後片付け（おやすみ中への書き換え）を走らせない
        bell.goodbye_done = True
        await bell.close()


def main():
    parser = argparse.ArgumentParser(
        description="起動時のパネルを、指定した日時に起動したとみなして試す"
    )
    parser.add_argument(
        "when", nargs="?", type=parse_when,
        help='起動したとみなす日時（例: "2026-09-26 19:00"）。省略すると今',
    )
    parser.add_argument("--post", action="store_true", help="判定だけでなく、実際にパネルを貼る")
    parser.add_argument("--goodbye", action="store_true", help="貼ってあるパネルを「おやすみ中」に書き換える")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("discord").setLevel(logging.WARNING)

    when = args.when or datetime.now()
    if args.when or not args.goodbye:
        describe(when)

    if not (args.post or args.goodbye):
        print("\n（判定だけ表示しました。実際に貼るなら --post を付けてください）")
        return 0

    if not bot.TOKEN:
        print(".env の DISCORD_TOKEN が空です")
        return 1

    asyncio.run(run(when, args.post, args.goodbye))
    return 0


if __name__ == "__main__":
    sys.exit(main())
