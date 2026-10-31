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

## P11 CET 设置界面的位置语义（实测于 repeatable_ncpd_gigs, 2026-11-01）

`common.lua` 的包装器把「显示名」和「键」混在同一个参数上，工具只按「像不像
句子」判证据，于是两类错都发生过：

- `settings.category("general", …)` 的 arg0 = 键。它同时进三处：`settingsKey =
  category.name.."."..name`（`settings.json` 的持久化键）、`path = tab:getPath()..
  "/"..name`（nativeSettings 子分类路径）、`getText(keyPrefix..name.."-label")`
  （显示文案的查表键）。改成中文 → 旧键孤立、9 处派生 label 失联（面板显示
  `numOfAvaiableGigsInAct1` 这类裸标识符）。
- `settings.tab(id, label, obj)` 的 arg0 = 键（`getPath()`），arg1 = 纯显示串，
  `getLabel` 直接 `return self.label`，**没有任何 loc 查找** → 必须翻。

工具侧：
1. `CALL_RULES` 补 `tab / subcategory`（带 `settings.` `nativeSettings.` 前缀优先）；
2. `_wrapper_rules` 现在只认 `local function name(a,b)`，而 `common.lua` 写的是
   `tab=function(name, label, obj)`，且定义在**另一个文件**里。要支持赋值式 /
   点号式声明，并在 `extract_root_ex` 里跨文件聚合后再逐文件抽取；
3. `getLabel / getActionLabel / getDescription = function() return "…" end` 直接取
   return 字面量（现在靠兜底判成「长句文本」）；
4. 包装器参数要带 `role`（key / display），写回前做「键守卫」：改了 KEY 位置的
   字面量就拒绝，和骨架守卫同级。

## P12 空译文不重试，写回前也不对账

`translate_batch` 保证返回长度一致，但模型会对短串返回**空串**；`web_app.py:1962`
`if not text: continue` 静默跳过，只在报告里计一句「模型返回了空译文（N 条）」。
用户点「应用」时，没译文的行根本不在 `items` 里，于是文件成功写回一半，没有任何
提示。实测 repeatable_ncpd_gigs：31 条候选，7 条显示文案 + 5 条查表键没落盘，
`memory.json` 里全 MISS（模型没给译文），`apply.log` 干净（写回没失败）。

1. 批次循环后对 `empty` 逐条 `translate_one` 重试，提示词换成「UI 按钮/标签名」
   专用（2–4 个词，只给短语）；
2. `apply()` 前比对：同文件还有空译文的候选 → 返回 `pending` 列表，前端弹
   「先补翻 / 仍然写回」；
3. `_do_apply_source` 返回 `{written, skipped:[{line, orig, reason}]}`，状态栏给
   `候选 31 · 写入 26 · 跳过 5` 的角标。

## P13 查表键被当成候选文案

`label=c.getTextOrKey(keyPrefix.."reward.numOfDraws-label")`：`_arg_strings()` 会钻进
子调用取证，把查表键当成这一行的文案收进候选（本 mod 5 条）。翻掉就是查表断供，
而且表面正常（回退显示的就是那句中文）。`_arg_strings` 碰到 `getText` /
`getTextOrKey` / `GetLocalizedTextByKey` / `TDB.GetLocKey` 时不下钻，内部实参一律
`drop(NOISE.FAMILY_KEY)`；`is_prop_loc_key()` 已是现成判据，只是没接到 Lua 路径。

## P14 拼接上下文丢尾随空格

`label="Restrict "..gig:GetQuestName()` 写成 `label="限制"..gig:GetQuestName()`，面板
显示成「限制Japantown…」。写回时若字面量左右紧邻 `..`，保留原文首尾空白。

## P15 归档内容没变也重打包

apply 时 `_apply_resource` 无论内容是否有变化都重建 `.archive`（本 mod：69 条文本
0 处变化，8 KB 重写一遍，时间戳与字节都变了）。重建后与旧文件做 sha256 比对，
相同就跳过写盘。

## P16 「疑似代码片段」拒绝没有出口，一条能干掉整批

`apply_many_ex` 把两种性质完全不同的情况塞进同一条失败通道：

