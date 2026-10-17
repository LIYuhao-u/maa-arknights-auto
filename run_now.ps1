#Requires -Version 5.1
# 作者: leo <1193725859@qq.com> · 创建 2026-10-12 · 最近修改 2026-10-17 · 项目: maa-arknights-auto（MIT License）
<#
.SYNOPSIS
    立即手动运行一次 MAA 自动挂机（前台显示实时日志）。

.EXAMPLE
    .\run_now.ps1
    启动雷电模拟器 → 等 adb → 驱动 MAA 执行任务队列 → 退出模拟器 → 打印报告

.EXAMPLE
    .\run_now.ps1 -DryRun
    干跑：只做环境自检、打印将要执行的任务队列并生成一份报告，不启动模拟器、不消耗游戏资源
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$KeepEmulator
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here '_common.ps1')
Initialize-MaaConsole

$configPath = Join-Path $here 'config.json'
$config = Get-MaaConfig -ConfigPath $configPath
$python = Resolve-MaaPython -Config $config
$runner = Join-Path $here 'maa_runner.py'

$arguments = @($runner, '--config', $configPath, '--mode', 'manual')
if ($DryRun) { $arguments += '--dry-run' }
if ($KeepEmulator) { $arguments += '--keep-emulator' }

Write-Host "Python : $python" -ForegroundColor DarkGray
Write-Host "配置   : $configPath" -ForegroundColor DarkGray
if ($DryRun) { Write-Host "模式   : 干跑（不会启动模拟器、不会执行任务）" -ForegroundColor Yellow }
Write-Host ''

& $python @arguments
$code = $LASTEXITCODE

Write-Host ''
switch ($code) {
    0 { Write-Host "运行成功（exit=0）" -ForegroundColor Green }
    2 { Write-Host "部分完成：有任务失败或未执行（exit=2）" -ForegroundColor Yellow }
    3 { Write-Host "运行失败（exit=3）" -ForegroundColor Red }
    4 { Write-Host "环境自检失败：检查 config.json 里的路径（exit=4）" -ForegroundColor Red }
    5 { Write-Host "超时或被判定卡死（exit=5）" -ForegroundColor Yellow }
    12 { Write-Host "已有一次运行在进行（并发保护），本次未启动（exit=12）" -ForegroundColor Yellow }
    default { Write-Host "未知退出码（exit=$code）" -ForegroundColor Red }
}
exit $code
