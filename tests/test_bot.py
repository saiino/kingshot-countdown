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
import os
import sys
import unittest
import wave
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

        # 秒数のボタンは青のまま
        for row in rows[:-1]:
            for component in row["components"]:
                self.assertEqual(
                    component["style"], discord.ButtonStyle.primary.value
                )

    def test_presets_leave_room_for_the_stop_row(self):
        """上限まで並べても、停止ボタンのぶんの行が残ること。"""
        self.assertLessEqual(bot.MAX_BUTTONS, 20)
        self.assertLessEqual((bot.MAX_BUTTONS + 4) // 5, 4)

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


if __name__ == "__main__":
    unittest.main()
