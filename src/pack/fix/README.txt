zzz CP2077 汉化补丁
====================

整个包就是一个 CET mod：mods\zzz_CP2077_Localization\

   init.lua     运行时。读账本，在 MCM 里把「模组名 / 分类名 / 设置项名 /
                描述」换成中文。只改显示层，不动任何模组源文件，也不动
                sourceId（所以已存预设、集合、快照都不会失配）。
   imgui_hook.lua
                给「自己用 ImGui 画设置页」的模组用（CTO、AMM 这一类）：
                init.lua 读完账本后会接管 ImGui 的 Text / Checkbox / Button /
                SliderFloat / BeginTabItem 等绘制函数，把文本参数查表换成
                中文。不认识的原样传下去，最坏是漏译。日志里会写
                「ImGui 钩子：接管 N 个绘制函数」，N=0 说明一个都没接上。
   names.lua    sourceId -> 中文显示名（人工整理的优先，工具只补缺的）
   ledger\      账本，由 2077 Mod Translator 生成，别手改
     mods\*.lua 一个模组一份译文表（原文 o -> 译文 z）
     all.lua    运行时汇总，init.lua 实际读这一个文件
     index.json 索引：模组 / 文件 / sourceId / 条目数

   zzz_ 前缀别去掉：CET 按目录名字母序加载，必须排在 MCM 之后，patch 它的
   provider 才轮得到。

   （2026-11-20 之前这里是两个目录——zzz_mod_text_override 放运行时、
   zzz_CP2077_mod_fix 放账本，后者原本还是第一人称反射修复。已合并。）

依赖：Cyber Engine Tweaks + ModConfigurationMenuAPI

改完账本要重启游戏——CET 只在启动时载入一次 mod 的 init.lua。

排查看日志：
    bin\x64\plugins\cyber_engine_tweaks\scripting.log
    搜 zzz_CP2077_Localization

正常启动长这样：
    [zzz_CP2077_Localization] 账本 …\ledger\all.lua：312 条
    [zzz_CP2077_Localization] names.lua：47 条模组名
    [zzz_CP2077_Localization] ImGui 钩子：接管 N 个绘制函数
    [zzz_CP2077_Localization] 已挂接 3/3 个 provider，查表 312 条

账本为空时本 mod 不生效（日志写「找不到账本」），游戏里不会报错。
