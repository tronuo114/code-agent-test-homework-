@echo off
REM ===========================================================================
REM  build.cmd —— build.ps1 的包装，用来绕开 PowerShell 的执行策略限制。
REM
REM  默认情况下 Windows 不允许直接运行 .ps1 脚本（"在此系统上禁止运行脚本"），
REM  这里显式带上 -ExecutionPolicy Bypass，只影响本次调用，不改动系统设置。
REM
REM  用法：  build.cmd
REM          build.cmd -SkipRuntime
REM
REM  ⚠ 本文件必须用 CRLF 换行，并且不要加 BOM。
REM  cmd.exe 按 OEM 代码页（中文 Windows 上是 936）解析批处理文件：若用 LF 换行，
REM  GBK 解码器会把多字节汉字的尾字节与后面的换行吃成一个字符，整份脚本被拆散，
REM  报出 "'略限制。' is not recognized as an internal or external command" 之类的错误。
REM
REM  ⚠ 失败提示统一用 ASCII：cmd 的 echo 原样输出字节，UTF-8 中文在 936 控制台上
REM  一定显示成乱码（build.ps1 的中文提示由 PowerShell 输出，不受此限制）。
REM ===========================================================================
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
set EXITCODE=%ERRORLEVEL%
if not "%EXITCODE%"=="0" (
    echo.
    echo  Build failed, exit code %EXITCODE%
)
endlocal & exit /b %EXITCODE%
