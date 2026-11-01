# 待办与已知问题

按优先级记，条目只在「决定做」时往下排，做完就删掉这一节，别让文档腐烂。
详细分析写进 `docs/P*.md`，这里只留「还要做什么」和「为什么不做什么」。

P11 已按实测定论关闭（见下「已修」），留下的三条（`CALL_RULES` 补
tab/subcategory、赋值式包装函数声明、键守卫）**不做**：全量 240 个键位字面量里
0 个会被当候选（键都是 camelCase 单词），加了只会让规则更绕。真出现带空格的键
（`settings.category("reward money", …)`）再加键守卫，那时它才是硬需求。

v2.43.0（批量输出目录 + 遮罩）、v2.43.1（M1–M6 译文层修复）、v2.43.2（遮罩层级）
v2.43.3（批量产物卫生 + 标记守恒）、v2.44.0（自动部署 + 归档层）、v2.45.0（机翻提速）
都已发布，见下「已修」。

## 待办（v2.45.1 起）

1. **抽语言包类时丢掉 `@if(ModuleExists(...))`，会让整层 REDscript 编译失败**（v2.45.1 修）。
   `CyberwareEx.reds` 这类「语言包 + 逻辑」混写文件，头部是：

   ```reds
   module CyberwareEx
   import Codeware.Localization.*
   @if(ModuleExists("CyberwareEx.Customization"))
   import CyberwareEx.Customization.*
   ```

   `_package_class_block()` 抽语言包类时只保留以 `module ` / `import ` 开头的行
   （`src/locale_pack.py:1310`），`@if` 被丢掉 → 生成的
   `CyberwareEx\ChineseSimplified.reds` 第 3 行是无保护的
   `import CyberwareEx.Customization.*`。缺装那个可选子模块的玩家这里直接
   `UNRESOLVED_IMPORT`（游戏日志 `redscript_rCURRENT*.log` 实测：
   `At …\CyberwareEx\ChineseSimplified.reds:3:1: module 'CyberwareEx.Customization'
   has no members or does not exist`），然后
   `REDScript compilation has failed` —— **整个脚本层所有模组都不加载**。

   影响面：全量 1949 个 `.reds` 里指向不存在 module 的 import 有 50 条，未加 `@if`
   的只有这 1 条（用户 06:21 已手工补上）；「同一 module 别处加了 `@if`、语言包里
   没加」的潜伏形态 0 处。用当前代码现场跑 `_package_class_block()` 仍会吐 3 行头，
   所以**删掉那个语言包重新生成就会复发**。

   修法：`_package_class_block` 的头部改成「按顺序走，`module`/`import` 保留；
   遇到 `import` 时往回捡紧贴着的 `@if` / `@else`（跳过空行与 `//` 注释）」。
   不能简单保留所有 `@` 开头的行——同文件里修饰 `public func`
   （`@if(ModuleExists("CyberwareEx.Customization"))` + `public func IsCustomMode()`）
   的注解会变成悬空注解。配套自测：源文件带 `@if` 守卫的 import，抽出的新文件里
   必须仍带着它；并断言「头部里每个 import 在原文件里被 `@if` 包着时，新文件也包着」。

2. **`run.jsonl` 部署后打 `deployed` 标记**（原 B9）：部署过的模组在同一套参数下
   重跑会被当成「已完成」整行跳过，得先「清空输出」。复原流程只要走清空就没问题，
   所以先不做；真出现「我只想重跑这一个」再加按模组的作废。
3. **「重译这个模组（丢弃本轮记忆）」入口**（原 B10）。`mod_deploy.forget_in()`
   已经能按清单里的 `(orig, tgt)` 调 `Memory.forget()`，缺的是面板上那个按钮。
   在这之前，还原后重跑仍会先命中 TM、拿回同一份坏译文——这是复原流程里唯一的坑。
4. **工具栏溢出**（v2.42.1 记）：工具条那一行按钮不换行，窗口窄的时候右侧会被裁。
   已把「导出 Excel」收进「更多」，还剩 6 个（浏览 / 加载·提取 / 应用到模组 /
   撤销上次应用 / 批量翻译 / 设置 / 更多）+ 一个 grow 输入框。
   根治要么给 `.row` 加 `flex-wrap`，要么继续往菜单里收。
   判据：窗口宽 1280 时整行不溢出。
5. **dry-run 跑一轮真实清单**：`batch_deploy_dry` 默认开，第一次真部署之前先看
   `userdata\batch\deploy-<时间>.csv` 对不对。
6. **全量 127 模组重跑复验**：`.bak`=0、撤销记录=0、部署清单核对
   （v2.43.3 只在 3 个模组上验过，v2.44.0 的部署更是只跑了自测）；
   顺便量一次 v2.45.0 提速后的真实总时长。
