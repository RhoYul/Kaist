# PhysicalAI KAIST Platform — NAS 다이렉트 랜 연결 설정 (촬영 PC 에서 한 번 실행)
#   촬영 PC 랜 포트 ↔ NAS 랜 포트를 케이블 한 줄로 바로 연결했을 때 쓴다 (공유기 · 스위치 없음 → DHCP 없음).
#   1) 케이블이 꽂힌 유선 랜 어댑터 찾기 (속도 확인)
#   2) NAS 주소 정하기
#        - 고정 IP (권장): NAS DSM 에서 그 랜 포트를 수동 IP(예: 192.168.100.2/24, 게이트웨이 없음)로 설정해 두고,
#          이 PC 어댑터에도 같은 대역 고정 IP(192.168.100.1/24, 게이트웨이 없음)를 준다 (관리자 승인 창).
#        - 자동(169.254.x.x): 양쪽 다 DHCP 그대로일 때 자동으로 잡힌 주소를 찾아 쓴다. 재부팅 후 바뀔 수 있어 임시용.
#   3) SMB(445) 연결 확인 → NAS 로그인 정보를 이 주소용으로 저장 (cmdkey · Windows 는 주소마다 따로 로그인한다)
#   4) production-config\export.json: nas_root = 다이렉트 랜 경로, nas_root_fallback = 예전 경로 (다이렉트가 끊기면 자동으로 예비 경로 사용)
#   5) 쓰기 속도 확인 → export 서버 재시작
#   게이트웨이를 넣지 않으므로 Wi-Fi · 사내망 인터넷은 그대로 쓴다.
param([string]$ConfigDir = "")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $ConfigDir) { $ConfigDir = Join-Path $root "production-config" }
$expPath = Join-Path $ConfigDir "export.json"
function Ask($q, $default) { $a = Read-Host "$q [$default]"; if ([string]::IsNullOrWhiteSpace($a)) { $default } else { $a.Trim() } }
function YesNo($q, $default = "Y") { $a = Read-Host "$q (Y/N) [$default]"; if ([string]::IsNullOrWhiteSpace($a)) { $a = $default }; $a -match '^[Yy]' }
function Test-Smb($ip) { try { $c = New-Object Net.Sockets.TcpClient; $ok = $c.ConnectAsync($ip, 445).Wait(1500) -and $c.Connected; $c.Close(); $ok } catch { $false } }

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " NAS 다이렉트 랜 연결 설정" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
if (-not (Test-Path $expPath)) { Write-Host "설정이 없습니다: $expPath (Setup-CapturePC.cmd 를 먼저 실행)" -ForegroundColor Red; exit 1 }
$exp = Get-Content $expPath -Raw -Encoding UTF8 | ConvertFrom-Json
$oldRoot = [string]$exp.nas_root
$m = [regex]::Match($oldRoot, '^\\\\([^\\]+)(\\.+)$')
if (-not $m.Success) { Write-Host "지금 nas_root 가 \\서버\공유 형식이 아닙니다: '$oldRoot'" -ForegroundColor Red; exit 1 }
$oldHost = $m.Groups[1].Value; $sharePart = $m.Groups[2].Value   # 예: 10.20.32.24 / \kaist-data\rgbd_raw
Write-Host "지금 NAS 경로: $oldRoot"

# ── 1. 유선 어댑터 ──
$wired = @(Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq "Up" -and $_.InterfaceDescription -notmatch "Wi-?Fi|Wireless|802\.11|Bluetooth" -and $_.Name -notmatch "Wi-?Fi|Bluetooth" })
if (-not $wired) { Write-Host "[1/5] 연결된(케이블 꽂힌) 유선 랜 어댑터가 없습니다. 케이블 · NAS 전원을 확인하세요." -ForegroundColor Red; exit 1 }
if ($wired.Count -eq 1) { $nic = $wired[0] } else {
    Write-Host "[1/5] 연결된 유선 어댑터가 여러 개입니다:"
    for ($i = 0; $i -lt $wired.Count; $i++) { Write-Host "   $($i+1)) $($wired[$i].Name) — $($wired[$i].InterfaceDescription) · $($wired[$i].LinkSpeed)" }
    $nic = $wired[[int](Ask "  NAS 케이블이 꽂힌 번호" "1") - 1]
}
Write-Host "[1/5] 어댑터: $($nic.Name) — $($nic.InterfaceDescription) · $($nic.LinkSpeed)" -ForegroundColor Green
if ($nic.ReceiveLinkSpeed -lt 1e9) {
    Write-Host "  주의: 링크 속도가 $($nic.LinkSpeed) 입니다. 100 Mbps 면 초당 약 11 MB → 에피소드(약 370~490 MB)당 40초 이상 걸려 Wi-Fi 보다 느릴 수 있습니다." -ForegroundColor Yellow
    $fast = @(Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne $nic.Name -and $_.InterfaceDescription -match "2\.5G|GbE|Gigabit|10G" -and $_.InterfaceDescription -notmatch "Wi-?Fi|Wireless" })
    if ($fast) { Write-Host "        이 PC 에 더 빠른 어댑터가 있습니다: $(($fast | ForEach-Object { "$($_.Name) ($($_.InterfaceDescription), $($_.Status))" }) -join ', ') — 케이블을 그쪽에 꽂는 것을 권장합니다." -ForegroundColor Yellow }
    if (-not (YesNo "  그래도 이 어댑터로 계속할까요?" "Y")) { exit 1 }
}
$ifx = $nic.ifIndex
$curIp = (Get-NetIPAddress -InterfaceIndex $ifx -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -First 1).IPAddress

