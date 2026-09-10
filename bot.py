"""出陣ベル — カウントダウンをDiscordのボイスチャンネルで流すBot。

    /countdown 45      … その場で秒数を指定して流す
    /panel             … よく使う秒数のボタンを設置（普段はこっち）
    /stop              … 再生を止めて退出

音声は voice_countdown.py が焼いた wav をそのまま流すだけ。
Discordのボイスは 48kHz・ステレオ・16bit しか受け取らないが、
VOICEVOXに最初からその形式で出させているので変換処理は要らない。

    .venv\\Scripts\\python bot.py
"""

import asyncio
import io
import logging
import logging.handlers
import os
import sys
import wave
from types import SimpleNamespace

import discord
from discord import app_commands

import voice_countdown as vc

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
LOG_FILE = os.path.join(LOG_DIR, "bot.log")

log = logging.getLogger("bell")

# カウント開始前の無音と合図。Botが通話に入るまでの間があるので、
# いきなり数字が始まらないようにしておく。
LEAD_SECONDS = 3.0
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


ENV = load_env(os.path.join(HERE, ".env"))

TOKEN = ENV.get("DISCORD_TOKEN", "")
SPEAKER = int(ENV.get("VOICEVOX_SPEAKER", 3))
SPEED = float(ENV.get("VOICEVOX_SPEED", 1.2))
HOST = ENV.get("VOICEVOX_HOST", vc.DEFAULT_HOST)

# パネルに並べるボタン。
# Discordは1行5個・最大5行なので、合計25個まで置ける。
# 6個以上を渡すと discord.py が自動で次の行へ折り返す。
MAX_BUTTONS = 25
PRESETS = [
    int(value)
    for value in ENV.get("COUNTDOWN_PRESETS", "45,60,30").split(",")
    if value.strip()
][:MAX_BUTTONS]


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


# --------------------------------------------------------------------- 記録


def setup_logging():
    """画面とファイルの両方に記録する。

    discord.py は自前でログ設定をするので、client.run(log_handler=None) と
    組にして使うこと。そうすると接続や切断のイベントも同じファイルに残り、
    「あのとき落ちていたのか」を後から追える。

    ファイルは 1MB を超えたら世代交代させ、3世代まで残す。
    放っておいても際限なく太らない。
    """
    os.makedirs(LOG_DIR, exist_ok=True)

    formatter = logging.Formatter(
        "[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    to_file = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    to_file.setFormatter(formatter)

    to_screen = logging.StreamHandler()
    to_screen.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(to_file)
    root.addHandler(to_screen)


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


async def play_countdown(interaction, start, end):
    """呼んだ人のいるボイスチャンネルでカウントダウンを流す。"""
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
        path = await asyncio.to_thread(ensure_wav, start, end)
        pcm = await asyncio.to_thread(read_pcm, path)
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


# --------------------------------------------------------------------- パネル


class CountdownButton(discord.ui.Button):
    def __init__(self, seconds):
        super().__init__(
            label=f"{seconds}秒",
            style=discord.ButtonStyle.primary,
            # custom_id を固定しておくと、Bot再起動後もボタンが生き続ける
            custom_id=f"kingshot:countdown:{seconds}",
        )
        self.seconds = seconds

    async def callback(self, interaction):
        await play_countdown(interaction, self.seconds, 0)


class CountdownPanel(discord.ui.View):
    def __init__(self):
        # timeout=None で、時間が経ってもボタンが死なない
        super().__init__(timeout=None)
        for seconds in PRESETS:
            self.add_item(CountdownButton(seconds))


# ------------------------------------------------------------------- Bot本体


class ShutsujinBell(discord.Client):
    def __init__(self):
        # 特権インテントは使わない。スラッシュコマンドとボタンだけで完結する。
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.ready_once = False

    async def setup_hook(self):
        # 再起動しても既存のパネルのボタンが反応するように登録し直す
        self.add_view(CountdownPanel())

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
    await interaction.response.send_message(
        "**出陣カウントダウン**\nボイスチャンネルに入ってから押してください。",
        view=CountdownPanel(),
    )


@client.tree.command(name="stop", description="再生を止めて退出します")
@app_commands.guild_only()
async def stop_command(interaction: discord.Interaction):
    log_use(interaction, "停止")
    voice_client = interaction.guild.voice_client
    if voice_client is None:
        await respond(interaction, "いま再生していません。")
        return
    voice_client.stop()
    await voice_client.disconnect()
    await respond(interaction, "止めました。")


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
    log.info(f"起動します（記録: {LOG_FILE}）")

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
        # Ctrl+C で止めた場合はここを通る。
        # ウィンドウの×で閉じるとプロセスごと消えるので記録は残らない。
        log.info("終了しました")

    return 0


if __name__ == "__main__":
    sys.exit(main())
