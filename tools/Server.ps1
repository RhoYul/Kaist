# PhysicalAI KAIST Platform 서버 관리 — 창 없이 백그라운드로 켜고, 카메라를 정상적으로 닫으며 끈다.
#   powershell -ExecutionPolicy Bypass -File tools\Server.ps1 start|stop|restart|status [-Role all|capture|export] [-ConfigDir production-config]
# - start  : 꺼져 있으면 창 없이 켠다 (export → capture 순서). 로그는 logs\<역할>.out.log · .err.log
# - stop   : 서버에 '정상 종료'를 요청 → 카메라 스트림을 닫고 스스로 끝난다. 15초 안에 안 꺼질 때만 강제 종료
#            촬영 · QC · 전송 · NAS 저장 중이면 끄지 않고 멈춘다 (데이터 보호)
# - restart: stop 다음 start (카메라가 잘 안 나올 때)
# - status : 켜져 있는지 · 카메라 연결 수
param(
    [ValidateSet("start", "stop", "restart", "status")][string]$Action = "status",
    [ValidateSet("all", "capture", "export", "control")][string]$Role = "all",
    [string]$ConfigDir = ""
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $ConfigDir) { $ConfigDir = if ($env:STUDIO_CONFIG) { Join-Path $root $env:STUDIO_CONFIG } else { Join-Path $root "production-config" } }
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force $logs | Out-Null
$names = @{ capture = "촬영 PC 서버"; export = "export 서버"; control = "관제 서버" }
$controlDir = if ($env:STUDIO_CONTROL_CONFIG) { Join-Path $root $env:STUDIO_CONTROL_CONFIG } else { Join-Path $root "control-config" }
function Cfg-Path($r) { if ($r -eq "control") { Join-Path $controlDir "control.json" } else { Join-Path $ConfigDir "$r.json" } }
function Token-Of($r, $cfg) { if ($r -eq "control") { $cfg.admin_password } else { $cfg.transfer_token } }

function Find-Python {
    $bundled = Join-Path $root "python\python.exe"   # 설치 패키지에 같이 들어 있는 Python (가장 먼저)
    if (Test-Path $bundled) { return $bundled }
    if ($env:STUDIO_PYTHON -and (Test-Path $env:STUDIO_PYTHON)) { return $env:STUDIO_PYTHON }
    foreach ($p in @((Join-Path $root ".venv\Scripts\python.exe"), "$env:USERPROFILE\Desktop\ui_install\python\python.exe", "C:\PhysicalAI\python\python.exe")) { if (Test-Path $p) { return $p } }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "Python 을 찾지 못했습니다 (STUDIO_PYTHON 환경변수로 지정하세요)"
}
function Get-Cfg($r) {
    $f = Cfg-Path $r
    if (-not (Test-Path $f)) { throw "설정 파일이 없습니다: $f" }
    return Get-Content $f -Raw -Encoding UTF8 | ConvertFrom-Json
}
function Test-Up($port) { [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) }

function Start-One($r) {
    $cfg = Get-Cfg $r; $port = [int]$cfg.port
    if (Test-Up $port) { Write-Host "  $($names[$r]) 이미 켜져 있음 (포트 $port)"; return $true }
    $out = Join-Path $logs "$r.out.log"; $err = Join-Path $logs "$r.err.log"
    foreach ($l in $out, $err) { if ((Test-Path $l) -and (Get-Item $l).Length -gt 20MB) { Move-Item $l "$l.old" -Force } }
    $py = Find-Python
    $cfgArg = (Cfg-Path $r)
    # 로그 파일은 덮어쓰지 않도록 시작마다 새로 만든다 (직전 로그는 .prev 로 남김)
    foreach ($l in $out, $err) { if (Test-Path $l) { Move-Item $l "$l.prev" -Force } }
    $proc = Start-Process -FilePath $py -ArgumentList "-m", "studio", "serve", "--config", "`"$cfgArg`"" -WorkingDirectory $root `
        -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        if ($proc.HasExited) { break }   # 시작하다 죽었으면 바로 알린다 (기다리지 않음)
        if (-not (Test-Up $port)) { continue }   # 포트가 열리기 전에는 접속을 시도하지 않는다 (Windows 는 닫힌 포트 접속에 2초씩 걸림)
        try { Invoke-RestMethod "http://127.0.0.1:$port/api/info" -TimeoutSec 2 | Out-Null; Write-Host "  $($names[$r]) 켜짐 (포트 $port)" -ForegroundColor Green; return $true } catch {}
    }
    if ($proc.HasExited) { Write-Host "  $($names[$r]) 가 시작하다 종료되었습니다 (코드 $($proc.ExitCode)). 로그: $err" -ForegroundColor Red }
    else { Write-Host "  $($names[$r]) 가 30초 안에 응답하지 않습니다. 로그를 확인하세요: $err" -ForegroundColor Red }
    if (Test-Path $err) { Get-Content $err -Tail 15 | ForEach-Object { Write-Host "    $_" } }
    return $false
}

