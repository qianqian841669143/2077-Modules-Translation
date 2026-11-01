# 待办与已知问题

按优先级记，条目只在「决定做」时往下排，做完就删掉这一节，别让文档腐烂。
详细分析写进 `docs/P*.md`，这里只留「还要做什么」和「为什么不做什么」。

P11 已按实测定论关闭（见下「已修」），留下的三条（`CALL_RULES` 补
tab/subcategory、赋值式包装函数声明、键守卫）**不做**：全量 240 个键位字面量里
0 个会被当候选（键都是 camelCase 单词），加了只会让规则更绕。真出现带空格的键
（`settings.category("reward money", …)`）再加键守卫，那时它才是硬需求。

v2.43.0 已发（批量输出目录 + 批量遮罩，见下「已修」）。剩下的按 M1–M6 三批做：
译文层的失败和「journal ok 但游戏里不生效」优先，UI 细节与自动部署最后。

## 待办：v2.43（译文正确性 + 覆盖率）

v2.42.6 修掉了「目标文件被英文冲掉」，但 2026-11-01 18:17 那轮（127 个模组）全量
比对 `batch\out` 之后，654 条失败只落在 5 个根因上，另外还有 4 个模组「journal 全
ok、游戏里却没有中文」。下面把旧条目和这些新发现按「同一处代码、同一类失效合并」
重排，分 3 批，每批单独编译验证。

基线：127 模组 / rows 24,074 / applied 23,249 / failed 654 / untouched 37；
简体语言包 7,509 个条目里 458 个仍不是中文（6.1%）。

### 批 1（v2.43.1）：先不产生坏文件

1. **M1 目标语言文件定位统一**（合并旧「lua 语言包文件名」+ 本轮 IAH / Reinforcements）
   现状：只认 `TARGET_NAMES` 算出的那一个名字（reds 是 `ChineseSimplified`，lua 是
   `zh_CN`）。模组自带的 `zh-cn.reds`（类 `SimpChinese`）、`zh_cn.reds`（类 `GR_zh_cn`）
   认不出来，工具反而去复制英文包 → 类名冲突 → 跳过，229 条没写进去
   （Inventory Adjustments Hub 192、Reinforcements Gang Vs Gang 37）。
   lua 那边同理：CET 是 `require("localization/" .. language)`、`language` 取自游戏
   设置（`zh-cn`），按 `TARGET_NAMES` 生成的 `zh_CN.lua` 读不到（这轮 staging 里
   0 个 `zh-cn.lua`，没触发，但判据要从根上解决）。
   做法：抽一个解析函数，`target_rel` / `_existing_target` / `prepare_codeware_package`
   共用，顺序
   ① 同目录已有变体（`zh-cn` / `zh_cn` / `ChineseSimplified` / `SimpChinese`，大小写
      与 `-`/`_` 不敏感）→ 用它；
   ② provider 里 `case n"zh-cn"` 实际指向的类所在文件 → 用它；
   ③ 都没有才按 `TARGET_NAMES` 新建。
   判据：变体匹配只认同目录且文件名/类名带目标语言标记，不许跨目录挑。
2. **M2 失败不留半成品**
   `ensure_target` 先把英文包复制成目标文件，`prepare_codeware_package` 再报「类名已被
   占用」并 `continue`，复制出来的文件没人删。本轮实测两个模组因此装进游戏会编译失败
   （`SYM_REDEFINITION`）：
   - IAH `locales/ChineseSimplified.reds` 与 `en-us.reds` 逐字节相同，同 module
     `IAHub.Localization`、同 `class English`；
   - Reinforcements `Packages/ChineseSimplified.reds` 与 `en_us.reds` 逐字节相同，
     同 module `Gibbon.GR.Localization.Packages`、同 `class GR_en_us`。
   做法：失败分支删掉本轮新建的 `dst` 和 `.bak`（只删本轮新建的）。
3. **M6 卫生**
   `copy_mod` 排除 `*.bak`（out 里现在 624 个 / 20.1 MB；Cyberpunk 按 `*.reds` /
   `init.lua` 扫描，这些不生效但占地方）；写入前确认真有替换再备份（现在 5 次内容没
   变也备份了）；删掉 `src/tmp_wa1.py`、`src/tmp_wa2.py`（10/30 的暂存脚本）。

### 批 2（v2.43.2）：覆盖率

