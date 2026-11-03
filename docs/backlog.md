# 待办与已知问题

按优先级记，条目只在「决定做」时往下排，做完就删掉这一节，别让文档腐烂。
详细分析写进 `docs/P*.md`，这里只留「还要做什么」和「为什么不做什么」。

P11 已按实测定论关闭（见下「已修」），留下的三条（`CALL_RULES` 补
tab/subcategory、赋值式包装函数声明、键守卫）**不做**：全量 240 个键位字面量里
0 个会被当候选（键都是 camelCase 单词），加了只会让规则更绕。真出现带空格的键
（`settings.category("reward money", …)`）再加键守卫，那时它才是硬需求。

v2.43.0（批量输出目录 + 遮罩）、v2.43.1（M1–M6 译文层修复）、v2.43.2（遮罩层级）
v2.43.3（批量产物卫生 + 标记守恒）、v2.44.0（自动部署 + 归档层）、v2.45.0（机翻提速）、
v2.45.1（`@if` 守卫 + JSON 单词 + S3）、v2.45.2（S4/S6 + 全量复验）、
v2.45.3（覆盖回原模组的勾选真的生效）、v2.46.0（成对文本表 + 事件名守卫 + 官方弱匹配）、
v2.47.0（英文界面 100% + 功能说明）、v2.48.0（注册器别名 + 引擎指纹 + 记忆订正）、
v2.48.1（`<Rich …>` 标记补两遍 / 不闭合）、v2.49.0（设置页缓存清理）、
v2.50.0（批量结果导出）、v2.51.0（占位符顺序不再被换掉）、v2.51.1（缩写徽标不再被翻）、
v2.51.2（代号按值判断，shortName 真文案照收）、v2.51.3（归档预筛认 .json）、
v2.51.4（带空格的查表键不收）、v2.51.5（两段式查表键不收 + 跳过勾选持久化）
都已发布，见下「已修」。真实部署一轮已经在 v2.48.0 之前跑过（`deploy-20261102-090450.csv`）。

## 待办（v2.48.0 起）

1. **分组标题仍然只有兜底一条路**：NCPD First Response 的 `vehicles.lua` 用
   `groupOf()` 返回 `'MaxTac'` / `'Bikes'` / `'SUVs, vans & tactical'` /
   `'Patrol cars'`，这些字符串显示在选车菜单的分组头上，但既不是 `label =`、
   也不是 `return "…", opts` 那种菜单标题，位置证据为零；只有四词以上的
   `SUVs, vans & tactical` 被长句兜底收走。要收得再加一条「返回值被当分组名」的
   规则，而同一个文件里 `return 'roadblock'` / `'pursuit'` 是状态枚举——按形状分不开，
   暂时不做。
2. **对话框内菜单 `add(label, fn)` 的短标签同样收不到**：`_wrapper_rules` 见到
   `fn` / `callback` 形参就整条跳过（`safeCall` 的 label 只进日志，不能翻）。本地
   `add` 与 `safeCall` 形状完全一样，区别只在函数体把 label 存进选项表还是只写日志，
   要靠函数体区分，收益（每条菜单省一次英文）不如风险大，暂时不做。

**不做**：`run.jsonl` 部署后打 `deployed` 标记（原 B9）。部署过的模组在同一套参数下
重跑会被当成「已完成」整行跳过，得先「清空输出」；复原流程只要走清空就没问题，
「只想重跑这一个」现在有面板上的「↻ 重译」按钮（v2.45.1），按模组作废整行记录
已不需要。

**不做**：`copy_mod` 逐文件记 `mtime_ns` 指纹判「跑完后原件被改过」（原 B5）。
还原就是被允许的动作，改了也照覆盖，记一份指纹只是给清单加一列备注，不值当。
「原件已含中文」已经在部署清单里按文件标出来了。

## 已修（v2.51.5）