```python
if guard and looks_like_code_fragment(orig):
    failures.append((idx, line, orig, trans, "疑似代码片段, 拒绝写回"))
...
if guard and not same_code_shape(original_text, text, lang):
    return 0, [(...) for i, p in enumerate(pairs)]   # 一条坏 → 全批 0 条写入
```

- 前者是**判断题**（形态可疑，可能误报），后者是**硬伤**（结构变了，写进去必崩），
  用户都只看到「应用失败」。
- 没有出口：明知是文案也只能被反复拒绝，也没法告诉工具「这条是代码，别再报」。
- 骨架校验失败是一次全局 `return 0`，失败明细里每条都写同一句原因，看不出是谁。

改法（按性价比）：

1. **自动收窄（先做，零 UI）**：`looks_like_code_fragment(orig)` 只有在
   `text_evidence(...)` 拿不到 `display` 证据时才拒。`label=` / `description=` /
   `showMessage(...)` / `settings.tab(_, label)` 这些位置本身就是强证据，放行；
   放在 `s"…"` 插值内或独立语句里的照旧拒。后面还有骨架校验兜底，误放跑不掉。
2. **两级裁决**：soft = 形态可疑（可覆盖）；hard = 骨架不一致 / 未找到原文 /
   读写失败（永不开放）。`apply_many_ex(path, pairs, allow_soft=False)`，
   失败明细带上级别字段。
3. **单条覆盖**：`apply_one(path, line, orig, trans, allow_soft=False)` = 单元素
   `apply_many_ex`，复用 undo / TM / 回滚。前端在 soft 失败行给「仍要写入」，
   弹窗必须给足判据：diff、命中片段、上下文 ±40 字符、骨架预演（token 种类序列）。
   默认焦点在「跳过」，`Esc` = 跳过。写入后追加 `logs/override.log`。
4. **「标记为代码」**：`(相对路径, 原文 sha1)` 进用户排除表（`user_skip.json`），
   下次抽取直接过滤，候选表里标灰。这才是把用户判断持久化的形态，不是每次弹窗。
5. **逐条预演**（可选）：先在内存生成新文本，校验 token/字面量数、kind 序列、
   括号引号配平、`.reds` 里每个 `s"…"` 的 `\(` 个数；只回滚坏的那条，缓解 1 拖 N。

注意：单条应用也必须走 `apply_many_ex` 的同一个入口，否则撤销记录、记忆记账、
失败回滚会出现两套行为。

## P17 模型原样返回（echo）被算成「已填」，还会反复复发

实测 INCF-CORE.reds 第 23 行的通知串（4 个 `{codeware_req}` 类占位符 + 7 个 `\n`）：

- 模型把英文原样吐回来，`trans` 有了内容 → `app.js:1260` 的
  `filled = rows.filter(r => r.trans)` 把它计成「已填」，界面上看不出异常；
- `tm.py:104` 对 `src.strip() == text.strip()` **有意不落库**（身份译文进记忆毫无
  价值），所以它既不会显示成「记忆译文」，下次加载又回到未填 → 再翻再 echo，
  用户看到的是「这条怎么永远翻不了」；
- 事后只能靠工具栏筛选「原文=译文」找（`app.js:698-706`），还要先排除
  `already` 行，属于纯人工兜底。

改法（与 P12 合并做，同一处落值逻辑）：

1. 落值前比对 `out.strip() == orig.strip()` 且原文含 `[A-Za-z]` → 不写 `trans`，
   计入 `echo` 计数，并对该条单条重试一次（提示词强调「占位符 `{…}` 与 `\n`
   必须原样保留，只翻译自然语言部分」）。
2. 重试仍 echo → 行上标 `echo=1`，状态栏报「N 条模型原样返回」，进筛选器可见；
   别混进「已填」，否则 P12 的「候选 vs 写入」对账也会失真。
3. 占位符密集（`{…}` ≥ 3 或 `\n` ≥ 4）的串直接走单条通道，别进批量——批量里
   这类串最容易整条被跳过或原样返回。