4. **M3 Provider 接线失败要报错**
   `_add_provider_case` 只认 `switch` + `default:`。SenSen 那几个模组写成
   `if Equals(language, n"en-us") {...}`，找不到 `default` 就原样返回，且非
   `exact_rels` 路径不报错 → 中文包已经生成（如 `ChineseSimplified-skeletonkey.reds`，
   翻译完整）但没有任何代码引用它，73 条译文白做，journal 还全 ok。
   做法：支持 `if Equals(...)` 形式；无 `default:` 时插在 `return null;` 前；
   接不上就返回 error —— 宁可报失败，也不要「ok 但没生效」。
5. **M4 目标包缺条目时追加**
   Redscript Config Framework 自带的 `ChineseSimplified.reds` 只有 19 条（模板 stub），
   `English.reds` 有 194 条 → 174 条报「目标语言包里没有这个条目」。
   做法：`apply_text_target` 找不到 key 时往 `DefineTexts` 末尾追加
   `this.Text("key", "译文")`，缩进与换行风格沿用相邻行；定位不到块尾就不追加、报失败。

### 批 3（v2.43.3）：结构改写，单独一版

6. **M5 混合 .reds 只抽 package 类**
   Garage Core 的 `GarageCoreLocalization.reds` 同时有 `LocalizationProvider`（`switch`
   里只有 en-us / de-de / kr-kr）和 `English` 包（226 条 `this.Text`），整文件复制会重复
   声明所以被拒绝；Cyberware-EX、INCF CORE 同理，共 236 条没写。
   做法：抽出 `class X extends ModLocalizationPackage {…}` 整块生成新文件，再给 provider
   的 `switch` 插 `case n"zh-cn": return new …;`。
   硬约束：块边界按括号配对 + 字符串/注释处理（`this.Text` 的值里有 `</>` 富文本），
   抽完自检「恰好 1 个类、括号平衡、`this.Text` 条数 = 源文件」，不自检就报错拒绝，
   **不许退回整文件复制**。

### 验收判据（改完重跑一遍批量）

| 指标 | 现在 | 目标 |
|---|---|---|
| `failed` | 654 | 6（4 条「未找到对应原文」+ 2 条「疑似代码片段」，都是合理拒绝） |
| 简体语言包未译条目 | 458 / 7,509（6.1%） | 24 / 7,509（0.3%），RCF 另多 174 条 |
| 重复类文件 | 2 个 | 0 |
| 译文写入但没有代码引用 | 4 个模组 | 0 |
| `.bak` | 624 个 / 20.1 MB | 0 |

### 硬约束

- **自动部署（下节第 3 条）必须排在 M2 / M5 之后**：现在 `out` 里还有会让模组编译
  不过的文件，先上线自动部署等于把它们直接推进 staging。
- M5 单独一版编译，出问题好定位；M1 / M2 / M6 可以一次编译完。

## 待办：UI 与部署（v2.44.0 起）

v2.42 把批量换成单模组流程之后，批量跑一遍就会把单模组层的毛病放大 127 倍。
以下按「用户已决定后续处理」记着，暂时不改：

1. **工具栏溢出**（v2.42.1 记）：工具条那一行按钮不换行，窗口窄的时候右侧会被裁。
   已把「导出 Excel」收进「更多」，还剩 6 个（浏览 / 加载·提取 / 应用到模组 /
   撤销上次应用 / 批量翻译 / 设置 / 更多）+ 一个 grow 输入框。
   根治要么给 `.row` 加 `flex-wrap`，要么继续往菜单里收。
   判据：窗口宽 1280 时整行不溢出。
2. **批量翻译面板加筛选：隐藏「无需翻译」的条目**（v2.42.1 记）。
   扫描后列表里把待译行数为 0 的 mod 折起来，只留能翻的。
   实测定量：255 个 staging 目录里只有 127 个有待译行，其余 128 个 `todo == 0`，
   一屏里近一半是噪音。
   做法：`batch_scan()` 的输出里每项都带 `todo`（`probe_text()` 顺带算的），
   前端 `renderQueue()` 按这个值过滤就行，不用再扫一遍盘。
   默认值定「显示」还是「隐藏」待实跑后再定；勾选状态要能留在面板里不随扫描重置。