| 条目 | 做法 |
|---|---|
| `Gang Retaliation` 把 130 条**查表键**当正文收进表（`this.T("GR-MenuBoss")`、`text("GR-UiHomes", "中文兜底")`，一个 mod 130 行；键翻了游戏里显示原始键或空） | 模组自己封的查表函数 `this.T(…)` / `text(键, 兜底)` 既不在 `LOC_CALLS` 里、键又是两段式（`is_loc_key()` 要三段以上），两处一起漏。新增 `mod_text_engine.is_short_loc_key()`（大写缩写前缀 + 驼峰段、无空格、≤48 字符）与 `is_lookup_call()`（`LOC_CALLS` ∪ `t/tr/text/translate/loc`），在 `take()`、lua 长句兜底、reds 的 plain 档三处，只在「查表调用实参 / 没有位置证据」时拦。全盘 282 个模组实测：拦下的 130 行全在这一个 mod（init.lua 70 / Menu.reds 57 / Hud.reds 3），其余 281 个 mod 行数不变。自测 `check_short_loc_key()` |
| 表格里的「跳过」勾选不持久化（重载模组就复原，只能靠应用失败明细里的「标记为代码」） | 拆成两份记录：`user_skip.json` 还是「标记为代码」（隐藏），新增 `user_skips.json` 存「跳过」（`user_skip.py` 的 `skip_marks()` / `set_skip()`），键同代码标记（模组目录名 + 相对路径 + 原文 sha1）。前端 `setRowsSkip()` / `setFileSkip()` 调新接口 `remember_skips(items)`（`web_app.py`），加载时命中就把行**默认勾上**而不是隐藏——跳过是「暂时不想翻」，还得能取消；提示行「N 条按上次的「跳过」自动勾选」。实测：勾一条 → 重新加载 389 行里该行带 `skip=1`、`autoSkip=1`；取消后再加载回到 0。自测 `check_skip_persist()` |

## 已修（v2.51.4）

| 条目 | 做法 |
|---|---|
| `Immersive Cyberware` 的 `slotSupport.reds:79` 把查表键 `"Gameplay-Items-Item Type-Prt_Fragment"` 当长句文本收进表（`@wrapMethod` 顶掉 `UItemsHelper.GetSlotName` 后裸 `return`） | `is_loc_key()` 要求整串无空格、`getText`/`@runtimeProperty` 的排除只看调用位置，都管不到「裸返回的带空格键」。新增 `mod_text_engine.is_spaced_loc_key()`（带空格 + 至少两处**紧贴**的连字符 + 无 `A - B` 散开连字符 + 无句子标点 + ≤8 词），挂在长句兜底（lua）与 reds 的 plain 档两处，命中记 `FAMILY_LOCKEY`。只拦「没有位置证据」的字面量：`this.Text(key, value)` 的 value（`Desperate Sons-of-Bitches`，同文件 1321 条里的 2 条）照收。全盘 282 个模组实测只拦下 1 条（就是上面那个键），总行数 18,409。自测 `check_spaced_loc_key()` |

## 已修（v2.51.3）

| 条目 | 做法 |
|---|---|
| `Immersive Cyberware` 的物品名/描述（`Holo Display: Ads` 那句）抽不出来，单模组「加载/提取」只出 29 条 `.reds` 设置项 | 文本在归档里 `phoenicia\immersivecyberware\translations\translation_strings.json`；`wkit.localization_files()` 这个解包**前**的预筛只认路径段 `localization`/`l10n`/`onscreens`，三个都不占 → `has_localization()` 返回 False，`web_app.py:996` 直接跳过整包。改成「归档里有没有 `.json`」（真正的分类本来就是解包后看 CR2W 头，`archive.candidate_json()` 一直这么做）；纯资源归档没有 json 照样不解包。`_toc_key()` 加版本号 `v2` 作废旧缓存（缓存存的是布尔结论，不换号改了代码也不生效）。实测该 mod 现在 43 条、组包产物 `ImmersiveCyberware_zh-cn.archive` + `.xl` 正常。自测新增 `check_archive_prefilter()`，并订正 `check_own_product_and_prefilter()` 里「名字不像的 json 不放行」这条旧断言 |

