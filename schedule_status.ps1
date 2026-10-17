#Requires -Version 5.1
# 作者: leo <1193725859@qq.com> · 创建 2026-10-12 · 最近修改 2026-10-17 · 项目: maa-arknights-auto（MIT License）
<#
.SYNOPSIS
    查看 MAA 定时任务的状态、触发器、下次/上次运行结果，以及最近几次挂机结果。

.EXAMPLE
    .\schedule_status.ps1
#>
[CmdletBinding()]
param(
    [string]$TaskName,
    [int]$Recent = 5,
    [switch]$IncludeDryRun
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here '_common.ps1')
Initialize-MaaConsole

$config = Get-MaaConfig -ConfigPath (Join-Path $here 'config.json')
if (-not $TaskName) { $TaskName = [string]$config.schedule.task_name }
if (-not $TaskName) { $TaskName = 'MAA-Arknights-Auto' }

function ConvertFrom-MaaTaskResult {
    param([int]$Code)
    switch ($Code) {
        0 { '成功' }
        2 { '部分完成（有任务失败或未执行）' }
        3 { '失败' }
        4 { '环境自检失败（检查 config.json 路径）' }
        5 { '超时或被判定卡死' }
        9 { '运行器崩溃' }
        12 { '被并发锁挡下（已有运行在进行）' }
        267009 { '正在运行' }
        267011 { '尚未运行过' }
        267014 { '被用户终止' }
        default { "未知结果码" }
    }
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host ''
    Write-Host "计划任务 '$TaskName' 还没注册（执行 .\register_schedule.ps1 即可）。" -ForegroundColor Yellow
    Write-Host '下面照样会显示历史运行记录。' -ForegroundColor DarkGray
} else {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host ''
    Write-Host "计划任务：$TaskName" -ForegroundColor Green
    Write-Host "  状态     : $($task.State)"
    Write-Host "  运行账号 : $($task.Principal.UserId)（$($task.Principal.LogonType)）"
    $boundaries = @($task.Triggers | ForEach-Object { $_.StartBoundary } | Where-Object { $_ })
    $registeredTimes = @($boundaries | ForEach-Object { try { ([datetime]$_).ToString('HH:mm') } catch { } } | Where-Object { $_ })
    if ($registeredTimes.Count -gt 0) {
        Write-Host "  触发时间 : $($registeredTimes -join ' / ')（每天）"
    }

    # 关键提醒：config.json 的 schedule.times 只在「注册那一刻」被读取，
    # 事后改配置不会自动改计划任务，所以这里做漂移检测并直接给出修复命令。
    $configuredTimes = @($config.schedule.times | ForEach-Object { [string]$_ })
    if ($configuredTimes.Count -gt 0 -and $registeredTimes.Count -gt 0) {
        $drifted = Compare-Object -ReferenceObject ($configuredTimes | Sort-Object) -DifferenceObject ($registeredTimes | Sort-Object)
        if ($drifted) {
            Write-Host ''
            Write-Host '  ⚠ 配置与计划任务不一致：任务会按【计划任务】里的时间跑！' -ForegroundColor Red
            Write-Host "     config.json 里写的是 : $($configuredTimes -join ' / ')" -ForegroundColor Yellow
            Write-Host "     计划任务实际触发的是 : $($registeredTimes -join ' / ')" -ForegroundColor Yellow
            Write-Host '     修复：在本目录执行  .\register_schedule.ps1' -ForegroundColor Cyan
            Write-Host ''
        } else {
            Write-Host '  时间配置 : 与 config.json 一致' -ForegroundColor DarkGray
        }
    }
    Write-Host "  下次运行 : $($info.NextRunTime)"
    Write-Host "  上次运行 : $($info.LastRunTime)   结果：$(ConvertFrom-MaaTaskResult -Code $info.LastTaskResult)（$($info.LastTaskResult)）"
    if ($info.NumberOfMissedRuns -gt 0) {
        Write-Host "  错过次数 : $($info.NumberOfMissedRuns)（已按 StartWhenAvailable 处理，未处理的会在下次可用时补跑）" -ForegroundColor Yellow
    }
    Write-Host ''
    Write-Host "  执行动作 : $($task.Actions[0].Execute) $($task.Actions[0].Arguments)" -ForegroundColor DarkGray
}
Write-Host ''

# 最近几次运行结果（来自 logs\runs.jsonl；默认不显示干跑记录，用 -IncludeDryRun 可一并显示）
$logRoot = Join-Path $here ([string]$config.report.dir)
$indexFile = Join-Path $logRoot 'runs.jsonl'
if (Test-Path -LiteralPath $indexFile) {
    $records = Get-Content -LiteralPath $indexFile -Encoding UTF8 | Where-Object { $_.Trim() } | ForEach-Object {
        try { $_ | ConvertFrom-Json } catch { }
    }
    if (-not $IncludeDryRun) {
        $records = $records | Where-Object { $_.result -ne 'dry-run' }
    }
    $lines = @($records | Select-Object -Last $Recent)
    if ($lines.Count -gt 0) {
        Write-Host "最近 $($lines.Count) 次运行：" -ForegroundColor Green
        foreach ($run in $lines) {
            $color = switch ($run.result) {
                'success' { 'Green' }
                'dry-run' { 'Cyan' }
                'partial' { 'Yellow' }
                default { 'Red' }
            }
            Write-Host ("  {0}  {1,-10} 任务 {2}/{3}  作战 {4}  理智药 {5}  {6}" -f `
                    $run.started_at, $run.result, $run.tasks_completed, $run.tasks_total, `
                    $run.battles, $run.medicine_used, (Split-Path -Leaf $run.dir)) -ForegroundColor $color
        }
        Write-Host ''
        Write-Host "  最新报告：$($lines[-1].dir)" -ForegroundColor DarkGray
    } else {
        Write-Host '还没有真实运行记录（干跑记录不算）。先执行 .\run_now.ps1 试一次。' -ForegroundColor Yellow
    }
} else {
    Write-Host "还没有运行记录（$indexFile 不存在）。先执行 .\run_now.ps1 试一次。" -ForegroundColor Yellow
}
Write-Host ''
