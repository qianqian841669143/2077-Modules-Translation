# -*- coding: utf-8 -*-
r"""localization —— CP2077 资源模式（归档 / CR2W）翻译引擎。

与 src/locale_pack.py + mod_text_engine.py 的「源码模式」并列：
  * 源码模式：mod 里是明文 .reds / .json / .lua，按行回写。
  * 资源模式：mod 里是 .archive 里的 CR2W，需要解包、定位、重新打包。

模块分工：
    wkit        WolvenKit CLI 调用（独立进程，GPL 隔离）
    cr2w        CR2W 结构：三种 schema、entryId、文本读写
    xl          ArchiveXL 的 .xl 配置解析与生成
    archive     定位游戏、解析目标 mod、解包
    builder     组包：出 <name>.archive + <name>.archive.xl
    verifier    产物校验（离线 + 读 ArchiveXL 日志）
    pipeline    门面：Session.scan() → set_text() → build()

典型用法：

    from localization import wkit, pipeline, verifier

    cli = wkit.find_cli()
    s = pipeline.open_session(r"E:\...\archive\pc\mod\Foo.archive.xl", cli, workdir)
    meta = s.scan()                       # meta["total"] 条可翻
    row = s.rows[0]
    s.set_text(row["file"], row["entryId"], "译文")
    result = s.build(outdir=r".\output")
    print(verifier.verify_build(result, s.last_spec, cli, s.workdir).text())
"""
from __future__ import annotations

from . import archive, builder, cr2w, pipeline, verifier, wkit, xl

__all__ = ["archive", "builder", "cr2w", "pipeline", "verifier", "wkit", "xl"]

VERSION = "0.1.0"
