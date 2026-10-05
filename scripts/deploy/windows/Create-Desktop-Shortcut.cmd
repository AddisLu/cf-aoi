@echo off
rem Create a desktop shortcut "CF-AOI Control" pointing to control\CfAoiControl.exe (no admin needed).
set "DIR=%~dp0control"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$w=New-Object -ComObject WScript.Shell; $p=[Environment]::GetFolderPath('Desktop')+'\CF-AOI Control.lnk'; $s=$w.CreateShortcut($p); $s.TargetPath='%DIR%\CfAoiControl.exe'; $s.WorkingDirectory='%DIR%'; $s.IconLocation='%DIR%\CfAoiControl.exe,0'; $s.Save(); Write-Host ('OK: ' + $p)"
pause
