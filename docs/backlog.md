# 待办与已知问题

按优先级记，条目只在「决定做」时往下排，做完就删掉这一节，别让文档腐烂。

## P4 换了目录但加载失败时不要改状态

「浏览」和「打开压缩包」都是先 `$("path").value = p; state.modPath = p;` 再调用
`loadMod()`。加载被拒（后端 `busy`、路径无效）时这两个值已经变了，之后
`root = state.root || state.modPath` 会指向一个跟表格毫无关系的目录。

改成加载成功后再落状态：路径先存本地变量，`onLoaded` 里再写进 `state`。

## P6 加载前没有确认，未应用的译文会静默丢失

机翻中提取已经被遮罩挡住了，但**空闲时**点「加载 / 提取」仍然会覆盖当前表格：
`onLoaded` 换掉 `state.rows`，还没应用回磁盘的手改与机翻结果一起消失，没有提示。

`state.dirty` 现在也不够用——`onLoaded` 不清它、`onApplied` 也不清它，加载一次
之后永远是 true，判断不出「有没有未应用的修改」。要用它做确认框，得先把
生命周期补齐：

1. `onLoaded` 里复位 `state.dirty = false`
2. `onApplied` 成功后 `state.dirty = false`
3. 机翻回填（`rows` 事件）也置 `state.dirty = true`，否则机翻结果算不进去
4. `loadMod()` 开头 `if (state.dirty && !confirm(...)) return;`

## 已知但不打算改

- 编辑器没有「取消本次编辑」：敲进去就进了内存，想放弃只能手动删。加 Esc 回滚
  需要记住进入编辑时的原值，收益不大。
- `setBusy()` 会把所有 `.btn` / `.chip` 逐个禁用。机翻已改成遮罩（不再调它），
  但其余路径还在用，行为和以前一致。
