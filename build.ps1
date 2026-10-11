<#
.SYNOPSIS
    构建 CodeReview 免安装整包：编译启动器 exe + 制作精简 Python 运行时。

.DESCRIPTION
    产物：
        CodeReview.exe      启动器（用 Windows 自带的 csc.exe 编译，无需下载任何东西）
        runtime\            精简后的 Python 运行时（去掉 site-packages 等体积大头）
        main.py / codeagent\  Agent 本体（已存在于仓库中，本脚本不改动）

    之所以是"精简"而不是直接拷贝：完整运行时里 90% 的体积是 numpy / pandas /
    Pillow 这类第三方包（本 Agent 是纯标准库的，一个都不需要）。
    去掉 site-packages 后体积从 ~165MB 降到 ~40MB。

.PARAMETER PythonSource
    作为运行时来源的 Python 安装目录。默认自动探测（优先 DSH 自带的那份）。

.PARAMETER SkipRuntime
    只重新编译 exe，不动 runtime\。

.EXAMPLE
    .\build.ps1
    .\build.ps1 -PythonSource "C:\Python312"
    .\build.ps1 -SkipRuntime -Force
#>
[CmdletBinding()]
param(
    [string]$PythonSource = "",
    [switch]$SkipRuntime,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
if (-not $root) { $root = (Get-Location).Path }

function Write-Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "    $text" -ForegroundColor Green }
function Write-Warn2($text){ Write-Host "    $text" -ForegroundColor Yellow }
function Write-Err($text)  { Write-Host "    $text" -ForegroundColor Red }

function Get-DirSizeMB($path) {
    if (-not (Test-Path $path)) { return 0 }
    $sum = (Get-ChildItem $path -Recurse -File -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum).Sum
    if (-not $sum) { return 0 }
    return [math]::Round($sum / 1MB, 1)
}

# --------------------------------------------------------------------------- #
# 0. 前置检查
# --------------------------------------------------------------------------- #
Write-Step "检查构建环境"

$csc = Get-ChildItem "$env:WINDIR\Microsoft.NET\Framework64\v*\csc.exe" -ErrorAction SilentlyContinue |
       Sort-Object FullName | Select-Object -Last 1
if (-not $csc) {
    Write-Err "找不到 csc.exe（.NET Framework 自带的 C# 编译器）。"
    Write-Err "本脚本刻意不依赖 Roslyn / .NET SDK，请确认系统完整。"
    exit 1
}
Write-Ok "编译器：$($csc.FullName)"

# --------------------------------------------------------------------------- #
# 1. 编译启动器
# --------------------------------------------------------------------------- #
Write-Step "编译 CodeReview.exe"
$source = Join-Path $root 'launcher\CodeReview.cs'
$outExe = Join-Path $root 'CodeReview.exe'
if (-not (Test-Path $source)) { Write-Err "找不到启动器源码：$source"; exit 1 }

if ((Test-Path $outExe) -and -not $Force) {
    Write-Warn2 "CodeReview.exe 已存在，重新编译覆盖。"
}

# /codepage:65001 让 csc 按 UTF-8 读取源码，否则源码里的中文会变乱码
& $csc.FullName /nologo /target:exe /platform:anycpu `
    /codepage:65001 /optimize+ `
    "/out:$outExe" $source
if ($LASTEXITCODE -ne 0) { Write-Err "编译失败（csc 退出码 $LASTEXITCODE）"; exit 1 }
Write-Ok "已生成 $outExe  ($([math]::Round((Get-Item $outExe).Length/1KB,1)) KB)"

