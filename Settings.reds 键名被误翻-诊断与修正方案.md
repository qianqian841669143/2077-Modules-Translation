# NightCityLivingStreets `Settings.reds` 键名被误翻 —— 诊断与修正方案

## 0. 先纠正上一份方案（已作废）

上一份方案（`NIS 设置项翻译修正方案.md`）定位错了，两个说法都不成立：

| 上一条的说法 | 实际情况 |
|---|---|
| `pct_guard()` 在 `mod_text_engine.py`，需要改它 | `mod_text_engine.py` 里**没有** `pct_guard`；它只存在于 [`localization/pipeline.py:66`](file:///D:/Module%20Translation/src/localization/pipeline.py#L66)，且只管 CR2W 归档写回，与 `.reds` 无关 |
| `pct_spec_type_guard()` 是本次问题原因 | 该函数早在之前会话就加好了（[`mod_text_engine.py:2532`](file:///D:/Module%20Translation/src/mod_text_engine.py#L2532)），本次 `Settings.reds` 里**一个格式符都没丢**，全部 424 条 `@runtimeProperty` 都能被它正常放行 |

所以：**这次的问题和占位符守卫完全无关**，改那两个守卫不会有任何效果。

---

## 1. 真实根因

### 现象

`Settings.reds` 里 424 条 `@runtimeProperty`，有 **205 条**的第一个参数（MCM 键名）被换成了第二个参数的中文译文：

```reds
原档：  @runtimeProperty("ModSettings.mod", "Living Streets")
两次后： @runtimeProperty("鲜活街道", "鲜活街道")

原档：  @runtimeProperty("ModSettings.displayName", "Living Streets enabled")
两次后： @runtimeProperty("鲜活街道 已启用", "鲜活街道 已启用")
```

### 为什么设置页只剩两个

MCM 注册设置项**只认** `ModSettings.mod` / `ModSettings.category` / `ModSettings.displayName` / `ModSettings.description` / `ModSettings.category.order` / `min` / `max` / `step` 这些固定键名。键名变成中文后：

- `ModSettings.mod` 丢失 → 该条目不属于任何 mod 面板
- `ModSettings.category` 丢失 → 分类注册失败
- 注册是**按顺序**进行的，中途抛错/落空后，**后面的分类全部不再注册**

这与 [`localization/pipeline.py:50-53`](file:///D:/Module%20Translation/src/localization/pipeline.py#L50-L53) 注释里 v2.52.9 实测到的现象完全一致：「那条描述挂第 3 个分类，它一崩，后面的分类全部不再注册，设置页只剩前两个」。

### 代码层面的根因

[`mod_text_engine.py:2821-2827`](file:///D:/Module%20Translation/src/mod_text_engine.py#L2821-L2827) 的写回匹配有三层兜底：

```python
lit = pick(by_key.get((line_no, orig), []))   # 1. 行号+原文
if lit is None:
    lit = pick(by_text.get(orig, []))         # 2. 全文匹配原文
if lit is None:
    lit = pick(by_line.get(line_no, []))      # 3. 只按行号 → 取该行第一个字面量
```

**第一次应用**：该行是 `@runtimeProperty("ModSettings.mod", "Living Streets")`，`by_key[(9,"Living Streets")]` 命中第二个参数，正确。

**第二次应用**：该行的值已经是 `"鲜活街道"`，而译文表里存的还是 `(行 9, 原文 "Living Streets", 译文 "鲜活街道")`：

- `by_key[(9, "Living Streets")]` → 空（值已是中文）
- `by_text["Living Streets"]` → 空（全文已无此串）
- **兜底 3 `by_line[9]`** → 该行还剩两个字面量，`pick` 取列表**第一个** → 正是键名 `"ModSettings.mod"` → 于是键名被写成 `"鲜活街道"`

一句话：**兜底 3 不知道「`@runtimeProperty` 的第一个参数是键名、不是文案」，把键名当成了待替换的目标。**

代码里已经防住了同类情况 —— [`mod_text_engine.py:2781-2791`](file:///D:/Module%20Translation/src/mod_text_engine.py#L2781-L2791) 为 Codeware 的 `this.Text("key","value")` 算出了 `key_starts` 并排除。但 `@runtimeProperty` 是**注解**，不在 `call_spans` 里，所以没被这条兜住。

### 为什么只有 205 条，而不是 424 条

值没被翻译的那些行没事。例如：

```reds
@runtimeProperty("ModSettings.category", "General")   ← "General" 没翻，键名完好
@runtimeProperty("ModSettings.displayName", "Enabled") ← "Enabled" 没翻，键名完好
@runtimeProperty("ModSettings.category.order", "1")     ← 纯数字，键名完好
@runtimeProperty("ModSettings.step", "5")               ← 纯数字，键名完好
```

被破坏的正好是「值被翻成了中文」的那 205 条（65 × `mod` + 65 × `category` + 65 × `displayName` + 部分 `description`，减去值未被翻的）。

### 关键佐证：行结构完全没坏

```
bak 行数: 605      当前行数: 605      不同的行: 205
```

行数一致、差异行数正好等于坏掉的键名条数，说明**译文本身是对的**，只有键名被顶掉。因此不需要重新翻译，只要把键名还原。

---

## 2. 修正方案

### 2-A 引擎补丁（防止再犯，必做）

文件：`D:\Module Translation\src\mod_text_engine.py`
位置：第 2784 行 `key_starts = set()` 之后（`except` 块结束处）

```python
    # @runtimeProperty("ModSettings.mod", "Living Streets") 的第一个参数是 MCM
    # 键名，永远不是文案。它是注解而非函数调用，call_spans 认不出来，上面那条
    # key_starts 兜不住它 —— 于是兜底 3（只按行号）会挑中同一行的键名。
    #
    # 触发条件是「同一文件第二次应用」：该行的值已写成译文，按原文/全文都匹配
    # 不上，兜底只剩「该行第一个字面量」，正是键名。结果变成
    # @runtimeProperty("鲜活街道", "鲜活街道")：MCM 收不到 ModSettings.mod /
    # category / displayName，注册在中途落空，设置页只剩前两个分类。
    # 实测 NightCityLivingStreets 的 Settings.reds：424 条里 205 条键名被顶掉。
    for _m in re.finditer(r'@runtimeProperty\(\s*(?=["\'])', text):
        key_starts.add(_m.end())        # lit.start 就是引号所在偏移
```

> `(?=["\'])` 是零宽断言，`_m.end()` 恰好落在开引号上，与 `Lit.start` 语义一致；同时兼容单引号写法。

**补丁效果**：键名进入 `key_starts` → 被 `key_lines` 标记并从 `by_key/by_text/by_line` 三个候选池全部排除 → 兜底 3 再也不会选中键名。若该行值已翻过、匹配不到，则正确报出

```
这一行只剩查表键, 写进去会打断本地化引用   （hard）
```

而不是静默把键名改坏。

**无副作用**：第二个参数（真正的文案）不受影响，仍在三个候选池里，正常替换。`ModSettings.category.order` / `min` / `max` / `step` 的纯数字值本来就不会进翻译表。

### 2-B 还原已损坏的 `Settings.reds`（本次要修的）

不需要重新翻译，用 `.bak` 做**逐行结构还原**：

- 前提已确认：两文件均 605 行，一一对应
- 规则：`.bak` 第 N 行是 `@runtimeProperty("ModSettings.xxx", "英文")`，当前第 N 行是 `@runtimeProperty("中文", "中文")` → 只把当前行的**第一个参数**换成 `ModSettings.xxx`，第二个参数（中文译文）原样保留
- 安全措施：
  - 行数不等 → 直接中止，不写
  - 只改「`.bak` 键名合法 且 当前键名不合法」的行，其余行一字不动
  - 先输出 `.restored` 副本供比对，确认后才覆盖
  - 覆盖前把当前损坏文件另存 `.broken`

还原脚本已写好：[`restore_settings_keys.py`](file:///D:/Module%20Translation/restore_settings_keys.py)（默认 dry-run，加 `--apply` 才落盘）

### 2-C 顺带确认

- `Police.reds`（880 行）、其余 9 个文件：**没有** `@runtimeProperty`，未受影响
- `Police.reds.bak`、`Settings.reds.bak` 都在，可用于还原
- 本次不需要改动 `pct_guard` / `pct_spec_type_guard` / `swapped_spec_order` 任何一行

---

## 3. 验证方法

改完引擎后，重新对同一文件应用**第二次**翻译（重复应用是触发条件），然后跑：

```powershell
python diag_settings.py
```

期望输出：

```
@runtimeProperty 总数: 424
  键名合法(ModSettings.*): 424
  键名被破坏:            0
```

以及用 `.bak` 对比：

```
bak 行数: 605   当前行数: 605
```

再启动游戏确认 MCM 里 Living Streets 的分类和条目全部回来。

---

## 4. 执行顺序建议

1. 先打 **2-A 引擎补丁**（否则还原后一应用又坏）
2. 再跑 **2-B 还原脚本** 的 dry-run，看报告
3. 确认无误后 `--apply` 落盘
4. 进游戏验证设置页

需要我直接动手改的话，说一声，我按这个顺序执行。
