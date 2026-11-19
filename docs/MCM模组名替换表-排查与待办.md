# MCM 模组名替换表 / 翻译记忆 —— 排查记录与待办

> 记录时间：2026-11-20 03:00
> 记录人：另一个会话（只读排查，未改动本仓库任何源码）
> 面向：后续接手本工具改动的会话

这份文档只记录「查到了什么、证据在哪、别动什么、建议怎么改」。
本会话**没有修改任何源码**，`src\mod_text_engine.py` 仍是 181302 B / 11-14 21:20:55。

---

## 一、一句话结论

`labels.lua` 那 48 条假模组名的根因已经在 `src\mod_labels.py` 里修好了（`_strip_comments`），
但**当时运行的 exe 还是旧的**；另外主翻译流程**没有排除** `zzz_mcm_label_override` 目录，
`labels.lua` 里的 `id`（MCM 内部主键）随时可能被当普通文案翻掉。这是当前最危险的一条。

---

## 二、48 条假 sourceId 的完整因果链

### 证据 1：日志（`dist\userdata\logs\batch.log`）

```
11-20 02:04:58 labels: 扫到 111 个 sourceId，待补 60，写入 56，翻失败 4（重启游戏后生效）
11-20 02:31:04 labels: 扫到 111 个 sourceId，待补 60，写入 56，翻失败 4（重启游戏后生效）
11-20 02:01:58 labels: 翻模组名失败 连接失败: [WinError 10061]
11-20 02:30:37 labels: 翻模组名失败 连接失败: [WinError 10061]
```

### 证据 2：数字全对得上

| 数字 | 含义 |
|---|---|
| **111** | 真实 sourceId **63** 个 + `mod_settings\packed.reds` 里被注释掉的示例 **48** 个 |
| **待补 60** | 48 个假名 + 12 个真名（其中 4 个随后翻失败） |
| **写入 56** | 48 个假名 + 8 个真名：`Not So Good Draw Distance`、`General Shadows Fixes`、`In-World Navigation`、`Enemy Multiplier RS`、`Drive Modes and Cruise Control`、`ITP/TFPC Compatibility Fix`、`Cyberpunk Render Tweaks`、`Takedown System` |
| **翻失败 4** | `Audioware`、`XUtils`、`ReFRACT`、`Blur Begone`（模型服务连不上，`WinError 10061`） |

真名统计口径（2026-11-20 全量重扫）：游戏已部署 1512 个 `.reds` + 暂存 2434 个 `.reds`，
**真实生效的 sourceId 63 个**，其中 16 个本身已经是中文（`智能商贩`、`追踪控制`、
`插槽 插槽 插槽`、`已恢复悬赏`、`改进的敌人定位标签` …），剩下 47 个走运行时替换表。

### 证据 3：假名的来源文件

`red4ext\plugins\mod_settings\packed.reds` 第 422 行往后，作者写的一整段示例代码，
而且**是注释掉的**：

```
// @runtimeProperty("ModSettings.mod", "Test mod")
// public let testValue: Float = 0.0;
// @runtimeProperty("ModSettings.mod", "Stone Mod")
```

名字形态是 `Bell Mod` / `Cake Mod` / `Toothsome Mod` / `Waggish Mod` / `Weary Mod` 这类
「形容词 + Mod」，一共 48 个，**MCM 里永远不会出现**。

### 证据 4：修复已在源码里

`src\mod_labels.py`（**今天 02:37 新建，git 里还是 `??` 未跟踪**）：

- 第 90 行 `def _strip_comments(text)` —— 去掉 `//`、`/* */`，字符串内原样保留
- 第 162 行 `scan_ids()` 里调用它之后再跑 `_MOD_RE`

所以**下一次运行应该报「扫到 63」而不是 111**。这是验证修复是否真的进包的唯一判据。

⚠ `dist\2077ModTranslator.exe` 在 02:37 重建过，但 PyInstaller 压缩了模块，
直接扫二进制拿不到特征串（我试了 `_strip_comments` / `作者编的占位名`，都是 0 命中）。
**要确认新逻辑进包，只能跑一次看日志里的 sourceId 数量。**

---

## 三、三个待修问题

### P0（最危险）主翻译流程没有排除 `zzz_mcm_label_override`

```
src\mod_text_engine.py:51    SRC_EXTS = {".reds", ".xml", ".lua", ".yaml", ".yml"}
src\mod_text_engine.py:2968  def iter_source_files(root, include_lua=True, ...)
src\mod_text_engine.py:2969      for dirpath, dirnames, filenames in os.walk(root)
src\mod_text_engine.py:2970          dirnames[:] = [... if d.lower() not in SKIP_DIRS]
src\mod_text_engine.py:3027  for path in iter_source_files(root, ...)   # 唯一枚举入口
```

- 扫描根目录就是 `E:\Vortex Mods\cyberpunk2077`（`apply.log` 每次开头的 `root:` 行可以证明）
- `labels.lua` 位于 `...\zzz_CP2077_mod_fix\bin\x64\plugins\cyber_engine_tweaks\mods\zzz_mcm_label_override\`
- 全仓库只有 `mod_labels.py` 提到过 `zzz_mcm_label_override`，**主流程没有任何排除**

后果：`id = 'Anti-Tracking Breach'` 一旦被当成可译文案翻掉，注册处的 `"XXX"`
和读取处的 `n"XXX"` 就脱钩 —— 页签裂成一个中文空壳 + 一个原英文页，
设置项读不到已存的值而回退默认，界面上看不出异常。

**建议改法（一处，最小）**：在 `iter_source_files` 的 `os.walk` 里按目录名整目录剪掉：

```python
PROTECTED_DIRS = {"zzz_mcm_label_override", "zzz_cp2077_mod_fix"}

