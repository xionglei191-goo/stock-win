# Registers (idempotently) the daily and weekly research-platform maintenance tasks.
# Daily task runs Mon-Fri 16:30 (A-share post-close). Weekly task runs Saturday 09:00.

$dailyAction = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File D:\Project\stock\scripts\ops_daily.ps1" `
    -WorkingDirectory "D:\Project\stock"
$weeklyAction = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File D:\Project\stock\scripts\ops_weekly.ps1" `
    -WorkingDirectory "D:\Project\stock"

$dailyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 16:30
$weeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At 09:00

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

foreach ($task in @(
        @{ Name = "ResearchPlatform-DailyOps"; Action = $dailyAction; Trigger = $dailyTrigger },
        @{ Name = "ResearchPlatform-WeeklyOps"; Action = $weeklyAction; Trigger = $weeklyTrigger })) {
    $existing = Get-ScheduledTask -TaskName $task.Name -ErrorAction SilentlyContinue
    if ($existing) {
        Set-ScheduledTask -TaskName $task.Name -Action $task.Action -Trigger $task.Trigger -Settings $settings | Out-Null
        Write-Output ("updated: " + $task.Name)
    } else {
        Register-ScheduledTask -TaskName $task.Name -Action $task.Action -Trigger $task.Trigger `
            -Settings $settings -Description "Local read-only research platform maintenance; never places orders." | Out-Null
        Write-Output ("registered: " + $task.Name)
    }
}
