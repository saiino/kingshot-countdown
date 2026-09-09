"""キングショット出陣カウントダウン（音声版 / Phase 3）

VOICEVOX で「45」「44」…「0」の音声を作り、無音を挟んで
正確に1秒間隔で並べた wav を1本焼く。本番はそれを再生するだけ。

  → カウント中に合成しないので、再生中にズレようがない。

前提: VOICEVOX（またはVOICEVOX ENGINE）をローカルで起動しておくこと。
      起動していると http://127.0.0.1:50021 で HTTP API が待ち受ける。

    python voice_countdown.py --list-speakers   # 話者一覧を見る
    python voice_countdown.py 45 0              # 音声を作る（キャッシュされる）
    python voice_countdown.py 45 0 --play       # 作って再生（2回目以降は再生だけ）
    python voice_countdown.py 60 30 --speaker 3 --speed 1.2 --lead 3 --cue よーい
"""

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import wave

from countdown import build_schedule

DEFAULT_HOST = "http://127.0.0.1:50021"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice")

# 数字をそのまま渡すと読みが不自然になるものだけ、ひらがなで上書きする。
READINGS = {0: "ゼロ"}

# 最後の数字が鳴り終わるまでの余白（秒）
TAIL_SECONDS = 1.0


# ---------------------------------------------------------------- VOICEVOX API


class VoicevoxError(RuntimeError):
    """VOICEVOX と話せなかったときのエラー。"""


def _request(url, payload=None, timeout=30):
    """VOICEVOX に POST してレスポンスの生バイト列を返す。

    requests を使うほうが短く書けるが、標準ライブラリだけで完結させたいので
    urllib.request を使っている。
    """
    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise VoicevoxError(f"APIがエラーを返しました ({exc.code}): {url}") from exc
    except urllib.error.URLError as exc:
        raise VoicevoxError(
            f"VOICEVOXに接続できません ({url})\n"
            f"  理由: {exc.reason}\n"
            f"  VOICEVOXを起動してから、もう一度実行してください。"
        ) from exc