4. 术语污染是同一批问题的另一面：记忆库现有 15 条含 `Codeware` 的记录，
   其中 `Codeware → 义体软件` 是错译，会被 4 行同时复用。术语表补
   `Codeware → Codeware`（框架名保名），并对「源串是单个驼峰/全大写专名」
   的机翻结果不给自动填入（宁可留空让用户看一眼）。

## P18 `a|b|c|20` 这种数据行被当成句子收进候选

GhxstCompanion.reds:1227 `private func StoryBeats() -> array<String>` 里是一张表：

```reds
// [key, pool, facts (any of, comma), extra wait seconds]
"takemura|story_takemura|q112_done|20",
```

`:1266` / `:1278` 用 `StrSplit(b, "|")` 拆开，四段全是 id：`p[0]` 拼
`SetFact("ghxst_beat_" + p[0])`、当 `StringToName(p[0])` 的 CName、还被
`Equals(p[0], "ponr")` 直接比字面量；`p[1]` 是语音池名，进
`Speak(StringToName(p[1]))`。

翻掉任一字段的后果：`p[1]` 变中文 → 语音池查不到、角色哑火；`p[0]` 变中文 →
fact 名变、旧存档的 beat 状态失配；`ponr` 那条（`:1251`）被翻 → 生死关头的
BGM 分支直接死。模型这次只翻了第一段纯属侥幸，换次采样就可能整串翻掉。

工具侧：这类串没有位置证据，落在「长句兜底」档，`is_translatable` 只要求
「≥2 个英文词」就收。实测该文件 168 条候选里 **24 条**是数据行（1229–1251 整块
+ 1196 的音效序列），约占 14%。

三层改法：

1. **形状层** `looks_like_data_row(s)`：含 ≥2 个 `|`（`;` `\t` 同理），所有段
   匹配 `^[A-Za-z0-9_.:,\-]+$`（无空格、无 CJK、无句末标点），且至少一段是
   snake_case 或纯数字 → `NOISE.FAMILY_DATA`，不进候选。
2. **上下文层**：文件里出现 `StrSplit(x, "|")` 时，该文件内「段数 ≥2 且全段都是
   标识符形状」的字面量默认判数据行。用代码本身当证据，不内置表；能顺带覆盖
   `w:3.0|s:ghxst_sfx_connect|`、`g:…|g:…` 随机池。
3. **写回守卫**（硬线，不给覆盖开关）：原文 `|` 段数 ≥2 时，译文段数必须相同，
   且每段的「ASCII 标识符 / CJK」属性不许变。坏法（哑火、BGM 不播、fact 失配）
   在文件层面完全看不出来，只能靠守卫挡。

白名单：段内含空格或段首像句子（`Alive|Dead`、`Yes|No`）仍按文案处理，避免误杀
真正的二选一 UI 文本。另外这类名字真要本地化，正确做法是在 mod 里加显示列，
不是翻译字符串。

## P19 代码碎片守卫实测：45 条拦截全是误伤，`lead:,` 一条规则吃掉 43 条

> v2.40.1 已按下面前两条修好（规则收窄 + 中文原文放行）。同一份 366 个文件的回放：
> 拦截从 **45 条降到 1 条**（只剩 DVCore 自带的 `")"`）。回归在
> `tools/selftest.py::check_code_fragment_guard`。第 3、4 条（标记为代码 / soft-hard
> 分级）仍未做。

P16 记的是机制（没出口、一条干掉整批）。实测连判据本身也不成立。失败原因不落盘
（`logs/apply.log` 只有「N 条未匹配」，`undo/*/manifest.json` 只存备份），所以用工具
自己的 `extract_from_file` + `looks_like_code_fragment` 回放：全部撤销清单里工具真实
写过 365 个 `.reds/.lua`，命中 **45 条**，分布 7 个模组（SenSen Core 28、Virtue 6、
Skeleton Key 4、Herald 3、ITP 2、DigitalVixen 1、TWR 1）。

`_CODEY_FRAGMENT_RE = r"\\\(|\)\)|\)\s*;|^\s*[\)\],]"` 的命中分布：

