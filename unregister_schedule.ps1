#Requires -Version 5.1
# 作者: leo <1193725859@qq.com> · 创建 2026-10-12 · 最近修改 2026-10-12 · 项目: maa-arknights-auto（MIT License）
<#
.SYNOPSIS
    取消（删除）之前注册的 MAA 定时任务。

.EXAMPLE
    .\unregister_schedule.ps1
#>
[CmdletBinding()]
param(
    [string]$TaskName,
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here '_common.ps1')
Initialize-MaaConsole

$config = Get-MaaConfig -ConfigPath (Join-Path $here 'config.json')
if (-not $TaskName) { $TaskName = [string]$config.schedule.task_name }
if (-not $TaskName) { $TaskName = 'MAA-Arknights-Auto' }

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "没有找到计划任务：$TaskName（无需处理）" -ForegroundColor Yellow
    exit 0
}

if (-not $Yes) {
    $answer = Read-Host "确认删除计划任务 '$TaskName'？(y/N)"
    if ($answer -notmatch '^(y|Y|yes|YES)$') {
        Write-Host '已取消。' -ForegroundColor DarkGray
        exit 0
    }
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "已删除计划任务：$TaskName" -ForegroundColor Green
Write-Host '（历史运行报告仍保留在 logs 目录里）' -ForegroundColor DarkGray
