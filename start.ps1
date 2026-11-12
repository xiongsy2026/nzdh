# 女装带货前端服务保活：进程退出后 3 秒自动拉起。任意路径执行均可。
$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONPATH = $PSScriptRoot
$env:OPEN_BROWSER = "1"

$logDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir "service.log"
$restart = 0

function Write-ServiceLog([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $logFile -Value $line -Encoding UTF8
    Write-Host $line
}

while ($true) {
    $restart++
    Write-ServiceLog "start #$restart"
    & python (Join-Path $PSScriptRoot "app.py")
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 0 }
    Write-ServiceLog "exit code=$code, restart in 3s"
    Start-Sleep -Seconds 3
}
