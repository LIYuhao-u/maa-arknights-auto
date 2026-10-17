#Requires -Version 5.1
# 作者: leo <1193725859@qq.com> · 创建 2026-10-17 · 最近修改 2026-10-17 · 项目: maa-arknights-auto（MIT License）
<#
.SYNOPSIS
    重建并打开 MAA 挂机可视化看板（dashboard\index.html）。

.DESCRIPTION
    看板是零依赖的静态单文件，双击即可看，不需要启动任何服务。
    数据来自 logs\runs.jsonl 与各次运行的 report.json。
    每次运行结束脚本会自动重建；这里用于手动刷新或直接打开。

.EXAMPLE
    .\dashboard.ps1
    重建看板并打印路径。

.EXAMPLE
    .\dashboard.ps1 -Open
    重建后直接用默认浏览器打开。

.EXAMPLE
    .\dashboard.ps1 -NoBuild -Open
    不重建，直接打开现有看板（数据可能不是最新）。

.EXAMPLE
    .\dashboard.ps1 -IncludeDryRun
    图表统计口径也包含 dry-run 自检记录。
#>
[CmdletBinding()]
param(
    [switch]$Open,
    [switch]$NoBuild,
    [switch]$IncludeDryRun,
    [switch]$NoTaskQuery,
    [int]$MaxRuns = 0
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here '_common.ps1')
Initialize-MaaConsole

$configPath = Join-Path $here 'config.json'
$config = Get-MaaConfig -ConfigPath $configPath
$builder = Join-Path $here 'dashboard\build_dashboard.py'
$html = Join-Path $here 'dashboard\index.html'

if (-not (Test-Path -LiteralPath $builder)) {
    throw "找不到看板生成器：$builder"
}

if ($NoBuild) {
    if (-not (Test-Path -LiteralPath $html)) {
        throw "看板还没有生成过，先去掉 -NoBuild 跑一次。"
    }
    Write-Host "跳过重建，使用现有看板：$html" -ForegroundColor DarkGray
} else {
    $python = Resolve-MaaPython -Config $config
    $arguments = @($builder, '--config', $configPath)
    if ($IncludeDryRun) { $arguments += '--include-dry-run' }
    if ($NoTaskQuery) { $arguments += '--no-task-query' }
    if ($MaxRuns -gt 0) { $arguments += @('--max-runs', "$MaxRuns") }

    & $python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "看板生成失败（exit=$LASTEXITCODE）"
    }
    $item = Get-Item -LiteralPath $html
    Write-Host ("  文件：{0}" -f $item.FullName) -ForegroundColor DarkGray
    Write-Host ("  大小：{0:N1} KB    生成于 {1}" -f ($item.Length / 1KB), $item.LastWriteTime) -ForegroundColor DarkGray
}

if ($Open) {
    Start-Process -FilePath $html
    Write-Host '已用默认浏览器打开看板。' -ForegroundColor Green
} else {
    Write-Host "用默认浏览器打开：$html" -ForegroundColor Cyan
    Write-Host '或执行：.\dashboard.ps1 -Open' -ForegroundColor DarkGray
}
