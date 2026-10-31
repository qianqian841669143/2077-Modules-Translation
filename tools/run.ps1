#Requires -Version 5.1
<#
    从源码直接启动 (不打包)
        .\tools\run.ps1                      # pywebview 版
        .\tools\run.ps1 "E:\...\某个模组"     # 带模组路径
        .\tools\run.ps1 -Tk                  # 旧 Tkinter 版
#>
param(
    [string]$ModPath = "",
    [switch]$Tk
)

$root = Split-Path -Parent $PSScriptRoot
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    $guess = "C:\Users\$env:USERNAME\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
    if (Test-Path $guess) { $py = Get-Item $guess } else { throw "找不到 python.exe" }
}
$script = if ($Tk) { Join-Path $root "src\mod_translate_gui.py" }
          else      { Join-Path $root "src\web_app.py" }
if ($ModPath) { & $py.Source $script $ModPath } else { & $py.Source $script }
