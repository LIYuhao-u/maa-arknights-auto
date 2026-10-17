#Requires -Version 5.1
# 作者: leo <1193725859@qq.com> · 创建 2026-10-17 · 最近修改 2026-10-17 · 项目: maa-arknights-auto（MIT License）
<#
.SYNOPSIS
    创建 / 删除 A+ 方案的桌面一键入口（3 个快捷方式）。

.DESCRIPTION
    A+ = 静态看板 + 桌面快捷方式，**不常驻任何进程、不开端口**。
    生成的快捷方式：
      · MAA 立即运行   → 跑一次完整流程（约 15~25 分钟，会弹控制台看进度）
      · MAA 打开看板   → 重建并打开 dashboard\index.html
      · MAA 定时状态   → 看定时任务状态与最近结果（窗口保持不关）

    重复执行是幂等的（覆盖同名快捷方式）。

.EXAMPLE
    .\make_shortcuts.ps1
    创建三个桌面快捷方式。

.EXAMPLE
    .\make_shortcuts.ps1 -Remove
    删除这三个快捷方式（不动工程本身）。

.EXAMPLE
    .\make_shortcuts.ps1 -DesktopDir 'D:\我的桌面'
    指定快捷方式放到哪里（默认用当前用户的桌面）。
#>
[CmdletBinding()]
param(
    [switch]$Remove,
    [string]$DesktopDir
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here '_common.ps1')
Initialize-MaaConsole

if (-not $DesktopDir) {
    $DesktopDir = [Environment]::GetFolderPath('Desktop')
}
if (-not (Test-Path -LiteralPath $DesktopDir)) {
    throw "桌面目录不存在：$DesktopDir"
}

$config = Get-MaaConfig -ConfigPath (Join-Path $here 'config.json')

# 图标来源：优先用 MAA 自带图标，其次系统图标
$maaIcon = Join-Path ([string]$config.maa.install_dir) 'MAA.exe'
$iconRun = if (Test-Path -LiteralPath $maaIcon) { $maaIcon } else { "$env:SystemRoot\System32\shell32.dll" }
$iconBoard = "$env:SystemRoot\System32\shell32.dll"
$iconState = "$env:SystemRoot\System32\shell32.dll"

$items = @(
    @{
        Name = 'MAA 立即运行'
        Script = 'run_now.ps1'
        Args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $here 'run_now.ps1'))
        Icon = "$iconRun,0"
        Desc = '立即跑一次 MAA 自动挂机（已有运行在进行时会自动退出）'
    },
    @{
        Name = 'MAA 打开看板'
        Script = 'dashboard.ps1'
        Args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $here 'dashboard.ps1'), '-Open')
        Icon = "$iconBoard,13"
        Desc = '重建并打开 MAA 运行看板'
    },
    @{
        Name = 'MAA 定时状态'
        Script = 'schedule_status.ps1'
        Args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-NoExit', '-File', (Join-Path $here 'schedule_status.ps1'))
        Icon = "$iconState,21"
        Desc = '查看 MAA 定时任务状态与最近运行结果'
    }
)

$shell = New-Object -ComObject WScript.Shell

foreach ($item in $items) {
    $lnk = Join-Path $DesktopDir ($item.Name + '.lnk')
    if ($Remove) {
        if (Test-Path -LiteralPath $lnk) {
            Remove-Item -LiteralPath $lnk -Force
            Write-Host "已删除：$lnk" -ForegroundColor Yellow
        } else {
            Write-Host "不存在：$lnk" -ForegroundColor DarkGray
        }
        continue
    }

    $argumentLine = ($item.Args | ForEach-Object {
            if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
        }) -join ' '

    $shortcut = $shell.CreateShortcut($lnk)
    $shortcut.TargetPath = (Join-Path $PSHOME 'powershell.exe')
    $shortcut.Arguments = $argumentLine
    $shortcut.WorkingDirectory = $here
    $shortcut.IconLocation = $item.Icon
    $shortcut.Description = $item.Desc
    $shortcut.WindowStyle = 1
    $shortcut.Save()
    Write-Host ("已创建：{0}" -f $lnk) -ForegroundColor Green
    Write-Host ("          → powershell.exe {0}" -f $argumentLine) -ForegroundColor DarkGray
}

if (-not $Remove) {
    Write-Host ''
    Write-Host '说明：' -ForegroundColor Cyan
    Write-Host '  · 这些快捷方式只是"多一次双击"，不常驻进程、不开端口、不需要管理员。'
    Write-Host '  · 「立即运行」如果撞上一次正在进行的运行，会直接退出并提示（并发保护，退出码 12）。'
    Write-Host '  · 想删除：.\make_shortcuts.ps1 -Remove'
}
