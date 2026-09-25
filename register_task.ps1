$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c e:\Website\Alpha_3.0\start_app.bat" -WorkingDirectory "e:\Website\Alpha_3.0"
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 9:00AM
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 8) -Priority 4
Register-ScheduledTask -TaskName "ProjectAlpha_9AM_Startup" -Action $action -Trigger $trigger -Settings $settings -Description "Wakes PC from sleep and launches Project Alpha 2.0/3.0 Backend & UI at 9:00 AM on trading days" -Force
Write-Host "Task ProjectAlpha_9AM_Startup registered successfully with WakeToRun and StartWhenAvailable enabled."
