CP2077 UI Translator 1.0.0
==========================

做什么
------
游戏里很多模组把界面文字直接硬编码在 .reds 脚本里，只出英文。
这个插件在运行时把最后落到界面上的那串英文，整串换成词典里的中文。

关键点：模组文件一个字都没动。
所以模组重装、升级、换版本，译文都还在，不会像以前那样一更新就变回英文。


装法
----
把整个 "CP2077 UI Translator" 文件夹（或 zip）拖进 Vortex 安装，然后启用。
需要 RED4ext 1.0 以上（已经装过好几个 red4ext 插件了，环境是齐的）。

文件会落到：
    <游戏>\red4ext\plugins\CP2077UITranslator\
        CP2077UITranslator.dll
        dictionary.tsv


词典
----
dictionary.tsv 是纯文本，UTF-8，一行一条：

    英文原文<TAB>中文译文

以 # 开头的行是注释，不参与匹配。

想加词、改词，直接用记事本 / VS Code 打开改，存成 UTF-8，重启游戏生效。
不需要重新编译任何东西。

当前 38,833 条，来自项目里已翻过的翻译记忆（dist\userdata\memory.json）。


匹配规则
--------
只做整串精确匹配，不做子串替换。
界面上的文字和词典里的英文一个字都不差才会被换，否则原样放过。
已经是中文的（含汉字）直接跳过，不会二次替换。


日志
----
    <游戏>\red4ext\logs\cp2077uitranslator-<日期>-<时间>.log

开头几行是启动信息：

    [init] dictionary=...\dictionary.tsv
    [init] entries=38833
    [init] attach=OK
    [init] inkTextWidget class=0x...

替换成功会记前 120 条：

    [hit #1] "Cyberware" -> "义体"

之后每 500 次记一条 [hits] N。

如果一句英文在游戏里没变中文，先看日志里有没有它的 [hit]，
没有就是词典里没有这条，加进 dictionary.tsv 即可。


怎么临时关掉
------------
把 CP2077UITranslator.dll 改名成 CP2077UITranslator.dll.off，重启游戏。
或者直接在 Vortex 里停用这个模组。
