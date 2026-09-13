# 出陣ベルとVOICEVOXを止める。タスクスケジューラから呼ぶ想定。
#
#   pwsh -ExecutionPolicy Bypass -File tools\stop_bot.ps1
#
# まず stop.request というメモを置いて、Botに自分で終わってもらう。
# Botはメモを見つけると、起動時に貼ったパネルを「停止しました」に書き換え、
# ログに「終了しました」を残してから終わる。
# $GraceSeconds 待っても終わらなければ、以前と同じく強制終了する。

param(
    [int]$GraceSeconds = 20
)

$root = Split-Path -Parent $PSScriptRoot
$request = Join-Path $root "stop.request"

# ログは起動した日ごとに1本（bot-YYYY-MM-DD.log）。
# 土19時起動 → 日3時停止のように日付をまたぐので、「今日の日付」で
# 組み立てると外れる。いま開かれているもの＝いちばん新しいものを狙う。
$log = Get-ChildItem (Join-Path $root "logs") -Filter "bot-*.log" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1 -ExpandProperty FullName

# 日付別にする前の版が動いている間は bot.log に書かれている
if (-not $log) {
    $legacy = Join-Path $root "logs\bot.log"
    if (Test-Path $legacy) { $log = $legacy }
}

function Write-Log($message) {
    if (-not $log) {
        Write-Host "ログファイルが見つかりませんでした"
        return
    }
    # Botがログファイルを開いたままなので、書けないこともある。失敗しても進む。
    try {
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Add-Content -Path $log -Value "[$stamp] $message" -Encoding utf8 -ErrorAction Stop
    } catch {
        Write-Host "ログに書けませんでした: $($_.Exception.Message)"
    }
}

function Test-Alive($processes) {
    @($processes | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }).Count -gt 0
}

# このプロジェクトの Bot だけを対象にする。他のPythonまで巻き添えにしないよう絞り込む。
# start_bot.bat は .venv のランチャーを起動し、ランチャーが本体のPythonを子として起動する。
# コマンドラインは相対パス（src\bot.py）なので、場所ではなく「ランチャーの実体が
# このフォルダの下にあるか」と「その子か」で見分ける。
$pythons = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'")
$launchers = @($pythons | Where-Object {
    $_.ExecutablePath -like "$root\*" -and $_.CommandLine -like "*bot.py*"
})
$launcherIds = @($launchers | ForEach-Object { $_.ProcessId })
$bots = @($launchers) + @($pythons | Where-Object {
    $launcherIds -contains $_.ParentProcessId -and $_.CommandLine -like "*bot.py*"
})

if ($bots.Count -eq 0) {
    # 絞り込みで取れないときは bot.py だけを手がかりにする
    $bots = @($pythons | Where-Object { $_.CommandLine -like "*bot.py*" })
}

if ($bots.Count -gt 0) {
    Write-Log "スケジュールにより停止します"

    # Botに自分で終わってもらう
    Set-Content -Path $request -Value (Get-Date -Format "yyyy-MM-dd HH:mm:ss") -Encoding utf8
    $deadline = (Get-Date).AddSeconds($GraceSeconds)
    while ((Test-Alive $bots) -and ((Get-Date) -lt $deadline)) {
        Start-Sleep -Milliseconds 500
    }

    if (Test-Alive $bots) {
        foreach ($p in $bots) {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        }
        Write-Log "${GraceSeconds}秒待っても終わらなかったので強制終了しました"
        Write-Host "止めました（強制終了）: 出陣ベル"
    } else {
        Write-Host "止まりました: 出陣ベル（自分で終了）"
    }

    # 使われなかったメモが残ると、次に起動した瞬間に止まってしまう
    Remove-Item -Path $request -ErrorAction SilentlyContinue
} else {
    Write-Host "出陣ベルは動いていませんでした"
}

# start_bot.bat を開いたままのウィンドウ（cmd）も閉じる
Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" |
    Where-Object { $_.CommandLine -like "*start_bot.bat*" } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "止めました: 起動用ウィンドウ (PID $($_.ProcessId))"
    }

$voicevox = Get-Process -Name "VOICEVOX" -ErrorAction SilentlyContinue
if ($voicevox) {
    $voicevox | Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Host "止めました: VOICEVOX"
} else {
    Write-Host "VOICEVOXは動いていませんでした"
}
