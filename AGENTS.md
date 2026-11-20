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

## 当前基线（2026-11-20 11:20）

- 最新提交：`7f66ca2 feat(runtime): 运行时接管 ImGui 绘制函数，自绘设置页的模组也能出中文`
- `dist\2077ModTranslator.exe`：14,099,945 字节，2026-11-20 11:20:05，
  SHA256 前 32 位 `5736BE3E8B1C926F0054E7734DD58656`
- 这一版从 exe 里读回：CArchive 有 `pack\fix\{init.lua,imgui_hook.lua,README.txt}`，
  `init.lua` 正文含 `imgui_hook.lua` 的 loadfile 调用
- 10:59 / 10:46 两版的修复仍在：归档行走进组包通道、批量跑完不被遮罩盖回来
- **装上后**：`汉化包`页点「同步账本」就能把新运行时并进 `CP2077 Localization
  Pack.zip`（`_sync_ledger_to_zip` 走 `_patch_map`），不必重跑整轮批量；
  单模组的包要重新「应用」一次才有 `imgui_hook.lua`
- 装完 zip 要在 Vortex 里 Deploy，然后重启游戏（CET 只在启动时读一次
  `init.lua`）

## 待办：产物名字会撞车

`mod_ledger.folder_name()` 按「文本文件最多的子目录」投票。合集
`CTO_takedowns-0.24.1_hotfix`（内含 4 个 CET 子模组）和主模组
`Complete Third Person Overhaul 38266 …` **都会推成 `CompleteThirdPersonOverhaul`**
（实测两个 root 都返回这个名）。后果：产物 zip 同名、`ledger/mods/<folder>.lua`
同名、`all.lua` 里 `['CompleteThirdPersonOverhaul']` 是同一段 → 后写的挤掉先写的。

改法：根目录下有多个 CET 子模组时改用 staging 目录名做 folder 键与产物名。
**这轮没改**。



