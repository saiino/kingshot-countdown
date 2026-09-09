"""キングショット出陣カウントダウン（テキスト版）

45秒から0秒まで、1秒ごとに残り秒数を表示する。
全員が同じカウントを聞き、自分の行軍時間の数字が読まれたら出陣する。

    python countdown.py            # 45 -> 0
    python countdown.py 60         # 60 -> 0
    python countdown.py 60 30      # 60 -> 30
    python countdown.py 60 0 --step 5 --dense 10
    python countdown.py 10 0 --naive   # ズレ補正なし（比較用）
"""

import argparse
import time

# 1目盛りあたりの実時間（秒）。ここを小さくすれば早回しでテストできる。
TICK = 1.0


def build_schedule(start, end, step=1, dense=0):
    """読み上げる (経過秒, 残り秒) のリストを作る。

    start から end まで1秒ずつ下がるが、読み上げるのは
      - 残りが dense 秒以下 … 毎秒
      - それ以外            … step の倍数のときだけ
    先頭(start)と末尾(end)は必ず読む。

    >>> build_schedule(5, 0)
    [(0, 5), (1, 4), (2, 3), (3, 2), (4, 1), (5, 0)]
    >>> build_schedule(20, 0, step=5, dense=3)
    [(0, 20), (5, 15), (10, 10), (15, 5), (17, 3), (18, 2), (19, 1), (20, 0)]
    """
    schedule = []
    for remaining in range(start, end - 1, -1):
        elapsed = start - remaining
        if (
            remaining == start
            or remaining == end
            or remaining <= dense
            or remaining % step == 0
        ):
            schedule.append((elapsed, remaining))
    return schedule


def sleep_until(deadline):
    """monotonic時計で deadline になるまで待つ（ズレ補正あり）。

    time.time() ではなく time.monotonic() を使う理由:
      - システム時刻の変更やNTP同期の影響を受けない
      - 「開始から何秒経ったか」を測るのが本来の用途
    """
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def run(schedule, naive=False):
    """スケジュール通りに読み上げ、実測の経過秒を返す。"""
    started = time.monotonic()
    previous_elapsed = 0

    for elapsed, remaining in schedule:
        if naive:
            # ダメな書き方: 「1目盛り分寝る」を繰り返す。
            # 表示や計算にかかった時間、sleep自体の誤差が毎回上乗せされ、
            # 少しずつ遅れが蓄積していく。
            time.sleep((elapsed - previous_elapsed) * TICK)
        else:
            # 良い書き方: 開始時刻からの絶対位置で待つ。
            # 途中で少し遅れても、次のtickで自動的に取り戻せる。
            sleep_until(started + elapsed * TICK)

        lag = time.monotonic() - started - elapsed * TICK
        print(f"\r  {remaining:>3}   (ずれ {lag:+.3f}s)   ", end="", flush=True)
        previous_elapsed = elapsed

    print()
    return time.monotonic() - started


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="出陣タイミングを合わせるためのカウントダウン",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("start", type=int, nargs="?", default=45, help="開始する残り秒数")
    parser.add_argument("end", type=int, nargs="?", default=0, help="終了する残り秒数")
    parser.add_argument("--step", type=int, default=1, help="読み上げる間隔（秒）")
    parser.add_argument("--dense", type=int, default=0, help="残りこの秒数以下は毎秒読む")
    parser.add_argument("--naive", action="store_true", help="ズレ補正なしで動かす（比較用）")

    args = parser.parse_args(argv)

    # argparse は型変換まではやってくれるが、値どうしの関係は自分で見る必要がある。
    if args.start <= args.end:
        parser.error(f"開始({args.start})は終了({args.end})より大きくしてください")
    if args.end < 0:
        parser.error("終了秒に負の数は指定できません")
    if args.step < 1:
        parser.error("--step は1以上にしてください")

    return args


def main():
    args = parse_args()
    schedule = build_schedule(args.start, args.end, args.step, args.dense)

    mode = "ズレ補正なし" if args.naive else "ズレ補正あり"
    print(f"{args.start} -> {args.end}  ({len(schedule)}回読み上げ / {mode})")
    print("Ctrl+C で中断\n")

    try:
        actual = run(schedule, naive=args.naive)
    except KeyboardInterrupt:
        print("\n中断しました")
        return

    expected = (args.start - args.end) * TICK
    print(f"\n所要 {actual:.3f}s（想定 {expected:.3f}s / 誤差 {actual - expected:+.3f}s）")


if __name__ == "__main__":
    main()
