"""出陣ベル — カウントダウンをDiscordのボイスチャンネルで流すBot。

    /countdown 45      … その場で秒数を指定して流す
    /panel             … よく使う秒数のボタンと、終わりの秒数の選択欄を設置（普段はこっち）
    /stop              … 再生を止めて退出

起動したら .env の PANEL_CHANNEL_IDS のチャンネルにパネルを貼り、
止まるときにそのパネルを「おやすみ中」へ書き換える。

音声は voice_countdown.py が焼いた wav をそのまま流すだけ。
Discordのボイスは 48kHz・ステレオ・16bit しか受け取らないが、
VOICEVOXに最初からその形式で出させているので変換処理は要らない。

    .venv\\Scripts\\python src\\bot.py
"""

import asyncio
import io
import json
import logging
import os
import sys
import time
import wave
from datetime import datetime
from types import SimpleNamespace

import discord
from discord import app_commands

import voice_countdown as vc

HERE = os.path.dirname(os.path.abspath(__file__))
# ソースは src/ に置いてあるが、.env と logs/ はリポジトリ直下にある。
ROOT = os.path.dirname(HERE)
LOG_DIR = os.path.join(ROOT, "logs")

# ログは起動した日ごとに1本。日付をまたいでも切り替えない。
# 土19時〜日3時のように夜をまたぐ運用で、1回ぶんが1ファイルに収まる。
LOG_KEEP_DAYS = 7

log = logging.getLogger("bell")

# カウント開始前の無音と合図。Botが通話に入るまでの間があるので、
# いきなり数字が始まらないようにしておく。
# 「よーい」は最初の数字の0.3秒前に鳴り終わるよう置かれる（voice_countdown.py）。
# 合図は約0.46秒なので、1.8秒にすると入ってから約1秒で「よーい」が鳴る。
# 3.0秒だったころは約2.2秒後だった。「入って1秒でいい」という要望で短くした。
# 変えると音声のファイル名が変わるので、パネルの秒数は焼き直しになる。
LEAD_SECONDS = 1.8
CUE_TEXT = "よーい"

# Discordのボイスが受け取る形式: 48000Hz / 2ch / 16bit
# 1秒あたりのバイト数。再生時間の見積もりに使う。
DISCORD_BYTES_PER_SECOND = 48000 * 2 * 2


# ----------------------------------------------------------------- 設定の読み込み


def load_env(path):
    """.env を読む。python-dotenv を入れないための小さな実装。"""
    values = {}
    if not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


ENV = load_env(os.path.join(ROOT, ".env"))

TOKEN = ENV.get("DISCORD_TOKEN", "")
SPEAKER = int(ENV.get("VOICEVOX_SPEAKER", 3))
SPEED = float(ENV.get("VOICEVOX_SPEED", 1.2))
HOST = ENV.get("VOICEVOX_HOST", vc.DEFAULT_HOST)

# パネルに並べるボタン。
# Discordは1行5個・最大5行。一番上を終わりの選択欄、最下段を停止ボタンに使うので、
# 秒数は間の3行ぶんまで。
MAX_BUTTONS = 15
PRESETS = [
    int(value)
    for value in ENV.get("COUNTDOWN_PRESETS", "45,60,30").split(",")
    if value.strip()
][:MAX_BUTTONS]

# 終わりの秒数の選択肢。5秒刻みで50まで。選択欄に並べられるのは25個まで。
# 開始秒以下を選ぶとそのボタンは数えるものがないので、パネルでは灰色にする。
END_CHOICES = list(range(0, 55, 5))

# サーバーごとの「終わり」の設定。再起動しても黙って0に戻らないよう、ファイルに残す。
SETTINGS_PATH = os.path.join(ROOT, "panel_settings.json")

# 起動したときにパネルを貼るチャンネル（カンマ区切りのID）。空なら貼らない。
# ゲーム用のサーバーに毎回貼ると通知がうるさいので、今はテストサーバーだけにしている。
PANEL_CHANNEL_IDS = ENV.get("PANEL_CHANNEL_IDS", "")

# 起動時に貼ったパネルのメッセージID。次の起動で消す・止まるときに書き換えるのに使う。
ANNOUNCED_PATH = os.path.join(ROOT, "announced_panels.json")

