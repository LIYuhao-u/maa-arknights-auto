# 作者: leo <1193725859@qq.com> · 创建 2026-10-12 · 最近修改 2026-10-12 · 项目: maa-arknights-auto（MIT License）
# MAA 自动挂机脚本组的公共函数（被其他 ps1 点源引入）
# 说明：这里只做「读配置 / 挑 Python / 设置控制台编码」三件事。

Set-StrictMode -Version Latest

function Initialize-MaaConsole {
    <# 让中文在 PowerShell 控制台正常显示 #>
    try {
        [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
        $global:OutputEncoding = [System.Text.Encoding]::UTF8
    } catch { }
    # 让子进程 Python 明确用 UTF-8 输出，避免控制台代码页不是 65001 时中文乱码
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUTF8 = '1'
    if ($Host.Name -eq 'ConsoleHost') {
        try { chcp 65001 | Out-Null } catch { }
    }
}

function Get-MaaConfig {
    <# 读取 config.json 并返回对象；路径不存在直接抛错 #>
    param(
        [Parameter(Mandatory = $true)][string]$ConfigPath
    )
    if (-not (Test-Path -LiteralPath $ConfigPath)) {
        throw "找不到配置文件：$ConfigPath"
    }
    return (Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Resolve-MaaPython {
    <# 按 配置 > PATH > 常见安装目录 的顺序找一个 Python 3.10+；
       -PreferWindowless 时返回同目录下的 pythonw.exe（计划任务静默运行用） #>
    param(
        [Parameter(Mandatory = $true)]$Config,
        [switch]$PreferWindowless
    )

    $candidates = New-Object System.Collections.Generic.List[string]
    if ($Config.run.python_path) { $candidates.Add([string]$Config.run.python_path) }
    foreach ($name in @('python.exe', 'python3.exe')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $candidates.Add($cmd.Source) }
    }
    $candidates.Add((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'))
    $candidates.Add((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'))
    $candidates.Add((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'))

    $tried = @()
    foreach ($candidate in $candidates) {
        if (-not $candidate -or -not (Test-Path -LiteralPath $candidate)) { continue }
        $tried += $candidate
        try {
            $probe = & $candidate -c "import sys;print(1 if sys.version_info >= (3, 10) else 0)" 2>$null
        } catch {
            continue
        }
        if ("$probe".Trim() -ne '1') { continue }

        if ($PreferWindowless) {
            $windowless = Join-Path (Split-Path -Parent $candidate) 'pythonw.exe'
            if (Test-Path -LiteralPath $windowless) { return $windowless }
        }
        return $candidate
    }

    throw ("找不到可用的 Python 3.10+（MAA 的 Python 接口需要 3.10 及以上）。`n" +
        "已尝试：`n  " + ($tried -join "`n  ") + "`n" +
        "请在 config.json 的 run.python_path 里填写完整的 python.exe 路径。")
}
