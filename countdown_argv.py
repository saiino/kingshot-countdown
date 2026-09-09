"""Phase 2 の学習用: countdown.py と同じことを sys.argv だけで書いた版。

argparse を使わないと何を自分でやることになるのかを見るためのもの。
実運用は countdown.py のほうを使う。

    python countdown_argv.py 45
    python countdown_argv.py 60 30
"""

import sys

from countdown import build_schedule, run

USAGE = "使い方: python countdown_argv.py [開始秒] [終了秒]"


def main():
    # sys.argv[0] はスクリプト名なので、実際の引数は [1:]
    argv = sys.argv[1:]

    # ヘルプは自分で用意する必要がある（argparse なら -h が自動で付く）
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    # 引数の個数チェックも自前
    if len(argv) > 2:
        print(f"引数が多すぎます\n{USAGE}", file=sys.stderr)
        return 2

    # 文字列で届くので int への変換も自前。失敗すれば ValueError が飛ぶ。
    try:
        start = int(argv[0]) if len(argv) >= 1 else 45
        end = int(argv[1]) if len(argv) >= 2 else 0
    except ValueError:
        print(f"秒数は整数で指定してください\n{USAGE}", file=sys.stderr)
        return 2

    if start <= end:
        print(f"開始({start})は終了({end})より大きくしてください", file=sys.stderr)
        return 2

    print(f"{start} -> {end}\n")
    try:
        run(build_schedule(start, end))
    except KeyboardInterrupt:
        print("\n中断しました")
    return 0


if __name__ == "__main__":
    # 終了コードを返すのが行儀のよい CLI（0=正常, 2=引数エラー）
    sys.exit(main())