## 已修（v2.51.2）

| 条目 | 做法 |
|---|---|
| `Enemies of NC` 的设置页签变成「神经连接」；同时 v2.51.1 按字段名排除 `shortName` 会连带遮住真文案（`shortName = "Auto Drive"` 这类） | 原文就是 `nativeSettings.addTab("/ENC", "ENC")`——模组名缩写，模型把 ENC 猜成 Neural Connection。改成**按值判断**：`is_acronym()` 认定 2-4 个字母的全大写串是代号，`is_label_text()` 据此不收（`shortName` 字段重新入 `SETTINGS_KEYS`，`_BADGE_FIELDS` 否决撤掉）；`_PLAIN_CAPS_WORDS` 白名单收「全大写但是英文词」的一批（SELL/NONE/RESET/CLEAR/GUN/JUMP…，按记忆库实测频次挑的），5 个字母以上一律照翻。已写坏的 `Enemies of NC\Modules\Native Settings.lua:10` 改回 `ENC`（游戏目录 + staging 硬链同一处），记忆库删掉 `ENC→神经连接`。自测 `check_acronym_guard()` 替换 `check_shortname_badge()` |

## 已修（v2.51.1）

| 条目 | 做法 |
|---|---|
| MCM 左侧模组列表每行前面顶着「中央情报…」，看着像模组名的一部分 | 那是 provider 的缩写徽标：`bridges\native_settings\provider.lua` 里 `shortName = "CET"`（Cyber Engine Tweaks）被模型猜成 Central Intelligence Agency，`bridges\rcf\provider.lua` 的 `"RCF"`（Redscript Configuration Framework）被猜成「快速破解」。`FIELD_TYPES` 去掉 `shortname`、`text_evidence()` 加缩写字段否决（`shortName`/`abbr` 一律当内部字段），已写坏的两处改回 `CET` / `RCF`（游戏目录 + staging + 批量副本），记忆库删掉 `CET→中央情报局`、`RCF→快速破解`。自测 `check_shortname_badge()` 钉住「`shortName` 不进表、同文件的 `name`/`label` 照常提取」|

## 已修（v2.51.0）

| 条目 | 做法 |
|---|---|
| MCM 里点「应用」后整块面板报 `bad argument #3 to 'format' (number expected, got string)`（`ModConfigurationMenuAPI\modules\core.lua:1344`） | 不是 MCM 的 bug：`string.format` 按位置取参数，译文把顺序换了。原文 `"Applied %d change(s) for %s."` 传的是 (数字, 名字)，中文写成「已为 %s 应用 %d 项更改。」——占位符个数一个不少，旧校验（`repair_markup` 的 `_count`）只数个数，于是照样写回，运行时第 3 个参数要 `%d` 却拿到 mod 名。新增 `mod_text_engine.swapped_spec_order()`：占位符序列不一致（个数相同）即软失败「占位符顺序变了, string.format 按位置取参数会崩」，`guard_level()` 与 `web_app._do_apply_source()` 两处写回都挂上；机翻提示词补一条「顺序也不能变」。已写坏的 4 处（`core.lua` 1344/1345、`workflows\collections.lua` 1068/1182）改写成保持原顺序的中文句子（参数未动），翻译记忆里 4 条同步订正。自测 `check_spec_order()` |
| 排查手段 | `tools/scan_lua_format.py`（新入库）：静态扫全盘 `string.format` 字面量，比对占位符个数与实参个数，并按 `#x` / `tostring(…)` / `.name` / 表构造 推断类型，报出「%d 拿到字符串」的必崩点，报告落到 `userdata\reports\lua-format-<时间>.txt`。全盘（CET 全部 353 个 `.lua`）跑完只剩 1 条与翻译无关的上游 bug：`MuchBetterContracts\gigTest.lua:1705` 用 `%d` 接 `GetFactStr()` 的返回值（模型作者自己的调试串）。另有一次性脚本用记忆库反查「顺序被换」的译文落在哪个文件的哪一行，定位到上述 4 处 |

