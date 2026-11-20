# PhysicalAI KAIST Platform — 촬영 PC 설치 (카메라 4대가 연결된 PC 에서 한 번 실행)
#   이 PC 에서 촬영 서버(8765, 이 PC 전용) + export 서버(8766, NAS 저장 · 관제 PC 접속용)가 함께 돈다.
#   1) Python 환경 확인 (설치 패키지에 든 python 폴더를 쓴다 — Python 설치 · 인터넷 불필요.
#      python 폴더가 없는 소스만 받은 경우에만 .venv 를 만들고 패키지 설치 — 인터넷 필요)
#   2) 설정 만들기 (촬영 PC 이름 · NAS 폴더 · 원본 임시 폴더) — 이미 있으면 그대로 둔다
#   3) 방화벽 8766 열기 (관제 PC 가 붙을 수 있게 · 관리자 승인 창이 뜬다)   4) 로그온 때 자동 실행
#   5) 서버 켜기 → 관제 PC 에 등록할 주소 · 토큰을 보여 준다
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
function Ask($q, $default) { $a = Read-Host "$q [$default]"; if ([string]::IsNullOrWhiteSpace($a)) { $default } else { $a.Trim() } }
function YesNo($q, $default = "Y") { $a = Read-Host "$q (Y/N) [$default]"; if ([string]::IsNullOrWhiteSpace($a)) { $a = $default }; $a -match '^[Yy]' }
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " PhysicalAI KAIST Platform 촬영 PC 설치  ($root)" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# ── 1. Python ──
$check = "import fastapi,uvicorn,httpx,numpy,cv2,scipy,pyrealsense2; assert hasattr(cv2,'aruco') and hasattr(cv2.aruco,'CharucoDetector'); print('ok')"
function Test-Py($py) { if (-not $py -or -not (Test-Path $py)) { return $false }; try { $o = & $py -c $check 2>$null; return ($o -eq "ok") } catch { return $false } }
$candidates = @((Join-Path $root "python\python.exe"), $env:STUDIO_PYTHON, (Join-Path $root ".venv\Scripts\python.exe"), "$env:USERPROFILE\Desktop\ui_install\python\python.exe", "C:\PhysicalAI\python\python.exe")
$py = $candidates | Where-Object { Test-Py $_ } | Select-Object -First 1
if ($py) {
    Write-Host "[1/5] Python 준비됨: $py" -ForegroundColor Green
} else {
    Write-Host "[1/5] 필요한 패키지가 있는 Python 이 없습니다 — .venv 를 만들고 설치합니다 (인터넷 필요)" -ForegroundColor Yellow
    $base = $null
    foreach ($c in @(@("py", "-3.10"), @("py", "-3"), @("python"))) {
        try { $v = & $c[0] $c[1..9] -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null; if ($v -and [version]$v -ge [version]"3.10") { $base = $c; break } } catch {}
    }
    if (-not $base) { Write-Host "Python 3.10 이상(64비트)을 python.org 에서 설치한 뒤 다시 실행하세요." -ForegroundColor Red; exit 1 }
    & $base[0] $base[1..9] -m venv (Join-Path $root ".venv")
    $py = Join-Path $root ".venv\Scripts\python.exe"
    & $py -m pip install --upgrade pip
    & $py -m pip install -r (Join-Path $root "requirements.txt")
    if (-not (Test-Py $py)) { Write-Host "패키지 설치 후에도 확인에 실패했습니다. 위 오류를 확인하세요." -ForegroundColor Red; exit 1 }
    Write-Host "      설치 완료: $py" -ForegroundColor Green
}
$env:STUDIO_PYTHON = $py

# ── PhysicalAI 와 카메라 충돌 확인 ──
if (Get-ScheduledTask -TaskName "PhysicalAI" -ErrorAction SilentlyContinue) {
    Write-Host "  주의: 이 PC 에 PhysicalAI 자동 실행이 있습니다. PhysicalAI 서버가 켜져 있으면 카메라를 쓸 수 없습니다." -ForegroundColor Yellow
    Write-Host "        관리자 PowerShell 에서: Disable-ScheduledTask -TaskName PhysicalAI ; C:\PhysicalAI\bin\PhysicalAI.ps1 stop" -ForegroundColor Yellow
}

