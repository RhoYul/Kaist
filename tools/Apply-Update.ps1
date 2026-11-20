# PhysicalAI KAIST Platform — 원격 업데이트 적용 (export 서버가 업데이트를 받으면 혼자 따로 띄운다. 직접 실행할 일 없음)
#   1) 서버 정상 종료 (촬영 · NAS 저장 중이면 끄지 않고 업데이트를 미룬다)
#   2) 지금 코드를 updates\backup 에 보관  3) 새 코드로 교체  4) 서버 켜기 → 새 버전이 뜨는지 확인
#   5) 안 뜨면 보관한 코드로 되돌리고 다시 켠다.  결과: updates\last-update.json · logs\update.log
param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)][string]$Version)
$ErrorActionPreference = "Stop"
$upd = Join-Path $Root "updates"; $staging = Join-Path $upd "staging"; $backup = Join-Path $upd "backup"
$logDir = Join-Path $Root "logs"; New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir "update.log"
$statusFile = Join-Path $upd "last-update.json"
$from = ""
try { $from = (Get-Content $statusFile -Raw -Encoding UTF8 | ConvertFrom-Json).from } catch {}
$utf8 = New-Object System.Text.UTF8Encoding($false)
function Log($m) { [IO.File]::AppendAllText($log, "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [$Version] $m`r`n", $utf8) }
function State($status, $msg) {
    $o = [ordered]@{ status = $status; version = $Version; from = $from; message = $msg; at = (Get-Date).ToUniversalTime().ToString("o") }
    [IO.File]::WriteAllText($statusFile, ($o | ConvertTo-Json), $utf8); Log "$status · $msg"
}
# Server.ps1 을 창 없이 돌리고 끝날 때까지 기다린다 (출력 파이프를 물리지 않아야 서버 프로세스 때문에 멈추지 않는다)
function Run-Server($action) {
    $psi = New-Object Diagnostics.ProcessStartInfo "powershell.exe", "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $Root 'tools\Server.ps1')`" $action"
    $psi.UseShellExecute = $true; $psi.WindowStyle = "Hidden"; $psi.WorkingDirectory = $Root
    $p = [Diagnostics.Process]::Start($psi); $p.WaitForExit(); return $p.ExitCode
}
$cfgDir = if ($env:STUDIO_CONFIG) { Join-Path $Root $env:STUDIO_CONFIG } else { Join-Path $Root "production-config" }
$ports = @(foreach ($r in "export", "capture") { $f = Join-Path $cfgDir "$r.json"; if (Test-Path $f) { [int](Get-Content $f -Raw -Encoding UTF8 | ConvertFrom-Json).port } })
function Running-Version-Ok {
    foreach ($port in $ports) {
        try { $i = Invoke-RestMethod "http://127.0.0.1:$port/api/info" -TimeoutSec 5; if ($i.version -ne $Version) { return $false } } catch { return $false }
    }
    return $true
}
# 서버가 시작하다 남긴 마지막 오류 줄 (되돌릴 때 관제 화면에 이유로 보여 준다)
function Start-Error {
    $found = @()
    foreach ($r in "export", "capture") {
        $f = Join-Path $logDir "$r.err.log"
        if (Test-Path $f) {
            $line = Get-Content $f -Encoding UTF8 -Tail 40 | Where-Object { $_ -match 'Error|Exception|오류' } | Select-Object -Last 1
            if ($line) { $found += "$r`: $($line.Trim())" }
        }
    }
    if ($found) { return " · 원인: " + ($found -join " / ") } else { return "" }
}
function Read-List($name) { @(Get-Content (Join-Path $staging $name) -Encoding UTF8 | Where-Object { $_ }) }
function Copy-Rel($fromDir, $toDir, $rel) {
    $src = Join-Path $fromDir ($rel -replace '/', '\'); $dst = Join-Path $toDir ($rel -replace '/', '\')
    New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null; Copy-Item -LiteralPath $src -Destination $dst -Force
}

function Restore-Backup {
    foreach ($rel in $new) { if ($cur -notcontains $rel) { Remove-Item -LiteralPath (Join-Path $Root $rel) -Force -ErrorAction SilentlyContinue } }
    foreach ($rel in $cur) { if (Test-Path -LiteralPath (Join-Path $backup $rel)) { Copy-Rel $backup $Root $rel } }
    Get-ChildItem (Join-Path $Root "studio") -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}
$touched = $false; $new = @(); $cur = @()
try {
    Start-Sleep 2   # 업데이트를 보낸 요청에 응답이 먼저 나가게
    State "stopping" "서버를 정상 종료하는 중"
    if ((Run-Server "stop") -ne 0) {
        Run-Server "start" | Out-Null
        State "refused" "촬영 · QC · NAS 저장 중이라 서버를 끌 수 없어 업데이트하지 않았습니다. 끝난 뒤 다시 누르세요"
        exit 1
    }
    $new = Read-List "new-files.txt"; $cur = Read-List "current-files.txt"
    State "applying" "코드 교체 중 (파일 $($new.Count)개 · 이전 코드는 updates\backup 에 보관)"
    if (Test-Path $backup) { Remove-Item $backup -Recurse -Force }
    foreach ($rel in $cur) { if (Test-Path -LiteralPath (Join-Path $Root $rel)) { Copy-Rel $Root $backup $rel } }
    [IO.File]::WriteAllText((Join-Path $backup "files.txt"), (($cur -join "`n") + "`n"), $utf8)
    $touched = $true
    foreach ($rel in $new) { Copy-Rel (Join-Path $staging "files") $Root $rel }
    foreach ($rel in $cur) { if ($new -notcontains $rel) { Remove-Item -LiteralPath (Join-Path $Root $rel) -Force -ErrorAction SilentlyContinue } }
    Get-ChildItem (Join-Path $Root "studio") -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    State "starting" "새 코드로 서버를 켜는 중"
    $code = Run-Server "start"
    if ($code -eq 0 -and (Running-Version-Ok)) { State "ok" "업데이트 완료 · 서버 재시작됨"; exit 0 }
    # ── 되돌리기 ──
    $why = Start-Error
    State "rolling_back" "새 코드로 서버가 정상적으로 켜지지 않아 이전 코드로 되돌리는 중$why"
    Run-Server "stop" | Out-Null
    Restore-Backup
    $code = Run-Server "start"
    State "rolled_back" "새 코드로 서버가 켜지지 않아 이전 코드로 되돌렸습니다$why$(if ($code -ne 0) { ' · 이전 코드로도 켜지지 않았습니다. 촬영 PC 에서 Start-PhysicalAI_KAIST.cmd 를 실행하세요' })"
    exit 1
} catch {
    $msg = $_.Exception.Message; $restored = ""
    if ($touched) { try { Restore-Backup; $restored = " · 이전 코드로 되돌렸습니다" } catch { $restored = " · 되돌리기도 실패 — updates\backup 을 확인하세요" } }
    try { Run-Server "start" | Out-Null } catch {}
    State "failed" "업데이트 중 오류: $msg$restored"
    exit 1
}
