@echo off
REM ===========================================================================
REM  build.cmd —— build.ps1 的包装，用来绕开 PowerShell 的执行策略限制。
REM
REM  默认情况下 Windows 不允许直接运行 .ps1 脚本（"在此系统上禁止运行脚本"），
REM  这里显式带上 -ExecutionPolicy Bypass，只影响本次调用，不改动系统设置。
REM
REM  用法：  build.cmd
REM          build.cmd -SkipRuntime
REM ===========================================================================
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
set EXITCODE=%ERRORLEVEL%
if not "%EXITCODE%"=="0" (
    echo.
    echo  构建失败，退出码 %EXITCODE%
)
endlocal & exit /b %EXITCODE%