## 已修（v2.50.0）

| 条目 | 做法 |
|---|---|
| 批量跑完只能在面板上看结果，想留档 / 发给别人看只能手抄（`deploy-<时间>.csv` 只有部署那几列，没有翻译统计） | 批量面板底部新增「导出结果」：`mod_batch.export_rows()` 把本轮 `_mb_results` 转成行（模组 / 状态 / 总行 / 待译 / 已有中文 / 记忆命中 / 机翻 / 写入 / 失败 / 未译 / 复制文件 / 耗时 / 部署 / 覆盖 / 新增 / 备注），`web_app.batch_export()` 弹保存框写 UTF-8 BOM 的 CSV（Excel 双击就开）。跑完同时把这份结果落进 `batch\last-run.json`，重开程序后还能导（`stale` 标记，提示「上次跑完的」；跑新一轮覆盖、`batch_clear` 与缓存清理一起删）。取消掉的模组不算跑完、不导出；批量运行中拒绝导出；没有结果时说清楚而不是写一个空文件。自测 `check_batch_export_rows()` 钉住「取消的不导出 / 状态与部署列对得上 / 每行列数与表头一致」 |

## 已修（v2.49.0）

| 条目 | 做法 |
|---|---|
| 跑过几轮批量之后 `userdata\` 越滚越大（实测批量副本 2.4 GB、中间文件 513 MB、官方语料导出 621 MB、撤销快照 146 MB），只能手工去资源管理器里翻 | 新增 `src/cache_tools.py` + 设置页「缓存清理」区块：六类缓存（`cache` / `work` / `batch\out` / `undo` / `corpus` / `logs`+`reports`）各显示大小与文件数，勾选后一次清掉并回报释放量。只删目录**里面**的东西，目录本身与批量输出目录的 `.mb-out-root` 印章留着（印章没了 `batch_clear` 会拒绝认领）；`memory.json` / `official_tm.sqlite` / `settings.json` / `glossary.json` / `locales\` 一个字节不动。`cache_clear` 在批量或机翻运行时拒绝执行。自测 `check_cache_tools()` 钉住「只删选中的 / 印章与记忆文件不动 / `../` 越界拒绝」 |

## 已修（v2.48.1）

| 条目 | 做法 |
|---|---|
| 义体说明在游戏里整段显示裸标记：`<Rich color="TooltipText.cyberwareDescription…">3% 受到的伤害…`（SenSen 四个模组 9 处） | 两个原因。① `repair_markup()` 拿**原文**（`.reds` 字面量，带 `\"`）去判断「开头标签在不在译文里」，模型的译文是裸引号，`lead in trans` 永远不命中 → 又补一份 → `<Rich …><Rich …>{int_0}%</>`：一开一闭不配对，游戏不再解析标记。改为包含判断前先还原转义引号（`_plain_escapes()`）。② 关闭标记以前只在「原文恰好一个 `</>`」时补；一句话多段高亮（`成本: -{int_2}%`）漏掉的 `</>` 就一直缺着。改为按两边 `</>` 数量差补，缺几个补几个，统一补在末尾（高亮范围偏大一点，文本照常显示）。自测 `check_repair_markup_escaped()` 钉住两种情形 |
| 已写坏的数据 | 4 个语言包删掉 9 处重复标签、3 条不闭合说明补上 `</>`，翻译记忆里 8 条同形条目一起改；全量 1,976 个 `.reds` 复查 `opens == closes` 归零 |

## 已修（v2.48.0）

