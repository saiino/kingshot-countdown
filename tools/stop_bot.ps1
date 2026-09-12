# 出陣ベルとVOICEVOXを止める。タスクスケジューラから呼ぶ想定。
#
#   pwsh -ExecutionPolicy Bypass -File tools\stop_bot.ps1
#
# 強制終了なので bot.py 側の finally は通らない。
# 「なぜ止まったか」が後から分かるよう、ここでログに1行足しておく。

$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root "logs\bot.log"

function Write-Log($message) {
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