# ── 2. 설정 ──
$cfgDir = Join-Path $root "production-config"
if ((Test-Path (Join-Path $cfgDir "capture.json")) -and (Test-Path (Join-Path $cfgDir "export.json"))) {
    Write-Host "[2/5] 설정이 이미 있습니다 — 그대로 씁니다 ($cfgDir)" -ForegroundColor Green
} else {
    Write-Host "[2/5] 설정 만들기"
    $defaultStation = (($env:COMPUTERNAME.ToLower() -replace '[^a-z0-9_-]', '-').Trim('-'))
    if (-not $defaultStation) { $defaultStation = "capture-01" }
    while ($true) {
        $station = Ask "  촬영 PC 이름 (관제 화면 · 로그에 표시 · 영문 · 숫자 · - · _ 만, 예: capture-01)" $defaultStation
        if ($station -cmatch '^[A-Za-z0-9_-]{1,60}$') { break }
        Write-Host "  '$station' 은 쓸 수 없습니다. 한글 · 공백 없이 영문 · 숫자 · - · _ 로 입력하세요." -ForegroundColor Yellow
    }
    $nas = Ask "  NAS 저장 폴더" "\\10.20.32.24\kaist-data\rgbd_raw"
    if (-not (Test-Path $nas)) { Write-Host "  주의: 지금 이 PC 에서 NAS 폴더에 접근할 수 없습니다. 탐색기에서 $nas 를 열어 로그인(자격 증명 저장)한 뒤 쓰세요." -ForegroundColor Yellow }
    $data = Ask "  원본 임시 폴더 (NAS 저장이 확인되면 자동 삭제)" (Join-Path $root "runtime-real")
    & $py -m studio init --directory $cfgDir --station $station --nas-root $nas --data-dir $data --remote-access | Out-Host
    if ($LASTEXITCODE -ne 0) { Write-Host "설정 만들기에 실패했습니다." -ForegroundColor Red; exit 1 }
}
$cap = Get-Content (Join-Path $cfgDir "capture.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$exp = Get-Content (Join-Path $cfgDir "export.json") -Raw -Encoding UTF8 | ConvertFrom-Json

# ── 3. 방화벽 (관제 PC 접속용 8766) ──
$ruleName = "PhysicalAI KAIST export 8766"
if ($exp.host -eq "0.0.0.0") {
    if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) {
        Write-Host "[3/5] 방화벽 규칙 있음 ($ruleName)" -ForegroundColor Green
    } elseif (YesNo "[3/5] 관제 PC 가 이 PC 에 붙을 수 있게 방화벽에서 TCP 8766 을 열까요? (관리자 승인 창이 뜹니다)") {
        $cmd = "New-NetFirewallRule -DisplayName '$ruleName' -Direction Inbound -Protocol TCP -LocalPort 8766 -Action Allow -Profile Domain,Private | Out-Null"
        Start-Process powershell -Verb RunAs -Wait -ArgumentList "-NoProfile", "-Command", $cmd
        if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) { Write-Host "      열었습니다" -ForegroundColor Green } else { Write-Host "      규칙이 만들어지지 않았습니다 (승인 거부?)" -ForegroundColor Yellow }
    }
} else { Write-Host "[3/5] export 서버가 이 PC 전용(127.0.0.1)입니다 — 관제 PC 접속을 쓰려면 export.json 의 host 를 0.0.0.0 으로" }

# ── 4. 로그온 때 자동 실행 (시작프로그램 바로가기 · 관리자 권한 불필요) ──
$lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "PhysicalAI KAIST.lnk"
if (Test-Path $lnk) { Write-Host "[4/5] 로그온 자동 실행 등록됨" -ForegroundColor Green }
elseif (YesNo "[4/5] 로그온할 때 서버가 자동으로 켜지게 할까요?") {
    $sh = New-Object -ComObject WScript.Shell; $s = $sh.CreateShortcut($lnk)
    $s.TargetPath = Join-Path $root "Start-PhysicalAI_KAIST.cmd"; $s.WorkingDirectory = $root; $s.WindowStyle = 7; $s.Save()
    Write-Host "      등록했습니다 ($lnk)" -ForegroundColor Green
}
# Start-PhysicalAI_KAIST.cmd 가 이 Python 을 쓰게 저장
[Environment]::SetEnvironmentVariable("STUDIO_PYTHON", $py, "User")

# ── 5. 서버 켜기 ──
Write-Host "[5/5] 서버 켜기"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "tools\Server.ps1") start
$ips = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254*" -and $_.InterfaceAlias -notmatch "vEthernet|Loopback" }).IPAddress -join ", "
Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " 설치 완료" -ForegroundColor Cyan
Write-Host "  촬영 화면 (이 PC 브라우저): http://localhost:8765"
Write-Host "  작업자 비밀번호 · 관리자 비밀번호: production-config\capture.json 의 worker_password · admin_password"
Write-Host ""
Write-Host "  관제 PC 에 등록할 정보 (관제 화면 → ＋ 촬영 PC 등록)" -ForegroundColor Yellow
Write-Host "    주소 : $env:COMPUTERNAME   (안 되면 IP: $ips)"
Write-Host "    토큰 : $($exp.transfer_token)"
Write-Host "  이 PC 를 처음 쓰면 촬영 화면에서 캘리브레이션을 새로 하세요." -ForegroundColor Yellow
Write-Host "==================================================" -ForegroundColor Cyan
