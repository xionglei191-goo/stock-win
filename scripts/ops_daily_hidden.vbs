' Silent launcher for daily research-platform maintenance (no console window).
CreateObject("Wscript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""D:\Project\stock\scripts\ops_daily.ps1""", 0, False
