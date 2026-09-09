# キングショット出陣カウントダウン

複数人で同時着弾させるための共通カウントダウン。

行軍時間は人によって違う（Aさん30秒、Bさん31秒、Cさん38秒…）ので、
**全員に同じカウントを流し、各自が自分の秒数のところで出陣する**。
カウント側は誰が何秒かを知る必要がない。45から0まで正確に読むだけでよい。

Discordの通話に入った状態でこれをPCで動かし、画面共有（音声共有）で全員に届ける。
Discord botは不要。

## ファイル

| ファイル | 中身 |
|---|---|
| `countdown.py` | Phase 1+2。テキスト表示のカウントダウン |
| `countdown_argv.py` | 学習用。同じことを `sys.argv` だけで書いた版 |
| `voice_countdown.py` | Phase 3。VOICEVOXで音声を焼いて再生 |

依存ライブラリなし（標準ライブラリのみ）。Python 3.8以上。

## Phase 1 / 2：テキスト版

```bash
python countdown.py
```

```bash
python countdown.py 60 30
```

| オプション | 意味 |
|---|---|
| `start` `end` | 開始・終了の残り秒数（既定 45 → 0） |
| `--step N` | N秒刻みで読む |
| `--dense N` | 残りN秒以下だけは毎秒読む |
| `--naive` | ズレ補正なしで動かす（比較用） |

例：60秒から5秒刻み、残り10秒からは毎秒。

```bash
python countdown.py 60 0 --step 5 --dense 10
```

### ズレ補正について

`time.sleep(1)` を45回繰り返すと、表示処理の時間とsleep自体の誤差が
毎回上乗せされ、じわじわ遅れていく。

このスクリプトは開始時刻を `time.monotonic()` で記録し、
**「開始からn秒の絶対位置」で待つ**（`sleep_until()`）。
途中で少し遅れても次のtickで取り戻すので、誤差が蓄積しない。

`time.time()` ではなく `time.monotonic()` を使うのは、
システム時刻の変更やNTP同期の影響を受けないため。

`--naive` を付けると補正なしの実装で動くので、末尾に出る誤差を見比べられる。
1秒刻みだと差が小さいので、早回しで試すと差がはっきりする。

```bash
python -c "import countdown; countdown.TICK=0.02; countdown.run(countdown.build_schedule(200,0), naive=True)"
```

## Phase 3：音声版

事前に1本のwavに焼いておき、本番はそれを再生するだけ。
カウント中に合成しないので、再生中にズレようがない。

### 準備

VOICEVOX（またはVOICEVOX ENGINE）を起動しておく。
起動していると `http://127.0.0.1:50021` でHTTP APIが待ち受ける。

```bash
python voice_countdown.py --list-speakers
```

話者IDの一覧が出れば疎通OK。

### 使い方

```bash
python voice_countdown.py 45 0 --play
```

1回目は音声を作って `voice/` に保存し、2回目以降はキャッシュを再生するだけ。
場所ごとに秒数が違うなら、事前に必要なぶんを作っておく。

```bash
python voice_countdown.py 45 0
```

```bash
python voice_countdown.py 60 30
```

| オプション | 意味 |
|---|---|
| `--speaker N` | 話者ID（既定 3） |
| `--speed N` | 話す速さ。読みが1秒に収まらないときは上げる |
| `--lead N` | 再生開始からカウント開始までの無音（秒） |
| `--cue よーい` | カウント開始前に読む合図 |
| `--play` | 作ったあと再生する |
| `--force` | キャッシュを無視して作り直す |
| `--host URL` | VOICEVOXのURL |

```bash
python voice_countdown.py 45 0 --lead 3 --cue よーい --speed 1.2 --play
```

### 仕組み

1. VOICEVOXに「45」「44」…を1つずつリクエストして音声を得る
   - `/audio_query` で読み・抑揚の設計図(JSON)を作る
   - 設計図の `prePhonemeLength` / `postPhonemeLength` を0にする
     （前後の無音を消さないと、鳴り始めが目盛りからずれる）
   - `/synthesis` に設計図を渡して音声を得る
2. 先に全体ぶんのゼロ埋めバッファ（＝無音）を用意し、
   各音声を `経過秒 × サンプリングレート` の位置に上書きしていく
   - 「無音を挟む」のではなく「絶対位置に置く」ので誤差が積み上がらない
   - Phase 1 の `sleep_until()` と同じ考え方
3. `wave` モジュールで1本のwavとして書き出す

`pydub` を使えば連結はもっと短く書けるが、`wave` なら外部依存なしで完結する。

### 確認できていること

- 45→0 のwavは46回とも1秒の目盛りちょうどから鳴り始める（誤差0サンプル）
- ファイルの長さは 45秒 + 末尾余白1秒
- 読みが1秒に収まらない場合は警告が出る（`--speed` を上げる）

## 運用メモ

- Phase 1だけでも、画面を見ながらの運用なら実用になる
- Phase 3まで作れば、画面を見ずに耳だけで出陣できる
- Discord経由だと音が遅れる可能性があるので、本番前に一度
  相手にどう聞こえるか確認しておくとよい
