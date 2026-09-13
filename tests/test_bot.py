"""Botの分岐の検証。

Discordに繋がずに確かめたいので、interaction（コマンドが叩かれたときに
渡ってくる箱）とボイス接続をまるごと偽物にする。

ここで見ているのは、実際に運用中に踏んだ失敗ばかり:
  - ボイスチャンネルに入らずに押したときに、ちゃんと案内が返るか
  - 再生中に二重で押されても壊れないか
  - VOICEVOXが落ちているときに、黙って失敗せず理由を返すか
  - /countdown の秒数指定が、そのまま再生に渡っているか
"""

import asyncio
import io
import json
import os
import sys
import unittest
import wave
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import discord

import bot
import voice_countdown as vc

ONE_SECOND = bot.DISCORD_BYTES_PER_SECOND


def make_interaction(in_voice=True, playing=False, connected=True,
                     can_connect=True, can_speak=True):
    """コマンドが叩かれた状況を組み立てる。"""
    interaction = mock.MagicMock()
    interaction.guild.name = "テストサーバー"

    # 応答は「まだ返していない」状態から始まり、defer すると「返した」に変わる
    interaction.response.is_done.return_value = False

    async def defer(**kwargs):
        interaction.response.is_done.return_value = True

    interaction.response.defer = mock.AsyncMock(side_effect=defer)
    interaction.response.send_message = mock.AsyncMock()
    interaction.followup.send = mock.AsyncMock()

    channel = mock.MagicMock()
    channel.name = "一般"
    permissions = mock.MagicMock()
    permissions.connect = can_connect
    permissions.speak = can_speak
    channel.permissions_for.return_value = permissions

    interaction.user.voice = mock.MagicMock(channel=channel) if in_voice else None

    voice_client = mock.MagicMock()
    voice_client.is_playing.return_value = playing
    voice_client.is_connected.return_value = connected
    voice_client.channel = channel
    voice_client.disconnect = mock.AsyncMock()
    voice_client.move_to = mock.AsyncMock()

    def play(source, after=None):
        # 本物は再生し終わってから after を呼ぶ。ここでは即座に終わったことにする
        if after is not None:
            after(None)

    voice_client.play.side_effect = play
    channel.connect = mock.AsyncMock(return_value=voice_client)

    interaction.guild.voice_client = voice_client if playing else None
    interaction._channel = channel
    interaction._voice_client = voice_client
    return interaction


def replies(interaction):
    """Botが返した文言を全部集める。"""
    said = []
    for call in interaction.response.send_message.await_args_list:
        said.append(call.args[0])
    for call in interaction.followup.send.await_args_list:
        said.append(call.args[0])
    return said


class LoadEnvTest(unittest.TestCase):
    def test_reads_key_and_value(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, ".env")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    "# コメントは無視する\n"
                    "\n"
                    "DISCORD_TOKEN=abc123\n"
                    "VOICEVOX_SPEED = 1.2 \n"
                    "壊れた行\n"
                )
            values = bot.load_env(path)

        self.assertEqual(values["DISCORD_TOKEN"], "abc123")
        self.assertEqual(values["VOICEVOX_SPEED"], "1.2")
        self.assertNotIn("壊れた行", values)

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(bot.load_env("/nowhere/.env"), {})


class CountdownArgsTest(unittest.TestCase):
    def test_always_asks_for_the_format_discord_needs(self):
        args = bot.countdown_args(45, 0)
        self.assertEqual(args.sample_rate, 48000)
        self.assertTrue(args.stereo)

    def test_carries_the_range(self):
        args = bot.countdown_args(65, 40)
        self.assertEqual((args.start, args.end), (65, 40))

    def test_uses_the_configured_lead_and_cue(self):
        args = bot.countdown_args(45, 0)
        self.assertEqual(args.lead, bot.LEAD_SECONDS)
        self.assertEqual(args.cue, bot.CUE_TEXT)


class ReadPcmTest(unittest.TestCase):
    def test_returns_raw_frames_without_the_header(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "a.wav")
            with wave.open(path, "wb") as writer:
                writer.setnchannels(2)
                writer.setsampwidth(2)
                writer.setframerate(48000)
                writer.writeframes(b"\x00" * ONE_SECOND)

            pcm = bot.read_pcm(path)

        self.assertEqual(len(pcm), ONE_SECOND)
        self.assertNotIn(b"RIFF", pcm[:16])


class PlayCountdownTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.wav = mock.patch.object(bot, "ensure_wav", lambda s, e: "fake.wav")
        self.pcm = mock.patch.object(bot, "read_pcm", lambda p: b"\x00" * ONE_SECOND)
        self.wav.start()
        self.pcm.start()
        self.addCleanup(self.wav.stop)
        self.addCleanup(self.pcm.stop)

    async def test_asks_the_user_to_join_a_voice_channel_first(self):
        interaction = make_interaction(in_voice=False)

        await bot.play_countdown(interaction, 45, 0)

        self.assertIn("先にボイスチャンネルに入ってください。", replies(interaction))
        # 音声を用意する前に断っているので、待たせる必要がない
        interaction.response.defer.assert_not_awaited()

    async def test_refuses_a_second_press_while_playing(self):
        interaction = make_interaction(playing=True)

        await bot.play_countdown(interaction, 45, 0)

        self.assertTrue(any("いま再生中です" in m for m in replies(interaction)))
        interaction._voice_client.play.assert_not_called()

    async def test_reports_missing_voice_permissions(self):
        """権限で弾かれると、以前は「考え中」のまま固まっていた。"""
        interaction = make_interaction(can_speak=False)

        await bot.play_countdown(interaction, 45, 0)

        self.assertTrue(any("権限がありません" in m for m in replies(interaction)))

    async def test_reports_that_voicevox_is_unavailable(self):
        def explode(start, end):
            raise vc.VoicevoxError("VOICEVOXに接続できません")

        interaction = make_interaction()
        with mock.patch.object(bot, "ensure_wav", explode):
            await bot.play_countdown(interaction, 45, 0)

        self.assertTrue(
            any("音声を用意できませんでした" in m for m in replies(interaction))
        )
        interaction._voice_client.play.assert_not_called()

    async def test_plays_then_leaves(self):
        interaction = make_interaction()

        await bot.play_countdown(interaction, 45, 0)

        interaction._channel.connect.assert_awaited_once()
        interaction._voice_client.play.assert_called_once()
        interaction._voice_client.disconnect.assert_awaited_once()

    async def test_tells_the_user_which_range_is_playing(self):
        """打った秒数と流れる秒数が食い違う事故を、ここで気づけるようにした。"""
        interaction = make_interaction()

        await bot.play_countdown(interaction, 65, 40)

        self.assertTrue(any("65 → 40" in m for m in replies(interaction)))

    async def test_hands_discord_raw_pcm(self):
        interaction = make_interaction()

        await bot.play_countdown(interaction, 45, 0)

        source = interaction._voice_client.play.call_args.args[0]
        self.assertIsInstance(source, discord.PCMAudio)

    async def test_moves_to_the_channel_the_presser_is_in(self):
        interaction = make_interaction()
        elsewhere = mock.MagicMock()
        interaction.guild.voice_client = interaction._voice_client
        interaction._voice_client.channel = elsewhere
        interaction._voice_client.is_playing.return_value = False

        await bot.play_countdown(interaction, 45, 0)

        interaction._voice_client.move_to.assert_awaited_once_with(
            interaction._channel
        )

    async def test_records_who_used_it(self):
        interaction = make_interaction()

        with self.assertLogs("bell", level="INFO") as captured:
            await bot.play_countdown(interaction, 45, 0)

        joined = "\n".join(captured.output)
        self.assertIn("45 → 0", joined)
        self.assertIn("テストサーバー", joined)


