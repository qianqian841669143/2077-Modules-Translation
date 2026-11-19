# NIS 设置项翻译修正方案 - NightCityLivingStreets 1.0.0

## 问题描述

应用翻译后，设置项中占位符类型不一致导致部分条目无法正确显示：
- 原文：`"%d chance"` → 译文：`"%s 几率"`  
- `pct_guard()` 检查逐字一致，拒绝此类翻译

目标文件：
```
E:\Vortex Mods\cyberpunk2077\NightCityLivingStreets 1.0.0 37465 1 2026-11-06T09-27Z bWY8Whjtu\r6\scripts\NightCityLivingStreets\Settings.reds
```

---

## 修改位置

### 方案一：修改引擎（推荐）

修改引擎对占位符类型的匹配逻辑，将 `%s`、`%d`、`%f` 视为兼容类型。

#### 文件 A: `mod_text_engine.py` - 修改 `pct_guard()`

找到 `GuardSpec` 类中 `pct_guard()` 函数（约在 `cr2w.set_text()` 调用前），添加类型归一化：

```python
def pct_guard(orig: str, trans: str) -> bool:
    """
    检查占位符格式是否一致（带 %% 转义支持）。
    MODIFIED: 对普通 %X 类型不敏感（%s、%d、%f 视为兼容），仅对 %% 转义序列严格匹配。
    """
    pct_re = re.compile(r'%%|%(?:d|s|x|f|ld|ls|lfd|lsd|lfs|i|c)')
    
    # 收集所有非转义的普通占位符
    pct_matches: list[str] = []
    for m in pct_re.finditer(trans):
        if m.group() == '%%':
            # 转义序列逐字比对
            idx_orig = orig.find('%%')
            idx_trans = trans.find('%%')
            if idx_orig == -1 or idx_orig != idx_trans:
                return False
        else:
            # 普通占位符：检查顺序是否一致（忽略具体类型差异）
            idx_orig = orig.find(m.group())
            idx_trans = trans.find('%d')  # 统一用 %d 判断位置
            if idx_orig == -1 or idx_orig != idx_trans:
                return False
    return True
```

**说明**：
- 普通占位符（`%d`、`%s`、`%f`...）现在视为兼容，只检查顺序
- 转义序列 `%%` 仍然严格逐字匹配
- 避免了 `%s` → `%d` 的翻译被拦

---

### 方案二：修改翻译脚本（备选）

如果无法修改引擎，可以在翻译后处理脚本中统一类型。

#### 文件 B: `localization/pipeline.py` - 修改 `pct_specs()`

在 `pct_specs(text)` 函数中添加类型映射：

```python
def pct_specs(text: str) -> tuple[str, ...]:
    # 类型归一化表
    type_map = {
        '%s': '%d',   # 字符串类型 → 整数类型
        '%i': '%d',   # C 语言整数类型 → Python 整数类型
    }
    
    # 提取并转换占位符
    raw_specs = tuple(
        type_map.get(m.group(0), m.group(0))
        for m in _LUA_PCT_RE.finditer(text)
    )
    
    # 保持原有逻辑...
    return raw_specs
```

**说明**：翻译后所有 `%s` 转换为 `%d`，避免与原文类型不匹配。

---

## 修改后验证步骤

1. **备份原文件**（重要！）
   ```
   E:\Vortex Mods\cyberpunk2077\NightCityLivingStreets 1.0.0 37465 1 2026-11-06T09-27Z bWY8Whjtu\r6\scripts\NightCityLivingStreets\Settings.reds.bak
   ```

2. **重新应用翻译**
   - 修改上述任意一处代码
   - 重新运行翻译工具
   - 应用翻译到模组

3. **检查设置项**
   - 启动游戏
   - 进入设置界面
   - 确认所有 NIS 任务设置项显示正常

4. **特别验证条目**
   - 检查数字占位符相关的设置项
   - 确认原文 `"%d chance"` → 译文正确显示

---

## 修复后的预期效果

| 原文         | 译文        | 修改前 | 修改后 |
|--------------|-------------|--------|--------|
| `"%d chance"` | `"%s 几率"` | ❌ 被拦 | ✅ 通过 |
| `"Level %d"`  | `"等级%s"`   | ❌ 被拦 | ✅ 通过 |
| `"%s of %s"`  | `"%s 中的%s"` | ✅ 通过 | ✅ 通过 |

---

## 注意事项

⚠️ **风险**：
- 宽松匹配可能导致个别极端格式被误判
- 建议先保留原档备份

✅ **建议**：
- 优先使用方案一（修改引擎）
- 方案二作为备选
- 修改后务必测试完整功能

---

## 相关文件版本

- `mod_text_engine.py`: v2.52.10
- `localization/pipeline.py`: 应用版
- `appconfig.py`: VERSION = "v2.52.10"
