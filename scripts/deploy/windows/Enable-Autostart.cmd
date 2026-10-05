@echo off
rem Start CF-AOI Control automatically after Windows login (shortcut in the user's Startup folder).
rem To disable: delete "CF-AOI Control.lnk" from shell:startup.
set "DIR=%~dp0control"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$w=New-Object -ComObject WScript.Shell; $p=[Environment]::GetFolderPath('Startup')+'\CF-AOI Control.lnk'; $s=$w.CreateShortcut($p); $s.TargetPath='%DIR%\CfAoiControl.exe'; $s.WorkingDirectory='%DIR%'; $s.Save(); Write-Host ('OK: ' + $p)"
pause