| 分支 | 条数 | 实际是什么 |
|---|---|---|
| `^\s*[\)\],]` 的 `,` | 40 | 拼 UI 文本的惯用写法，不是碎片 |
| `^\s*[\)\],]` 的 `)` | 3 | `')'`、`'), …'`、`') 重试后'` |
| `\)\s*;` | 2 | ITP 两句屏幕说明里正好有「(…);」 |
| `\\\(` / `\)\)` | 0 | 插值切分已在 678d8ac 修掉，守卫只剩误报 |

真实样本（这 8 条至今还是英文，就是被拦下的那批）：NonLethalVehicles.reds:2700
`", landings after it"`、:2704、BreachLethal.reds:353 `", off (read)."` / `", on (read)."`、
HeraldCascade.reds:577 `", but has no hop for"`、ITP init.lua:10826 / :10932、
ChineseSimplified.reds:59 `")"`。其余 37 条原文已是中文。

**第二个 bug 更严重**：守卫只看 `orig`，原文已是中文的行照样被拒。`app.js:2241`
跳过 `trans === orig`，所以平时不报；用户一旦想改「, 已计入」的措辞，`trans != orig`
→ 必拒，这 37 条等于**永久锁死、改不动**。

改法：

1. 首字符去掉 `,`（保留 `)` / `]`）；`\)\s*;` 单独出现不算数，要么同时命中
   `\(` 或 `))`，要么首字符就是 `)`。这一步放掉 45 里的 43 条，真碎片仍挡住。
2. 已翻行不进守卫也不计失败：写回前 `is_target_language_text(orig)` 为真直接放行。
   否则「改措辞」这条路对任何历史中文行都是死的。
3. 剩下确实没法翻的纯符号（`")"`）走 P16 的「标记为代码」单行豁免，不靠正则猜。
4. 顺序上先做 1+2，P16 的 soft/hard 分级才有意义——收窄后的判据才够格当 soft。

顺带（与守卫无关，另案）：NonLethalVehicles.reds 2699、2705 各有一个片段重复出现
两次（`(触发 ragdoll 状态`、`(一辆摩托车在你刚被命名后撞上了你`），像两条不同原文拿到
了同一句译文，需要单独核。

## P20 两个静默失败：非 ASCII 标识符让整份文件消失 / 目标语言判断大小写敏感

> v2.40.1 已修：`scan_source` 只认 ASCII 标识符与数字；`is_target_language_text`
> 先用 `_locale_key()` 归一化（表键也改成小写）；`extract_root_ex` 新增
> `meta["parse_errors"]` + 样本，加载结果带 `parseErrors`，状态栏显示
> 「N 个文件解析失败（已跳过）」。回归在
> `tools/selftest.py::check_parse_and_locale_robustness`。

扫安装缓存目录（286 个模组）时撞到的，两个都表现为「看起来没问题，其实全错」。

**A. `scan_source` 遇到非 ASCII 标识符直接抛异常，整份文件被吞掉**

```python
if c.isalpha() or c == "_":
    m = IDENT_TOK_RE.match(text, i)     # 纯 ASCII 正则
    toks.append(Tok("ident", m.group(0), ...))   # CJK 时 m 为 None -> AttributeError
```

`str.isalpha()` 对中日韩字符返回 True，`IDENT_TOK_RE` 匹配不到 → `m.group(0)` 抛
`AttributeError`。`extract_from_file` / `extract_root_ex` 外层是 `except Exception`，
于是**这份文件一条都不进表**，界面只表现为「这个模组没有文本」。`c.isdigit()` 同理
（全角数字 `０-９` 会命中 `isdigit()` 但匹配不到 `NUM_TOK_RE`）。

实例：INCF-CORE.reds（1.4）在 07:03 被一次 `particles → 粒子效果` 的整文件替换改坏，
`node.particleSystem` 变 `node.粒子效果ystem`、`CParticleSystem` 变 `C粒子效果ystem`、
`emitter.maxParticles` 变 `emitter.max粒子效果`，于是这份 13 KB 的模组在扫描里被
判成「有源码但提取不到文案」。文件本身当时也确实编译不过（redscript 整层不加载），
只是工具没报错、还给了个「没有文本」的结论。