| 条目 | 做法 |
|---|---|
| 模组自己的菜单注册器不认别名：NCPD First Response 全篇 `A('cruiser', 'Spawn police car', fn, '…')`，`A` 只是 `local A = Settings.RegisterAction` 的别名，于是这些标签只剩「长句兜底」（四词起）一条路——两三词的动作名永远进不了表，而已是中文的行因为命中兜底的中文分支照常提取，界面上就是「一半中文一半英文」 | `CALL_RULES` 补 `registeraction`（`(id, label, fn, desc)` → 参数 1 显示名称、3 描述）；新增 `_callee_aliases()` 认 `local X = Y.Register*` 这类别名，**只认名字里带 register 的**——MCM 的 `local t = ctx.text` 是查表函数，`t("settings.section.general")` 的实参是查表键，收进来就会把键翻掉（P13 那一类静默损坏），自测里专门钉住这条反例。实测 NCPD First Response 3 → 22 行；48 个模组 A/B：只有它变了，其余一条不差 |
| 菜单标题 `return "Request backup", opts` 不进表：模组自己把标题画在窗口顶栏，两三个词，兜底收不到 | 新增一条按形状收的规则（`\breturn "…", opts`），类型给「显示名称」。全量 904 个 lua 里只有 5 处命中，全在 NCPDFR（4 处英文 + 1 处已是中文），不会误伤 `return 'roadblock'` 这类状态枚举。加上这条 NCPDFR 共 26 行、18 个菜单项 + 1 个标题补上中文 |
| 升级换了提取规则，已部署的模组被「已是最新」整块跳过，新规则认出来的行永远进不了表（v2.46.0 的成对表、v2.48.0 的别名都吃这个亏） | `mod_deploy.extraction_stamp()`：`mod_text_engine.py` / `locale_pack.py` 的 sha1 记进 `deployed.json` 的 `done["ext"]`，`up_to_date()` 要求一致。对不上就重新过一遍，文件内容没变照样不重写（部署侧本来就有 sha1 相同即跳过）。算不出指纹时按「不能跳」处理 |
| 记忆里 5 条明显错的译文会一直被搬进新模组：`Siren→妖姬`、`Frisk→弗里斯克`、`Backup menu→备份菜单`、`Cuff→手铐`、`Stop / pull over→停车/靠边` | 用 `Memory.put(origin="manual")` 订正为 警笛 / 搜身 / 支援菜单 / 上铐 / 拦停/靠边。手改 rank 最高，之后机翻不会再顶掉 |

## 已修（v2.46.0）

| 条目 | 做法 |
|---|---|
| `Key = { "标签", "描述" }` 形状的文本表整片不提取（原待办 3）：ITP 的 `TEXT` 表 60 多条只有描述被长句兜底收走，短标签永远进不了行 | `_pair_table_entries()` 按 token 形状认表：整张表的**直接条目**都是 1~2 个裸字符串、带 `{}` 的条目过半、至少 3 条；左值当「显示名称」、右值当「描述」。值再过 `_entry_value_ok()`：无空格 token 里有 `.` / `_` / 数字 / 全小写就当成名字（`player_crouch_enter`、`NPCStatPreset.X`），色值走 `_HEX_COLOR_RE`。记录表（`desc = { district = "WESTBROOK" }`、`GameUI.Event = { Camera = "Camera" }`）因为 `{}` 条目占比为 0 被排除。实测 48 个模组：3,421 → 3,532 行，ITP 6 → 131；多收的是 ITP 标签与语音标签（ACM +3），少收的是事件名（ITP −5、GHXST −7、SenSen Herald −2） |
| `safeCall("事件名", fn)` 的首参被当文案翻（原待办 4）：写进过 `fppCamera:重置俯仰角`、`SetLocalPosition(来源)`、`交易系统：从插槽移除物品 (…)` | `looks_like_code_token()`：无空格却含 `:` / `()` / `=` / `<>` 的字面量不算可译文本（`is_translatable` 直接拒），`fppCamera:ResetPitch`、`handle:GameSessionEvent`、`TakeOverControlSystem:EnableX(false)` 都被挡；`_wrapper_rules` 遇到 `callback` / `cb` / `fn` / `handler` 形参的包装函数不再推参数位（`safeCall` 的 label 只进日志）。正常包装 `switch(path, label, desc)` 不受影响，自测钉住 |
| `official-weak` 对语言包单词条给错义（原待办 2）：`Body → 尸体`、`Black → 布莱克`，Garage Manager 13/22 条、Vehicle Core 6/20 条就是这么填进文件的 | `baseline.fill_rows` 对「`kind == json` 且原文是单个英文词」的行不采用官方弱匹配（`_weak_word_label`），由模型按词面译（实测 Body→身体、Black→黑色、Beige→米色）；多词条目的官方命中照旧，源码层（`.lua`/`.reds`）的单词条也照旧。记忆里已存下的 `official-weak` 同样不许顶上来。代价是少省 13/6 次模型调用，换掉两类别扭译法 |

