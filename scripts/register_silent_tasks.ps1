# Re-registers the three research-platform scheduled tasks so they run silently:
#   - USPIT forward capture: pythonw.exe + logging wrapper (no console)
#   - Daily/weekly ops: wscript.exe VBS wrappers (no console flash)
$ErrorActionPreference = "Stop"
$pythonw = "C:\Users\Administrator\AppData\Local\Programs\Python\Python314\pythonw.exe"
$root = "D:\Project\stock"

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

$workerAction = New-ScheduledTaskAction -Execute $pythonw `
    -Argument '"D:\Project\stock\scripts\uspit_forward_capture.pyw"' `
    -WorkingDirectory $root
$workerTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)

$dailyAction = New-ScheduledTaskAction -Execute "wscript.exe" `
    -Argument '"D:\Project\stock\scripts\ops_daily_hidden.vbs"' `
    -WorkingDirectory $root
$dailyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 16:30

$weeklyAction = New-ScheduledTaskAction -Execute "wscript.exe" `
    -Argument '"D:\Project\stock\scripts\ops_weekly_hidden.vbs"' `
    -WorkingDirectory $root
$weeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At 09:00

foreach ($task in @(
        @{ Name = "ResearchPlatform-USPIT-ForwardCapture"; Action = $workerAction; Trigger = $workerTrigger;
           Desc = "Append-only US PIT post-close capture; read-only market data; silent." },
        @{ Name = "ResearchPlatform-DailyOps"; Action = $dailyAction; Trigger = $dailyTrigger;
           Desc = "Local read-only research platform maintenance; silent." },
        @{ Name = "ResearchPlatform-WeeklyOps"; Action = $weeklyAction; Trigger = $weeklyTrigger;
           Desc = "Local read-only research platform maintenance; silent." })) {
    $existing = Get-ScheduledTask -TaskName $task.Name -ErrorAction SilentlyContinue
    if ($existing) {
        Set-ScheduledTask -TaskName $task.Name -Action $task.Action -Trigger $task.Trigger | Out-Null
        Write-Output ("updated: " + $task.Name)
    } else {
        Register-ScheduledTask -TaskName $task.Name -Action $task.Action -Trigger $task.Trigger `
            -Settings $settings -Description $task.Desc | Out-Null
        Write-Output ("registered: " + $task.Name)
    }
}
