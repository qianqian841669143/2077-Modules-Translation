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

## 当前基线（2026-11-20 10:46）

- 最新提交：`83f6be9 fix(batch): 批量跑完不再被遮罩盖回来，遮罩新增「已翻译 / 总文本数」`
- `dist\2077ModTranslator.exe`：14,091,193 字节，2026-11-20 10:46，
  SHA256 前 32 位 `03F318A5DA988CD633CEEF0FE6D42E13`
- 这一版从 exe 里读回校验过 13 项：`queue.js` 有 `noMask` / `mbGlobalTxt` /
  `onMbPacking`，`events.js` 分发 `mb-packing`，`bind.js` 有「批量已经结束」，
  `web_app` 推 `mb-packing`（入口脚本在 CArchive 里，用 `marshal.loads` 读，
  不在 PYZ）
- 上一版（10:07）的改造仍然有效：撤销/还原已删、`revert_source` 不在包里、
  预扫描按文件记明细
- **装上后要点一次「预扫描」**：老 `mods_scan.json` 只有条数没有 `perFile`，
  304 个模组会被重扫一遍，展开才有文件列表

## 待办：成批「源文件不存在」导致重翻白跑

批量日志里大量模组是 `applied=0 failed=N`，错误
`base\yv\localization\*.json: 源文件不存在`（`mod_pack.py:238`），例如
`void_Shirt_Jacket` 20 条机翻一条都没写回去、`They Will Remember` 422 条只
写进 54 条。**这轮只修了遮罩，没碰这里**，下一轮单独查
`mod_pack.build_pack` 的取源路径。

