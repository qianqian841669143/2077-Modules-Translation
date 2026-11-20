# 项目规则（给 AI 代理）

这是用户的生产工具，不是练习场。`dist\2077ModTranslator.exe` 是他实际运行的成品，
`src\` 是它的源码，改源码不重新打包等于什么都没改。

## 铁律

1. **没有本轮的明确批准，一个字都不改。**
   「检查一下」「看看为什么」「分析下」都是只读请求；等用户说「改」「可以」「动手」
   再落笔。有疑问就问，别自己替用户做决定。

2. **禁止用 `write` 覆盖已存在的文件。**
   新建文件可以用 `write`；修改任何已存在的文件，**只能用 `edit` 做定点替换**，
   不论它多少行。替换前必须先读到目标区域，一次只替换一处。
   绝不能用「写整个文件」的方式重排、重写、顺手美化。
   2026-11-10 就是这么把 2900 行的 `mod_text_engine.py` 写成了 98 行，工具当场报废。

3. **先留退路再动手。**
   改之前确认 `git status` 干净、能 `git checkout HEAD -- <路径>`；
   否则先 `Copy-Item <文件> <文件>.bak-<时间>`。

4. **不清理、不删除用户的东西。**
   `dist\userdata\`（翻译记忆、术语表、日志）、`E:\Vortex Mods\` 下的模组、
   `docs\` 都不动。临时脚本只删自己建的，用完就删。

5. **打包要单独申请。**
   重建 `dist\2077ModTranslator.exe` 会替换用户正在用的程序，属于改变运行时，
   同样先问。打完包要能从 exe 里读回代码对象，证明新符号真的进去了。

6. **一次只改一件事。**
   报错先定位、先解释；别「顺手再试一处」。连续两次没修好就停下来汇报，
   而不是继续猜着改。

7. **改完要提交。**
   每轮对 `src\` 的改动做完之后跑 `git add` + `git commit`，提交信息写清
   「改了什么、为什么这么改」，格式沿用 `type(scope): 中文说明`。
   工作区长期压着几十个未提交改动，回滚和对比都会失去意义——铁律 3 的
   「先留退路」也就成了空话。提交前确认备份文件没被带上：`*.bak-*` 和
   `src/_bak/` 已经在 `.gitignore` 里。

## 改完必须自证

```powershell
python -c "import py_compile; py_compile.compile(r'<文件>', doraise=True)"
git diff --stat
```

- **必须贴 diff。** 回复里至少给 `git diff --stat`，以及每个改动文件的
  `git diff <文件>` 关键片段。没贴 diff 一律视为未完成，不许说「改好了」。
- 回复里给具体证据：行号、字节数、时间戳、校验结果
- 不要只说「已完成」「已修复」——用户没法验证，只能再问一遍

## 已知的坑

- 运行时是 exe，不是 `src\`。用户报错时先看 `dist\userdata\logs\apply.log`
- 源码损坏的恢复路径：`git checkout HEAD -- src/<文件>`
- `mod_translate_gui.py` 不参与打包，入口是 `web_app.py`

## 已完成：删掉「还原翻译」和「撤销应用」（2026-11-20）

用户原话：「还原翻译 和撤销应用都可以删除了，下次修改的时候一起改即便我没提，
你改完说一声就行。」——已按下面的清单做完，提交 `1177b86`。**别再「找回」这两个
功能。** 现在要退一次应用，就是在 Vortex 里卸掉那个 `<模组名>_<语言>.zip`。

删掉的东西（`git show 1177b86` 可查）：

- 前端：菜单里两个按钮、`bind.js` 两个 onclick、`apply.js` 111 行（
  `undoLastApply` / `onUndone` / `revertMod` / `onReverted`）、`events.js` 的
  `undone`/`reverted` 分发与两个失败分支、`table.js` 的 `btn-undo` 显示逻辑、
  `core.js` 的 `state.revertUndo`
- 后端：`web_app.py` 里 `undo_last` / `_undo_work` / `undo_status` /
  `revert_plan` / `revert_mod` / `_revert_work`、`l10n_undo_root`、`_revert_cache`、
  所有 `undoId` / `undoAvailable` 返回字段、`_apply_resource`/`_apply_mixed`/
  `_do_apply_source` 的 `tx` 参数、`make_undo` 参数
- `src/revert_source.py` 整个文件
- 建快照的三处：`mod_deploy.deploy_mod`（`undo_root=` 参数没了）、
  `_apply_mixed`、`_do_apply_source`
- `cache_tools.py` 的「撤销记录」项**保留了**，改叫「旧撤销记录」——
  `dist\userdata\undo\` 里的历史快照还在，得留个入口让用户清掉

**保留** `src/undo_store.py`（模块在，只是没人调用）和 `dist\userdata\undo\`
里的历史数据（铁律 4）。

**已知残留**：`tools\selftest.py` 里还有 4 处针对已删功能的断言
（`check_revert_source` 整个函数、`undo_store.undo_last` 调用、
`"def revert_plan" in src` 之类的源码文本断言），跑 selftest 会失败。
selftest 本来就因为引用已删除的 `src\web\app.js` 跑不通，下次整理工具时一并清。

## 预扫描现在会记文件明细（2026-11-20，提交 `9c1ddec`）

模组清单展开时列出的文件，有两个来源：有账本的读账本（`mod_ledger.file_stats`），
没账本的读预扫描缓存 `userdata\mods_scan.json`。后者以前只存总条数，展开只能显示
一句空话（`没有未填的条目`）——跟左边 `0 / 215` 自相矛盾。

现在 `mod_batch.probe_text()` 多返回一个 `perFile`（`[{file, total, todo}]`，
按待翻条数降序），`mods_prescan` 存成 `[路径, 总条数, 待翻条数]` 三元组，
`Api._scan_files()` 读回来给前端。`_scan_need()` 顺带补扫旧缓存：**只有条数、
没有 `perFile` 的条目会被重扫**，所以升级后点一次「预扫描」就能把 304 个模组的
文件列表补齐（要跑几分钟，属正常）。

前端展开时**所有文件都列**，已翻完的也列，只是标「已翻完」；空列表按
`orphan` / 未扫过 / 只有条数 / 账本里没有可翻文本四种情况分别给话。

## 批量收尾：遮罩不许自己盖回来（2026-11-20，提交 `83f6be9`）

10:35 那轮 309/309 跑完，zip 也并在 10:35:37 完成了，界面却永远停在
「批量翻译进行中…」，整窗 `inert`，只能关程序。是竞态，不是卡死：

- `mod_batch.py:616` 的 `mb-done` 在 `_flush_pack()` **之前**就发出
- `web_app.py` 把 `_mb_running` 置 False 在并包**之后**（每晚约 4 秒）
- `onMbDone` 关掉遮罩后又调 `refreshQueueInfo()`，此刻问 `batch_info` 仍是
  `running=true` → `enterBatchMask()` 把遮罩重新盖上，此后再无事件能摘

两个判断「真卡住」的指纹：计数还写着 `309 / 309`（`enterBatchMask` 只在
`state.mbIndex` 为空时重写数字），但第三行模组名是空的（它会被清掉）。

改法：`refreshQueueInfo(noMask)`，`onMbDone` 传 `true`；「停止批量」调完
`batch_cancel` 再问一次 `batch_info`，`!running` 就直接 `exitBatchMask()`
（原来只置标志位就返回，worker 早已结束时没有 `mb-done` 跟回来，按钮变灰
就再也出不来）。

遮罩第三行和并包那句话里的「已翻译 X / Y 条」：`X = Σ(already + applied)`，
`Y = Σ(rows)`，与 `mod_batch.finish()` 里 `already`（本来就是中文）/
`applied`（这轮真写回去）的定义一致。前端那行是**本轮涉及**的累计（被续跑
跳过的模组不会计入），并包那句用后端从 `_mb_results` 算的准数。

## 批量遮罩改成按「条」显示进度（2026-11-20）

用户原话：「批量翻译遮罩的进度看不到啊 … 不是模组数量，是总条数的那个」。
原来的遮罩只有两样东西在动，都不是他要的：

- `ov-count` / `ov-bar`：**第几个模组 / 共几个**（`onMbItem` 的 `done/total`）
- 第三行 `ov-now`：当前模组名 + 这个模组内部的 `done / total 条`
- 全局条数只有 `mbGlobalTxt()`，而且只在某个模组**跑完之后**追加到第三行，
  分母 `Y = Σ(rows)` 只累计已完成的模组 → 分母自己会长，根本不是进度条

现在：

- `mod_batch.run()` 的 `mb-start` 事件多带一个 `textTotal`
- `web_app.batch_start()` 用 `_scan_cache_read(staging)` 给每个 item 填
  `textTotal = 缓存里的 entries`（预扫写进 `userdata\mods_scan.json` 的总条数，
  含本来就是中文的行）。没扫过的目录是 0，前端整块退回按模组数显示
- 前端 `renderMaskCount()`（`queue.js`）：`X = 已完成模组的 already+applied +
  当前模组机翻进度`，`Y = max(预扫条数, 已完成模组的实际 rows)`，`ov-count`
  显示 `已翻译 X / Y 条（M / N 个模组）`，`ov-bar` 按 X/Y 走
- `mbGlobalTxt()` 删了（重复的第二个条数）

**那个 `max()` 不能省**：预扫只看明文层，归档里的行它看不见。实测 Smart Vendor
预扫只给出 2 条，实际有 33 条（31 条在 `.archive` 里）；只用预扫值的话，模组
刚开始跑进度条就 100%。用已完成模组的实际 `rows` 托底，分母会随进度长大，
但不会说谎。X 超过 Y 时显示按 Y 封顶。

**这个改动要重新打包 exe 才生效**（exe 里内联了 `queue.js` / `events.js` 和
`web_app.py` / `mod_batch.py`）。

## 归档层曾经整批写不进去（2026-11-20，提交 `d9fa20e`）

10:09 那轮 `archive=True`，309 个模组里 **87 个、4,387 条归档文本被机翻完就丢**。
日志症状是 `base\yv\localization\*.json: 源文件不存在`——这 132 个路径磁盘上一个
都没有，它们是**归档内部**的路径（WolkenKit 解开后落在
`userdata\work\<模组>\raw\<内部路径>`）。

链条，别再犯：

- `localization/cr2w.py` 给归档行打的 `kind = "resource"`，`file` 是归档内路径
- `mod_pack.route()` **只看扩展名和路径串**，`localization/` 命中 `_LOCAL_HINTS`
  就判成 `pack`
- `build_pack()` 于是去模组目录找那个文件 → 不存在 → 整批计入 fail
- `route()` 只对 `.archive`/`.xl` 返回 `"archive"`，而行永远不带这种扩展名，
  所以 `classify()["archive"]` **恒为空**：`_apply_all` 里重建归档的分支、
  `_do_apply` 里按 `kind` 分流的代码，全都是死代码——批量和单模组「应用」
  都写不进去

现在：`classify()` 见 `kind == "resource"` 直接进 `archive` 通道；批量把它交给
`w._apply_resource()` 重建 `<名字>_zh-cn.archive` + `.xl`，产物塞进
`_pack_pending_state()`，由 `_flush_pack` 跟覆盖文件一起并进 zip（不要每个模组
单独 `_merge_archive_into_zip`，那等于把总包重写 87 遍）。

**续跑**：`_mb_journal_read()` 现在把 `failed > 0` 的记录排除在「已完成」之外。
否则修好写回之后，那批模组会被整块跳过，永远补不上。

## 运行时接管 ImGui，自绘设置页也能翻（2026-11-20，提交 `7f66ca2`）

现象：CTO（Complete Third Person Overhaul）装了单模组包、也 Deploy 了，游戏里
设置页还是全英文。查下来不是部署也不是译文的问题：

- 这个模组的设置页是**自己用 ImGui 画的**：`M.UI.check` = `ImGui.Checkbox`、
  `M.UI.head` = `ImGui.TextColored`、`M.UI.tip` = `ImGui.SetTooltip`
  （`...\mods\CompleteThirdPersonOverhaul\init.lua:6475-6481`）
- 全包 4 个子模组搜 `nativeSettings` / `ModConfigurationMenu` / `addTab(`，
  **一次都没有**
- 而运行时 `init.lua` 只挂 `MCM.providers.*.listMods/loadMod`，全文没有一处
  `ImGui` → 这类界面一条也替不到

改法：

- 新增 `src/pack/fix/imgui_hook.lua`：把「第一个参数是显示文字」的绘制函数
  （`Text`/`TextWrapped`/`Checkbox`/`Button`/`Selectable`/`MenuItem`/
  `SliderFloat`/`SliderInt`/`Combo`/`BeginCombo`/`CollapsingHeader`/
  `BeginTabItem`/`TreeNode`/`RadioButton`/`InputText`/`SetTooltip` …）和
  `TextColored`（文字在最后一位）包一层，调用前用账本的 `globalMap` 换整串。
  **`Begin`/`End`/`BeginChild`/`PushID` 传的是 id，一律不碰**（改了会串窗口）。
- `src/pack/fix/init.lua` 读完账本后 `loadfile` 钩子并 pcall 调用，日志写
  `ImGui 钩子：接管 N 个绘制函数`。

**排障第一个要看的是那行日志**：`N > 0` 说明接管成功；`N = 0` 说明 CET 的
`ImGui` 表是只读的，这条路走不通，得改回「改字面量当覆盖文件」（风险大，
445 KB 的脚本改错一处模组就废）。

运行时现在**纳入仓库**了（`src/pack/fix/`）。以前只活在
`userdata\pack\fix\`，没进版本控制、改完没 diff、没法回滚。`_patch_map()`
以仓库这份为准，`userdata\pack\fix` 只补内置没有的文件。`spec` 里
`collect(SRC/pack, "pack")` 把它当数据目录打进去。

## 汉化包不再投递逻辑脚本（2026-11-20，提交 `9e61d31`）

11:28 那次启动，RedRequire 弹「您的模组脚本未编译」，redscript 报 214 条错，
一条都进不了游戏。查下来是**我们自己**造成的：

- 包里有 301 个 `.reds`，全是「某个模块的一部分编译单元」
- 游戏目录里 7 个目录（TrueFirstPersonCamera / BetterNetrunning /
  DigitalVixenCore / ITP_TFPC_Compat / EnemyMultiplierRS / TakedownSystem /
  immersive_third_person）**只剩我们的副本，模组原件一个都没部署**
- 于是 `module X` 的声明文件不在 → `UNRESOLVED_IMPORT` → 整条依赖链崩
- 对话框里那句「TakedownSystem 被安装了两次」也是：用户根本没启用独立
  TakedownSystem，是我们投了它的 `Playback.reds`，撞上 CTO 自带的
  `XnTdProbe.reds`（`SYM_REDEFINITION`）

现在 `.reds` 分两类：

| 类型 | 判据 | 处置 |
|---|---|---|
| Codeware 本地化包 | 内容含 `ModLocalizationPackage` / `ModLocalizationProvider` | 允许进包 |
| 逻辑 / UI 脚本 | 其余全部 | 一条都不投，文案只进账本 |

- `mod_pack.is_localization_text/path()`：判据就这两个基类，别再加
- `classify()` 先**按目录**预扫（`lang_dirs`）：整目录都是本地化包才放行，
  单文件判不够 —— 目录里还有 `LocalizationProvider` 之类没被翻的兄弟
- `build_pack()` 有兜底闸门，非本地化包的 `.reds` 直接跳过并记 error
- `_sync_ledger_to_zip()` 会调 `_bad_reds_names()` 把 zip 里**旧的**逻辑
  `.reds` 删掉（不删的话重装一次 zip 又全回来了）

**批量与单模组「应用」共用 `classify()`**，所以两条路一起生效；单模组产物由
`_build_standalone_zip()` 每次全量重写，不会残留旧条目。

代价（量化过）：包里 `.reds` 去重中文串 14,148 条，其中 12,632 条（89.3%）
账本里也有对应条目；只剩 1,516 条只在 `.reds`，抽样看大半是 `log()`/`print()`
的调试输出。实测分流：MuchBetterContracts 只放行 `Localization` 两个目录
（12 个文件），BetterNetrunning 只放行 `Localization`（2 个），
TrueFirstPersonCamera 与 CTO 的 `.reds` 全部走账本。

## 运行时找不到账本：CET 沙箱只认裸相对路径（2026-11-20，提交 `dfcf0d3`）

`scripting.log` 里三次 `[zzz_CP2077_Localization] 找不到账本…本 mod 不生效`
（11:01:53 / 11:26:18 / 11:39:50），而 `ledger/all.lua` 明明在。两个实测结论：

- CET 里 `tostring(package.path)` 是**空串**（02:27 那次旧 mod 的日志可查），
  所以 `findLedger()` 靠 `package.path` 推路径那一段永远拿不到候选
- CET 1.38 的 `io.open` 是沙箱：**只认「相对本 mod 目录」的路径**。带
  `mods/`、`plugins/cyber_engine_tweaks/mods/` 前缀的相对路径会被拒
  （`Tried to access invalid path 'plugins/…'`）；社区 mod 自己也这么写
  （`NovaOptics` 用 `io.open("settings.json")`，文件就在 mod 目录里）

改法：`findLedger()` 第一个候选是裸的 `"ledger/all.lua"`；新增
`relPath(base, name)`，`base` 为空串时返回裸文件名（`names.lua` /
`imgui_hook.lua` / `text_override.log` 全走它）。

**排障先看 `scripting.log`**：改对了会打出
`账本 ledger/all.lua：N 条` 和 `ImGui 钩子：接管 M 个绘制函数`；
还打「找不到账本」就是路径又不对。

## 当前基线（2026-11-20 12:08）

- 最新提交：`cf51a8e fix(runtime): ImGui 是 userdata，判 table 导致钩子一个都没接上`
  （前一个：`10701b4 fix(runtime): MCM 设置项在 cat.settings 里，不是 cat.items`）
- `dist\2077ModTranslator.exe`：14,103,597 字节，2026-11-20 12:08:08，
  SHA256 前 32 位 `849BE07CB31FC17CEE1DF98177516890`
  备份 `dist\2077ModTranslator.exe.bak-20261120-1207`（12:07 那版 14,102,762 B；
  再往前是 `bak-20261120-1154`，11:54 那版）
- 从 exe 里读回：`init.lua` 13,083 B（`cat.settings` / `cat.items` 回退、
  `add("ledger/all.lua")`、`relPath`）、`imgui_hook.lua` 4,336 B
  （`kind ~= "userdata"`、`local seen = 0`）
- 11:54 那版的两条修复都在：`.reds` 只投本地化包、账本走裸相对路径
- **MCM 文本链**：MCM UI 读 `ModConfigurationMenuAPI`，API 的 provider 来自三个
  bridge（`native_settings` / `ms` / `rcf`），**设置项一律放在
  `categories[i].settings`**（不是 `items`），字段是 `label` / `description`。
  改这段之前先看 `bridges/*/provider.lua`，别照老结构猜
- 账本覆盖率：Codeware「原生设置」的 2,540 个 displayName/description 里，
  1,559 条（61.4%）能在账本里查到原文；没命中的多是数字与含引号嵌套的串
- 装上新包后：`sync` 一次 → Vortex 重装 zip → Deploy → 重启游戏；
  日志应见 `账本 ledger/all.lua：N 条` + `ImGui 钩子：接管 M 个绘制函数`
  （M=0 时新版会打出「读到 K 个绘制函数，但一个都写不回去」，据此判断
  是类型不对还是只读）

## 待办：产物名字会撞车

`mod_ledger.folder_name()` 按「文本文件最多的子目录」投票。合集
`CTO_takedowns-0.24.1_hotfix`（内含 4 个 CET 子模组）和主模组
`Complete Third Person Overhaul 38266 …` **都会推成 `CompleteThirdPersonOverhaul`**
（实测两个 root 都返回这个名）。后果：产物 zip 同名、`ledger/mods/<folder>.lua`
同名、`all.lua` 里 `['CompleteThirdPersonOverhaul']` 是同一段 → 后写的挤掉先写的。

改法：根目录下有多个 CET 子模组时改用 staging 目录名做 folder 键与产物名。
**这轮没改**。