class CountdownCommandTest(unittest.IsolatedAsyncioTestCase):
    """スラッシュコマンドの入口の検証。"""

    async def call(self, start, end):
        interaction = make_interaction()
        with mock.patch.object(bot, "play_countdown", mock.AsyncMock()) as played:
            await bot.countdown_command.callback(interaction, start, end)
        return interaction, played

    async def test_passes_the_range_through_untouched(self):
        _, played = await self.call(65, 40)
        played.assert_awaited_once()
        self.assertEqual(played.await_args.args[1:], (65, 40))

    async def test_rejects_a_start_below_the_end(self):
        interaction, played = await self.call(10, 20)
        played.assert_not_awaited()
        self.assertTrue(any("より大きくして" in m for m in replies(interaction)))

    async def test_rejects_equal_values(self):
        interaction, played = await self.call(30, 30)
        played.assert_not_awaited()

    async def test_both_arguments_are_required(self):
        """省略できると、値を入れずに送信できてしまい既定値が黙って使われる。"""
        parameters = {p.name: p for p in bot.countdown_command.parameters}
        self.assertTrue(parameters["start"].required)
        self.assertTrue(parameters["end"].required)


class LoggingTest(unittest.TestCase):
    """ログは起動した日ごとに1本、古いものは自動で消す。"""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patch = mock.patch.object(bot, "LOG_DIR", self.tmp.name)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def make(self, name, days_old):
        import time

        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x")
        stamp = time.time() - days_old * 86400
        os.utime(path, (stamp, stamp))
        return path

    def test_file_is_named_after_todays_date(self):
        from datetime import datetime

        expected = f"bot-{datetime.now():%Y-%m-%d}.log"
        self.assertEqual(os.path.basename(bot.log_path_for_today()), expected)

    def test_sweep_removes_logs_past_the_keep_window(self):
        self.make("bot-old.log", bot.LOG_KEEP_DAYS + 1)
        self.make("bot-recent.log", bot.LOG_KEEP_DAYS - 1)

        bot.sweep_old_logs()

        left = os.listdir(self.tmp.name)
        self.assertNotIn("bot-old.log", left)
        self.assertIn("bot-recent.log", left)

    def test_sweep_leaves_unrelated_files_alone(self):
        """logs/ に置いた他のファイルを巻き添えにしない。"""
        self.make("メモ.txt", 365)
        self.make("bot.log", 365)          # 旧方式の名前。bot- で始まらない

        bot.sweep_old_logs()

        left = os.listdir(self.tmp.name)
        self.assertIn("メモ.txt", left)
        self.assertIn("bot.log", left)


class StopTest(unittest.IsolatedAsyncioTestCase):
    """/stop と赤いボタンは同じ処理を通る。"""

    async def test_button_stops_and_disconnects(self):
        interaction = make_interaction(playing=True)
        button = next(
            b for b in bot.CountdownPanel().children
            if b.custom_id == "kingshot:stop"
        )

        await button.callback(interaction)

        interaction._voice_client.stop.assert_called_once()
        interaction._voice_client.disconnect.assert_awaited_once()
        self.assertTrue(any("止めました" in m for m in replies(interaction)))

    async def test_command_stops_and_disconnects(self):
        interaction = make_interaction(playing=True)

        await bot.stop_command.callback(interaction)

        interaction._voice_client.stop.assert_called_once()
        interaction._voice_client.disconnect.assert_awaited_once()

    async def test_says_so_when_nothing_is_playing(self):
        interaction = make_interaction()
        interaction.guild.voice_client = None

        await bot.stop_command.callback(interaction)

        self.assertTrue(any("再生していません" in m for m in replies(interaction)))

    async def test_log_says_the_command_was_used(self):
        interaction = make_interaction(playing=True)

        with self.assertLogs("bell", level="INFO") as captured:
            await bot.stop_command.callback(interaction)

        self.assertIn("停止（コマンド）", chr(10).join(captured.output))

    async def test_log_says_the_button_was_used(self):
        """赤いボタンが使われているかを、ログから判別できるようにした。"""
        interaction = make_interaction(playing=True)
        button = next(
            b for b in bot.CountdownPanel().children
            if b.custom_id == "kingshot:stop"
        )

        with self.assertLogs("bell", level="INFO") as captured:
            await button.callback(interaction)

        self.assertIn("停止（ボタン）", chr(10).join(captured.output))