dirnames[:] = [d for d in dirnames
               if d.lower() not in SKIP_DIRS
               and d.lower() not in PROTECTED_DIRS]
```

注意 `dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]` 这行在
第 2970 行和 3099 行**一字不差重复**（3099 在 `_count_locale_lua` 里，只做计数），
定点替换时必须带上 `def iter_source_files` 的上下文，否则会匹配到两处。

### P1 `parse_labels` 只认单行条目

```
src\mod_labels.py:43-46  _ENTRY_RE = re.compile(
    r"^\s*\{\s*id\s*=\s*'((?:[^'\\]|\\.)*)'\s*,\s*"
    r"label\s*=\s*'((?:[^'\\]|\\.)*)'\s*\}\s*,?\s*$")
src\mod_labels.py:175    def parse_labels(path)
```

要求 `{ id = '…', label = '…' },` 全在一行。文件一旦被写成两行格式，
`parse_labels` 返回空表 → `update()` 认为整张表都没有 → 把所有 id 当「待补」
重新插一遍，产生重复条目（`write_labels` 的 `have` 集合也是从同一份解析来的，挡不住）。

**建议改法**：解析前先把 `return { … }` 之间的条目块按 `{` / `}` 配对合并成逻辑行，
或让正则用 `re.S` 并允许 `\s*` 跨行。

### P2 翻译记忆没有作用域、没有导出

```
src\tm.py:72     class Memory          # 落盘 dist\userdata\memory.json（8.66 MB）
src\tm.py:169-171  _key(src, tgt) = sha1(tgt + "\x00" + src)
src\tm.py:208-229  put(src, tgt, text, origin)   # 条目 {s,t,g,o,n,ts}
```

- 已有的来源等级 `manual(4) > official(3) > official-weak(2) > memory(2) > machine(1)`
  就是账本需要的「状态」列，可以直接用
- 缺 `scope`：现在 key 只有 `(目标语言, 原文)`，分不清这条用在
  「模组名 / 分类 / 设置项名 / 描述 / 游戏内文本」哪一层
- 缺导出：全仓库 `tsv` 零命中，没有把 `memory.json` 导成表格的能力

**建议改法（不动 key，零迁移）**：
1. `put()` 多一个可选参数 `scope=""`，条目里多存一个 `"sc"` 字段；老条目默认空
2. 新增 `src\tm_export.py`：`memory.json` → `strings.tsv`
   （列：`scope / original / translated / origin / hits / updated`），支持 `--scope` 过滤
3. 打算接运行时层时，同一份数据再导出一个 `zh_cn.lua`（`return { [...]=... }`）

---

## 四、不要动的地方（硬约束）

1. **`labels.lua` 里的 `id` 绝不能翻成中文**，它是 MCM 的 sourceId 主键。
2. **`labels.lua` 现在是被用户手工整理过的 47 条单行格式**，位于：
   ```
   E:\Vortex Mods\cyberpunk2077\zzz_CP2077_mod_fix\bin\x64\plugins\cyber_engine_tweaks\mods\zzz_mcm_label_override\labels.lua
   ```
   2026-11-20 02:47 用户手工改过两条，后续任何批量写入都不能覆盖：
   - `Curbside` → `车辆召唤修改`
   - `The Grid` → `灯光网格`
3. `zzz_mcm_label_override\init.lua` 是已经调通的运行时补丁（15028 B），
   它靠 `require("labels")` 读表 —— **CET 不暴露 mod 自身路径**，
   `package.path` 在 `onInit` 时是空的，`debug.getinfo` 拿到的 init.lua chunk 名不带路径，
   所以别把加载方式「优化」成 `loadfile`/`io.open` 相对路径，会直接失效
   （2026-11-20 已经踩过一次：`Tried to access invalid path`）。
4. `dist\userdata\` 下的 `memory.json`、`official_tm.sqlite`、`glossary.json`、`undo\` 一律不动。

---

## 五、运行侧现状（供对照）

- 运行时补丁：`zzz_mcm_label_override`（CET），日志只写
  `bin\x64\plugins\cyber_engine_tweaks\scripting.log`（搜 `zzz_mcm_label_override`）
- 正常启动日志应为：
  ```
  载入规则 47 条（来源 require）
  已挂接 3/3 个 provider，改名规则 47 条，隐藏 0 条
  首次命中：Anti-Tracking Breach → 反追踪破解
  ```
- 打包产物：`E:\Vortex Mods\zzz_CP2077_mod_fix.zip`，28262 B，
  含 `zzz_CP2077_mod_fix`（反射修复）与 `zzz_mcm_label_override`（本补丁）两项

---

## 六、验证清单

改完 P0 之后，按顺序验：

1. `python -c "import py_compile; py_compile.compile(r'src\mod_text_engine.py', doraise=True)"`
2. 跑一次批量扫描，`batch.log` 里 `labels:` 那行必须变成 **扫到 63**，不再是 111
3. 确认 `labels.lua` 的 47 条 `id` 一个都没变（`git diff` 或哈希对比）
4. 确认 `Curbside` / `The Grid` 两条仍是被用户改过的值