function Stop-One($r) {
    $cfg = Get-Cfg $r; $port = [int]$cfg.port
    if (-not (Test-Up $port)) { Write-Host "  $($names[$r]) 꺼져 있음"; return $true }
    Write-Host "  $($names[$r]) 정상 종료 요청$(if ($r -eq 'capture') { ' — 카메라를 닫고 끝납니다' })"
    try {
        Invoke-RestMethod "http://127.0.0.1:$port/api/machine/shutdown" -Method Post -Headers @{ Authorization = "Bearer $(Token-Of $r $cfg)" } -TimeoutSec 10 | Out-Null
    } catch {
        $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
        if ($code -eq 409) {
            Write-Host "  지금은 끌 수 없습니다: $($_.ErrorDetails.Message)" -ForegroundColor Yellow
            Write-Host "  진행 중인 작업이 끝난 뒤 다시 실행하세요. (서버는 그대로 켜져 있습니다)"
            return $false
        }
        Write-Host "  정상 종료 요청 실패 ($code) — 기다린 뒤 필요하면 강제 종료합니다" -ForegroundColor Yellow
    }
    for ($i = 0; $i -lt 30 -and (Test-Up $port); $i++) { Start-Sleep -Milliseconds 500 }
    if (Test-Up $port) {
        Write-Host "  정상 종료되지 않아 강제 종료합니다$(if ($r -eq 'capture') { ' (카메라가 안 나오면 USB 를 다시 꽂으세요)' })" -ForegroundColor Yellow
        foreach ($c in Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { & taskkill.exe /T /F /PID $c.OwningProcess | Out-Null }
        Start-Sleep 2
    } else {
        Write-Host "  $($names[$r]) 꺼짐"
    }
    if ($r -eq "capture") { Start-Sleep 2 }   # 카메라 장치가 풀릴 시간
    return $true
}

function Show-One($r) {
    $cfg = Get-Cfg $r; $port = [int]$cfg.port
    if (-not (Test-Up $port)) { Write-Host "  $($names[$r]) : 꺼짐 (포트 $port)"; return }
    $line = "  $($names[$r]) : 켜짐 (포트 $port)"
    if ($r -eq "capture") {
        try {
            $s = New-Object Microsoft.PowerShell.Commands.WebRequestSession
            Invoke-RestMethod "http://127.0.0.1:$port/api/login" -Method Post -ContentType "application/json" -Body (@{ role = "worker"; password = $cfg.worker_password } | ConvertTo-Json) -WebSession $s -TimeoutSec 5 | Out-Null
            $st = Invoke-RestMethod "http://127.0.0.1:$port/api/state" -WebSession $s -TimeoutSec 20
            $line += " · 카메라 $(@($st.cameras | Where-Object connected).Count)/4 연결"
        } catch {}
    }
    Write-Host $line
}

# all = 이 PC 에 설정이 있는 서버만 (촬영 PC: export · capture / 관제 PC: control)
$present = @("export", "capture", "control") | Where-Object { Test-Path (Cfg-Path $_) }
$roles = if ($Role -eq "all") { if ($Action -eq "stop") { [array]::Reverse($present); $present } else { $present } } else { @($Role) }
if (-not $roles) { Write-Host "이 PC 에 서버 설정이 없습니다. Setup-CapturePC.cmd (촬영 PC) 또는 Setup-ControlPC.cmd (관제 PC) 를 먼저 실행하세요." -ForegroundColor Red; exit 1 }
$ok = $true
try {
    switch ($Action) {
        "start"   { Write-Host "== PhysicalAI KAIST Platform 켜기" -ForegroundColor Cyan; foreach ($r in $roles) { if (-not (Start-One $r)) { $ok = $false } } }
        "stop"    { Write-Host "== PhysicalAI KAIST Platform 끄기" -ForegroundColor Cyan; foreach ($r in $roles) { if (-not (Stop-One $r)) { $ok = $false } } }
        "restart" {
            Write-Host "== PhysicalAI KAIST Platform 재시작" -ForegroundColor Cyan
            foreach ($r in $roles) { if (-not (Stop-One $r)) { $ok = $false } }
            if ($ok) { foreach ($r in $roles) { if (-not (Start-One $r)) { $ok = $false } } }
        }
        "status"  { Write-Host "== PhysicalAI KAIST Platform 상태" -ForegroundColor Cyan; foreach ($r in $roles) { Show-One $r } }
    }
} catch {
    Write-Host "오류: $($_.Exception.Message)" -ForegroundColor Red; $ok = $false
}
if ($ok -and $Action -in "start", "restart") {
    if ($roles -contains "capture") { Write-Host "촬영 화면: http://localhost:8765  (새로고침 후 로그인)" -ForegroundColor Green }
    if ($roles -contains "control") { Write-Host "관제 화면: http://localhost:8767" -ForegroundColor Green }
}
if (-not $ok) { exit 1 }