## 已修（v2.45.3）

| 条目 | 做法 |
|---|---|
| 「跑完覆盖回原模组」勾上等于没勾：`save_settings` 是白名单式的，`batch_deploy` / `batch_deploy_dry` / `batch_hide_todo` 漏在名单外，前端存了也被丢掉，`batch_start` 读到的永远是默认值（不覆盖、只预演） | 三个键加进 `save_settings` 的布尔白名单；自测钉住「存进去 → 出现在 settings → 名单外的键仍被拒」。v2.44.0 上线以来没人踩到是因为一直手工剪切覆盖 |

## 已修（v2.45.2）

| 条目 | 做法 |
|---|---|
| S4「加载与机翻流水线重叠」（原待办 3） | `mod_batch` 拆成 `prepare`（复制 + 加载）与 `finish`（翻译 + 写回 + 部署），`run()` 用单线程池把**下一个模组**的 `prepare` 排在当前模组 `finish` 之前跑起来。`prepare` 不发事件，事件流仍是单线程按模组顺序发。实测 42 个模组：Σprepare 68.7 s + Σwork 96.0 s = 164.7 s 串行，实际 **151.2 s**（省 8%；加载已经比机翻便宜了，收益主要在归档重的模组）。顺带：`batch_cancel` 要把两个会话都喊停；`tm.save()` 加锁，别让两个会话同时写同一个 `.tmp` |
| S6「复跑整模组跳过」（原待办 3） | `deploy_mod` 多记一份 `done`（行数 / 已写 / 未译 / 失败）；`mod_deploy.up_to_date()` 要求「上一轮 `untranslated` 与 `failed` 都是 0」且「清单里每个文件的 sha1 与磁盘一致」——后者同时证明译文还在、也没被解压还原过。`_skip_reason()` 只在「跑完覆盖回原模组」真开着时生效（部署关掉时跳过等于不产出）。面板显示「已是最新」，部署清单 CSV 里标一句。实测临时目录里两个模组：第一轮 1.1 s、第二轮 0.1 s 全部跳过；GHXST Companion 这类（1,123 个文件）把清单全量核对一遍 0.46 s，实际只核对上一轮写入的几十个文件 |
| 全量复验（原待办 2） | 261 个 staging 目录预扫 21.4 s（41 个有待译行）；跑 42 个模组 151.2 s：20,049 行、写入 10,697（其中 7,694 行是「目标文件里本来就有」被跳过）、未译 134 条（代码碎片/缩写/型号名）、失败 7 条（xml/reds 个别行未匹配）、**`.bak` 0 个** |
| dry-run 真实清单（原待办 1） | `deploy-20261102-071452.csv`：42 行、覆盖 63 / 新增 2 / 其余相同；备注里两条「原件已含中文，已按覆盖策略覆盖」（AMM 的 `init.lua`、DVCore 的 `TrueFirstPerson\ChineseSimplified.reds`） |
| 目标语言包是模板 stub 时一条也补不进去（MBC 1475 条失败） | `apply_text_target` 的括号守卫把追加的行数**算了两遍**：`before` 是追加之后的形状，却又加了 `appended`。于是每次都对不上、整份文件放弃写入，报错里打的 `before`/`after` 还一模一样。改成直接比 `before == after`；MBC 现在一次写入 1,483 条、失败 0 |
| 批量卡死半小时以上（v2.45.0 引入并行读目录后开始出现） | `wkit.run` 的输出从管道改成临时文件：CLI 启动的子进程会继承管道写端，主进程退出后管道仍读不到 EOF，`subprocess.run` 超时后还会再无超时地 `communicate()` 一次 → 永久挂死。现在写文件、只 `wait()` 主进程、超时 `taskkill /T` 连树杀，并用模块级锁串行「起进程」这一段（Windows 句柄继承是进程级、有窗口期）。自测用一个「起孙进程占着 stdout 再退出」的假 CLI 钉住 |

