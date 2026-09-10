"""カウントの組み立てと、ズレ補正の検証。

ここは外部と通信しないのでモックは要らない……と言いたいところだが、
時間の検証だけは別。本物の time を使うと45秒待つことになるうえ、
実行環境の気まぐれで結果が変わってテストにならない。

そこで **時計そのものを偽物に差し替える**。こうすると
「7回目の読み上げで0.4秒もたついた」という状況を意図的に作れて、
補正が効くかどうかを一瞬で、しかも毎回同じ結果で確かめられる。
"""

import contextlib
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import countdown


class FakeClock:
    """進めた分だけ時刻が進む、嘘の時計。

    sleep しても実際には待たず、内部の時刻を進めるだけ。
    どれだけ寝るよう指示されたかを sleeps に記録しておく。
    """

    def __init__(self, start=1000.0):
        self.now = start
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def waste(self, seconds):
        """処理に手間取った状況を作る（寝ずに時刻だけ進める）。"""
        self.now += seconds


class BuildScheduleTest(unittest.TestCase):
    def test_reads_every_second_by_default(self):
        self.assertEqual(
            countdown.build_schedule(5, 0),
            [(0, 5), (1, 4), (2, 3), (3, 2), (4, 1), (5, 0)],
        )

    def test_elapsed_and_remaining_always_add_up_to_start(self):
        for elapsed, remaining in countdown.build_schedule(45, 0):
            self.assertEqual(elapsed + remaining, 45)

    def test_step_thins_out_the_middle(self):
        schedule = countdown.build_schedule(20, 0, step=5)
        self.assertEqual([n for _, n in schedule], [20, 15, 10, 5, 0])

    def test_dense_reads_every_second_near_the_end(self):
        schedule = countdown.build_schedule(20, 0, step=5, dense=3)
        self.assertEqual([n for _, n in schedule], [20, 15, 10, 5, 3, 2, 1, 0])

    def test_endpoints_survive_a_step_that_does_not_divide_them(self):
        # 47 も 12 も 5 の倍数ではないが、始点と終点は必ず読む
        numbers = [n for _, n in countdown.build_schedule(47, 12, step=5)]
        self.assertEqual(numbers[0], 47)
        self.assertEqual(numbers[-1], 12)

    def test_partial_range_stops_at_end(self):
        numbers = [n for _, n in countdown.build_schedule(60, 40)]
        self.assertEqual(numbers[0], 60)
        self.assertEqual(numbers[-1], 40)
        self.assertEqual(len(numbers), 21)


class DriftTest(unittest.TestCase):
    """補正あり／なしで、もたつきの扱いがどう変わるか。"""

    def run_with(self, clock, naive, stumble_at=None, stumble=0.0):
        """偽の時計で走らせ、実測の所要時間を返す。

        stumble_at 回目の読み上げで stumble 秒だけ余計に時間を食わせる。
        """
        printed = {"count": 0}

        def slow_print(*args, **kwargs):
            printed["count"] += 1
            if stumble_at is not None and printed["count"] == stumble_at:
                clock.waste(stumble)

        schedule = countdown.build_schedule(5, 0)
        with mock.patch.object(countdown, "time") as fake_time:
            fake_time.monotonic.side_effect = clock.monotonic
            fake_time.sleep.side_effect = clock.sleep
            with mock.patch("builtins.print", slow_print):
                return countdown.run(schedule, naive=naive)

    def test_no_stumble_means_exactly_on_time(self):
        clock = FakeClock()
        self.assertAlmostEqual(self.run_with(clock, naive=False), 5.0, places=9)

    def test_corrected_run_absorbs_a_stumble(self):
        # 2回目の読み上げで0.4秒もたつく
        clock = FakeClock()
        elapsed = self.run_with(clock, naive=False, stumble_at=2, stumble=0.4)

        # 全体では予定どおり5秒。遅れは次の待ち時間から差し引かれている。
        # 1回目は開始と同時なので寝ない。よって sleeps[0] が2回目ぶん。
        self.assertAlmostEqual(elapsed, 5.0, places=9)
        self.assertAlmostEqual(clock.sleeps[0], 1.0, places=9)
        self.assertAlmostEqual(clock.sleeps[1], 0.6, places=9)  # 0.4秒ぶん短縮

    def test_naive_run_carries_the_stumble_to_the_end(self):
        # 同じもたつきでも、補正なしだと最後まで残る
        clock = FakeClock()
        elapsed = self.run_with(clock, naive=True, stumble_at=2, stumble=0.4)

        self.assertAlmostEqual(elapsed, 5.4, places=9)
        # 遅れを見ずに毎回きっちり1秒寝るので、取り返す機会がない
        self.assertEqual(clock.sleeps, [0.0, 1.0, 1.0, 1.0, 1.0, 1.0])

    def test_naive_run_accumulates_many_small_stumbles(self):
        """1回ごとの遅れが小さくても、積もれば無視できなくなる。"""
        clock = FakeClock()
        original_sleep = clock.sleep

        def sloppy_sleep(seconds):
            # sleep は「指定以上」しか保証しない。毎回2ms余計に寝る想定
            original_sleep(seconds)
            clock.waste(0.002)

        schedule = countdown.build_schedule(45, 0)
        with mock.patch.object(countdown, "time") as fake_time:
            fake_time.monotonic.side_effect = clock.monotonic
            fake_time.sleep.side_effect = sloppy_sleep
            with mock.patch("builtins.print"):
                elapsed = countdown.run(schedule, naive=True)

        # 読み上げは46回（45から0まで）。その回数ぶんの2msが丸ごと残る
        self.assertEqual(len(clock.sleeps), 46)
        self.assertAlmostEqual(elapsed, 45.0 + 46 * 0.002, places=9)


class ArgumentTest(unittest.TestCase):
    def assertRejected(self, argv):
        """argparse は error() で使い方を stderr に吐いて SystemExit する。

        その出力はテスト結果に混ざるだけなので捨てる。
        """
        with open(os.devnull, "w") as sink:
            with contextlib.redirect_stderr(sink):
                with self.assertRaises(SystemExit):
                    countdown.parse_args(argv)

    def test_defaults(self):
        args = countdown.parse_args([])
        self.assertEqual((args.start, args.end), (45, 0))

    def test_positional_arguments(self):
        args = countdown.parse_args(["60", "40"])
        self.assertEqual((args.start, args.end), (60, 40))

    def test_start_must_exceed_end(self):
        self.assertRejected(["10", "20"])

    def test_equal_values_are_rejected(self):
        self.assertRejected(["10", "10"])

    def test_negative_end_is_rejected(self):
        self.assertRejected(["10", "-5"])

    def test_step_must_be_positive(self):
        self.assertRejected(["45", "0", "--step", "0"])


if __name__ == "__main__":
    unittest.main()
