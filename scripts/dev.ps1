# VeriForge 本地开发引导脚本（PowerShell）
#
# 用法：
#   .\scripts\dev.ps1 setup     # 安装前后端依赖（幂等）
#   .\scripts\dev.ps1 verify    # 类型检查 + 前端构建 + 前端测试 + 后端测试
#   .\scripts\dev.ps1 frontend  # 只装前端依赖
#   .\scripts\dev.ps1 backend   # 只装后端依赖

param(
    [Parameter(Position = 0)]
    [ValidateSet('setup', 'verify', 'frontend', 'backend')]
    [string]$Command = 'setup'
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Frontend = Join-Path $Root 'frontend'
$Backend = Join-Path $Root 'backend'
$Venv = Join-Path $Backend '.venv'

function Find-Node {
    $cmd = Get-Command node -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $found = Get-ChildItem "$HOME\.workbuddy\binaries\node\versions\*\node.exe" -ErrorAction SilentlyContinue |
        Sort-Object { [version]($_.Directory.Name) } | Select-Object -Last 1
    if ($found) { return $found.FullName }
    return $null
}

function Find-Python {
    foreach ($name in @('python', 'python3', 'py')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    $found = Get-ChildItem "$HOME\.workbuddy\binaries\python\versions\*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object { [version]($_.Directory.Name) } | Select-Object -Last 1
    if ($found) { return $found.FullName }
    return $null
}

# 托管的 node 目录里 npm 是 shell 脚本，PowerShell 下必须用 npm.cmd，
# 否则统一走 node + npm-cli.js。
function Invoke-Npm {
    param([Parameter(ValueFromRemainingArguments = $true)] $Args)

    $nodeBin = $script:NodeBin
    if (-not $nodeBin) { throw '未找到 Node.js' }

    $nodeDir = Split-Path -Parent $nodeBin
    $npmCmd = Join-Path $nodeDir 'npm.cmd'
    $npmCli = Join-Path $nodeDir 'node_modules\npm\bin\npm-cli.js'

    if (Test-Path $npmCmd) {
        & $npmCmd @Args
    }
    elseif (Test-Path $npmCli) {
        & $nodeBin $npmCli @Args
    }
    else {
        throw "在 $nodeDir 下找不到 npm.cmd 或 npm-cli.js"
    }
    if ($LASTEXITCODE -ne 0) { throw "npm $($Args -join ' ') 失败，退出码 $LASTEXITCODE" }
}

function Get-VenvPython {
    $win = Join-Path $Venv 'Scripts\python.exe'
    if (Test-Path $win) { return $win }
    $unix = Join-Path $Venv 'bin/python'
    if (Test-Path $unix) { return $unix }
    return $null
}

function Setup-Frontend {
    $script:NodeBin = Find-Node
    if (-not $script:NodeBin) { throw '未找到 Node.js。请安装 Node 20+，或确认 ~/.workbuddy/binaries/node 下有托管版本。' }
    Write-Host "[dev.ps1] Node: $($script:NodeBin)"
    Write-Host "[dev.ps1] 安装前端依赖 -> $Frontend"
    Push-Location $Frontend
    try { Invoke-Npm 'ci' '--no-audit' '--no-fund' } finally { Pop-Location }
    Write-Host '[dev.ps1] 前端依赖就绪'
}

function Setup-Backend {
    $py = Find-Python
    if (-not $py) { throw '未找到 Python。请安装 Python 3.11+，或确认 ~/.workbuddy/binaries/python 下有托管版本。' }
    Write-Host "[dev.ps1] Python: $py"

    $vpy = Get-VenvPython
    if (-not $vpy) {
        Write-Host "[dev.ps1] 创建虚拟环境 -> $Venv"
        & $py '-m' 'venv' $Venv
        if ($LASTEXITCODE -ne 0) { throw 'venv 创建失败' }
        $vpy = Get-VenvPython
    }

    Write-Host '[dev.ps1] 安装后端依赖'
    Push-Location $Backend
    try {
        # 不要在这里升级 pip：沙箱环境下 pip 自升级会因无法删除旧 exe 而失败。
        & $vpy '-m' 'pip' 'install' '--disable-pip-version-check' '-r' 'requirements.txt' '-r' 'requirements-dev.txt'
        if ($LASTEXITCODE -ne 0) { throw '后端依赖安装失败' }
    } finally { Pop-Location }
    Write-Host '[dev.ps1] 后端依赖就绪'
}

function Invoke-Verify {
    $failed = $false

    $script:NodeBin = Find-Node
    if (-not $script:NodeBin) { throw '未找到 Node.js' }

    Write-Host '== 1/4 前端类型检查 + 构建 =='
    Push-Location $Frontend
    try { Invoke-Npm 'run' 'build' } catch { Write-Warning $_; $failed = $true } finally { Pop-Location }

    Write-Host '== 2/4 前端单元测试 =='
    Push-Location $Frontend
    try { Invoke-Npm 'run' 'test' } catch { Write-Warning $_; $failed = $true } finally { Pop-Location }

    $vpy = Get-VenvPython
    if (-not $vpy) {
        Write-Warning "未找到 $Venv，跳过后端测试。请先执行 .\scripts\dev.ps1 backend"
        $failed = $true
    }
    else {
        Write-Host '== 3/4 后端测试 =='
        Push-Location $Backend
        try {
            & $vpy '-m' 'pytest' '-q'
            if ($LASTEXITCODE -ne 0) { $failed = $true }
        } finally { Pop-Location }

        Write-Host '== 4/4 后端依赖自检 =='
        Push-Location $Backend
        try {
            & $vpy '-c' 'import fastapi, uvicorn, aiosqlite; print("fastapi/uvicorn/aiosqlite 导入正常")'
            if ($LASTEXITCODE -ne 0) { $failed = $true }
        } finally { Pop-Location }
    }

    if ($failed) { throw '[dev.ps1] 验证失败' }
    Write-Host '[dev.ps1] 全部验证通过'
}

switch ($Command) {
    'setup' { Setup-Frontend; Setup-Backend }
    'frontend' { Setup-Frontend }
    'backend' { Setup-Backend }
    'verify' { Invoke-Verify }
}
