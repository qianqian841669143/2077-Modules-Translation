# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（相对路径版）。

原来这份放在 build/ 里、路径全写成 D:/Module Translation/...，既是绝对路径
又跟着 build/ 一起被 .gitignore 吞掉——换台机器就复现不出同样的包。现在挪到
项目根、用 SPECPATH 拼路径，构建命令：

    python -m PyInstaller --noconfirm --clean 2077ModTranslator.spec

产物照旧落在 dist/，中间文件在 build/。
"""
import os

ROOT = SPECPATH                     # spec 所在目录 = 项目根
SRC = os.path.join(ROOT, "src")
ASSETS = os.path.join(ROOT, "assets")


def collect(root, dest):
    """把一个目录下的文件收进包里，跳过备份和开发用的假数据。

    以前这里直接写 (目录, dest)：PyInstaller 会把目录下所有文件原样搬走，
    于是 src/locales 里的 en-US.json.bak-2026… 和 src/web/__fake.js（开发时
    顶替 pywebview 的假 API）都跟着进了 exe。备份文件纯占体积，__fake.js
    更是根本不该出现在用户机器上——它虽然没被 index.html 引用、不会执行，
    但谁要是在别处引一下就是灾难。
    """
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            low = fn.lower()
            if ".bak" in low or low.endswith((".tmp", ".log", ".orig", ".swp")):
                continue
            if fn == "__fake.js":
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(dirpath, root)
            out.append((full, dest if rel == "." else os.path.join(dest, rel)))
    return out


a = Analysis(
    [os.path.join(SRC, "web_app.py")],
    pathex=[SRC],
    binaries=[],
    datas=(collect(os.path.join(SRC, "locales"), "locales")
           + collect(os.path.join(SRC, "web"), "web")),
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'pandas', 'PIL', 'lxml', 'matplotlib', 'scipy',
              'pytest', 'setuptools', 'pip', 'docx', 'pptx', 'unittest',
              'pydoc_data'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='2077ModTranslator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=os.path.join(ASSETS, "version_info.txt"),
    icon=[os.path.join(ASSETS, "app.ico")],
    manifest=os.path.join(ASSETS, "app.manifest"),
)
