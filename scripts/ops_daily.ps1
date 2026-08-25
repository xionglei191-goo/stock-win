# Daily research-platform maintenance (A-share post-close).
# Appends structured log lines under data/ops_logs/. Never places real orders.
$ErrorActionPreference = "Continue"
$root = "D:\Project\stock"
$logDir = Join-Path $root "data\ops_logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("daily_{0:yyyyMM}.log" -f (Get-Date))
Set-Location $root

function Step($name, $cmdArgs) {
    Add-Content $log ("[{0:yyyy-MM-dd HH:mm:ss}] START {1}" -f (Get-Date), $name)
    & py -3 -m research_platform @cmdArgs 2>&1 | Out-File -Append -FilePath $log -Encoding utf8
    Add-Content $log ("[{0:yyyy-MM-dd HH:mm:ss}] EXIT {1} code={2}" -f (Get-Date), $name, $LASTEXITCODE)
}

Step "refresh-feedback" @("refresh-feedback")
Step "cash-instrument-status" @("cash-instrument-status")
Add-Content $log ("[{0:yyyy-MM-dd HH:mm:ss}] DONE daily ops" -f (Get-Date))