# --------------------------------------------------------------------------- #
# 2. 制作精简运行时
# --------------------------------------------------------------------------- #
if ($SkipRuntime) {
    Write-Step "跳过 runtime 制作（-SkipRuntime）"
} else {
    Write-Step "制作精简 Python 运行时"

    if (-not $PythonSource) {
        $candidates = @(
            "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python"
        )
        foreach ($c in $candidates) { if (Test-Path (Join-Path $c 'python.exe')) { $PythonSource = $c; break } }
    }
    if (-not $PythonSource -or -not (Test-Path (Join-Path $PythonSource 'python.exe'))) {
        Write-Err "找不到可用的 Python 安装目录。请用 -PythonSource 显式指定。"
        exit 1
    }
    $PythonSource = (Resolve-Path $PythonSource).Path
    Write-Ok "来源：$PythonSource  ($(Get-DirSizeMB $PythonSource) MB)"

    $runtimeDir = Join-Path $root 'runtime'
    if (Test-Path $runtimeDir) {
        Write-Warn2 "清空旧的 runtime\ ..."
        Remove-Item $runtimeDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    # 排除的是体积大头：第三方包、测试套件、开发期才用的东西
    $excludeDirs = @(
        'site-packages', 'test', 'tests', 'idlelib', 'tkinter', 'turtledemo',
        'lib2to3', 'ensurepip', 'include', 'libs', 'Scripts', 'Tools', 'Doc',
        'tcl', 'pip', 'setuptools', '__pycache__'
    )
    $excludeFiles = @('*.pyc', '*.pyo', '*.pdb', '*.lib', '*.exp')

    $roboArgs = @($PythonSource, $runtimeDir, '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/R:1', '/W:1')
    $roboArgs += '/XD'
    $roboArgs += $excludeDirs
    $roboArgs += '/XF'
    $roboArgs += $excludeFiles

    & robocopy @roboArgs | Out-Null
    $roboCode = $LASTEXITCODE
    if ($roboCode -ge 8) { Write-Err "robocopy 失败（退出码 $roboCode）"; exit 1 }

    Write-Ok "已生成 runtime\  ($(Get-DirSizeMB $runtimeDir) MB)"

    # CPython 的许可证要随分发一起带上
    foreach ($lic in @('LICENSE.txt', 'LICENSE')) {
        $from = Join-Path $PythonSource $lic
        if (Test-Path $from) {
            Copy-Item $from (Join-Path $runtimeDir 'PYTHON-LICENSE.txt') -Force
            Write-Ok "已附带 CPython 许可证（PYTHON-LICENSE.txt）"
            break
        }
    }

    # 冒烟测试：精简后的解释器还能不能起来、标准库还在不在
    Write-Step "验证精简运行时"
    $smoke = & (Join-Path $runtimeDir 'python.exe') -c "import json,re,pathlib,urllib.request,ssl,unicodedata,sys; print(sys.version.split()[0])" 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Err "精简运行时不可用：$smoke"
        exit 1
    }
    Write-Ok "解释器可用，版本 $smoke"
}

# --------------------------------------------------------------------------- #
# 3. 汇总
# --------------------------------------------------------------------------- #
Write-Step "构建完成"
$items = @(
    @{ Name = 'CodeReview.exe'; Path = (Join-Path $root 'CodeReview.exe') },
    @{ Name = 'runtime\';       Path = (Join-Path $root 'runtime') },
    @{ Name = 'codeagent\';     Path = (Join-Path $root 'codeagent') },
    @{ Name = 'main.py';        Path = (Join-Path $root 'main.py') }
)
$total = 0
foreach ($i in $items) {
    if (Test-Path $i.Path) {
        $isDir = (Get-Item $i.Path).PSIsContainer
        $mb = if ($isDir) { Get-DirSizeMB $i.Path } else { [math]::Round((Get-Item $i.Path).Length/1KB,1)/1024 }
        $total += $mb
        Write-Host ("    {0,-16} {1,8:N1} MB" -f $i.Name, $mb)
    }
}
Write-Host ("    {0,-16} {1,8:N1} MB" -f '合计', $total) -ForegroundColor Cyan
Write-Host ""
Write-Host "    直接双击 CodeReview.exe，或运行 .\CodeReview.exe help" -ForegroundColor Green
Write-Host ""
