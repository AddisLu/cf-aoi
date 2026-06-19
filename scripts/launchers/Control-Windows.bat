@echo off
REM 雙擊啟動 Control（Windows，需已裝 .NET 8 SDK）。
cd /d "%~dp0..\..\control\src"
dotnet run
echo.
pause
