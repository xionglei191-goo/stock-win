# Weekly research-platform maintenance.
# Appends structured log lines under data/ops_logs/. Read-only with respect to TDX client state.
$ErrorActionPreference = "Continue"
$root = "D:\Project\stock"
$logDir = Join-Path $root "data\ops_logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("weekly_{0:yyyyMM}.log" -f (Get-Date))
Set-Location $root

function Step($name, $cmdArgs) {
    Add-Content $log ("[{0:yyyy-MM-dd HH:mm:ss}] START {1}" -f (Get-Date), $name)
    & py -3 -m research_platform @cmdArgs 2>&1 | Out-File -Append -FilePath $log -Encoding utf8
    Add-Content $log ("[{0:yyyy-MM-dd HH:mm:ss}] EXIT {1} code={2}" -f (Get-Date), $name, $LASTEXITCODE)
}

Step "doctor" @("doctor")
Step "refresh-weekly-observations" @("refresh-weekly-observations")
Step "us-pit-forward-status" @("us-pit", "forward-status")
Add-Content $log ("[{0:yyyy-MM-dd HH:mm:ss}] DONE weekly ops" -f (Get-Date))
