# 出陣ベルとVOICEVOXを止める。タスクスケジューラから呼ぶ想定。
#
#   pwsh -ExecutionPolicy Bypass -File tools\stop_bot.ps1
#
# 強制終了なので bot.py 側の finally は通らない。
# 「なぜ止まったか」が後から分かるよう、ここでログに1行足しておく。

$root = Split-Path -Parent $PSScriptRoot

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

# このプロジェクトの bot.py だけを対象にする。
# 他のPythonまで巻き添えにしないよう、コマンドラインで絞り込む。
$bots = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*bot.py*" -and $_.CommandLine -like "*$($root.Replace('\','\\'))*" }

if (-not $bots) {
    # 絞り込みで取れないときは bot.py だけを手がかりにする
    $bots = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*bot.py*" }
}

if ($bots) {
    Write-Log "スケジュールにより停止します"
    foreach ($p in $bots) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "止めました: 出陣ベル (PID $($p.ProcessId))"
    }
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