# tools/stop_bot.ps1 が置く「止まってね」のメモ。見つけたら後片付けをして自分で終わる。
STOP_REQUEST_PATH = os.path.join(ROOT, "stop.request")
STOP_POLL_SECONDS = 2

# 止まるときの書き換えにかける時間の上限。stop_bot.ps1 は20秒で強制終了に切り替えるので、
# それより十分短くしておく。
GOODBYE_TIMEOUT = 10


# ------------------------------------------------------------------- 音声の用意


def countdown_args(start, end):
    """voice_countdown が期待する引数の束を組み立てる。"""
    return SimpleNamespace(
        start=start,
        end=end,
        step=1,
        dense=0,
        speaker=SPEAKER,
        speed=SPEED,
        lead=LEAD_SECONDS,
        cue=CUE_TEXT,
        host=HOST,
        # Discordのボイスが受け取れる唯一の形式
        sample_rate=48000,
        stereo=True,
    )


def ensure_wav(start, end):
    """焼いてあればそのまま、なければ焼いてからパスを返す。

    VOICEVOXへのリクエストは同期的で数十秒かかるので、
    呼ぶ側は必ず asyncio.to_thread 越しに使うこと。
    """
    args = countdown_args(start, end)
    path = vc.cache_path(args)
    if not os.path.exists(path):
        os.makedirs(vc.CACHE_DIR, exist_ok=True)
        vc.build_countdown_wav(path, args)
    return path


def read_pcm(path):
    """wavのヘッダを外して、生のPCMバイト列だけを取り出す。

    discord.PCMAudio はヘッダなしの生PCMを読むので、ここで剥がしておく。
    """
    with wave.open(path, "rb") as reader:
        return reader.readframes(reader.getnframes())


def trim_to_end(pcm, end):
    """start→0 の音声を、start→end の長さに切り詰める。

    数字は「開始から何秒目」の決まった位置に焼いてあるので、start→end は
    start→0 の頭の部分とバイト単位で同じになる（voice/ にあった8組で確かめた）。
    違うのは後ろの長さだけなので、末尾から end 秒ぶん落とせばよい。
    頭の合図で lead が伸びていても、後ろから削るので影響を受けない。

    切り口は end-1 が鳴り始める位置ちょうどなので、次の数字は入らない。
    """
    if end <= 0:
        return pcm
    return pcm[: len(pcm) - end * DISCORD_BYTES_PER_SECOND]


# ----------------------------------------------------- 手元に残す小さな記録


def read_json(path):
    """JSONを辞書として読む。無い・壊れている・辞書でないときは空として扱う。"""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path, data):
    # 書いている途中で落ちても元のファイルが壊れないよう、一時ファイルから差し替える
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_end_settings():
    """サーバーごとの終わりの設定を読む。"""
    return read_json(SETTINGS_PATH)


def end_setting_for(guild_id):
    """(終わりの秒数, 最後に変えた人) を返す。未設定なら (0, None)。"""
    entry = load_end_settings().get(str(guild_id))
    if not isinstance(entry, dict):
        return 0, None
    end = entry.get("end", 0)
    if not isinstance(end, int) or end < 0:
        return 0, None
    return end, entry.get("by")


def save_end_setting(guild_id, end, by):
    data = load_end_settings()
    data[str(guild_id)] = {"end": end, "by": by}
    write_json(SETTINGS_PATH, data)


# --------------------------------------------------------------------- 記録


def log_path_for_today():
    return os.path.join(LOG_DIR, f"bot-{datetime.now():%Y-%m-%d}.log")


def sweep_old_logs():
    """保持期間を過ぎたログを消す。

    起動のたびに1回だけ走らせる。動かしっぱなしでも増え続けないのは、
    1回の起動で1ファイルしか作らないため。
    """
    limit = time.time() - LOG_KEEP_DAYS * 86400
    for name in os.listdir(LOG_DIR):
        if not (name.startswith("bot-") and name.endswith(".log")):
            continue
        path = os.path.join(LOG_DIR, name)
        try:
            if os.path.getmtime(path) < limit:
                os.remove(path)
                log.info(f"古いログを削除: {name}")
        except OSError as exc:
            log.warning(f"古いログを消せませんでした {name}: {exc}")