# ── 2. NAS 주소 ──
# 지금 그 케이블로 보이는 NAS (자동 169.254 주소) 찾기: NetBIOS 이름 → 이 어댑터의 이웃 중 SMB 가 열린 주소
$found = $null
try {
    $nb = (nbtstat -A $oldHost 2>$null | Select-String '^\s*(\S+)\s+<00>\s+UNIQUE' | Select-Object -First 1)
    if ($nb) {
        $name = $nb.Matches[0].Groups[1].Value
        $found = (Resolve-DnsName $name -LlmnrNetbiosOnly -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -like "169.254.*" } | Select-Object -First 1).IPAddress
    }
} catch {}
if (-not $found) {
    $found = (Get-NetNeighbor -InterfaceIndex $ifx -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.State -notin "Permanent", "Unreachable" -and $_.IPAddress -notlike "224.*" -and $_.IPAddress -notlike "239.*" -and $_.IPAddress -notlike "*.255" } |
        Where-Object { Test-Smb $_.IPAddress } | Select-Object -First 1).IPAddress
}
Write-Host "[2/5] 이 PC 어댑터 주소: $curIp$(if ($found) { "  ·  케이블 너머 NAS 자동 주소: $found" })"
Write-Host "  1) 고정 IP (권장 · NAS DSM 에서 해당 랜 포트를 수동 IP 로 설정해 둔 경우)"
Write-Host "  2) 자동 주소 그대로 (169.254.x.x · 임시용, 재부팅하면 바뀔 수 있음)$(if (-not $found) { ' — 지금 찾지 못함' })"
$mode = Ask "  선택" $(if ($found -and $curIp -like "169.254.*") { "2" } else { "1" })
if ($mode -eq "2") {
    $nasIp = Ask "  NAS 주소" $found
} else {
    $nasIp = Ask "  NAS 랜 포트 고정 IP (DSM 제어판 → 네트워크 → 네트워크 인터페이스 → 해당 LAN → 수동 구성)" "192.168.100.2"
    $o = $nasIp.Split('.'); $pcDefault = "$($o[0]).$($o[1]).$($o[2]).$(if ($o[3] -eq '1') { '2' } else { '1' })"
    $pcIp = Ask "  이 PC 어댑터에 줄 고정 IP (같은 대역 · 서브넷 255.255.255.0 · 게이트웨이 없음)" $pcDefault
    if ($curIp -ne $pcIp) {
        Write-Host "  '$($nic.Name)' 에 $pcIp/24 를 설정합니다 (관리자 승인 창이 뜹니다)"
        $cmd = "`$ErrorActionPreference='Stop'; Set-NetIPInterface -InterfaceIndex $ifx -Dhcp Disabled; " +
               "Get-NetIPAddress -InterfaceIndex $ifx -AddressFamily IPv4 -ErrorAction SilentlyContinue | Remove-NetIPAddress -Confirm:`$false -ErrorAction SilentlyContinue; " +
               "New-NetIPAddress -InterfaceIndex $ifx -IPAddress $pcIp -PrefixLength 24 | Out-Null; " +
               "Set-DnsClientServerAddress -InterfaceIndex $ifx -ResetServerAddresses; " +
               "Set-NetConnectionProfile -InterfaceIndex $ifx -NetworkCategory Private -ErrorAction SilentlyContinue"
        Start-Process powershell -Verb RunAs -Wait -ArgumentList "-NoProfile", "-Command", $cmd
        Start-Sleep 3
        $now = (Get-NetIPAddress -InterfaceIndex $ifx -AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress
        if ($now -notcontains $pcIp) { Write-Host "  고정 IP 가 설정되지 않았습니다 (승인 거부?). 지금 주소: $now" -ForegroundColor Red; exit 1 }
        Write-Host "  설정했습니다: $pcIp" -ForegroundColor Green
    }
}

# ── 3. 연결 · 로그인 ──
Write-Host "[3/5] NAS $nasIp 연결 확인"
$ok = $false; for ($i = 0; $i -lt 10 -and -not $ok; $i++) { $ok = Test-Smb $nasIp; if (-not $ok) { Start-Sleep 1 } }
if (-not $ok) {
    Write-Host "  $nasIp 의 SMB(445) 에 연결할 수 없습니다. 케이블 · NAS 랜 포트 IP · 대역이 맞는지 확인하세요." -ForegroundColor Red; exit 1
}
$route = (Find-NetRoute -RemoteIPAddress $nasIp -ErrorAction SilentlyContinue | Select-Object -First 1).InterfaceAlias
if ($route -and $route -ne $nic.Name) { Write-Host "  주의: $nasIp 로 가는 길이 '$route' 입니다 (다이렉트 어댑터 '$($nic.Name)' 가 아님). 대역이 사내망과 겹치지 않게 하세요." -ForegroundColor Yellow }
$newRoot = "\\$nasIp$sharePart"
if (-not (Test-Path $newRoot)) {
    Write-Host "  이 주소로는 아직 NAS 로그인 정보가 없습니다. NAS 계정을 입력하세요 (이 PC · 이 Windows 사용자에 저장)."
    $user = Read-Host "  NAS 사용자 이름"
    $sec = Read-Host "  NAS 비밀번호" -AsSecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    & cmdkey.exe "/add:$nasIp" "/user:$user" "/pass:$plain" | Out-Null
    $plain = $null
    # (net use 는 쓰지 않는다 — 출력을 숨기면 보이지 않는 입력 대기로 멈춤. cmdkey 로 저장한 로그인을 Windows 가 알아서 쓴다)
    if (-not (Test-Path $newRoot)) { Write-Host "  $newRoot 에 접근할 수 없습니다 (계정 · 공유 폴더 권한 확인). 'cmdkey /delete:$nasIp' 로 지우고 다시 실행하세요." -ForegroundColor Red; exit 1 }
}
Write-Host "  접근 확인: $newRoot" -ForegroundColor Green

# ── 4. export.json ──
$fb = @()
if ($exp.PSObject.Properties.Name -contains "nas_root_fallback" -and $exp.nas_root_fallback) { $fb = @($exp.nas_root_fallback) }
if ($oldRoot -ne $newRoot -and $fb -notcontains $oldRoot) { $fb = @($oldRoot) + $fb }
$fb = @($fb | Where-Object { $_ -and $_ -ne $newRoot })
Copy-Item $expPath "$expPath.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
$exp.nas_root = $newRoot
if ($exp.PSObject.Properties.Name -contains "nas_root_fallback") { $exp.nas_root_fallback = $fb } else { $exp | Add-Member -NotePropertyName nas_root_fallback -NotePropertyValue $fb }
[IO.File]::WriteAllText($expPath, ($exp | ConvertTo-Json -Depth 10), (New-Object Text.UTF8Encoding $false))
Write-Host "[4/5] export.json 저장 — nas_root: $newRoot · 예비: $($fb -join ', ')" -ForegroundColor Green

# ── 5. 속도 확인 · 재시작 ──
if (YesNo "[5/5] NAS 쓰기 속도를 확인할까요? (200 MB 임시 파일을 쓰고 지움)" "Y") {
    $tmp = Join-Path $newRoot ".speedtest-$env:COMPUTERNAME.tmp"
    $buf = New-Object byte[] (4MB); (New-Object Random).NextBytes($buf)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $fs = [IO.File]::Open($tmp, "Create"); for ($i = 0; $i -lt 50; $i++) { $fs.Write($buf, 0, $buf.Length) }; $fs.Flush($true); $fs.Close()
    $sw.Stop(); Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    $mbs = [math]::Round(200 / $sw.Elapsed.TotalSeconds, 1)
    Write-Host "  쓰기 약 $mbs MB/s → 에피소드 490 MB 기준 약 $([math]::Round(490 / $mbs)) 초 (+ 체크섬 확인 읽기)" -ForegroundColor Green
}
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "tools\Server.ps1") restart -Role export
Write-Host ""
Write-Host "완료. 다이렉트 랜이 끊기면 예비 경로($($fb -join ', '))로 자동 저장하고 작업 로그에 남깁니다." -ForegroundColor Cyan
