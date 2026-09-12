# キングショット出陣カウントダウン

複数人で同時着弾させるための共通カウントダウン。

> - 同盟メンバー向けの使い方 … **[USAGE.md](USAGE.md)**
> - 実測値や比較の記録 … **[NOTES.md](NOTES.md)**
>
> このREADMEには、どういう考えで作ったかと、動かし方・開発の手順を書く。

行軍時間は人によって違う（Aさん30秒、Bさん31秒、Cさん38秒…）ので、
**全員に同じカウントを流し、各自が自分の秒数のところで出陣する**。
カウント側は誰が何秒かを知る必要がない。45から0まで正確に読むだけでよい。

**いまの本命は Phase 4 の Discord Bot。** ボイスチャンネルへ直接流すので、
画面共有も、誰かが通話に居続けることも要らない。

Phase 1〜3 はそこへ至る段階で、いまも単体で動く。
テキスト表示だけで済ませたいときや、音声ファイルを作り直すときに使う。

## ファイル

| ファイル | 中身 |
|---|---|
| `bot.py` | **Phase 4。Discord Bot 本体。いまの本命** |
| `start_bot.bat` | Botの起動用。ダブルクリックで動く |
| `voice_countdown.py` | Phase 3。VOICEVOXで音声を焼く。Botもこれを呼ぶ |
| `countdown.py` | Phase 1+2。テキスト表示のカウントダウン |
| `countdown_argv.py` | 学習用。同じことを `sys.argv` だけで書いた版 |
| `tests/` | テスト一式 |
| `tools/` | アイコン・バナーの生成、停止スクリプト |
| `voice/` | 焼いた音声（生成物。gitには入れない） |
| `logs/` | 動作の記録（生成物。gitには入れない） |