7. **机翻提速的下一步**（v2.45.0 只做了 S1/S2/S5）：
   - S3「没有待译行就不重写」：第 11 轮 104 个 `todo=0` 的模组里仍有 17,060 行被
     重写回文件，归档还要重建重打包；
   - S4「加载与机翻流水线重叠」：19.5 min 里 11.8 min 加载 + 7.7 min 机翻，两者
     本可并行，预估再省 30%，但改动大、要处理内存与取消路径；
   - S6「复跑整模组跳过」：指纹与 TM 版本都没变就跳过，20 分钟 → 1–2 分钟。

**不做**：`copy_mod` 逐文件记 `mtime_ns` 指纹判「跑完后原件被改过」（原 B5）。
还原就是被允许的动作，改了也照覆盖，记一份指纹只是给清单加一列备注，不值当。
「原件已含中文」已经在部署清单里按文件标出来了。

## 已修（v2.45.0）

| 条目 | 做法 |
|---|---|
| 批量 19.5 分钟里 60% 花在「没有待译行」的模组上，而模型只占很小一块 | 先量清楚：模型 8 条一批 0.90 秒（关思考后 83 token），WolvenKit 读一次目录 2.05 秒、`convert` 一个 5.5 KB 文件 2.8 秒——瓶颈是进程启动 |
| 归档里十几种语言全序列化（Nearby Auto Loot 19 个 / Equipment-EX 13 个，每个 2.8 秒） | `archive.lang_of_rel()` + `candidate_json(langs=...)`：只保留源语言与目标语言，语言码认不出的一律保留。`Session(source_lang=...)` 从 `res_source_lang` 传入。Nearby Auto Loot 58.8 s → 9.7 s、Equipment-EX 40.3 s → 10.0 s，行数不变（14 / 66） |
| 多归档模组串行读目录（Much Better Contracts 72 个、0 个有本地化，147 秒） | `_scan_resource_layer` 用 4 路线程池并行读，`wkit.has_localization` 结果缓存到 `userdata\cache\archive_toc.json`。176.9 s → 53.9 s（首次）→ 7.8 s（命中）。缓存键是「文件名 + 大小 + 开头 64 KB 的 sha1」，**不带路径与 mtime**——批量每次复制到输出目录，带 mtime 的版本第二轮仍 51 秒 |
| 模型侧零碎开销 | ① `warmup()` 一轮只调一次（同一对「接口+模型」30 分钟内复用），以前 127 个模组各一次；② 整批解析失败先整批重试一次（多给 256 token），再退化成逐条；③ 批大小按平均原文长度自适应（>160 字符用 4 条）；④ `serialize`/`deserialize` 前检查文件存在——输入不存在时 CLI 直接 `0xC0000005` 崩溃，不报错 |

**下一步（没做）**：S3「没有待译行就不重写」、S4「加载与机翻流水线重叠」、S6「复跑直接跳过」，见下文另一节。

## 已修（v2.44.3）

| 条目 | 做法 |
|---|---|
| 游戏里 `REDScript compilation has failed`：`DigitalVixenCore\translations\FACryCapacity\ChineseSimplified.reds:155` 两条 `this.Text(...)` 被拼成一行，少了 `);` 和换行 | 该语言包里 `FACC.UI.rowBodySlots` 出现两次（155 / 220 行，两个函数里同名）。写回按条目名定位字面量，两条同名行指向同一区间 → 两条编辑同位置；从后往前写第二条时旧 `end` 已失效，多切 5 个字符（`);` + 换行）。改为：同名条目只计一次但**每一处都写**（`index` 从「key→lit」改成「key→[lit]」，并跳过重复行），编辑按 `(start,end)` 去重；写盘前用 `locale_pack._punct_shape()` 对一次去掉字符串后的括号结构，对不上就整批放弃并报「写回会破坏括号结构」 |
| 影响面核实 | staging 1949 个 `.reds` 与原始下载包逐个比对（去字符串后数括号）：653 个被改过的文件里**只有这一个**结构变了。已就地修复（恢复两行 + 补 220 行的中文），staging 与游戏目录是硬链接，两边同时恢复；staging 与下载包结构现已一致 `(211,211,2,2)` |

## 已修（v2.44.2）

| 条目 | 做法 |
|---|---|
| 批量面板一屏近一半是「无需翻译」的目录（255 个 staging 目录里 128 个 `todo == 0`）（v2.42.1 记） | 面板加「隐藏无需翻译」勾选框：`renderQueue()` 只按 `batch_scan()` 已经算好的 `todo` 过滤，不重新扫盘；读取失败的目录不藏（那是要处理的信号），底部显示「已隐藏 N 个无需翻译的目录（M 个里）」。勾选状态存 `batch_hide_todo`，不随扫描重置 |