def fetch_speakers(host):
    """話者の一覧を取得する（こちらは GET）。"""
    try:
        with urllib.request.urlopen(f"{host}/speakers", timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.URLError as exc:
        raise VoicevoxError(f"VOICEVOXに接続できません: {exc.reason}") from exc


def synthesize(host, text, speaker, speed):
    """1つのテキストを合成して wav のバイト列を返す。

    VOICEVOX の合成は2段階に分かれている:
      1. /audio_query  … テキストから「読み・抑揚・速度」の設計図(JSON)を作る
      2. /synthesis    … その設計図を渡して実際の音声を作る
    間で設計図をいじれるのがこの分け方の利点。
    """
    # 1段階目: パラメータはボディではなくクエリ文字列で渡す
    query_url = f"{host}/audio_query?" + urllib.parse.urlencode(
        {"text": text, "speaker": speaker}
    )
    query = json.loads(_request(query_url))

    # 設計図を調整する。前後の無音を0にしておかないと、
    # 「音が鳴り始める瞬間」が1秒の目盛りからずれてしまう。
    query["speedScale"] = speed
    query["prePhonemeLength"] = 0.0
    query["postPhonemeLength"] = 0.0
    query["outputStereo"] = False

    # 2段階目: 設計図をJSONボディとして送ると wav が返ってくる
    synthesis_url = f"{host}/synthesis?" + urllib.parse.urlencode({"speaker": speaker})
    return _request(synthesis_url, payload=query)


# ------------------------------------------------------------------ wav の加工


def load_clip(wav_bytes):
    """wav のバイト列から (パラメータ, 生の波形バイト列) を取り出す。

    io.BytesIO を挟むと、ファイルに書かずにメモリ上のバイト列を
    ファイルのように扱える。
    """
    with wave.open(io.BytesIO(wav_bytes), "rb") as reader:
        params = reader.getparams()
        frames = reader.readframes(reader.getnframes())
    return params, frames


def build_track(placements, params, total_seconds):
    """(開始秒, 波形) のリストを、指定した長さの1本の波形に並べる。

    無音を「挟む」のではなく、先に全体ぶんの無音を用意しておき、
    そこへ各クリップを所定の位置に上書きしていく。
    こうすると位置の計算が毎回独立するので、誤差が積み上がらない。
    16bit PCM では値0が無音なので、ゼロ埋めのバッファがそのまま無音になる。
    """
    bytes_per_frame = params.nchannels * params.sampwidth
    total_frames = int(round(total_seconds * params.framerate))
    track = bytearray(total_frames * bytes_per_frame)

    for offset_seconds, frames in placements:
        start = int(round(offset_seconds * params.framerate)) * bytes_per_frame
        if start < 0:
            frames = frames[-start:]
            start = 0
        end = min(start + len(frames), len(track))
        if end > start:
            track[start:end] = frames[: end - start]

    return bytes(track)


def write_wav(path, params, frames):
    with wave.open(path, "wb") as writer:
        writer.setnchannels(params.nchannels)
        writer.setsampwidth(params.sampwidth)
        writer.setframerate(params.framerate)
        writer.writeframes(frames)


def clip_seconds(params, frames):
    return len(frames) / (params.nchannels * params.sampwidth * params.framerate)


# -------------------------------------------------------------------- 組み立て


def build_countdown_wav(path, args):
    """カウントダウン音声を合成してファイルに書き出す。"""
    schedule = build_schedule(args.start, args.end, args.step, args.dense)

    # 同じ数字を何度も合成しないよう、数字ごとに1回だけ作って使い回す。
    clips = {}
    params = None
    for _, remaining in schedule:
        if remaining in clips:
            continue
        text = READINGS.get(remaining, str(remaining))
        print(f"  合成中: {text}", end="\r", flush=True)
        clip_params, frames = load_clip(
            synthesize(args.host, text, args.speaker, args.speed)
        )
        if params is None:
            params = clip_params
        elif (clip_params.framerate, clip_params.nchannels, clip_params.sampwidth) != (
            params.framerate,
            params.nchannels,
            params.sampwidth,
        ):
            raise VoicevoxError("音声のフォーマットが途中で変わりました")
        clips[remaining] = frames
    print(" " * 30, end="\r")

    lead = float(args.lead)
    placements = []

    # 開始の合図。数字の1つ目にぶつからないよう、その直前で鳴り終わるように置く。
    if args.cue:
        cue_params, cue_frames = load_clip(
            synthesize(args.host, args.cue, args.speaker, args.speed)
        )
        cue_length = clip_seconds(cue_params, cue_frames)
        if lead < cue_length + 0.3:
            lead = cue_length + 0.3
            print(f"  合図が入るよう --lead を {lead:.1f} 秒に伸ばしました")
        placements.append((lead - cue_length - 0.3, cue_frames))

    longest = 0.0
    for elapsed, remaining in schedule:
        frames = clips[remaining]
        longest = max(longest, clip_seconds(params, frames))
        placements.append((lead + elapsed, frames))

    if longest > 1.0:
        print(
            f"  注意: 一番長い読み上げが {longest:.2f} 秒あり、次の数字に食い込みます。"
            f" --speed を上げてください。"
        )

    total = lead + schedule[-1][0] + TAIL_SECONDS
    write_wav(path, params, build_track(placements, params, total))
    return total


def cache_path(args):
    """設定が同じなら同じファイル名になるようにして、作り直しを避ける。"""
    name = f"countdown_{args.start}-{args.end}_sp{args.speaker}_x{args.speed}"
    if args.step != 1:
        name += f"_step{args.step}"
    if args.dense:
        name += f"_dense{args.dense}"
    if args.lead:
        name += f"_lead{args.lead}"
    if args.cue:
        name += f"_cue{args.cue}"
    return os.path.join(CACHE_DIR, name + ".wav")


# ---------------------------------------------------------------------- 再生


def play(path):
    """OSごとに用意されている再生手段を使う。"""
    if sys.platform == "win32":
        import winsound

        # SND_FILENAME: パスを渡す / 既定は再生し終わるまで戻ってこない
        winsound.PlaySound(path, winsound.SND_FILENAME)
        return

    for player in ("afplay", "aplay", "paplay"):
        if shutil.which(player):
            subprocess.run([player, path], check=False)
            return

    print(f"再生コマンドが見つかりません。手動で再生してください: {path}")


# ----------------------------------------------------------------------- CLI


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="VOICEVOXでカウントダウン音声を作って再生する",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("start", type=int, nargs="?", default=45, help="開始する残り秒数")
    parser.add_argument("end", type=int, nargs="?", default=0, help="終了する残り秒数")
    parser.add_argument("--step", type=int, default=1, help="読み上げる間隔（秒）")
    parser.add_argument("--dense", type=int, default=0, help="残りこの秒数以下は毎秒読む")
    parser.add_argument("--speaker", type=int, default=3, help="VOICEVOXの話者ID")
    parser.add_argument("--speed", type=float, default=1.0, help="話す速さ")
    parser.add_argument("--lead", type=float, default=0.0, help="開始前に入れる無音（秒）")
    parser.add_argument("--cue", default="", help="カウント開始前に読む合図（例: よーい）")
    parser.add_argument("--host", default=DEFAULT_HOST, help="VOICEVOXのURL")
    parser.add_argument("--play", action="store_true", help="作ったあと再生する")
    parser.add_argument("--force", action="store_true", help="キャッシュを無視して作り直す")
    parser.add_argument("--list-speakers", action="store_true", help="話者一覧を表示して終了")

    args = parser.parse_args(argv)

    if not args.list_speakers:
        if args.start <= args.end:
            parser.error(f"開始({args.start})は終了({args.end})より大きくしてください")
        if args.end < 0:
            parser.error("終了秒に負の数は指定できません")
        if args.step < 1:
            parser.error("--step は1以上にしてください")
        if args.speed <= 0:
            parser.error("--speed は正の数にしてください")

    return args


def main():
    args = parse_args()

    try:
        if args.list_speakers:
            for speaker in fetch_speakers(args.host):
                for style in speaker["styles"]:
                    print(f"{style['id']:>4}  {speaker['name']} ({style['name']})")
            return 0

        os.makedirs(CACHE_DIR, exist_ok=True)
        path = cache_path(args)

        if os.path.exists(path) and not args.force:
            print(f"キャッシュを使います: {path}")
        else:
            print(f"{args.start} -> {args.end} の音声を作ります（VOICEVOX: {args.host}）")
            total = build_countdown_wav(path, args)
            size = os.path.getsize(path) / 1024
            print(f"できました: {path}")
            print(f"  長さ {total:.1f} 秒 / {size:.0f} KB")

        if args.play:
            print("再生します（Ctrl+C で中断）")
            play(path)

    except VoicevoxError as exc:
        print(f"\nエラー: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n中断しました")

    return 0


if __name__ == "__main__":
    sys.exit(main())