3. **批量跑完直接替换 / 不能替换的作为新增**（v2.42.1 记）。
   现状：批量只写 `userdata/batch/out/<mod>`（固定 latest），staging 原目录全程只读，
   跑完还得用户自己搬，127 个 mod 手工搬不现实。
   目标：跑完自动部署 —— 能安全替换的**原地覆盖**原目录，不能的**作为新增**放进去，
   原目录一个字节都不动。
   判定「不能替换」的主要风险，排序：
   - **副本里缺文件**：`skip_assets=True` 跳掉贴图/音频/视频/字体，
     `with_archive=False` 跳掉 `.archive`（实测 297 个 9.93 GB）。
     照「删目录再拷」的方式替换 = 把这些原文件全删了，mod 直接坏。
     所以覆盖只能**按文件覆盖**，副本里没有的保留原样。
   - 跑完后原目录又被 Vortex 改过（mtime / 大小与扫描时记录的不一致）。
   - 原目录里本来就有中文（官方译或别人译），覆盖会丢。
   - 语言包命名变体（`zh-cn.lua`，见上述 M1）导致新文件没生效、旧的反而被顶掉。
   实现要点：部署前先记一份原目录清单（相对路径 + 大小 + mtime）做判据；
   替换要留 undo（现有 undo store 能回滚文件）；新增用带后缀的目录名
   （如 `name + " (zh)"`）避开同名冲突，不要覆盖已存在的同名目录。
   待定：以上风险里哪些算「不能替换」、要不要让用户逐个确认，开工前定。

## 已修（v2.43.0）

| 条目 | 做法 |
|---|---|
| 批量产物只能写 `userdata\batch\out`，想放别的盘得跑完再搬 | 新设置项 `batch_out_dir`（`appconfig.DEFAULT_SETTINGS`），面板与设置对话框都能改。老配置里没有这个键时补旧路径，升级后不会被空白挡住；用户主动清空之后才是真的空 |
| 输出目录留空时**仍然会跑** | 空 = 不跑：面板的「开始批量翻译」变灰并给提示，`batch_start` 直接返回「还没有设置批量输出目录」，一个目录都不复制 |
| 目录可自由设置后，`run_one` 的 `rmtree` 会落到模组本体上 | 两层校验。界面层拒绝盘根、与 staging / 游戏目录重叠、程序数据目录；`mod_batch.run_one` 再按路径关系挡一次（目标必须在输出目录内部，且不能与源目录互为祖先/后代——`out_root` 指到 staging 时 `dst` 正好就是那个模组） |
| 「清空输出」是递归删除，怕删到别人的文件夹 | 产物目录首次使用时盖 `.mb-out-root`；`batch_clear` 只清盖过章的目录，旧固定目录直接认领，其余一律报错不动手 |
| 换输出目录后，续跑记录还会把模组当成「已完成」 | `_mb_sig()` 把 out_root 算进参数指纹，换目录即作废 |
| 批量面板能中途关掉，关掉之后主界面就活了，能点出第二次批量 | 复用机翻的 `#overlay`：`enterBatchMask()` / `exitBatchMask()` 管 `main.app.inert`、进度条与 `N / M` 计数；面板运行时加 `.mask.top`(300) 浮在遮罩(80) 之上，关掉面板遮罩仍在，遮罩上留「显示面板」 |
| 面板页脚的「打开输出目录」 | 删掉，入口并到新那一行的「打开」；`q-staging` 那种「面板改了没保存、跑的还是旧值」的毛病一起修（开始前先 `save_settings`） |
| 顺带：`tools/selftest.py` 的 `Anti-Tracking Breach` 期望 67 条 | staging 里现在是 2.0 版，同一份代码（HEAD 与工作区各跑一次）都是 56 条，不是回归；期望值改成 56 |

## 已修（v2.42.6）

| 条目 | 做法 |
|---|---|
| `zh-cn.json` 里作者已译好的条目被**英文冲掉**（MCM：`"访问"` → `"Access"`，79 条） | 真因不在翻译，在 `locale_pack.write_json_target_ex`：它读 **en-us.json 当模板**、把 mapping 覆盖上去再整份写成 zh-cn.json。extract 没提取到的键（单个词、纯符号这类短值）就原样保留英文，把既有的中文挤掉了。改为**以目标文件为底**：目标存在就以它为准，源文件只补「源有、目标没有」的新键（`_merge_fill`）；目标不存在或损坏才复制源骨架。实测 MCM 242 条译文写入、80 条非 mapping 键全部保留 |
| 同一处还有两个口子会拿原文覆盖译文 | `apply_many_ex` 与 `mod_batch.run_one` 都加 `trans == orig` 跳过：模型对短词/专名原样返回时，写回只有坏处。两条路都堵，因为单模组模式走前者、批量模式走后者 |
| 批量失败 881 条**查不下去** | `_do_apply_source` 本来就把 `failed: [{file,line,orig,trans,reason,level}]` 返回了，但 `_mb_journal` 只挑固定字段写盘，`errors` 从没落过。现在 journal 增加 `reasons`（失败原因计数）与 `untouched`（没翻出来所以没写的条数），`batch.log` 的 done 行也带上原因摘要 |