def setup_logging():
    """画面とファイルの両方に記録する。

    discord.py は自前でログ設定をするので、client.run(log_handler=None) と
    組にして使うこと。そうすると接続や切断のイベントも同じファイルに残り、
    「あのとき落ちていたのか」を後から追える。

    ファイルは起動した日ごとに分ける（bot-2026-09-12.log）。
    日付で切り替えないのは、夜をまたぐ運用で1回ぶんが2つに割れると
    追いにくくなるため。古いものは起動時にまとめて消す。
    """
    os.makedirs(LOG_DIR, exist_ok=True)

    formatter = logging.Formatter(
        "[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    to_file = logging.FileHandler(log_path_for_today(), encoding="utf-8")
    to_file.setFormatter(formatter)

    to_screen = logging.StreamHandler()
    to_screen.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(to_file)
    root.addHandler(to_screen)

    sweep_old_logs()


def log_use(interaction, what):
    """誰が何を使ったかを記録する。

    Discord側の応答は本人にしか見えないので、記録がないと
    「さっき誰かが押したみたいだけど動いたのか」を後から確かめられない。
    """
    where = interaction.guild.name if interaction.guild else "DM"
    log.info(f"{interaction.user} — {what}（{where}）")


# --------------------------------------------------------------------- 再生


async def respond(interaction, message):
    """まだ応答していなければ応答、していれば追伸として送る。"""
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def play_countdown(interaction, start, end, reuse_full=False):
    """呼んだ人のいるボイスチャンネルでカウントダウンを流す。

    reuse_full=True のときは start→0 を焼いて（焼いてあればそれを使って）、
    end の手前で切って流す。パネルの秒数は start→0 を起動時に焼いてあるので、
    終わりをいくつに変えても待ち時間なしで鳴る。
    /countdown は開始秒が自由なので、今までどおり start→end をそのまま焼く。
    """
    log_use(interaction, f"{start} → {end}")

    voice_state = interaction.user.voice
    if voice_state is None or voice_state.channel is None:
        log.info("    → ボイスチャンネルに入っていないので中止")
        await respond(interaction, "先にボイスチャンネルに入ってください。")
        return

    channel = voice_state.channel
    voice_client = interaction.guild.voice_client

    if voice_client is not None and voice_client.is_playing():
        log.info("    → すでに再生中なので中止")
        await respond(interaction, "いま再生中です。止めるなら /stop。")
        return

    # 権限は先に見ておく。ここで弾いておかないと、connect() が例外を投げて
    # 「考え中」のまま何も返らない状態になり、原因が分からなくなる。
    permissions = channel.permissions_for(interaction.guild.me)
    if not permissions.connect or not permissions.speak:
        await respond(
            interaction,
            f"「{channel.name}」で接続または発言の権限がありません。\n"
            f"チャンネルの権限設定を確認してください。",
        )
        return

    # 音声を焼く場合は数十秒かかる。Discordは3秒以内に応答しないと
    # タイムアウトするので、先に「考え中」を返して時間を稼ぐ。
    await interaction.response.defer(ephemeral=True)

    try:
        path = await asyncio.to_thread(ensure_wav, start, 0 if reuse_full else end)
        pcm = await asyncio.to_thread(read_pcm, path)
        if reuse_full:
            pcm = trim_to_end(pcm, end)
    except vc.VoicevoxError as exc:
        await respond(interaction, f"音声を用意できませんでした。\n{exc}")
        return

    try:
        if voice_client is None:
            voice_client = await channel.connect()
        elif voice_client.channel != channel:
            await voice_client.move_to(channel)
    except (discord.ClientException, discord.HTTPException, asyncio.TimeoutError) as exc:
        # 何が起きても必ず返事をする。黙って落ちると「考え中」のまま残る。
        await respond(interaction, f"ボイスチャンネルに入れませんでした: {exc}")
        return

    # play() は別スレッドで再生し、終わると after が呼ばれる。
    # そのままだと待てないので、Eventで再生終了を拾えるようにする。
    finished = asyncio.Event()
    loop = asyncio.get_running_loop()

    voice_client.play(
        discord.PCMAudio(io.BytesIO(pcm)),
        after=lambda error: loop.call_soon_threadsafe(finished.set),
    )

    # 何を流しているかを明示する。指定した秒数と違えばここで気づける。
    seconds = len(pcm) / DISCORD_BYTES_PER_SECOND
    log.info(f"    → 再生開始「{channel.name}」 約{seconds:.0f}秒")
    await respond(interaction, f"**{start} → {end}** を流します。")

    # 異常切断などで after が呼ばれないと、ここで永久に待ち続けてしまう。
    # そうなると voice_client が繋がったままになり、以降ずっと
    # 「いま再生中です」と言い続ける詰み状態になる。長さ+30秒で打ち切る。
    try:
        await asyncio.wait_for(finished.wait(), timeout=seconds + 30)
    except asyncio.TimeoutError:
        log.warning(f"再生の終了を検知できませんでした（{start}→{end}）。切断します。")

    if voice_client.is_connected():
        await voice_client.disconnect()
    log.info("    → 再生終了、退出しました")


async def stop_playback(interaction, how):
    """再生を止めて退出する。/stop と停止ボタンの共通処理。

    how には呼び出し元を渡す。ログを見たときに、赤いボタンが使われたのか
    コマンドが打たれたのかを区別できるようにするため。
    """
    log_use(interaction, f"停止（{how}）")
    voice_client = interaction.guild.voice_client
    if voice_client is None:
        await respond(interaction, "いま再生していません。")
        return
    voice_client.stop()
    await voice_client.disconnect()
    await respond(interaction, "止めました。")


# --------------------------------------------------------------------- パネル


# 秒数のボタンと停止ボタンの custom_id は、終わりを選べるようにする前から変えていない。
# 変えると、各サーバーにすでに貼ってあるパネルが反応しなくなる。
# 古いパネルには選択欄がないが、ボタンは保存された終わりの設定で動く。


def panel_text(guild_id, note=None):
    end, by = end_setting_for(guild_id)
    who = f"（{by} が変更）" if by else ""
    lines = ["**出陣カウントダウン**"]
    if note:
        lines.append(note)
    lines.append(f"いまの設定：**{end} まで**数える{who}")
    lines.append("ボイスチャンネルに入ってから押してください。")
    return "\n".join(lines)


class EndSelect(discord.ui.Select):
    """何秒まで数えるかを選ぶ。

    要望: 終わりの秒数を選べるようにしたい。ゴースト部隊の位置で最速の人が変わるため。
    終わりは戦闘ごとに決まるもので、1回ごとに変わるものではない。なので押すたびに
    選ばせず、パネル上部でサーバーごとに一度決めておく形にした。
    """

    def __init__(self, current=0):
        super().__init__(
            custom_id="kingshot:end",
            placeholder="終わりの秒数を選ぶ",
            options=[
                discord.SelectOption(
                    label=f"{n} まで数える", value=str(n), default=(n == current)
                )
                for n in END_CHOICES
            ],
            row=0,
        )

    async def callback(self, interaction):
        end = int(self.values[0])
        try:
            save_end_setting(interaction.guild.id, end, str(interaction.user))
        except OSError as exc:
            log.warning(f"終わりの設定を保存できませんでした: {exc}")
            await respond(interaction, f"設定を保存できませんでした。\n{exc}")
            return

        log_use(interaction, f"終わりを {end} に変更")
        # 文言・選択欄・灰色のボタンを、新しい設定で描き直す。
        # 描き直さないと、Discord側の選択欄は押す前の表示に戻ってしまう。
        await interaction.response.edit_message(
            content=panel_text(interaction.guild.id),
            view=CountdownPanel(current=end),
        )


class CountdownButton(discord.ui.Button):
    def __init__(self, seconds, row=None):
        super().__init__(
            label=f"{seconds}秒",
            style=discord.ButtonStyle.primary,
            # custom_id を固定しておくと、Bot再起動後もボタンが生き続ける
            custom_id=f"kingshot:countdown:{seconds}",
            row=row,
        )
        self.seconds = seconds

    async def callback(self, interaction):
        # 押した瞬間の保存値を使う。パネルが2枚あって片方の表示が古くても、
        # 選択欄のない古いパネルから押されても、流れるのは最新の設定。
        end, _ = end_setting_for(interaction.guild.id)
        if end >= self.seconds:
            log_use(interaction, f"{self.seconds} → {end} は数えられないので中止")
            await respond(
                interaction,
                f"終わり（{end}）が {self.seconds}秒 以上なので数えられません。"
                f"終わりの秒数を下げてください。",
            )
            return
        await play_countdown(interaction, self.seconds, end, reuse_full=True)


class StopButton(discord.ui.Button):
    """要望: 終わりを選べても、やり直しですぐ掛け直すことがあるので残す。"""

    def __init__(self, row):
        super().__init__(
            label="止める",
            style=discord.ButtonStyle.danger,
            custom_id="kingshot:stop",
            row=row,
        )

    async def callback(self, interaction):
        await stop_playback(interaction, "ボタン")


class CountdownPanel(discord.ui.View):
    def __init__(self, current=0):
        # timeout=None で、時間が経ってもボタンが死なない
        super().__init__(timeout=None)

        self.add_item(EndSelect(current))

        # 行を明示する。自動配置に任せると停止ボタンが秒数の列に混ざり、
        # 慌てているときに押し間違える。選択欄が1行目なので、秒数は2行目から。
        # 上限で切っておかないと行が溢れて、起動時の登録で落ちる。
        presets = PRESETS[:MAX_BUTTONS]
        for index, seconds in enumerate(presets):
            button = CountdownButton(seconds, row=1 + index // 5)
            # 終わり以下の秒数は数えるものがないので押せなくする。
            # 表示が古いパネルから押された場合に備えて、callback 側でも断っている。
            button.disabled = seconds <= current
            self.add_item(button)

        # 秒数が使い切った次の行へ。いちばん下に赤で置く
        self.add_item(StopButton(row=1 + (len(presets) + 4) // 5))


# ------------------------------------------------------------ 起動と停止の案内


def parse_channel_ids(text):
    """「123, 456」を [123, 456] にする。数字でないものは知らせて飛ばす。"""
    ids = []
    for part in text.split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
        elif part:
            log.warning(f"PANEL_CHANNEL_IDS の「{part}」はチャンネルIDではないので飛ばします")
    return ids


STARTUP_NOTE = "🔔 出陣ベル、起動しました！"


def goodbye_text(now=None):
    """止まったときにパネルを書き換える文言。

    時刻は Discord のタイムスタンプ記法（<t:秒:f>）で書く。見る人の端末の
    時刻と言語で表示されるので、こちらでタイムゾーンを気にしなくてよい。
    -# で始まる行は、Discordでは小さい灰色の補足になる。
    """
    now = now or datetime.now()
    return (
        "**出陣カウントダウン**\n"
        "💤 出陣ベルはおやすみ中です。バイバイ〜👋\n"
        f"-# <t:{int(now.timestamp())}:f> に停止 ・ 次に起動すると、ここに新しいパネルが出ます"
    )


def clear_stale_stop_request():
    """前回の「止まってね」のメモが残っていたら消す。

    残っていると、起動した直後にメモを見つけて止まってしまう。
    stop_bot.ps1 も後で消しているが、途中で落ちた場合に備えてここでも見る。
    """
    try:
        os.remove(STOP_REQUEST_PATH)
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning(f"前回の停止のメモを消せませんでした: {exc}")
        return False
    log.info("前回の停止のメモが残っていたので消しました")
    return True


# ------------------------------------------------------------------- Bot本体


class ShutsujinBell(discord.Client):
    def __init__(self):
        # 特権インテントは使わない。スラッシュコマンドとボタンだけで完結する。
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.ready_once = False
        self.goodbye_done = False

    async def setup_hook(self):
        # 再起動しても既存のパネルのボタンが反応するように登録し直す
        self.add_view(CountdownPanel())
        # 停止のメモは起動直後から見張る。on_ready を待つと、その前に来たメモを取りこぼす
        self.stop_watcher = asyncio.create_task(self.watch_for_stop_request())

    async def on_ready(self):
        # on_ready は起動時だけでなく、再接続のたびに呼ばれる。
        # 毎回コマンドを配り直すと無駄だし、レート制限にも当たりうる。
        if self.ready_once:
            log.info("再接続しました")
            return
        self.ready_once = True

        # グローバル同期は反映に最大1時間かかるが、Botのプロフィールに出る
        # コマンド一覧はこちらを見ている。使い勝手のためのサーバー個別同期と
        # 両方やっておく。同名ならサーバー側が優先されるので二重には出ない。
        await self.tree.sync()

        # 参加中のサーバーへ直接配ると即座に使えるようになる。
        for guild in self.guilds:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

        log.info(f"ログイン: {self.user}")
        log.info(f"サーバー: {', '.join(g.name for g in self.guilds) or '(なし)'}")
        log.info(f"話者ID {SPEAKER} / 速さ {SPEED} / ボタン {PRESETS}")

        asyncio.create_task(self.prebake())
        await self.post_startup_panels()

    async def on_guild_join(self, guild):
        """新しいサーバーに追加されたときも、そこへコマンドを配る。

        on_ready は起動時にしか呼ばれない。これがないと、Botを動かしたまま
        サーバーに追加しても、再起動するまでコマンドが出てこない。
        """
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info(f"参加: {guild.name}（コマンドを配りました）")

    async def prebake(self):
        """ボタンぶんの音声を先に焼いておき、押した瞬間に鳴るようにする。"""
        for seconds in PRESETS:
            try:
                path = await asyncio.to_thread(ensure_wav, seconds, 0)
                log.info(f"  用意OK {seconds}秒: {os.path.basename(path)}")
            except vc.VoicevoxError as exc:
                log.warning(f"  用意できず {seconds}秒: {exc}")
                log.warning("  （VOICEVOXを起動すれば、押したときに焼き直します）")
                return

    # ------------------------------------------------------ 起動時のパネル

    async def post_startup_panels(self):
        """.env の PANEL_CHANNEL_IDS のチャンネルにパネルを貼る。

        前回の起動で貼ったものは消してから貼る。残すと毎週1枚ずつ溜まっていくし、
        新しく貼れば一番下に出るので、チャットが流れていても見つけやすい。
        """
        channel_ids = parse_channel_ids(PANEL_CHANNEL_IDS)
        if not channel_ids:
            return

        posted = read_json(ANNOUNCED_PATH)
        for channel_id in channel_ids:
            key = str(channel_id)
            channel = await self.find_channel(channel_id)
            if channel is None:
                continue

            old_id = posted.pop(key, None)
            if old_id is not None:
                await self.delete_quietly(channel, old_id)

            end, _ = end_setting_for(channel.guild.id)
            try:
                message = await channel.send(
                    panel_text(channel.guild.id, note=STARTUP_NOTE),
                    view=CountdownPanel(current=end),
                )
            except discord.HTTPException as exc:
                log.warning(
                    f"パネルを貼れませんでした「{channel.name}」: {exc}"
                    "（そのチャンネルでBotに「チャンネルを見る」「メッセージを送信」の権限があるか確認）"
                )
                continue

            posted[key] = message.id
            log.info(f"パネルを貼りました「{channel.guild.name} / {channel.name}」")

        self.save_announced(posted)

    async def find_channel(self, channel_id):
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except (discord.HTTPException, discord.InvalidData) as exc:
                log.warning(f"チャンネルが見つかりません（ID {channel_id}）: {exc}")
                return None
        if not hasattr(channel, "send"):
            # カテゴリのIDなど、メッセージを置けないもの
            log.warning(f"ID {channel_id} はメッセージを置けるチャンネルではありません")
            return None
        return channel

    async def delete_quietly(self, channel, message_id):
        try:
            await channel.get_partial_message(int(message_id)).delete()
        except discord.NotFound:
            pass  # 手で消されていた
        except (discord.HTTPException, ValueError, TypeError) as exc:
            log.warning(f"前回のパネルを消せませんでした（{message_id}）: {exc}")

    def save_announced(self, posted):
        try:
            write_json(ANNOUNCED_PATH, posted)
        except OSError as exc:
            log.warning(f"貼ったパネルの記録を保存できませんでした: {exc}")

    # ---------------------------------------------------------- 止まるとき

    async def watch_for_stop_request(self):
        """stop_bot.ps1 が置くメモを見張り、見つけたら自分で終わる。

        タスクスケジューラの停止は以前いきなり強制終了していたので、
        パネルを書き換える暇も、ログに「終了しました」を残す暇もなかった。
        """
        while not self.is_closed():
            if os.path.exists(STOP_REQUEST_PATH):
                log.info("停止のメモを受け取りました。後片付けをして終了します")
                try:
                    os.remove(STOP_REQUEST_PATH)
                except OSError:
                    pass
                await self.close()
                return
            await asyncio.sleep(STOP_POLL_SECONDS)

    async def say_goodbye(self):
        """起動時に貼ったパネルを「おやすみ中」に書き換えて、ボタンを外す。

        停止中に古いボタンが押されて「応答しませんでした」になるのを防ぐ。
        メッセージは消さずに残し、次の起動で消して貼り直す。
        """
        text = goodbye_text()
        for key, message_id in read_json(ANNOUNCED_PATH).items():
            try:
                channel = await self.find_channel(int(key))
                if channel is None:
                    continue
                await channel.get_partial_message(int(message_id)).edit(
                    content=text, view=None
                )
                log.info(f"パネルを停止中にしました「{channel.name}」")
            except (discord.HTTPException, ValueError, TypeError) as exc:
                log.warning(f"パネルを停止中にできませんでした（{key}）: {exc}")

    async def close(self):
        """終わる前に後片付けをする。

        stop_bot.ps1 のメモで止めたときも、Ctrl+C で止めたときもここを通る。
        ×で閉じる・強制終了・スリープではPythonが動けないので通らない。
        """
        if not self.goodbye_done:
            self.goodbye_done = True

            # 再生中なら切っておく。繋がったまま落ちると、しばらくVCに残って見える
            for voice_client in list(self.voice_clients):
                try:
                    voice_client.stop()
                    await voice_client.disconnect(force=True)
                except (discord.ClientException, discord.HTTPException, asyncio.TimeoutError) as exc:
                    log.warning(f"ボイスチャンネルから抜けられませんでした: {exc}")

            # ログイン前に止まった場合は、Discordに何も送れない
            if self.user is not None:
                try:
                    await asyncio.wait_for(self.say_goodbye(), timeout=GOODBYE_TIMEOUT)
                except asyncio.TimeoutError:
                    log.warning("パネルの書き換えが時間内に終わらなかったので、そのまま終了します")

        await super().close()


client = ShutsujinBell()


@client.tree.command(name="countdown", description="秒数を指定して出陣カウントダウンを流します")
@app_commands.guild_only()
@app_commands.describe(
    start="開始する残り秒数（例: 60）",
    end="終了する残り秒数（0まで読むなら 0）",
)
async def countdown_command(
    interaction: discord.Interaction,
    # start も end もあえて必須にしている。省略可にすると、値をフィールドに
    # 入れないまま送信できてしまい、黙って既定値が使われる。
    # 「65 40 と打ったのに 65→0 が流れる」という気づきにくい事故が実際に起きた。
    # Range を使うと範囲チェックはDiscord側がやってくれる。
    start: app_commands.Range[int, 1, 300],
    end: app_commands.Range[int, 0, 299],
):
    if start <= end:
        await respond(interaction, f"開始({start})は終了({end})より大きくしてください。")
        return
    await play_countdown(interaction, start, end)


@client.tree.command(name="panel", description="カウントダウンのボタンを設置します")
@app_commands.guild_only()
async def panel_command(interaction: discord.Interaction):
    log_use(interaction, "パネルを設置")
    end, _ = end_setting_for(interaction.guild.id)
    await interaction.response.send_message(
        panel_text(interaction.guild.id),
        view=CountdownPanel(current=end),
    )


@client.tree.command(name="stop", description="再生を止めて退出します")
@app_commands.guild_only()
async def stop_command(interaction: discord.Interaction):
    await stop_playback(interaction, "コマンド")


def main():
    setup_logging()

    if not TOKEN:
        log.error(
            ".env の DISCORD_TOKEN が空です。"
            " Discord Developer Portal > Bot > トークンをリセット で発行して、"
            " .env に貼ってください。"
        )
        return 1

    log.info("=" * 52)
    log.info(f"起動します（記録: {log_path_for_today()}）")
    clear_stale_stop_request()

    try:
        # log_handler=None にすると discord.py が自前のログ設定をしないので、
        # setup_logging() で用意したファイルと画面の両方に流れる。
        client.run(TOKEN, log_handler=None)
    except discord.LoginFailure:
        log.error(
            "トークンが拒否されました。.env の DISCORD_TOKEN を確認してください。"
            "（リセットすると古いトークンは無効になります）"
        )
        return 1
    finally:
        # Ctrl+C や stop_bot.ps1 のメモで止めた場合はここを通る。
        # ウィンドウの×で閉じる・強制終了ではプロセスごと消えるので記録は残らない。
        log.info("終了しました")

    return 0


if __name__ == "__main__":
    sys.exit(main())
