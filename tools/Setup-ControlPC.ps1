# PhysicalAI KAIST Platform — 관제 PC 설치 (관리자 노트북 · 이 PC 에서만 도는 관제 화면)
#   촬영 PC 들의 export 서버(8766)에 붙어 상태 · 작업 · NAS 저장 · 로그를 본다. 데이터는 이 PC 를 거치지 않는다.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
function YesNo($q, $default = "Y") { $a = Read-Host "$q (Y/N) [$default]"; if ([string]::IsNullOrWhiteSpace($a)) { $a = $default }; $a -match '^[Yy]' }
Write-Host "== PhysicalAI KAIST Platform 관제 PC 설치 ($root)" -ForegroundColor Cyan
$check = "import fastapi,uvicorn,httpx,pydantic; print('ok')"
function Test-Py($py) { if (-not $py -or -not (Test-Path $py)) { return $false }; try { (& $py -c $check 2>$null) -eq "ok" } catch { $false } }
$py = @((Join-Path $root "python\python.exe"), $env:STUDIO_PYTHON, (Join-Path $root ".venv\Scripts\python.exe"), "$env:USERPROFILE\Desktop\ui_install\python\python.exe", "C:\PhysicalAI\python\python.exe") | Where-Object { Test-Py $_ } | Select-Object -First 1
if (-not $py) {
    Write-Host "  Python 패키지가 없어 .venv 를 만듭니다 (인터넷 필요)" -ForegroundColor Yellow
    $base = $null
    foreach ($c in @(@("py", "-3.10"), @("py", "-3"), @("python"))) { try { $v = & $c[0] $c[1..9] -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null; if ($v -and [version]$v -ge [version]"3.10") { $base = $c; break } } catch {} }
    if (-not $base) { Write-Host "Python 3.10 이상을 설치한 뒤 다시 실행하세요." -ForegroundColor Red; exit 1 }
    & $base[0] $base[1..9] -m venv (Join-Path $root ".venv"); $py = Join-Path $root ".venv\Scripts\python.exe"
    & $py -m pip install "fastapi==0.135.3" "uvicorn>=0.30,<1" "httpx>=0.27,<1"
    if (-not (Test-Py $py)) { Write-Host "설치 확인 실패" -ForegroundColor Red; exit 1 }
}
Write-Host "  Python: $py" -ForegroundColor Green
[Environment]::SetEnvironmentVariable("STUDIO_PYTHON", $py, "User"); $env:STUDIO_PYTHON = $py
$cfg = Join-Path $root "control-config\control.json"
if (Test-Path $cfg) { Write-Host "  설정 있음: $cfg" -ForegroundColor Green }
else { & $py -m studio init-control --directory (Join-Path $root "control-config") | Out-Host }
$lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "PhysicalAI KAIST 관제.lnk"
if (-not (Test-Path $lnk) -and (YesNo "  로그온할 때 관제 서버가 자동으로 켜지게 할까요?")) {
    $sh = New-Object -ComObject WScript.Shell; $s = $sh.CreateShortcut($lnk); $s.TargetPath = Join-Path $root "Start-PhysicalAI_KAIST.cmd"; $s.WorkingDirectory = $root; $s.WindowStyle = 7; $s.Save()
}
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "tools\Server.ps1") start -Role control
$pw = (Get-Content $cfg -Raw -Encoding UTF8 | ConvertFrom-Json).admin_password
Write-Host ""
Write-Host "관제 화면: http://localhost:8767   (비밀번호: control-config\control.json 의 admin_password)" -ForegroundColor Green
Write-Host "화면에서 「＋ 촬영 PC 등록」으로 촬영 PC 설치 때 나온 주소 · 토큰을 넣으세요." -ForegroundColor Green
Start-Process "http://localhost:8767"
