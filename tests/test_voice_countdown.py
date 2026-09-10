"""VOICEVOXへのリクエストと、波形の組み立ての検証。

VOICEVOXは外部プロセスなので、テストのたびに起動させるわけにはいかない。
`urllib.request.urlopen` を偽物に差し替えて、
**何を送っているか**と**返ってきたものをどう組み立てるか**だけを見る。

こうするとVOICEVOXが落ちていてもテストは通るし、
「prePhonemeLength を0にし忘れた」ような取り違えを機械的に捕まえられる。
"""

import io
import json
import math
import os
import struct
import sys
import unittest
import urllib.error
import wave
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_countdown as vc

RATE = 24000


def wrap_wav(frames, rate=RATE, channels=1):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(frames)
    return buffer.getvalue()


def make_wav(seconds=0.5, rate=RATE, channels=1, freq=440):
    """テスト用のwavをメモリ上に作る。"""
    frames = bytearray()
    for i in range(int(rate * seconds)):
        value = int(20000 * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<h", value) * channels
    return wrap_wav(bytes(frames), rate, channels)


MARKER = 12345


def make_marker_wav(seconds=0.2, rate=RATE, channels=1):
    """全サンプルが同じ非ゼロ値の音。

    位置の検証にサイン波を使ってはいけない。sin(0) は 0 なので、
    先頭が無音と見分けられず「数バイトずれている」を見逃す。
    ずっと同じ値なら、始まりと終わりがバイト単位で確定する。
    """
    count = int(rate * seconds) * channels
    return wrap_wav(struct.pack("<h", MARKER) * count, rate, channels)


SILENT_FRAME = b"\x00\x00"
MARKER_FRAME = struct.pack("<h", MARKER)


class FakeResponse:
    """urlopen の戻り値のふり。with 文で使えるようにしておく。"""

    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeVoicevox:
    """audio_query と synthesis に順番に答える偽サーバー。

    受け取ったリクエストを requests に貯めるので、
    あとから「何を送ったか」を検査できる。
    """

    def __init__(self, query=None, wav=None):
        self.query = query if query is not None else {
            "speedScale": 1.0,
            "prePhonemeLength": 0.1,
            "postPhonemeLength": 0.1,
            "outputSamplingRate": 24000,
            "outputStereo": False,
        }
        self.wav = wav if wav is not None else make_wav()
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if "audio_query" in request.full_url:
            return FakeResponse(json.dumps(self.query).encode("utf-8"))
        return FakeResponse(self.wav)

    def sent_query(self):
        """synthesis に渡したJSON（＝調整後の設計図）を返す。"""
        return json.loads(self.requests[1].data)


class SynthesizeTest(unittest.TestCase):
    def test_calls_audio_query_then_synthesis(self):
        fake = FakeVoicevox()
        with mock.patch("urllib.request.urlopen", fake):
            vc.synthesize("http://x", "45", 3, 1.2)

        self.assertEqual(len(fake.requests), 2)
        self.assertIn("/audio_query", fake.requests[0].full_url)
        self.assertIn("/synthesis", fake.requests[1].full_url)

    def test_sends_text_and_speaker_in_the_query_string(self):
        fake = FakeVoicevox()
        with mock.patch("urllib.request.urlopen", fake):
            vc.synthesize("http://x", "38", 3, 1.0)

        url = fake.requests[0].full_url
        self.assertIn("text=38", url)
        self.assertIn("speaker=3", url)

    def test_strips_the_silence_around_each_number(self):
        """前後の無音を残すと、鳴り始めが目盛りからずれる。"""
        fake = FakeVoicevox()
        with mock.patch("urllib.request.urlopen", fake):
            vc.synthesize("http://x", "45", 3, 1.2)

        query = fake.sent_query()
        self.assertEqual(query["prePhonemeLength"], 0.0)
        self.assertEqual(query["postPhonemeLength"], 0.0)

    def test_applies_the_requested_speed(self):
        fake = FakeVoicevox()
        with mock.patch("urllib.request.urlopen", fake):
            vc.synthesize("http://x", "45", 3, 1.2)

        self.assertEqual(fake.sent_query()["speedScale"], 1.2)

    def test_defaults_to_mono_24k(self):
        fake = FakeVoicevox()
        with mock.patch("urllib.request.urlopen", fake):
            vc.synthesize("http://x", "45", 3, 1.0)

        query = fake.sent_query()
        self.assertEqual(query["outputSamplingRate"], 24000)
        self.assertFalse(query["outputStereo"])

    def test_can_ask_for_the_format_discord_needs(self):
        """Discordのボイスは48kHzステレオしか受け取らない。"""
        fake = FakeVoicevox()
        with mock.patch("urllib.request.urlopen", fake):
            vc.synthesize("http://x", "45", 3, 1.2, sample_rate=48000, stereo=True)

        query = fake.sent_query()
        self.assertEqual(query["outputSamplingRate"], 48000)
        self.assertTrue(query["outputStereo"])

    def test_connection_refused_becomes_a_readable_error(self):
        """VOICEVOXの起動忘れは一番ありがちなので、案内まで出す。"""
        def refuse(request, timeout=None):
            raise urllib.error.URLError("Connection refused")

        with mock.patch("urllib.request.urlopen", refuse):
            with self.assertRaises(vc.VoicevoxError) as caught:
                vc.synthesize("http://x", "45", 3, 1.0)

        message = str(caught.exception)
        self.assertIn("VOICEVOXに接続できません", message)
        self.assertIn("起動", message)


class BuildTrackTest(unittest.TestCase):
    """無音を挟むのではなく、絶対位置に置いていることの確認。"""

    def setUp(self):
        params, _ = vc.load_clip(make_wav(0.1))
        self.params = params

    def test_places_each_clip_at_its_exact_offset(self):
        """1フレームのずれも許さない。ここが緩いと数バイトのずれを見逃す。"""
        _, clip = vc.load_clip(make_marker_wav(0.2))
        placements = [(0.0, clip), (1.0, clip), (2.0, clip)]

        track = vc.build_track(placements, self.params, 3.0)

        bytes_per_second = self.params.framerate * self.params.sampwidth
        for second in (0, 1, 2):
            start = second * bytes_per_second
            # ちょうどこの位置から音が始まっている
            self.assertEqual(track[start:start + 2], MARKER_FRAME, f"{second}秒")
            # その1フレーム手前はまだ無音
            if second:
                self.assertEqual(track[start - 2:start], SILENT_FRAME, f"{second}秒")

    def test_clip_ends_exactly_where_it_should(self):
        _, clip = vc.load_clip(make_marker_wav(0.2))
        track = vc.build_track([(0.0, clip)], self.params, 1.0)

        end = len(clip)
        self.assertEqual(track[end - 2:end], MARKER_FRAME)
        self.assertEqual(track[end:end + 2], SILENT_FRAME)

    def test_gap_between_clips_is_exactly_silence(self):
        _, clip = vc.load_clip(make_marker_wav(0.2))
        track = vc.build_track([(0.0, clip), (1.0, clip)], self.params, 2.0)

        bytes_per_second = self.params.framerate * self.params.sampwidth
        gap = track[len(clip):bytes_per_second]
        self.assertEqual(set(gap), {0})

    def test_track_length_matches_the_requested_duration(self):
        _, clip = vc.load_clip(make_wav(0.2))
        track = vc.build_track([(0.0, clip)], self.params, 7.5)

        seconds = len(track) / (self.params.framerate * self.params.sampwidth)
        self.assertAlmostEqual(seconds, 7.5, places=6)

    def test_a_clip_running_past_the_end_is_cut_not_crashed(self):
        _, clip = vc.load_clip(make_wav(2.0))
        track = vc.build_track([(0.5, clip)], self.params, 1.0)

        seconds = len(track) / (self.params.framerate * self.params.sampwidth)
        self.assertAlmostEqual(seconds, 1.0, places=6)

    def test_later_clips_overwrite_the_tail_of_earlier_ones(self):
        """読みが1秒に収まらなくても、次の目盛りは必ず正しい位置から始まる。"""
        _, long_clip = vc.load_clip(make_marker_wav(1.4))
        track = vc.build_track([(0.0, long_clip), (1.0, long_clip)], self.params, 3.0)

        bytes_per_second = self.params.framerate * self.params.sampwidth
        # 2本目は1.0秒ちょうどから。1本目にはみ出されて後ろへ押されたりしない
        self.assertEqual(
            track[bytes_per_second:bytes_per_second + 2], MARKER_FRAME
        )
        # 2本目の終わりは 1.0 + 1.4 秒の位置
        end = bytes_per_second + len(long_clip)
        self.assertEqual(track[end - 2:end], MARKER_FRAME)
        self.assertEqual(track[end:end + 2], SILENT_FRAME)

        seconds = len(track) / bytes_per_second
        self.assertAlmostEqual(seconds, 3.0, places=6)


class CachePathTest(unittest.TestCase):
    def args(self, **overrides):
        base = dict(
            start=45, end=0, step=1, dense=0, speaker=3, speed=1.2,
            lead=0.0, cue="", sample_rate=24000, stereo=False,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_same_settings_give_the_same_name(self):
        self.assertEqual(vc.cache_path(self.args()), vc.cache_path(self.args()))

    def test_range_is_part_of_the_name(self):
        self.assertIn("45-0", vc.cache_path(self.args()))
        self.assertIn("65-40", vc.cache_path(self.args(start=65, end=40)))

    def test_speaker_and_speed_change_the_name(self):
        self.assertNotEqual(
            vc.cache_path(self.args(speaker=3)),
            vc.cache_path(self.args(speaker=8)),
        )
        self.assertNotEqual(
            vc.cache_path(self.args(speed=1.0)),
            vc.cache_path(self.args(speed=1.2)),
        )

    def test_discord_format_gets_its_own_file(self):
        """手元再生用とDiscord用が同じ名前になると、混ざって事故る。"""
        self.assertNotEqual(
            vc.cache_path(self.args()),
            vc.cache_path(self.args(sample_rate=48000, stereo=True)),
        )


class BuildCountdownWavTest(unittest.TestCase):
    """合成だけ差し替えて、丸ごと1本組み立てるところまで通す。"""

    def build(self, tmpdir, clip_seconds=0.5, **overrides):
        args = SimpleNamespace(
            start=5, end=0, step=1, dense=0, speaker=3, speed=1.2,
            lead=0.0, cue="", host="http://x",
            sample_rate=RATE, stereo=False,
        )
        for key, value in overrides.items():
            setattr(args, key, value)

        path = os.path.join(tmpdir, "out.wav")
        fake = lambda *a, **k: make_marker_wav(clip_seconds)
        with mock.patch.object(vc, "synthesize", fake):
            with mock.patch("builtins.print"):
                total = vc.build_countdown_wav(path, args)
        return path, total

    def test_duration_is_range_plus_tail(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            path, total = self.build(tmpdir)

            with wave.open(path, "rb") as reader:
                seconds = reader.getnframes() / reader.getframerate()

            self.assertAlmostEqual(total, 5 + vc.TAIL_SECONDS, places=6)
            self.assertAlmostEqual(seconds, total, places=6)

    def test_lead_shifts_everything_later(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            path, total = self.build(tmpdir, lead=3.0)

            with wave.open(path, "rb") as reader:
                seconds = reader.getnframes() / reader.getframerate()

            self.assertAlmostEqual(seconds, 3.0 + 5 + vc.TAIL_SECONDS, places=6)

    def test_every_tick_starts_on_the_second(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            path, _ = self.build(tmpdir, clip_seconds=0.4)

            with wave.open(path, "rb") as reader:
                rate = reader.getframerate()
                frames = reader.readframes(reader.getnframes())

            for tick in range(6):
                index = tick * rate * 2  # 16bitモノラルなので1フレーム2バイト
                self.assertEqual(frames[index:index + 2], MARKER_FRAME, f"{tick}秒")
                if tick:
                    self.assertEqual(frames[index - 2:index], SILENT_FRAME, f"{tick}秒")

    def test_zero_is_read_as_kana(self):
        """「0」のままだと読みが不自然なので、ひらがなに置き換えている。"""
        self.assertEqual(vc.READINGS[0], "ゼロ")

        import tempfile

        asked = []

        def spy(host, text, speaker, speed, sample_rate=24000, stereo=False):
            asked.append(text)
            return make_wav(0.3)

        args = SimpleNamespace(
            start=2, end=0, step=1, dense=0, speaker=3, speed=1.0,
            lead=0.0, cue="", host="http://x", sample_rate=RATE, stereo=False,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(vc, "synthesize", spy):
                with mock.patch("builtins.print"):
                    vc.build_countdown_wav(os.path.join(tmpdir, "o.wav"), args)

        self.assertEqual(asked, ["2", "1", "ゼロ"])


if __name__ == "__main__":
    unittest.main()