改法：`if c.isascii() and (c.isalpha() or c == "_")`，数字同理；并且
`extract_root_ex` 把解析异常计数进 `meta["parseErrors"]`，加载后状态栏报
「N 个文件解析失败」，别让文件无声消失。

**B. `is_target_language_text` 精确查 `_TARGET_SCRIPT_RE`，大小写不一致就静默返回 False**

```python
_TARGET_SCRIPT_RE = {"zh-CN": ..., "zh-TW": ..., "ja-JP": ...}
rx = _TARGET_SCRIPT_RE.get(target_locale)      # "zh-cn" -> None -> False
```

只有 app 内部用 `locale_pack.target_code()` 吐出的大写 `zh-CN` 才对得上；任何地方给
`zh-cn` / `zh_CN`（外部脚本、批量入口、以后新增的调用）都会让**所有中文行重新变成
待翻候选**。实测同一份 SenSen Core：传 `zh-cn` 得到 389 条「待翻」，传 `zh-CN`
得到 2 条待翻 + 387 条已译。

改法：`_locale_key()` 就写在下面几行却没人用——`is_target_language_text` 里先
`_locale_key(target_locale)` 归一化再查表，同时把 `_TARGET_SCRIPT_RE` 的键也改成
小写。

两个都属于「静默给出错误结论」，比崩溃更难发现；A 会让模组凭空少掉文本，B 会把
已经翻好的行当成待翻再翻一遍。

## 已知但不打算改

- 不提供「忽略骨架校验」开关。那等于把「写坏整层脚本、游戏里 redscript 全不加载」
  做成一个可点的按钮（DVCore 那次：`))` 被吃掉，括号 7/8 → 5/4，
  `REDScript compilation has failed`）。
- 不提供「整批强制写入」，只提供单条强制。批量强制一次写坏几十行，undo 只回滚
  文件，回滚不了用户没看见的错误。
- 编辑器没有「取消本次编辑」：敲进去就进了内存，想放弃只能手动删。加 Esc 回滚
  需要记住进入编辑时的原值，收益不大。
- `setBusy()` 会把所有 `.btn` / `.chip` 逐个禁用。机翻已改成遮罩（不再调它），
  但其余路径还在用，行为和以前一致。
- 清空某行译文会让这句的**机翻记忆**一起失效（`tm.Memory.forget` 只删
  machine / memory 来源，手改与官方不动）。副作用是别的模组里同一句也失去
  缓存，下次要再问一次模型。换来的是「清空 → 重翻」真的能拿到新结果，
  而不是把同一条从 TM 里原样搬回来。
- 单个英文单词的 UI 文本（`Close` / `Save` / `Cancel`）只在**语言包文件**里
  才收（`locale_pack._is_label_word`，模式 `^[A-Z][a-z]{2,}$`）。源码层仍用
  `mod_text_engine.is_translatable` 的「至少两个英文单词」判定，因为 `.reds` /
  `.lua` 的字符串常量里混着 `if x == "Close"` 这种逻辑比较，放宽会翻坏功能。
- lua 的多语言段识别只认 `["en-us"] = { ... }` 这种**语言键表**写法（v2.39.0）。
  写成 `texts.en = {...}`、`{ en = {...}, de = {...} }`（无引号键）或按语言拆成
  多个函数的，识别不到，里面其他语言的文本仍会被当成待翻原文。真实模组里
  目前只见过带引号键这一种。
- 不给「短串 / 无空格」做形状过滤。`Close`、`Apply`、`Reload` 都是正常按钮名，
  按形状过滤会误杀一大片；短串的问题在提示词和重试（P12），不在判据。
- 不把 `settings.category` 的键翻成中文（P11）。想改显示名走 `getLabel` 覆盖或
  资源层的 `<name>-label`，两条路都不动键。
- 语言包回填与写回都按 `Name =` 的条目名配对（v2.38.1）。
  完全没有条目名的裸字面量（纯 `{ "a", "b" }` 数组项）写回时会报「缺少条目名」
  而不是去猜行号 —— 宁可漏写一条，也不能把译文写到邻行。同理，目标语言包里
  找不到该条目时留空/报失败，绝不借邻行的译文。