## 已修（v2.45.1）

| 条目 | 做法 |
|---|---|
| 抽语言包类时丢掉 `@if(ModuleExists(...))`，`CyberwareEx\ChineseSimplified.reds:3` 无保护的 import → `UNRESOLVED_IMPORT` → 整层 REDscript 编译失败（原待办 1） | 新增 `_package_head()`：按顺序走 `module` / `import`，遇到 `import` 往回捡紧贴的 `@if` / `@else`（中间只允许空行与 `//` 注释）。不保留所有 `@` 行——修饰 `public func` 的注解会变悬空。自测：合成混写文件断言头部 4 行、函数注解不搬、`ensure_target` 生成的副本仍带守卫；真实 `CyberwareEx.reds` 现场复核，生成的 `ChineseSimplified.reds` 前 4 行带 `@if`，`this.Text` 2/2 |
| 「纯语言包」判据太宽：只有一个 `CODEWARE_PACKAGE_RE` 类、没有 `@addField` 就算纯，游离的 `public func` 会被整文件复制进语言包（同 module 重复声明） | `reds_is_pure_package` 再要求「类块之外只有 module / import / 空行 / 注释 / `@if`」。staging 里 692 个纯语言包逐个复核，0 个含游离行，收紧不误伤 |
| JSON 语言包里的单个单词不提取（`is_translatable` 只认两个词以上），车的部位名/颜色名整片英文（原审计的 45 条） | `extract()` 的 json 分支补 `_is_label_word`，与 reds/lua 判据一致。实测 Garage Manager 62→83 行（22 个单词）、Vehicle Core 12→32 行（20 个颜色名）；`weapon` / `true` / `12` 仍不收 |
| S3「没有待译行就不重写」：第 11 轮 104 个 `todo=0` 的模组里 17,060 行被原样重写回文件，还把它们当「本轮写回」重新记进记忆库 | `locale_pack.extract` 给「译文是从目标文件读回来的」行打 `inTarget`；`_do_apply_source` 在整组都是这类行且目标文件存在时跳过写回（`.reds` 的 Provider 注册/补条目仍照做），并把它们从写回后的记忆写入里剔除。json 另比一次叶子键（`json_needs_fill`），源文件新增的不可译键仍要补。`.bak` 比对抽成 `_drop_bak_if_same` 复用。实测 DigitalVixenCore 5,071 行不再重写（apply 0.51→0.32 s）、MCM 294 行里跳过 293 行；耗时省得有限，真正的批量大头仍是加载与机翻（v2.45.0 已处理） |
| 复原后重跑会先从 TM 拿回同一份坏译文（原待办 3 / B10） | 批量面板每行加「↻ 重译这个模组（丢弃本轮记忆）」：`batch_forget` 调 `mod_deploy.forget_in()`（只删 machine/memory 来源），再删 `run.jsonl` 里这个模组那行，续跑才会真重翻。面板只在部署清单里有该模组的译文对时显示按钮（`batch_scan` 里读一次清单，不逐个模组读） |
| 工具栏溢出（原待办 4） | 复核后确认已不溢出：`.row` 在 v2.42.1 之后就有 `flex-wrap`，加上「导出 Excel」收进「更多」，1280 与最小窗口 1000 都是整行装下（headless 渲染截图核对）。无代码改动 |

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