## 已修（v2.42.5）

| 条目 | 做法 |
|---|---|
| 批量跑到第 44 个 mod **闪退**（`0xc0000005` / `python314.dll+0x5283f`，WER 16:36:44，dump `21708.dmp`） | 崩溃栈落在 `tm.py:60 save()` → `json.dump` → `json/encoder.py:389 yield _encoder(key)`，而 `_encoder` 就是 C 的 `c_encode_basestring`。这已经是第三次栽在 CPython `_json` 的 C 编码器上（前两次是 `c_make_encoder` 的参数行），访问违例在 Python 层接不住，只能绕开 C 实现：`appconfig` 导入时把 `c_make_encoder` 置 None、`encode_basestring(_ascii)` 换成 `py_*` 版本 |
| 绕开 C 编码器后的开销 | 实测可忽略：18727 条 / 3.5 MB 的 `memory.json`，纯 Python 序列化 0.105 s（C 版约 0.02 s） |
| TM 每条补翻都写一次盘 | `Memory.save()` 加 3 秒节流（`force=True` 不限），`_run_translate` 里连续补翻不再反复序列化同一份字典 |
| 并发是否可疑 | 已排除：700 万次 `put` + 400 次并发 `save` 零异常（`put` 的 `_trim()` 会整体替换 `entries`，但旧 dict 有引用计数保护） |

## 已修（v2.42.4）

| 条目 | 做法 |
|---|---|
| 批量跑到第 25 个 mod **卡死** 11 分钟（这次不是崩溃：进程在、单核 100%、`batch.log` 不写、事件端口连不上、到 Ollama 零连接） | `py-spy` 抓栈在 `_extract_lua` 逐字面量的 `re.search` 上：pattern 用的是字符串字面量，只能靠 `re._cache`（512 项），线程一多就被别的 pattern 挤掉、每次重新编译。改成模块级 `ASSIGN_TAIL_RE` / `LIST_HEAD_RE` 预编译，再加 `{` / `=` 的 C 层剪枝。`Base.lua`（1.09 MB / 20857 个字面量）6.18 s → 0.93 s，`_do_load` 8.5 s → 1.5 s |
| 卡住时用户完全看不出来 | 面板新增「事件通道已经 N 秒没有响应，批量可能卡在某个模组上」+「关掉重开再点开始，已完成的会自动跳过」 |
| `fetch` 一失败就退回 pywebview 桥 | 卡死时 GIL 被占，退回桥只会把线程堆到 215 个（实测），越救越糟。改为原地重试，60 秒仍不通才停轮询并保留提示 |

## 已修（v2.42.3）

| 条目 | 做法 |
|---|---|
| 批量跑到第 54 个 mod 又整窗消失（`0xc0000005` READ at `-1` / `python314.dll`） | v2.42.2 只是把「后台推」换成「前端拉」，拉本身仍过 pywebview 桥（每调一次新开线程 + `evaluate_js` 回写）。这次把批量事件搬到 `src/evbus.py`：只监听 `127.0.0.1` 的随机端口，前端 `fetch` 取，整条路径不碰 COM；`batch_poll` 保留为 fetch 失败时的兜底 |
| 桥调用次数 | 一次 10 分钟批量从 1300+ 次降到 0 次（只有开面板/扫描/开始各 1 次） |

## 待办：把单模组的 `_emit` 也搬到 evbus（观察后再定）

单模组机翻 / 应用的进度仍是 `_emit` → `window.evaluate_js`（从工作线程）。
调用量比批量少一个量级，目前没崩过，这次先不动。
若在单模组里也复发：`_emit` 改 `self._bus.push(obj)`、前端常驻轮询即可，
`onEvt` 已能处理全部事件类型（`rows` / `progress` / `done` / `applied` …）。

## 已修（v2.42.2）

