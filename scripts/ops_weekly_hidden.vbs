' Silent launcher for weekly research-platform maintenance (no console window).
CreateObject("Wscript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""D:\Project\stock\scripts\ops_weekly.ps1""", 0, False