**`bot.py` 以外は標準ライブラリだけで動く。** Python 3.8以上。
Botだけが discord.py を要る（[準備](#準備-1)を参照）。

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
python voice_countdown.py 45 0 --speaker 3 --speed 1.2 --play
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

ずんだもんは `--speed 1.2` で全数字が1秒の目盛りに収まる。
他の話者に変えたときは、生成時に警告が出るかどうかで判断する。
（実測値は [NOTES.md](NOTES.md)）

## Phase 4：Discord Bot（出陣ベル）

カウントダウンをボイスチャンネルで直接流す。画面共有が要らなくなる。

### 使い方

| コマンド | 用途 |
|---|---|
| `/panel` | ボタンを設置する。**一度打てば以降は押すだけ** |
| `/countdown 開始 終了` | その場で秒数を指定して流す |
| `/stop` | 再生を止めて退出する |

ボイスチャンネルに入ってから使う。サーバーの誰が押してもよい。

### 準備

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install -r requirements.txt
```

`.env.example` をコピーして `.env` を作り、`DISCORD_TOKEN` を入れる。
`.env` は `.gitignore` で除外してあるのでコミットされない。

#### requirements.txt と requirements.lock

役割が違うので2本ある。**どちらもコミットする。**

| ファイル | 中身 | 誰が書く |
|---|---|---|
| `requirements.txt` | **宣言。** 自分で直接選んだものだけ | 人 |
| `requirements.lock` | **固定。** 依存を全部たどって `==` で止めたもの | pip-compile |

普段は `requirements.txt` を入れれば足りる。バージョンを完全に揃えたいときだけ
`requirements.lock` を使う。

```bash
.venv/Scripts/python -m pip install -r requirements.lock
```

`requirements.txt` を変えたら、ロックを作り直す。

```bash
.venv/Scripts/python -m piptools compile requirements.txt --output-file requirements.lock
```

`requirements.lock` は生成物なので手で編集しない。宣言側を直して作り直す。
**`pip freeze` の出力をそのまま requirements.txt にしないこと**
（理由と実測は [NOTES.md](NOTES.md)）。

### 起動

**`start_bot.bat` をダブルクリック**するのが手軽。コマンドからなら:

```bash
.venv/Scripts/python bot.py
```

起動時に、ボタンぶんの音声を先に焼いておくので押した瞬間に鳴る。
止めるときは Ctrl+C か、ウィンドウを閉じる。

### Botを別のサーバーに追加する

同盟のサーバーなどに入れるときの手順。**毎回忘れるのでここに残しておく。**

招待リンクは使い回せるので、一度作ったらメモしておいてもよい。

1. [Discord Developer Portal](https://discord.com/developers/applications) で
   **出陣ベル** のアプリを開く
2. 左メニューの **OAuth2** → 下にスクロールして **OAuth2 URL Generator**
3. **SCOPES** にチェック（この2つだけ）
   - `bot`
   - `applications.commands`
4. `bot` にチェックすると下に **BOT PERMISSIONS** が出るので、2つだけ選ぶ
   - **Connect**（ボイスチャンネルに入る）
   - **Speak**（喋る）
   - Administrator は絶対に付けない。この2つで足りる
5. 一番下の **GENERATED URL** をコピー
6. そのURLをブラウザで開く → サーバーを選んで「認証」

#### 先に確認すること（ここで詰まる）

**追加先のサーバーで「サーバー管理」権限を持っていないと追加できない。**
Administrator 全部は要らないが、この権限は必須。自分のサーバーなら問題ないが、
**他人が立てた同盟サーバーでは持っていないことが多い。**

権限がない場合は、次のどちらかになる。

**A. 同盟の管理者に「サーバー管理」権限をもらってから、自分で追加する**

「公開Bot」はOFFのままでよく、招待URLが漏れても他人には使えない。安全側。

**この権限が要るのは追加する瞬間だけ。** Botは一度入れば、権限を外しても
居続ける。なので「一時的に付けて、終わったら戻してください」で頼める。

頼むときに伝えると通りやすいこと:

- Administrator は不要。**「サーバー管理」だけ**で足りる
- 追加後に権限を戻してよい
- Botに与えるのは **Connect と Speak の2つだけ**。特権インテントを
  使っていないので、**メッセージもメンバー情報も読めない**
  （管理者がいちばん気にするのはここ）

**B. 「公開Bot」をONに戻し、同盟の管理者にURLを渡して追加してもらう**

権限をもらわずに済む。ただし**URLを持っている人なら誰でも自分のサーバーに
追加できる**ようになる。実害は「知らないサーバーで使われる」程度で、
しかもこちらのPCでBotを動かしている間だけだが、URLの配り先には気をつける。
入られたサーバーは起動ログの「サーバー:」行に出るので確認できる。

どちらでも動く。同盟の運営と相談しやすいほうを選ぶ。

#### そのほかの注意

- **追加できるのは自分（アプリの所有者）だけ。** 「公開Bot」をOFFにしてあるため。
  他人がURLを踏んでも追加できない（Bを選ぶとこれが変わる）
- **Botを動かしたまま追加してよい。** `on_guild_join` で参加時に
  コマンドを配るので、追加した瞬間からスラッシュコマンドが使える
- 追加したら、そのサーバーで一度だけ `/panel` を打ってボタンを置く

#### ハマったところ

**「公開Bot」をOFFにすると認証エラーが出ることがある。**

> プライベートアプリにはデフォルトの認証リンクを設定できません

先に **インストール**タブ → 「デフォルトの認証リンク」を **「設定しない」**
に変えてから、Bot タブで公開BotをOFFにする。順番が逆だと弾かれる。
デフォルトの認証リンクを消したぶん、招待URLは上の OAuth2 URL Generator で作る。

### 毎週の自動起動（タスクスケジューラ）

Windowsのタスクスケジューラに3つ登録してある。手で起動しなくても、
**土曜19時に立ち上がって、日曜3時に落ちる。**

| タスク | いつ | 何を |
|---|---|---|
| `\出陣ベル\1-Bot起動` | 土 19:00 | `start_bot.bat` |
| `\出陣ベル\2-VOICEVOX起動` | 土 19:00 | VOICEVOX本体 |
| `\出陣ベル\3-停止` | 日 03:00 | `tools/stop_bot.ps1` で両方を止める |

**スリープからの復帰を有効にしてある**ので、放置してスリープに入っていても
時刻になれば起きて動く。ただし**電源が切れていると動かない**。

時刻を変えたり止めたりするのは、タスクスケジューラのGUI（`taskschd.msc` →
タスク スケジューラ ライブラリ → 出陣ベル）か、PowerShellから。

```powershell
Get-ScheduledTask -TaskPath "\出陣ベル\" | Get-ScheduledTaskInfo
```

```powershell
Unregister-ScheduledTask -TaskPath "\出陣ベル\" -TaskName "2-VOICEVOX起動" -Confirm:$false
```

VOICEVOXは**焼いていない秒数を使うときの保険**でしかない。不要なら
上のコマンドでそのタスクだけ消してよい。

停止は強制終了なので `bot.py` の `finally` は通らない。代わりに
`stop_bot.ps1` がログへ「スケジュールにより停止します」と書き足す。

### 運用の覚え書き

忘れやすいことをまとめておく。

| やりたいこと | やること |
|---|---|
| Botを動かす | `start_bot.bat` をダブルクリック。**動かしている間だけ使える** |
| 新しい秒数を用意する | VOICEVOXを起動して `voice_countdown.py`（`--discord` 付き） |
| ボタンの秒数を変える | `.env` の `COUNTDOWN_PRESETS` を書き換えてBot再起動。**1行5個・最大25個**で、6個以上は自動で折り返す。増やしたら音声を先に焼いておく |
| 話者や速さを変える | `.env` の `VOICEVOX_SPEAKER` / `VOICEVOX_SPEED`。**クレジット表記も直す** |
| 合図や前置きを変える | `bot.py` 冒頭の `LEAD_SECONDS` / `CUE_TEXT`。音声の焼き直しが要る |
| コマンドの定義を変えた | **Bot再起動が必要。** 再起動しないと古い定義のまま |
| メンバー向けマニュアルを更新した | **共有メニューで「Shared version」を最新に選び直す。** 公開中は Latest を選べず、バージョンが固定される |

- **誰が何を使ったかは `logs/bot.log` に残る。** 画面にも同じものが出る。
  「さっき誰か押したけど動いたのか」はここで確認できる。
  Discord側の応答は本人にしか見えないため、記録はここだけ
  - discord.py の接続・切断イベントも同じファイルに入るので、
    「あのとき落ちていたのか」も追える
  - 1MBを超えたら世代交代し、3世代まで残る。放っておいても太らない
  - `logs/` は `.gitignore` 済み。手元にだけ残る
- **止めるときは Ctrl+C のほうがよい。** 「終了しました」が記録される。
  ウィンドウの×で閉じるとプロセスごと消えるので、終了の記録は残らない
- **PCがスリープするとBotは止まる。** 画面オフは問題ない。復帰すれば自動で再接続する
- **焼き済みの秒数を流すだけならVOICEVOXは不要。** 新しい秒数を作るときだけ起動する
- トークンは `.env`。**公開リポジトリなので絶対にコミットしない**（`.gitignore` 済み）

**Botを動かしている間だけ使える。** ゲームをやる前に起動しておく。

### VOICEVOXはいつ必要か

**焼き済みの秒数を流すだけならVOICEVOXは要らない。** 起動しておくのは
新しい秒数を初めて使うときだけ。未生成の秒数を指定すると、その場で
合成しようとして「VOICEVOXを起動してください」と返る。

よく使う秒数は先に焼いておくとよい。

```bash
python voice_countdown.py 50 0 --speaker 3 --speed 1.2 --lead 3 --cue よーい --discord
```

### 既知の制約

- **1つのサーバー内では同時に1本だけ。** ボイス接続が1つしか張れないため。
  別サーバーどうしなら同時に流せる。
- 設定（話者・速さ・ボタン）は全サーバー共通。
- 同じアカウントは同時に1つのVCにしか入れないので、遅延の確認は
  複数人でないとできない（Discordの仕様）。

### なぜ FFmpeg が要らないか

Discordのボイスは **48kHz・ステレオ・16bit のPCMしか受け取らない**。
普通は音声ファイルをFFmpegで変換して流すが、`--discord` を付けると
**VOICEVOXに最初からその形式で出させる**ので変換処理が丸ごと不要になる。

```bash
python voice_countdown.py 45 0 --discord
```

Python 3.13で標準ライブラリの `audioop`（音声変換）が削除されているため、
自前リサンプリングを避けられるのは効いてくる。

discord.py が同梱するOpusエンコーダは 48000Hz / 2ch / 3840バイトフレームを
要求するが、生成した wav はそのまま割り切れる。

### 画面共有との違い

聞こえるタイミングのばらつき自体は変わらない（どちらもDiscordの音声網を通る）。
ただし画面共有だと**共有者だけが自分のPCから直接聞くので遅延ゼロ**になり、
他の人だけが遅れる。Botなら全員が同じ経路になるので、この非対称が消える。

### 設定

`.env` で変えられる。

| キー | 既定 | 意味 |
|---|---|---|
| `DISCORD_TOKEN` | （必須） | Botのトークン |
| `VOICEVOX_SPEAKER` | `3` | 話者ID |
| `VOICEVOX_SPEED` | `1.2` | 話す速さ |
| `COUNTDOWN_PRESETS` | `45,60,30` | パネルのボタン（最大25個） |
| `VOICEVOX_HOST` | `http://127.0.0.1:50021` | VOICEVOXのURL |

前置きの無音と合図は `bot.py` 冒頭の `LEAD_SECONDS` / `CUE_TEXT` で変える。

## テスト

```bash
.venv/Scripts/python -m unittest discover -s tests -t .
```

60件。全部で1秒かからない。

| ファイル | 見ているもの | モック |
|---|---|---|
| `tests/test_countdown.py` | スケジュールとズレ補正 | 時計 |
| `tests/test_voice_countdown.py` | VOICEVOXへの要求と波形の組み立て | `urlopen` |
| `tests/test_bot.py` | コマンドの分岐と再生の流れ | interaction / ボイス接続 |

`test_bot.py` だけ discord.py が要る。残り39件は標準ライブラリだけで動く。

```bash
python -m unittest tests.test_countdown tests.test_voice_countdown
```

## Developer Portal の直リンク

アプリID `1547220441662496828`。毎回一覧から探すのが面倒なので控えておく。

| 用途 | URL |
|---|---|
| アイコン・バナー・概要 | https://discord.com/developers/applications/1547220441662496828/information |
| トークン・公開Bot設定 | https://discord.com/developers/applications/1547220441662496828/bot |
| 招待リンク作成 | https://discord.com/developers/applications/1547220441662496828/oauth2 |
| インストール設定 | https://discord.com/developers/applications/1547220441662496828/installation |

## アイコンとバナー

Developer Portal の General Information からアップロードする。

| ファイル | サイズ | 中身 |
|---|---|---|
| `assets/icon.png` | 1024x1024 | カウントダウンのリングの中にベル |
| `assets/banner.png` | 680x240 | 45→0 の音声の波形 |

どちらも Pillow で描いている。Bot の実行には要らないので
`requirements.txt` には入れていない。

```bash
.venv/Scripts/python -m pip install pillow
```

```bash
.venv/Scripts/python tools/make_icon.py assets/icon.png
```

```bash
.venv/Scripts/python tools/make_banner.py voice/countdown_45-0_sp3_x1.2.wav assets/banner.png
```

**アイコン** — Discordは円形に切り抜き、メンバー一覧では40px前後で表示するので、
細部を入れずに太い形と高いコントラストで作ってある。24pxまで縮めても輪郭が保てる。

**バナー** — 模様は作り物ではなく、実際に生成した 45→0 の音声そのものの波形。
1秒間隔で46回読み上げているので等間隔のパルス列になる。
Discordはバナーの左下にアイコンを重ねるため、文字はすべて上側に置いている。
別の秒数の音声を渡せば、その波形で作り直せる。

## クレジット

音声: **VOICEVOX:ずんだもん**

VOICEVOXの音源はクレジット表記が必須（商用・非商用を問わない）。
話者を変えたときは、その話者名に書き換えること。
表記の書式と条件は各キャラクターの利用規約に従う。

- [VOICEVOX](https://voicevox.hiroshiba.jp/)
- [東北ずん子・ずんだもん 音源利用規約](https://zunko.jp/con_ongen_kiyaku.html)