| 条目 | 做法 |
|---|---|
| 批量跑到第 28 个 mod 整窗消失（`0xc0000005` / `python314.dll`） | 后台线程不再直接 `window.evaluate_js`，事件先塞 `deque`，前端 `batch_poll` 每 150 ms 取走；后端线程完全不碰 WebView2 COM |
| 崩一次就白跑 | `userdata/batch/run.jsonl` 每个 mod 落一行，重跑自动跳过已完成的（参数指纹变了才作废）；「清空输出」连带删掉 |
| 没有现场 | `userdata/logs/crash.log`（`faulthandler`，含所有线程的 Python 栈）+ `batch.log`（每个 mod 的 start/done） |

## 已修（v2.42.1）

| 条目 | 做法 |
|---|---|
| 导出 Excel 收进「更多」 | 工具条腾出一个按钮位。`btn-export` 的绑定按 id 走，位置变了不用改 JS；菜单点击后 `setTimeout(close, 0)` 自动收起 |

## 已知但不打算改

- 不提供「忽略骨架校验」开关。那等于把「写坏整层脚本、游戏里 redscript 全不加载」
  做成一个可点的按钮（DVCore 那次：`))` 被吃掉，括号 7/8 → 5/4，
  `REDScript compilation has failed`）。
- 不提供「整批强制写入」，只提供单条强制（soft 级失败行上的「仍要写入」）。
  批量强制一次写坏几十行，undo 只回滚文件，回滚不了用户没看见的错误。
  hard 级（骨架不一致 / 数据行段数变了 / 能力清单）**连单条强制都没有**。
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
  按形状过滤会误杀一大片；短串的问题在提示词和单条补翻，不在判据。
- 不把 `settings.category` 的键翻成中文（P11）。想改显示名走 `getLabel` 覆盖或
  资源层的 `<name>-label`，两条路都不动键。
- 语言包回填与写回都按 `Name =` 的条目名配对（v2.38.1）。
  完全没有条目名的裸字面量（纯 `{ "a", "b" }` 数组项）写回时会报「缺少条目名」
  而不是去猜行号 —— 宁可漏写一条，也不能把译文写到邻行。同理，目标语言包里
  找不到该条目时留空/报失败，绝不借邻行的译文。
- 术语表不自动加 `Codeware → Codeware` 这类「保名」条目（P17 第 4 条）。
  现有记忆库里有 15 条含 Codeware 的记录，其中 1 条是错译（`义体软件`），
  要不要清洗是数据问题，不是代码问题；改它是「按当前库的错」做一次性动作，
  下次换个模组又会漂。

## 已修（v2.41.0）

细节见 `docs/P21位置证据.md`。

| 条目 | 做法 |
|---|---|
| P21 位置证据（新） | `name_position_starts`：方法名 / 插槽名 / CName / 能力清单按位置拦，全量 A/B 只掉 41/6517 条候选 |
| P18 竖线数据行 | `NOISE.is_data_row` + 写回时的段数/属性硬守卫 |
| P16 失败分级 | soft（代码碎片，可单条覆盖）/ hard（必坏，无出口）；骨架失败改为二分隔离肇事行，不再一条干掉整批 |
| P19-3/4 标记为代码 | `user_skip.json`，按 (相对路径, 原文 sha1) 持久化，加载时直接过滤 |
| P13 查表键 | `LOC_CALLS` 接到 Lua 路径（`repeatable_ncpd_gigs` 4 条假候选归零） |
| P11 显示名 getter | `getLabel=function() return "…" end` 直取字面量（`_GETTER_RE`）；全量命中 12 处全是显示名，其中 `repeatable_ncpd_gigs` 的 5 个按钮名是汉化漏掉的 |
| P14 拼接空格 | 字面量紧邻 `..` 时保留原文首尾空白 |
| P12 空译文本 | 批量后逐条补翻一次；应用前对账「这些文件还有 N 条没译」，写回后报「候选 / 写入 / 跳过」 |
| P17 echo | 原样返回不再算「已填」，进补翻通道；`{…}` ≥3 或 `\n` ≥4 的串直接走单条 |
| P15 归档未变 | `set_text` 记「内容真的变了」的条目，一条都没变就不重建归档 |
| P4 加载失败 | 路径先不落 `state.modPath`，加载成功后才写；失败时输入框退回 |
| P6 未应用改动 | `dirty` 生命周期补齐（加载/应用后清、机翻回填置），加载前确认 |
| P20 静默失败 | 非 ASCII 标识符不吞文件；目标语言码大小写无关（v2.40.1） |
