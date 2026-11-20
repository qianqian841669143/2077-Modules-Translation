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

## 待办（用户已批准，下次改动时一并做）

### 删掉「还原翻译」和「撤销应用」（2026-11-20 用户授权）

用户原话：「还原翻译 和撤销应用都可以删除了，下次修改的时候一起改即便我没提，
你改完说一声就行。」——**不用再申请批准，动手时顺手一并删，删完汇报即可。**

这两个功能在同一条链上：`revert_source` 判断「哪些文件被本工具碰过」靠的正是
`undo_store` 的快照，所以必须一起删，只摘按钮会留半截。

前端入口：

- `src/web/index.html:47`  `<button id="btn-undo">撤销上次应用</button>`
- `src/web/index.html:57`  `<button id="btn-revert">还原翻译</button>`（danger 样式）
- `src/web/js/bind.js:29`  `$("btn-undo").onclick = () => undoLastApply();`
- `src/web/js/bind.js:36`  `$("btn-revert").onclick = () => revertMod();`
- `src/web/js/apply.js:5`  `undoLastApply()` / `onUndone()`
- `src/web/js/apply.js:44` `revertMod()` / `onReverted()`
- `src/web/js/events.js:77`  `undone` 事件分发
- `src/web/js/events.js:78`  `reverted` 事件分发
- `src/web/js/events.js:171` undo 失败分支
- `src/web/js/events.js:175` revert 失败分支
- `src/web/js/table.js:230`  `$("btn-undo").style.display`
- `src/web/js/core.js:20`   `revertUndo` 状态位（连带 `state.revertUndo` 全部引用）

后端：

- `src/web_app.py:5030`  `undo_last()` / `_undo_work()` / `undo_status()`
- `src/web_app.py:5078`  `revert_plan()` / `revert_mod()` / `_revert_work()`
- `src/web_app.py:390`   `l10n_undo_root`、`_revert_cache`
- `src/revert_source.py` 整个模块（删文件）
- `apply` 返回值里的 `undoId` / `undoAvailable` 字段

创建快照的三处——不摘掉的话每应用一次就白写一份快照：

- `src/mod_deploy.py:340`  部署时建 undo 事务（`undo_root=` 参数）
- `src/web_app.py:4576`    `_apply_mixed()` 里建事务
- `src/web_app.py:4696`    `_do_apply_source()` 里建事务
- `src/mod_batch.py:455`   已经传 `make_undo=False`，确认没有别处漏网

一起摘掉：

- `src/cache_tools.py:31-32`  「撤销记录」清理项（功能没了它永远是空的）
- `src/locales/*.json` 的撤销/还原词条可以留着（`T()` 会回退原文，不影响）

**不删** `src/undo_store.py` 文件本身，也不删 `dist\userdata\undo\` 里的历史数据
（铁律 4：用户的东西不动）。只是不再往里写。

### 当前基线（2026-11-20 09:45）

- 最新提交：`d1811d7 feat(apply): 单模组应用改成产出独立 zip，不再往 staging 里写产物`
- `dist\2077ModTranslator.exe`：14,114,897 字节，2026-11-20 09:45:45，
  SHA256 前 32 位 `DD5B5DE1B6283DA49EEF29DC3C50A057`
- 这一版含：单模组应用出独立 zip（`<模组名>_<目标语言>.zip`）、批量遮罩去掉
  「显示面板」并显示当前模组与条数、勾选框自绘、进度条与「已填/总条数」合并成一列、
  列表滚动条自绘、处理中遮罩跟随主题
- 已知残留：归档模式下点应用会留一条指向临时目录的 undo 事务记录（删撤销功能时自然消失）
