# CP2077UITranslator —— 运行时 UI 文本替换插件

## 这是什么

一个 RED4ext 插件，在游戏运行时把界面上落到 `inkTextWidget` 的英文整串
换成词典里的中文。**不改任何模组文件**，所以模组重装、升级都不会丢译文。

这是取代 CET 账本方案的那条路。账本只代理 ImGui，覆盖不到 ink UI；
本插件挂在原生层，覆盖 `SetText` 那一整套。

## 产物布局

```
red4ext\plugins\CP2077UITranslator\
    CP2077UITranslator.dll
    config.ini                verbose / stat 两个开关
    dict\
        _global.tsv           兜底，12,715 条
        <模组键>.tsv           90 多片，按 module 名分
```

词典格式：`<英文原文>\t<中文译文>`，一行一条，`#` 开头为注释，UTF-8。
由 `src\ui_dict.py` 从 `dist\userdata\memory.json` 生成。

## 构建

```powershell
cmd /c "call C:\BuildTools\VC\Auxiliary\Build\vcvars64.bat >nul 2>&1 && ^
  cl /nologo /LD /std:c++20 /EHsc /O2 /utf-8 ^
  /I""D:\Module Translation\_tmp\RED4ext.SDK\include"" ^
  /Fe:CP2077UITranslator.dll Main.cpp /link user32.lib"
```

依赖 RED4ext.SDK 头文件（`_tmp\RED4ext.SDK\include`）。
产物是单个 DLL，不需要 plugin.toml —— 靠导出的 `Query` / `Main` / `Supports` 注册。

## 挂钩方式：两级

```
Main(Load)
  ├─ TryHookHandlers()
  │    从 CBaseFunction_Handlers 这张 native 实现表按 regIndex 取函数地址，
  │    只 hook 那 8 个文本函数。频率 = 界面设一次文本一次。最快，但……
  │
  └─ 失败 → InstallFallbackHook()
        hook CBaseFunction_InternalExecute（所有 redscript 调用的总入口）。
        能工作，但每帧要过 1.5 万次，实测掉 8% 帧，代价明显。
```

**两条路都必须在 `Main(Load)` 阶段完成。** 曾经试过把 fallback 挪进
`OnPostRegister` 回调里延迟执行，那次游戏必崩 —— RTI 注册回调期间 `Attach`
不安全。

## 为什么第一级从来没用上

`CBaseFunction_Handlers` 在 3.0.88 里是 null。查过游戏地址表
（`bin\x64\cyberpunk2077_addresses.json`，1,118,540 条），里面没有这个哈希。
SDK 的 `ExecuteNative` 里虽然写着 `auto func = GetHandler(GetRegIndex())`，
但游戏实际走的是 `GetInvokable()->Execute` 那条分支。

**所以现在实际运行的一定是 fallback 模式。** 日志里能看到：

```
[h] handler table still null
[h] fallback to InternalExecute hook: OK
```

## 覆盖范围

接住的 reg（`kTextRegs`）：

```
1858 SetText            1859 SetTextDirect
1860 SetLocalizedText   1861 SetLocalizedTextString
1863 SetLocalizationKey 1864 SetLocalizationKeyString
1865 SetTextFromParts   1835（第二套编号）
```

**不检查 `nativeType`**：`SetText` 系列是 `inkTextWidget` 的方法，能调用它们
的对象必然是它或子类（`inkRichTextBox` 之类），子类继承同样的字段布局，
`text` 一样在 `+0x258`。早期版本只认精确的 `inkTextWidget`，把标题那一整类
富文本控件全滤掉了。

## 翻不到的部分

| 部分 | 例子 | 原因 |
|---|---|---|
| 面板标题 | `Frame Generation SM86` | `textIdKey` 字段直设，不走任何文本函数 |
| 分类标题 | `General` / `DLSS-G` | 同上 |
| 下拉选项值 | `Auto` / `4X` | 同上 |

日志里能看到这些串命中过（`[hit] "Frame Generation SM86" -> "帧生成 SM86"`），
但界面纹丝不动 —— 说明控件有本地化键，游戏每次重绘都按 key 重新解析。
清 `textIdKey`（`+0x278`）试过，没用。

要覆盖它们只能每帧遍历 ink 界面树，性能和风险都不划算，暂时放弃。

## 性能

- `verbose=0`（默认）：日志路径完全不执行，连计数器都不碰
- `stat=0`（默认）：不统计频率
- 热路径不带 `__try/__except`：SEH 每次进出都要压/弹异常帧，而 fallback
  路径每帧走 1.5 万次，那是实打实的掉帧
- 全局 `red4ext\config.ini` 的 `flush_on` 建议 `warning`，别让日志每条都落盘

## 已知问题

- `CString` 赋值会在命中时分配一次内存，频率低（界面设置文本时才有）
- 词典里的 key 是英文原文，跨模组同名文本会互相覆盖（42 处冲突，取先加载的）
