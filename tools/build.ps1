#Requires -Version 5.1
<#
    打包 2077 模组翻译器 (pywebview 版) -> dist\2077ModTranslator\
    用法:
        .\tools\build.ps1
        .\tools\build.ps1 -Python "C:\Path\python.exe"
        .\tools\build.ps1 -Entry "src\mod_translate_gui.py" -Name "2077ModTranslatorTk"   # 旧 Tk 版
    说明: 默认输出 onedir 文件夹；加 -OneFile 输出单文件 exe。
#>
param(
    [string]$Python = "",
    [string]$Entry  = "src\web_app.py",
    [string]$Name   = "2077ModTranslator",
    [switch]$OneFile
)

$ErrorActionPreference = "Stop"
$root     = Split-Path -Parent $PSScriptRoot
$src      = Join-Path $root "src"
$entry    = Join-Path $root $Entry
$icon     = Join-Path $root "assets\app.ico"
$manifest = Join-Path $root "assets\app.manifest"
$versionFile = Join-Path $root "assets\version_info.txt"
$dist     = Join-Path $root "dist"
$build    = Join-Path $root "build"

if (-not (Test-Path $entry)) { throw "找不到入口脚本: $entry" }

# ---- 找 Python (优先命令行参数, 其次 PATH, 最后已知的本地运行时)
if (-not $Python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $Python = $cmd.Source }
}
if (-not $Python) {
    $guess = "C:\Users\$env:USERNAME\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
    if (Test-Path $guess) { $Python = $guess }
}
if (-not $Python -or -not (Test-Path $Python)) { throw "找不到 python.exe, 请用 -Python 参数指定" }

Write-Host "Python : $Python" -ForegroundColor Cyan
Write-Host "Entry  : $entry" -ForegroundColor Cyan
& $Python -c "import PyInstaller, openpyxl" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "该 Python 缺少 PyInstaller 或 openpyxl, 先执行: & '$Python' -m pip install pyinstaller openpyxl"
}

# sqlite3 must stay in: localization\official_tm.py imports it for the official
# corpus (baseline TM). Excluding it silently breaks that feature in the exe.
$excludes = @(
    "numpy","pandas","PIL","lxml","matplotlib","scipy","pytest","setuptools",
    "pip","docx","pptx","unittest","pydoc_data"
) | ForEach-Object { @("--exclude-module", $_) }

$mode = if ($OneFile) { "--onefile" } else { "--onedir" }
Write-Host "打包中 ($mode) ..." -ForegroundColor Cyan

# 单文件 exe 走项目根目录那份 spec：它用 SPECPATH 拼相对路径，能入库、换机器
# 也能复现。onedir 或改名时仍走下面的命令行形式（spec 把名字和形态写死了）。
$spec = Join-Path $root "$Name.spec"
$useSpec = $OneFile -and (Test-Path $spec)
Push-Location $src
try {
    if ($useSpec) {
        Write-Host "使用 spec: $spec" -ForegroundColor Cyan
        & $Python -m PyInstaller --noconfirm --clean `
            --distpath $dist --workpath $build $spec
    } else {
        & $Python -m PyInstaller --noconfirm --clean $mode --windowed `
            --name $Name `
            --icon $icon `
            --manifest $manifest `
            --version-file $versionFile `
            --add-data "$root\src\locales;locales" `
            --add-data "$root\src\web;web" `
            --distpath $dist `
            --workpath $build `
            --specpath $build `
            @excludes `
            $entry
    }
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败 (exit $LASTEXITCODE)" }
}
finally { Pop-Location }

if ($OneFile) {
    $out = Join-Path $dist "$Name.exe"
    $size = (Get-Item $out).Length / 1MB
} else {
    $out = Join-Path $dist $Name
    $size = (Get-ChildItem $out -Recurse | Measure-Object Length -Sum).Sum / 1MB
}
Write-Host ("完成: {0}  ({1:N1} MB)" -f $out, $size) -ForegroundColor Green