class CommandRegistrationTest(unittest.TestCase):
    def test_all_commands_are_guild_only(self):
        """DMから呼ばれると guild が無く、そのまま落ちる。"""
        for command in bot.client.tree.get_commands():
            self.assertTrue(command.guild_only, f"/{command.name}")

    def test_panel_buttons_keep_a_stable_id(self):
        """再起動しても既存のパネルが効き続けるのは、このIDが変わらないから。"""
        panel = bot.CountdownPanel()
        for button in panel.children:
            self.assertTrue(
                button.custom_id.startswith("kingshot:"), button.custom_id
            )
        countdowns = [
            b for b in panel.children
            if b.custom_id.startswith("kingshot:countdown:")
        ]
        self.assertEqual(len(countdowns), len(bot.PRESETS))
        self.assertIsNone(panel.timeout)

    def test_stop_button_sits_alone_on_the_last_row(self):
        """秒数の列に混ざると、慌てているときに押し間違える。"""
        rows = bot.CountdownPanel().to_components()
        last = rows[-1]["components"]

        self.assertEqual(len(last), 1, "最下段は停止ボタンだけ")
        self.assertEqual(last[0]["custom_id"], "kingshot:stop")
        self.assertEqual(last[0]["style"], discord.ButtonStyle.danger.value, "赤")

        # 秒数のボタンは青のまま（1行目は終わりの選択欄）
        for row in rows[1:-1]:
            for component in row["components"]:
                self.assertEqual(
                    component["style"], discord.ButtonStyle.primary.value
                )

    def test_presets_leave_room_for_the_stop_row(self):
        """上限まで並べても、選択欄と停止ボタンのぶんの行が残ること。"""
        self.assertLessEqual(bot.MAX_BUTTONS, 15)
        self.assertLessEqual((bot.MAX_BUTTONS + 4) // 5, 3)

    def test_panel_fits_within_discord_limits(self):
        """1行5個・最大5行を超えると、送信時に弾かれる。"""
        rows = bot.CountdownPanel().to_components()
        self.assertLessEqual(len(rows), 5, "行数が多すぎる")
        for row in rows:
            self.assertLessEqual(len(row.get("components", [])), 5, "1行が多すぎる")

    def test_presets_are_capped_not_crashed(self):
        """設定に多く書かれても、上限で切って落ちないこと。"""
        with mock.patch.dict(
            bot.ENV, {"COUNTDOWN_PRESETS": ",".join(str(n) for n in range(1, 40))}
        ):
            values = [
                int(v)
                for v in bot.ENV["COUNTDOWN_PRESETS"].split(",")
                if v.strip()
            ][: bot.MAX_BUTTONS]
        self.assertEqual(len(values), bot.MAX_BUTTONS)
        self.assertLessEqual(bot.MAX_BUTTONS, 25)


# ------------------------------------------------------------ 終わりの秒数を選べるパネル


def make_stereo_marker_wav(seconds):
    """48kHz・ステレオで、全サンプルが同じ非ゼロ値の音。

    位置の検証なので無音と見分けがつく値にしておく（サイン波は sin(0)=0 で紛れる）。
    """
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(48000)
        writer.writeframes(b"\x39\x30" * int(48000 * seconds) * 2)
    return buffer.getvalue()


class TrimToEndTest(unittest.TestCase):
    """start→0 を切って使ったものが、start→end を焼いたものと同じになるか。"""

    def build(self, tmpdir, start, end, cue_seconds=None):
        from types import SimpleNamespace

        args = SimpleNamespace(
            start=start, end=end, step=1, dense=0, speaker=3, speed=1.2,
            lead=bot.LEAD_SECONDS, cue="よーい" if cue_seconds else "",
            host="http://x", sample_rate=48000, stereo=True,
        )

        def fake_synthesize(host, text, *rest, **kwargs):
            if text == "よーい":
                return make_stereo_marker_wav(cue_seconds)
            return make_stereo_marker_wav(0.4)

        path = os.path.join(tmpdir, f"{start}-{end}.wav")
        with mock.patch.object(vc, "synthesize", fake_synthesize):
            with mock.patch("builtins.print"):
                vc.build_countdown_wav(path, args)
        return bot.read_pcm(path)

    def test_matches_a_freshly_built_range(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            full = self.build(tmpdir, 8, 0)
            for end in range(1, 8):
                with self.subTest(end=end):
                    self.assertEqual(
                        bot.trim_to_end(full, end), self.build(tmpdir, 8, end)
                    )

    def test_still_matches_when_the_cue_stretches_the_lead(self):
        """合図が長いと lead が伸びる。後ろから削るので、それでも一致するはず。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            full = self.build(tmpdir, 6, 0, cue_seconds=3.5)
            part = self.build(tmpdir, 6, 2, cue_seconds=3.5)

        self.assertEqual(bot.trim_to_end(full, 2), part)

    def test_zero_leaves_the_audio_untouched(self):
        pcm = b"\x01\x02\x03\x04" * 1000
        self.assertIs(bot.trim_to_end(pcm, 0), pcm)

    def test_cuts_on_a_frame_boundary(self):
        """4バイト（16bit×2ch）の途中で切ると、雑音になる。"""
        pcm = b"\x00" * (ONE_SECOND * 10)
        self.assertEqual(len(bot.trim_to_end(pcm, 3)) % 4, 0)


class EndSettingTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "panel_settings.json")
        patcher = mock.patch.object(bot, "SETTINGS_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unset_means_count_to_zero(self):
        """初めて使うサーバーでは、今の /panel と同じ動きになる。"""
        self.assertEqual(bot.end_setting_for(111), (0, None))

    def test_saved_value_is_read_back(self):
        bot.save_end_setting(111, 20, "dentam6626")
        self.assertEqual(bot.end_setting_for(111), (20, "dentam6626"))

    def test_each_server_keeps_its_own_value(self):
        bot.save_end_setting(111, 20, "a")
        bot.save_end_setting(222, 10, "b")
        self.assertEqual(bot.end_setting_for(111)[0], 20)
        self.assertEqual(bot.end_setting_for(222)[0], 10)

    def test_survives_a_restart(self):
        """メモリではなくファイルに持っているので、読み直しても残っている。"""
        bot.save_end_setting(111, 25, "a")
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["111"]["end"], 25)

    def test_broken_file_falls_back_instead_of_crashing(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{壊れた")
        self.assertEqual(bot.end_setting_for(111), (0, None))

    def test_strange_values_fall_back_to_zero(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"111": {"end": "20"}, "222": {"end": -5}, "333": 7}, handle)
        for guild_id in (111, 222, 333):
            self.assertEqual(bot.end_setting_for(guild_id), (0, None))

    def test_no_temporary_file_is_left_behind(self):
        bot.save_end_setting(111, 20, "a")
        self.assertEqual(os.listdir(self.tmp.name), ["panel_settings.json"])


class EndSelectPanelLayoutTest(unittest.TestCase):
    def test_end_choices_come_first(self):
        rows = bot.CountdownPanel().to_components()
        first = rows[0]["components"]
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["custom_id"], "kingshot:end")
        values = [int(option["value"]) for option in first[0]["options"]]
        self.assertEqual(values, bot.END_CHOICES)

    def test_current_value_is_shown_as_selected(self):
        select = bot.CountdownPanel(current=20).children[0]
        chosen = [o.value for o in select.options if o.default]
        self.assertEqual(chosen, ["20"])

    def test_end_can_be_set_as_high_as_fifty(self):
        self.assertIn(40, bot.END_CHOICES)
        self.assertEqual(max(bot.END_CHOICES), 50)
        self.assertLessEqual(len(bot.END_CHOICES), 25, "選択欄に並べられるのは25個まで")

    def test_buttons_that_cannot_count_are_greyed_out(self):
        """終わりが50なら、40・45・50秒は数えるものがない。押せなくしておく。"""
        with mock.patch.object(bot, "PRESETS", [80, 55, 50, 45, 40]):
            panel = bot.CountdownPanel(current=50)

        state = {
            b.seconds: b.disabled
            for b in panel.children if isinstance(b, bot.CountdownButton)
        }
        self.assertEqual(state, {80: False, 55: False, 50: True, 45: True, 40: True})
        self.assertFalse(panel.children[-1].disabled, "止めるは常に押せる")

    def test_nothing_is_greyed_out_when_counting_to_zero(self):
        panel = bot.CountdownPanel()
        self.assertFalse(any(getattr(c, "disabled", False) for c in panel.children))

    def test_stop_button_sits_alone_on_the_last_row(self):
        """要望: 終わりを選べても、やり直し用に止めるボタンは残す。"""
        last = bot.CountdownPanel().to_components()[-1]["components"]
        self.assertEqual(len(last), 1)
        self.assertEqual(last[0]["custom_id"], "kingshot:stop")
        self.assertEqual(last[0]["style"], discord.ButtonStyle.danger.value)

    def test_fits_within_discord_limits_even_with_many_presets(self):
        with mock.patch.object(bot, "PRESETS", list(range(100, 80, -1))):
            rows = bot.CountdownPanel().to_components()
        self.assertLessEqual(len(rows), 5)
        for row in rows:
            self.assertLessEqual(len(row["components"]), 5)

    def test_already_posted_panels_keep_working(self):
        """ボタンのIDは終わりを選べるようにする前と同じ。変えると貼ってあるパネルが死ぬ。"""
        with mock.patch.object(bot, "PRESETS", [80, 40]):
            ids = {c.custom_id for c in bot.CountdownPanel().children}
        self.assertEqual(
            ids,
            {"kingshot:end", "kingshot:countdown:80", "kingshot:countdown:40", "kingshot:stop"},
        )

    def test_stays_alive_after_restart(self):
        panel = bot.CountdownPanel()
        self.assertIsNone(panel.timeout)
        self.assertTrue(panel.is_persistent())


class EndSelectBehaviourTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(
            bot, "SETTINGS_PATH", os.path.join(self.tmp.name, "panel_settings.json")
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def interaction(self):
        interaction = make_interaction()
        interaction.guild.id = 111
        interaction.user.__str__ = lambda self: "dentam6626"
        interaction.response.edit_message = mock.AsyncMock()
        return interaction

    def button(self, seconds):
        return next(
            b for b in bot.CountdownPanel().children
            if getattr(b, "custom_id", "") == f"kingshot:countdown:{seconds}"
        )

    async def test_button_uses_the_saved_end(self):
        bot.save_end_setting(111, 20, "dentam6626")
        interaction = self.interaction()

        with mock.patch.object(bot, "PRESETS", [40]):
            button = self.button(40)
        with mock.patch.object(bot, "play_countdown", mock.AsyncMock()) as played:
            await button.callback(interaction)

        played.assert_awaited_once_with(interaction, 40, 20, reuse_full=True)

    async def test_button_counts_to_zero_when_nothing_is_set(self):
        interaction = self.interaction()

        with mock.patch.object(bot, "PRESETS", [40]):
            button = self.button(40)
        with mock.patch.object(bot, "play_countdown", mock.AsyncMock()) as played:
            await button.callback(interaction)

        self.assertEqual(played.await_args.args[1:], (40, 0))

    async def test_refuses_when_the_end_is_not_below_the_start(self):
        bot.save_end_setting(111, 30, "a")
        interaction = self.interaction()

        with mock.patch.object(bot, "PRESETS", [30]):
            button = self.button(30)
        with mock.patch.object(bot, "play_countdown", mock.AsyncMock()) as played:
            await button.callback(interaction)

        played.assert_not_awaited()
        self.assertTrue(any("数えられません" in m for m in replies(interaction)))

    async def test_choosing_an_end_saves_it_and_redraws_the_panel(self):
        interaction = self.interaction()
        select = bot.CountdownPanel().children[0]
        select._values = ["20"]

        with self.assertLogs("bell", level="INFO") as captured:
            await select.callback(interaction)

        self.assertEqual(bot.end_setting_for(111), (20, "dentam6626"))
        interaction.response.edit_message.assert_awaited_once()
        kwargs = interaction.response.edit_message.await_args.kwargs
        self.assertIn("20 まで", kwargs["content"])
        self.assertIn("dentam6626", kwargs["content"])
        redrawn = [o.value for o in kwargs["view"].children[0].options if o.default]
        self.assertEqual(redrawn, ["20"])
        self.assertIn("終わりを 20 に変更", "\n".join(captured.output))

    async def test_says_so_when_the_setting_cannot_be_saved(self):
        interaction = self.interaction()
        select = bot.CountdownPanel().children[0]
        select._values = ["20"]

        with mock.patch.object(bot, "save_end_setting", side_effect=OSError("書けない")):
            with self.assertLogs("bell", level="WARNING") as captured:
                await select.callback(interaction)

        self.assertIn("保存できませんでした", "\n".join(captured.output))
        interaction.response.edit_message.assert_not_awaited()
        self.assertTrue(any("保存できませんでした" in m for m in replies(interaction)))

    async def test_stop_button_logs_like_the_current_one(self):
        interaction = make_interaction(playing=True)
        stop = bot.CountdownPanel().children[-1]

        with self.assertLogs("bell", level="INFO") as captured:
            await stop.callback(interaction)

        interaction._voice_client.stop.assert_called_once()
        self.assertIn("停止（ボタン）", "\n".join(captured.output))


class ReuseFullAudioTest(unittest.IsolatedAsyncioTestCase):
    """パネルのボタンは start→0 を焼いて、それを切って流す。"""

    async def test_bakes_to_zero_and_plays_the_trimmed_length(self):
        baked = []

        def fake_ensure(start, end):
            baked.append((start, end))
            return "fake.wav"

        full = b"\x00" * (ONE_SECOND * (3 + 40 + 1))
        interaction = make_interaction()

        with mock.patch.object(bot, "ensure_wav", fake_ensure), \
                mock.patch.object(bot, "read_pcm", lambda path: full):
            await bot.play_countdown(interaction, 40, 20, reuse_full=True)

        self.assertEqual(baked, [(40, 0)], "40→20 を新しく焼かない")
        source = interaction._voice_client.play.call_args.args[0]
        self.assertEqual(len(source.stream.getvalue()), ONE_SECOND * (3 + 20 + 1))
        self.assertTrue(any("40 → 20" in m for m in replies(interaction)))

    async def test_command_still_bakes_the_exact_range(self):
        """/countdown は今までどおり。開始が300でも300→0を焼いたりしない。"""
        baked = []

        def fake_ensure(start, end):
            baked.append((start, end))
            return "fake.wav"

        with mock.patch.object(bot, "ensure_wav", fake_ensure), \
                mock.patch.object(bot, "read_pcm", lambda path: b"\x00" * ONE_SECOND):
            await bot.play_countdown(make_interaction(), 65, 40)

        self.assertEqual(baked, [(65, 40)])


# ------------------------------------------------------ 起動時のパネルと停止時の書き換え


def http_error(cls, status):
    response = mock.MagicMock(status=status, reason="x")
    return cls(response, "x")


class FakeChannel:
    """投稿・削除・書き換えを記録するだけのチャンネル。"""

    def __init__(self, channel_id=123, guild_id=111, send_error=None):
        self.id = channel_id
        self.name = "出陣VC"
        self.guild = mock.MagicMock(id=guild_id)
        self.guild.name = "テストサーバー"
        self.send_error = send_error
        self.sent = []
        self.deleted = []
        self.edited = []
        self.missing = set()
        self.next_id = 900

    async def send(self, content, view=None):
        if self.send_error is not None:
            raise self.send_error
        self.next_id += 1
        self.sent.append((content, view))
        return mock.MagicMock(id=self.next_id)

    def get_partial_message(self, message_id):
        channel = self

        class Partial:
            async def delete(self):
                if message_id in channel.missing:
                    raise http_error(discord.NotFound, 404)
                channel.deleted.append(message_id)

            async def edit(self, content=None, view="未指定"):
                if message_id in channel.missing:
                    raise http_error(discord.NotFound, 404)
                channel.edited.append((message_id, content, view))

        return Partial()


class AnnounceTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, filename in (
            ("SETTINGS_PATH", "panel_settings.json"),
            ("ANNOUNCED_PATH", "announced_panels.json"),
            ("STOP_REQUEST_PATH", "stop.request"),
        ):
            patcher = mock.patch.object(bot, name, os.path.join(self.tmp.name, filename))
            patcher.start()
            self.addCleanup(patcher.stop)

        # 手元の .env に書いた本物のチャンネルIDや基準日に左右されないようにする
        for name in ("BATTLE_ANCHOR_DATE", "PANEL_CHANNELS_SERVER_WAR", "PANEL_CHANNELS_DOMESTIC"):
            patcher = mock.patch.object(bot, name, "")
            patcher.start()
            self.addCleanup(patcher.stop)

        self.bell = bot.ShutsujinBell()
        self.channel = FakeChannel()
        self.bell.get_channel = lambda cid: self.channel if cid == self.channel.id else None
        self.bell.fetch_channel = mock.AsyncMock(side_effect=http_error(discord.NotFound, 404))

    def channels(self, text):
        patcher = mock.patch.object(bot, "PANEL_CHANNEL_IDS", text)
        patcher.start()
        self.addCleanup(patcher.stop)


class StartupPanelTest(AnnounceTestBase):
    async def test_does_nothing_when_no_channel_is_set(self):
        """ゲーム用のサーバーには貼らない。何も書かなければ何もしない。"""
        self.channels("")
        await self.bell.post_startup_panels()
        self.assertEqual(self.channel.sent, [])
        self.assertFalse(os.path.exists(bot.ANNOUNCED_PATH))

    async def test_posts_a_panel_and_remembers_it(self):
        self.channels("123")
        await self.bell.post_startup_panels()

        self.assertEqual(len(self.channel.sent), 1)
        content, view = self.channel.sent[0]
        self.assertIn("待機中", content)
        self.assertIn("ボイスチャンネルに入ってから", content)
        self.assertIsInstance(view, bot.CountdownPanel)
        self.assertEqual(bot.read_json(bot.ANNOUNCED_PATH), {"123": 901})

    async def test_panel_follows_the_saved_end(self):
        bot.save_end_setting(111, 40, "nori_26523")
        self.channels("123")
        with mock.patch.object(bot, "PRESETS", [45, 40]):
            await self.bell.post_startup_panels()

        content, view = self.channel.sent[0]
        self.assertIn("40 まで", content)
        disabled = {b.seconds: b.disabled for b in view.children
                    if isinstance(b, bot.CountdownButton)}
        self.assertEqual(disabled, {45: False, 40: True})

    async def test_replaces_the_panel_from_last_time(self):
        """毎週貼るたびに溜まっていかないこと。"""
        bot.write_json(bot.ANNOUNCED_PATH, {"123": 555})
        self.channels("123")

        await self.bell.post_startup_panels()

        self.assertEqual(self.channel.deleted, [555])
        self.assertEqual(bot.read_json(bot.ANNOUNCED_PATH), {"123": 901})

    async def test_clears_last_weeks_panel_from_a_channel_not_used_this_week(self):
        """鯖戦の週に493へ貼ったものが、次の週に残り続けないこと。"""
        last_week = FakeChannel(channel_id=777, guild_id=493)
        self.bell.get_channel = {123: self.channel, 777: last_week}.get
        bot.write_json(bot.ANNOUNCED_PATH, {"777": 555})
        self.channels("123")

        await self.bell.post_startup_panels()

        self.assertEqual(last_week.deleted, [555])
        self.assertEqual(last_week.sent, [])
        self.assertEqual(bot.read_json(bot.ANNOUNCED_PATH), {"123": 901})

    async def test_old_records_are_cleared_even_when_nothing_is_posted(self):
        bot.write_json(bot.ANNOUNCED_PATH, {"123": 555})
        self.channels("")

        await self.bell.post_startup_panels()

        self.assertEqual(self.channel.deleted, [555])
        self.assertEqual(bot.read_json(bot.ANNOUNCED_PATH), {})

    async def test_posts_to_the_battle_channel_on_a_battle_night(self):
        from datetime import datetime

        war = FakeChannel(channel_id=222, guild_id=493)
        self.bell.get_channel = {123: self.channel, 222: war}.get
        self.channels("123")

        with mock.patch.object(bot, "BATTLE_ANCHOR_DATE", "2026-09-12"), \
                mock.patch.object(bot, "PANEL_CHANNELS_SERVER_WAR", "222"):
            with self.assertLogs("bell", level="INFO") as captured:
                await self.bell.post_startup_panels(now=datetime(2026, 10, 10, 19, 0))

        self.assertEqual(len(war.sent), 1)
        self.assertEqual(len(self.channel.sent), 1, "毎週のチャンネルにも貼る")
        self.assertIn("今夜（10/10）は鯖戦", "\n".join(captured.output))
        self.assertEqual(bot.read_json(bot.ANNOUNCED_PATH), {"123": 901, "222": 901})

    async def test_skips_the_battle_channel_the_day_after(self):
        """鯖戦が終わった日曜の昼に起動しても、ゲーム用のサーバーには貼らない。"""
        from datetime import datetime

        war = FakeChannel(channel_id=222, guild_id=493)
        self.bell.get_channel = {123: self.channel, 222: war}.get
        self.channels("123")

        with mock.patch.object(bot, "BATTLE_ANCHOR_DATE", "2026-09-12"), \
                mock.patch.object(bot, "PANEL_CHANNELS_SERVER_WAR", "222"):
            with self.assertLogs("bell", level="INFO") as captured:
                await self.bell.post_startup_panels(now=datetime(2026, 9, 13, 14, 0))

        self.assertEqual(war.sent, [])
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("戦闘の夜ではない", "\n".join(captured.output))

    def battle_channels(self):
        """test / 鯖戦(493) / 国内戦(JYP) の3チャンネルを用意する。"""
        war = FakeChannel(channel_id=222, guild_id=493)
        war.guild.name = "493のサーバー"
        jyp = FakeChannel(channel_id=333, guild_id=777)
        jyp.guild.name = "【JYP】自由の誉火"
        self.bell.get_channel = {123: self.channel, 222: war, 333: jyp}.get
        self.channels("123")
        for name, value in (
            ("BATTLE_ANCHOR_DATE", "2026-09-12"),
            ("PANEL_CHANNELS_SERVER_WAR", "222"),
            ("PANEL_CHANNELS_DOMESTIC", "333"),
        ):
            patcher = mock.patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return war, jyp

    async def test_greets_the_alliance_server_on_a_server_war_night(self):
        """鯖戦の夜は、JYPにパネルは出さず「/panel で呼べるよ」とだけ出す。"""
        from datetime import datetime

        war, jyp = self.battle_channels()
        await self.bell.post_startup_panels(now=datetime(2026, 10, 10, 19, 0))

        self.assertIsInstance(war.sent[0][1], bot.CountdownPanel)
        self.assertEqual(len(jyp.sent), 1)
        content, view = jyp.sent[0]
        self.assertIsNone(view, "挨拶にはボタンを付けない")
        self.assertIn("待機中", content)
        self.assertIn("/panel", content)
        self.assertIn("パネルは493のサーバーに置いてあります", content)
        self.assertNotIn("テストサーバー", content, "テストサーバーの名前は出さない")
        self.assertEqual(
            bot.read_json(bot.ANNOUNCED_PATH), {"123": 901, "222": 901, "333": 901}
        )

    async def test_does_not_greet_the_shared_server_on_a_domestic_night(self):
        """国内戦の夜、493（他の同盟もいる共通サーバー）には何も出さない。"""
        from datetime import datetime

        war, jyp = self.battle_channels()
        await self.bell.post_startup_panels(now=datetime(2026, 9, 26, 19, 0))

        self.assertEqual(war.sent, [])
        self.assertIsInstance(jyp.sent[0][1], bot.CountdownPanel)

    async def test_greeting_links_to_the_command_when_its_id_is_known(self):
        from datetime import datetime

        war, jyp = self.battle_channels()
        self.bell.command_ids = {777: {"panel": 42, "stop": 43}}
        await self.bell.post_startup_panels(now=datetime(2026, 10, 10, 19, 0))

        self.assertIn("</panel:42>", jyp.sent[0][0])

    async def test_greeting_turns_into_goodbye_like_a_panel(self):
        from datetime import datetime

        war, jyp = self.battle_channels()
        await self.bell.post_startup_panels(now=datetime(2026, 10, 10, 19, 0))
        await self.bell.say_goodbye()

        self.assertEqual(len(jyp.edited), 1)
        self.assertIn("出陣完了", jyp.edited[0][1])

    async def test_old_panel_deleted_by_hand_is_fine(self):
        bot.write_json(bot.ANNOUNCED_PATH, {"123": 555})
        self.channel.missing.add(555)
        self.channels("123")

        await self.bell.post_startup_panels()

        self.assertEqual(len(self.channel.sent), 1)

    async def test_missing_permission_is_reported_not_crashed(self):
        self.channel.send_error = http_error(discord.Forbidden, 403)
        self.channels("123")

        with self.assertLogs("bell", level="WARNING") as captured:
            await self.bell.post_startup_panels()

        self.assertIn("メッセージを送信", "\n".join(captured.output))
        self.assertNotIn("123", bot.read_json(bot.ANNOUNCED_PATH))

    async def test_unknown_channel_is_skipped(self):
        self.channels("999, 123")

        with self.assertLogs("bell", level="WARNING") as captured:
            await self.bell.post_startup_panels()

        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("999", "\n".join(captured.output))

    async def test_something_without_messages_is_skipped(self):
        """カテゴリのIDを書いてしまった場合など。"""
        category = mock.MagicMock(spec=["id", "name", "guild"])
        self.bell.get_channel = lambda cid: category
        self.channels("123")

        with self.assertLogs("bell", level="WARNING"):
            await self.bell.post_startup_panels()

        self.assertFalse(os.path.exists(bot.ANNOUNCED_PATH) and bot.read_json(bot.ANNOUNCED_PATH))


class BattleWeekTest(unittest.TestCase):
    """鯖戦と国内戦は、それぞれ4週ごとに2週ずらして交互に来る。"""

    ANCHOR = "2026-09-12"

    def kind(self, year, month, day, anchor=None):
        from datetime import date

        return bot.battle_kind(date(year, month, day), self.ANCHOR if anchor is None else anchor)

    def test_alternates_every_two_weeks(self):
        self.assertEqual(self.kind(2026, 9, 12), "鯖戦")
        self.assertIsNone(self.kind(2026, 9, 19))
        self.assertEqual(self.kind(2026, 9, 26), "国内戦")
        self.assertIsNone(self.kind(2026, 10, 3))
        self.assertEqual(self.kind(2026, 10, 10), "鯖戦")
        self.assertEqual(self.kind(2026, 10, 24), "国内戦")

    def test_sunday_night_counts_as_the_saturday_before(self):
        """土19時〜日3時の運用なので、日曜の深夜に再起動しても切り替わらない。"""
        self.assertEqual(self.kind(2026, 9, 13), "鯖戦")
        self.assertEqual(self.kind(2026, 9, 27), "国内戦")

    def test_keeps_counting_far_ahead_and_behind(self):
        self.assertEqual(self.kind(2027, 9, 11), "鯖戦")   # 52週後
        self.assertEqual(self.kind(2026, 8, 29), "国内戦")  # 2週前

    def test_empty_anchor_means_no_battle_weeks(self):
        self.assertIsNone(self.kind(2026, 9, 12, anchor=""))

    def test_unreadable_anchor_is_reported(self):
        with self.assertLogs("bell", level="WARNING") as captured:
            self.assertIsNone(self.kind(2026, 9, 12, anchor="9/12"))
        self.assertIn("9/12", "\n".join(captured.output))


class ChannelsForWeekTest(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("BATTLE_ANCHOR_DATE", "2026-09-12"),
            ("PANEL_CHANNEL_IDS", "1"),
            ("PANEL_CHANNELS_SERVER_WAR", "2, 3"),
            ("PANEL_CHANNELS_DOMESTIC", "4,1"),
        ):
            patcher = mock.patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def ids(self, month, day, hour=19, minute=0):
        from datetime import datetime

        return bot.channel_ids_for(datetime(2026, month, day, hour, minute))

    def test_server_war_night_adds_the_server_war_channels(self):
        self.assertEqual(self.ids(9, 12), ("鯖戦", [1, 2, 3]))

    def test_domestic_night_adds_the_domestic_channels_without_duplicates(self):
        self.assertEqual(self.ids(9, 26), ("国内戦", [1, 4]))

    def test_saturday_without_a_battle_uses_only_the_every_week_channels(self):
        self.assertEqual(self.ids(9, 19), (None, [1]))

    def test_restart_after_midnight_is_still_the_battle_night(self):
        """戦闘中の日曜深夜に再起動しても、戦闘のチャンネルに貼り直す。"""
        self.assertEqual(self.ids(9, 13, hour=1), ("鯖戦", [1, 2, 3]))
        self.assertEqual(self.ids(9, 13, hour=5, minute=59), ("鯖戦", [1, 2, 3]))

    def test_the_day_after_does_not_post_to_the_battle_channel(self):
        """戦闘が終わった日曜の朝以降に起動しても、ゲーム用のサーバーには貼らない。"""
        self.assertEqual(self.ids(9, 13, hour=6), (None, [1]))
        self.assertEqual(self.ids(9, 13, hour=14), (None, [1]))

    def test_weekdays_never_post_to_the_battle_channel(self):
        self.assertEqual(self.ids(9, 11), (None, [1]))   # 鯖戦前日の金曜
        self.assertEqual(self.ids(9, 16), (None, [1]))   # 水曜

class HelloTest(unittest.TestCase):
    def test_only_on_a_server_war_night(self):
        with mock.patch.object(bot, "PANEL_CHANNELS_DOMESTIC", "4, 5"):
            self.assertEqual(bot.hello_channel_ids_for("鯖戦", [1, 2]), [4, 5])
            self.assertEqual(bot.hello_channel_ids_for("国内戦", [1, 4]), [])
            self.assertEqual(bot.hello_channel_ids_for(None, [1]), [])

    def test_a_channel_getting_a_panel_is_not_also_greeted(self):
        with mock.patch.object(bot, "PANEL_CHANNELS_DOMESTIC", "4, 5"):
            self.assertEqual(bot.hello_channel_ids_for("鯖戦", [4]), [5])

    def test_text_falls_back_to_plain_command(self):
        text = bot.hello_text("鯖戦", ["493のサーバー"])
        self.assertIn("`/panel`", text)
        self.assertTrue(text.splitlines()[-1].startswith("-# 今夜は鯖戦なので"))

    def test_text_uses_the_clickable_command(self):
        self.assertIn("</panel:42>", bot.hello_text("鯖戦", [], "</panel:42>"))

    def test_no_place_line_when_the_panel_could_not_be_posted(self):
        self.assertNotIn("-#", bot.hello_text("鯖戦", []))


class ParseChannelIdsTest(unittest.TestCase):
    def test_reads_comma_separated_ids(self):
        self.assertEqual(bot.parse_channel_ids("123, 456,,"), [123, 456])

    def test_skips_and_reports_things_that_are_not_ids(self):
        with self.assertLogs("bell", level="WARNING") as captured:
            ids = bot.parse_channel_ids("123,#出陣VC")
        self.assertEqual(ids, [123])
        self.assertIn("#出陣VC", "\n".join(captured.output))

    def test_empty_means_none(self):
        self.assertEqual(bot.parse_channel_ids(""), [])


class GoodbyeTest(AnnounceTestBase):
    async def test_turns_the_panel_into_a_goodbye_without_buttons(self):
        bot.write_json(bot.ANNOUNCED_PATH, {"123": 555})

        await self.bell.say_goodbye()

        self.assertEqual(len(self.channel.edited), 1)
        message_id, content, view = self.channel.edited[0]
        self.assertEqual(message_id, 555)
        self.assertIn("出陣完了", content)
        self.assertIsNone(view, "ボタンを外す")

    async def test_keeps_the_record_so_the_next_start_can_replace_it(self):
        bot.write_json(bot.ANNOUNCED_PATH, {"123": 555})
        await self.bell.say_goodbye()
        self.assertEqual(bot.read_json(bot.ANNOUNCED_PATH), {"123": 555})

    async def test_deleted_panel_does_not_stop_the_shutdown(self):
        bot.write_json(bot.ANNOUNCED_PATH, {"123": 555})
        self.channel.missing.add(555)

        with self.assertLogs("bell", level="WARNING"):
            await self.bell.say_goodbye()

    async def test_nothing_posted_means_nothing_to_do(self):
        await self.bell.say_goodbye()
        self.assertEqual(self.channel.edited, [])

    def test_goodbye_says_when_it_stopped(self):
        from datetime import datetime

        when = datetime(2026, 9, 20, 3, 0)
        text = bot.goodbye_text(when)
        self.assertIn("出陣完了", text)
        # 見る人の端末の時刻で出るよう、Discordのタイムスタンプ記法で書く
        self.assertIn(f"<t:{int(when.timestamp())}:f>", text)

    def test_time_line_is_small_print(self):
        """時刻の行は補足なので、Discordの小さい文字（-#）にする。"""
        lines = bot.goodbye_text().splitlines()
        self.assertTrue(lines[-1].startswith("-# "), lines[-1])


class CloseTest(AnnounceTestBase):
    def logged_in(self):
        self.bell._connection.user = mock.MagicMock()

    async def test_says_goodbye_once_then_closes(self):
        self.logged_in()
        self.bell.say_goodbye = mock.AsyncMock()

        with mock.patch.object(discord.Client, "close", mock.AsyncMock()) as parent_close:
            await self.bell.close()
            await self.bell.close()

        self.bell.say_goodbye.assert_awaited_once()
        self.assertEqual(parent_close.await_count, 2)

    async def test_skips_goodbye_before_login(self):
        self.bell._connection.user = None
        self.bell.say_goodbye = mock.AsyncMock()

        with mock.patch.object(discord.Client, "close", mock.AsyncMock()) as parent_close:
            await self.bell.close()

        self.bell.say_goodbye.assert_not_awaited()
        parent_close.assert_awaited_once()

    async def test_slow_goodbye_does_not_hold_up_the_shutdown(self):
        """stop_bot.ps1 に強制終了される前に、自分で終わりきること。"""
        self.logged_in()

        async def slow():
            await asyncio.sleep(5)

        self.bell.say_goodbye = slow
        with mock.patch.object(bot, "GOODBYE_TIMEOUT", 0.05), \
                mock.patch.object(discord.Client, "close", mock.AsyncMock()) as parent_close:
            with self.assertLogs("bell", level="WARNING"):
                await asyncio.wait_for(self.bell.close(), timeout=1)

        parent_close.assert_awaited_once()

    async def test_leaves_the_voice_channel_if_still_playing(self):
        self.logged_in()
        self.bell.say_goodbye = mock.AsyncMock()
        voice_client = mock.MagicMock()
        voice_client.disconnect = mock.AsyncMock()

        with mock.patch.object(bot.ShutsujinBell, "voice_clients",
                               new_callable=mock.PropertyMock, return_value=[voice_client]), \
                mock.patch.object(discord.Client, "close", mock.AsyncMock()):
            await self.bell.close()

        voice_client.stop.assert_called_once()
        voice_client.disconnect.assert_awaited_once()


class StopRequestTest(AnnounceTestBase):
    async def test_closes_when_the_note_appears(self):
        self.bell.close = mock.AsyncMock()
        with open(bot.STOP_REQUEST_PATH, "w", encoding="utf-8") as handle:
            handle.write("x")

        with mock.patch.object(bot, "STOP_POLL_SECONDS", 0.01):
            await asyncio.wait_for(self.bell.watch_for_stop_request(), timeout=1)

        self.bell.close.assert_awaited_once()
        self.assertFalse(os.path.exists(bot.STOP_REQUEST_PATH), "メモは片付ける")

    async def test_keeps_running_without_a_note(self):
        self.bell.close = mock.AsyncMock()

        with mock.patch.object(bot, "STOP_POLL_SECONDS", 0.01):
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(self.bell.watch_for_stop_request(), timeout=0.1)

        self.bell.close.assert_not_awaited()

    def test_leftover_note_is_cleared_at_startup(self):
        """残っていると、起動した瞬間に止まってしまう。"""
        with open(bot.STOP_REQUEST_PATH, "w", encoding="utf-8") as handle:
            handle.write("x")

        with self.assertLogs("bell", level="INFO"):
            self.assertTrue(bot.clear_stale_stop_request())
        self.assertFalse(os.path.exists(bot.STOP_REQUEST_PATH))
        self.assertFalse(bot.clear_stale_stop_request())


if __name__ == "__main__":
    unittest.main()
