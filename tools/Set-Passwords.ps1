# 작업자 · 관리자 비밀번호를 원하는 값으로 바꾼다 (capture.json · export.json 둘 다 같은 값으로).
#   powershell -ExecutionPolicy Bypass -File tools\Set-Passwords.ps1 [-ConfigDir production-config]
# 바꾼 뒤 두 서버를 다시 띄워야 적용된다. 비밀번호는 12자 이상.
param([string]$ConfigDir = (Join-Path (Split-Path -Parent $PSScriptRoot) "production-config"))

function Read-Pw($label) {
    while ($true) {
        $a = Read-Host "$label 비밀번호 (12자 이상)" -AsSecureString
        $b = Read-Host "$label 비밀번호 확인" -AsSecureString
        $pa = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($a))
        $pb = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($b))
        if ($pa -ne $pb) { Write-Host "두 입력이 다릅니다. 다시 입력하세요." -ForegroundColor Yellow; continue }
        if ($pa.Length -lt 12) { Write-Host "12자 이상이어야 합니다." -ForegroundColor Yellow; continue }
        return $pa
    }
}

$files = @("capture.json", "export.json") | ForEach-Object { Join-Path $ConfigDir $_ }
foreach ($f in $files) { if (-not (Test-Path $f)) { throw "설정 파일이 없습니다: $f" } }

$worker = Read-Pw "작업자"
$admin = Read-Pw "관리자"
foreach ($f in $files) {
    $c = Get-Content $f -Raw -Encoding UTF8 | ConvertFrom-Json
    $c.worker_password = $worker
    $c.admin_password = $admin
    [IO.File]::WriteAllText($f, ($c | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))
}
Write-Host "변경 완료: $($files -join ', ')" -ForegroundColor Green
Write-Host "두 서버(8765 · 8766)를 다시 띄워야 적용됩니다."