## 已修（v2.44.1）

| 条目 | 做法 |
|---|---|
| 批量跑到 Equipment-EX：`WolvenKit convert 失败（exit 3221225477）`，8 条译文全记为失败 | 0xC0000005 是 CLI 自己崩了：拿崩溃现场那份 JSON 重跑 20 次全过，与内容无关、是抖动。`builder.build` 改为逐资源重试一次；仍失败只跳过该资源（其余照常打包，`.xl` 不声明被跳过的），失败的行从「已写入」扣掉并报「N 个资源转换失败已跳过（对应 M 条未写入）」。现场（出错 JSON + CLI 完整输出）留在 `work\<mod>\build\<产物>\failed\` |
| CLI 崩溃的报错只有 `System.CommandLine` 调用链，看不出异常 | `wkit._error_tail()` 优先捞「异常/error」行再补尾部；以前只取末尾 8 行，恰好永远是最没信息的那 8 行 |

## 已修（v2.44.0）

| 条目 | 做法 |
|---|---|
| 部署过一次之后，`_resolve_dir` 会挑中我们自己写的 `X_zh-cn.archive`，把中文当原文再翻一遍 | `archive.is_own_product()`：名字带语言码后缀 **且** `.xl` 里的资源路径是我们的写法（`l10n\` / `localization\<lang>\subtitles\`）才认；`_resolve_dir` / `resolve_all` 先剔掉自家产物再挑；目录里只剩产物时直接报错。`_pick_xl()` 同时把「非空 > 带 extend > 配套归档更大」当排序依据，不再按 rglob 顺序随机命中 |
| 一个模组只扫一个归档（AMM 有 3 个，本地化表只在 `AMM_Characters.archive` 里）（`web_app.py:924`） | 新增 `archive.resolve_all()` + `pipeline.SessionGroup`：逐个归档扫、行上带 `archive`，`apply_rows` 按归档分流，`build` 各出各的包（产物名按归档名区分），`changed` 取并集；一个归档炸了不影响其它 |
| 未译行不进报告（`trans` 为空 / `trans == orig` 被静默剔除，约 1% 漏译显示 `failed=0`） | `mod_batch.run_one` 加 `untranslated` + `untranslatedSample`，面板与部署清单都显示 |
| `has_localization` 是子串判断，AMM 因此误报「有本地化」 | 抽出 `wkit.localization_files()` 按路径段判断（`localization` / `l10n` / `onscreens` 目录，或文件名带 `localization`），并在注释里写清它只是预筛，最终以扫出的行数为准 |
| 批量跑完还得手工把 127 个模组搬回 staging | 新增 `src/mod_deploy.py`：按文件覆盖（副本里没有的 `.archive` / 贴图一个字节不动），每个 mod 一条 `ApplyTransaction` 撤销记录，`dry_run` 只算不写；`batch_deploy` / `batch_deploy_dry` 两个设置 + 面板两个勾选框 |
| 部署后「解压还原 → 重跑」会拿到旧结果 | `deployed.json` 记 相对路径 + 部署后 sha1 + `added`：`copy_mod` 只在「路径命中**且** hash 相同」时跳过，还原过的文件 hash 一变就被读回来 |
| 批量跑完没有清单 | `userdata\batch\deploy-<时间>.csv`：一个 mod 一行（覆盖 / 新增 / 相同 / 未译 / 失败 / 备注），`mb-done` 里也带部署汇总 |

## 已修（v2.43.3）

| 条目 | 做法 |
|---|---|
| 批量产物里 633 个 `.bak`（19.2 MB）+ 127 条指向副本的撤销记录（112 MB） | `_do_apply(keep_backup=False, make_undo=False)`：批量写的是输出根里的副本，这份副本就是要部署的成品，备份和撤销都没有落脚点。`run_one` 写完再扫一遍删残留 `.bak`（`drop_backups()`，`prepare_codeware_package` 自己补的那几个也一起清） |
| 译文丢标记/占位符：`<Rich ...>` 被当成 HTML 整段吞掉、`{value}` 被写成 `%s` | 新增 `E.repair_markup()`：开头/结尾的标签、单个 `</>`、`{value}`↔`%s` 各一个时都能确定位置，直接补回；补不回来的占位符（只丢高亮配色不算）按软失败拦下。实测 6,465 条改写里 4 条自动修好、0 条误拦 |
| 一份 `.reds` 里多个语言包类时把每一段都当原文（BNFTweaks 74 条，法语段被写成中文；JBlinkDash 韩语段同样） | `text_noise._class_lang()` 认类名后缀与驼峰词（`...EnUs` / `JBlinkDashKorean` / `EnglishPackage`），命中两种以上语言时只留源语言段。BNFTweaks 74 → 37，法语/韩语段原样不动 |
| Provider 配对时整个文件都当语言包：`@runtimeProperty` 设置文案带着「写进 `ChineseSimplified.reds`」的目标去写回，永远匹配不上 | `extract(only_class=...)` + `_package_class_ranges()`：只收源语言那个类块里的条目。INCF 10 → 1（5 条「未找到对应原文」消失）、Cyberware-EX 6 → 2（4 条消失），三个小模组端到端实测失败归零 |

## 已修（v2.43.1）

| 条目 | 做法 |
|---|---|
| 模组自带的中文包换了名字就不认（IAH `zh-cn.reds`、Reinforcements `zh_cn.reds`） | `target_rel()` 多收一个 `root`，先在同目录找目标语言的变体（`zh-cn`/`zh_cn`/`ChineseSimplified`/`SimpChinese`，大小写与 `-`/`_` 不敏感；lua 的 `zh_CN.lua`/`zh-cn.lua` 同理），多个命中时按「有中文内容 > 作者自己的命名 > 字数多」挑。实测两个模组都改写进已有的中文包，`class` 对上 `SimpChinese`/`GR_zh_cn`，不再新建文件 |
| 接线失败后留下英文副本 → 同 module 两个同名类，模组装进游戏编译失败 | `_do_apply_source` 记住本轮是否由 `ensure_target` 新建，失败分支把 `dst` 与 `.bak` 删掉 |
| Provider 写成 `if Equals(language, n"en-us")` 时中文包没人引用（4 个 SenSen 模组，73 条） | `_add_provider_case` 增加 if/Equals 形式，插在 `return null;` 前；并且发现型 Provider 接不上时也报 error，不再「日志 ok 但不生效」 |
| 目标语言包是模板 stub，条目比英文少（RCF 19/194） | `_append_reds_texts` 把缺的条目追加到 `DefineTexts` 末尾：括号配对跳过字符串找到最后一条 `this.Text(...)`，缩进/换行沿用该行；定位不到就照常报失败 |
| 语言包与逻辑同文件，整份拒绝（Garage Core 224 / Cyberware-EX 6 / INCF 6） | `_package_class_block` 抽出 `class X extends ModLocalizationPackage {…}` 整块 + module/import 头部，改名写进新文件；自检「恰好 1 个类、`this.Text` 条数 = 源文件、能当纯语言包用」，不过关就拒绝。三个模组的 Provider 都补上了 `case n"zh-cn"` |
| `.bak` 被复制进副本（624 个 / 20 MB）、内容没变也留备份（5 处） | `copy_mod` 跳过 `.bak`；写回后比一次字节，正文没变就撤掉 `.bak`。`src/tmp_wa*.py` 也清掉 |

## 已修（v2.43.0）

| 条目 | 做法 |
|---|---|
| 批量产物只能写 `userdata\batch\out`，想放别的盘得跑完再搬 | 新设置项 `batch_out_dir`（`appconfig.DEFAULT_SETTINGS`），面板与设置对话框都能改。老配置里没有这个键时补旧路径，升级后不会被空白挡住；用户主动清空之后才是真的空 |
| 输出目录留空时**仍然会跑** | 空 = 不跑：面板的「开始批量翻译」变灰并给提示，`batch_start` 直接返回「还没有设置批量输出目录」，一个目录都不复制 |
| 目录可自由设置后，`run_one` 的 `rmtree` 会落到模组本体上 | 两层校验。界面层拒绝盘根、与 staging / 游戏目录重叠、程序数据目录；`mod_batch.run_one` 再按路径关系挡一次（目标必须在输出目录内部，且不能与源目录互为祖先/后代——`out_root` 指到 staging 时 `dst` 正好就是那个模组） |
| 「清空输出」是递归删除，怕删到别人的文件夹 | 产物目录首次使用时盖 `.mb-out-root`；`batch_clear` 只清盖过章的目录，旧固定目录直接认领，其余一律报错不动手 |
| 换输出目录后，续跑记录还会把模组当成「已完成」 | `_mb_sig()` 把 out_root 算进参数指纹，换目录即作废 |
| 批量面板能中途关掉，关掉之后主界面就活了，能点出第二次批量 | 复用机翻的 `#overlay`：`enterBatchMask()` / `exitBatchMask()` 管 `main.app.inert`、进度条与 `N / M` 计数。**遮罩永远在最上层**（`z-index: 400`，高于 `.mask.top` 的 300），面板压在下面；「显示面板」临时收起遮罩看列表，面板一关遮罩立刻回来（v2.43.2 修正：早先让面板 `.mask.top` 浮在遮罩上，违反「遮罩在最上面」） |
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
