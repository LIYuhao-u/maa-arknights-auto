#Requires -Version 5.1
# 作者: leo <1193725859@qq.com> · 创建 2026-10-12 · 最近修改 2026-10-12 · 项目: maa-arknights-auto（MIT License）
<#
.SYNOPSIS
    注册/更新「定时运行 MAA 明日方舟」的 Windows 计划任务。

.DESCRIPTION
    默认按 config.json 里的 schedule 段注册：
      - 每天在 schedule.times 列出的每个时间点各触发一次（例如 04:10 / 12:10 / 20:10）
      - 仅在当前用户登录时运行（雷电模拟器需要交互式桌面会话才能正常渲染）
      - StartWhenAvailable：错过的触发（例如当时关机）在可用时补跑
      - WakeToRun：允许唤醒睡眠中的电脑（按 config.json 开关）
      - MultipleInstances=IgnoreNew：上一次还没跑完时跳过本次触发，避免两个实例抢模拟器

    运行方式为静默：优先使用 pythonw.exe，没有则用隐藏窗口的 powershell 包一层。
    运行日志全部写在 logs\<run_id>\ 里，用 .\schedule_status.ps1 或看 logs\runs.jsonl 均可。

.EXAMPLE
    .\register_schedule.ps1
    按 config.json 的时间点注册（已存在则覆盖）。

.EXAMPLE
    .\register_schedule.ps1 -Times '04:10','16:30'
    临时指定时间点注册，不改 config.json。

.EXAMPLE
    .\schedule_status.ps1
    查看任务状态与最近一次运行结果。
#>
[CmdletBinding()]
param(
    [string[]]$Times,
    [string]$TaskName,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here '_common.ps1')
Initialize-MaaConsole

$configPath = Join-Path $here 'config.json'
$config = Get-MaaConfig -ConfigPath $configPath

if (-not $TaskName) { $TaskName = [string]$config.schedule.task_name }
if (-not $TaskName) { $TaskName = 'MAA-Arknights-Auto' }

if (-not $Times -or $Times.Count -eq 0) {
    $Times = @($config.schedule.times | ForEach-Object { [string]$_ })
}
if (-not $Times -or $Times.Count -eq 0) {
    throw '没有可用的触发时间点：请在 config.json 的 schedule.times 里配置，或用 -Times 传入。'
}

# 校验时间格式并转成触发器
$triggers = @()
foreach ($time in $Times) {
    if ($time -notmatch '^\d{1,2}:\d{2}$') {
        throw "时间格式不对：'$time'（应为 HH:mm，例如 04:10）"
    }
    $at = [datetime]::ParseExact($time, 'HH:mm', [System.Globalization.CultureInfo]::InvariantCulture)
    $trigger = New-ScheduledTaskTrigger -Daily -At $at
    $randomDelay = [int]$config.schedule.random_delay_minutes
    if ($randomDelay -gt 0) { $trigger.RandomDelay = "PT${randomDelay}M" }
    $triggers += $trigger
}

# 动作：静默运行 maa_runner.py
$python = Resolve-MaaPython -Config $config -PreferWindowless
$runner = Join-Path $here 'maa_runner.py'
$argsLine = "`"$runner`" --config `"$configPath`" --mode scheduled"
if ($python -match 'pythonw\.exe$') {
    $action = New-ScheduledTaskAction -Execute $python -Argument $argsLine -WorkingDirectory $here
} else {
    Write-Warning "没有找到 pythonw.exe，将用隐藏窗口的 powershell 包装运行：$python"
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"& '$python' '$runner' --config '$configPath' --mode scheduled`"" `
        -WorkingDirectory $here
}

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable:([bool]$config.schedule.start_when_available) `
    -WakeToRun:([bool]$config.schedule.wake_to_run) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours ([double]$config.schedule.execution_time_limit_hours))

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and -not $Force) {
    Write-Host "已存在同名任务：$TaskName" -ForegroundColor Yellow
    Write-Host '（本次将直接覆盖它；如需改名请用 -TaskName）' -ForegroundColor DarkGray
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Description 'MAA + 雷电模拟器 明日方舟 定时自动挂机（由 maa-arknights-auto 生成；报告见 logs 目录）' `
    -Force | Out-Null

$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ''
Write-Host "已注册计划任务：$TaskName" -ForegroundColor Green
Write-Host "  触发时间 : $($Times -join ' / ')（每天）"
Write-Host "  运行账号 : $env:USERDOMAIN\$env:USERNAME（仅登录时运行）"
Write-Host "  唤醒电脑 : $([bool]$config.schedule.wake_to_run)    错过补跑 : $([bool]$config.schedule.start_when_available)"
Write-Host "  运行命令 : $python"
Write-Host "  下次运行 : $($info.NextRunTime)"
Write-Host ''
Write-Host '想立即验证一次而不等到时间点：.\run_now.ps1' -ForegroundColor Cyan
Write-Host '查看状态与最近结果      ：.\schedule_status.ps1' -ForegroundColor Cyan
Write-Host '取消定时                ：.\unregister_schedule.ps1' -ForegroundColor Cyan
