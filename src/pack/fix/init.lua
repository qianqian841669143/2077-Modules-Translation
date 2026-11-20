--[[
  zzz_CP2077_Localization
  读 2077 Mod Translator 生成的账本（zzz_CP2077_Localization/ledger/all.lua），
  在 MCM 里把「模组名 / 分类名 / 设置项名 / 描述」换成中文。

  只改显示层，不动任何 mod 的源文件，也不动 sourceId：
    - listMods 返回的 mods[i].name
    - loadMod  返回的 schema.name / schema.categories[i].name /
               schema.categories[i].items[j].label / .description

  账本由工具按模组切分生成；本 mod 只读，不写任何账本。
  日志：CET 的 scripting.log（搜 zzz_CP2077_Localization）。

  依赖：Cyber Engine Tweaks + ModConfigurationMenuAPI
]]

local MOD_NAME  = "zzz_CP2077_Localization"
local LEDGER_MOD = "zzz_CP2077_Localization"
local LEDGER_REL = LEDGER_MOD .. "/ledger/all.lua"
local RETRY_MAX  = 900          -- 约 15 秒

---------------------------------------------------------------------------
-- 日志
---------------------------------------------------------------------------
local LOG_FILE = nil
local BASE = nil

local function log(msg)
  local text = tostring(msg)
  print(string.format("[%s] %s", MOD_NAME, text))

  if LOG_FILE ~= nil then
    local f = io.open(LOG_FILE, "a")
    if f then
      f:write(os.date("[%H:%M:%S] "), text, "\n")
      f:close()
    end
  end
end

---------------------------------------------------------------------------
-- 定位账本
--
--   CET 加载 init.lua 时 chunk 名不带路径，debug.getinfo 拿不到盘上位置；
--   但 package.path 里有每个 mod 的绝对路径（CET 加的），从这里截出
--   ...\cyber_engine_tweaks\mods，再换成账本所在的 mod 目录。
---------------------------------------------------------------------------
local function readable(path)
  local f = io.open(path, "rb")
  if not f then
    return false
  end
  f:close()
  return true
end

local function findLedger()
  local cands, seen = {}, {}

  local function add(p)
    if type(p) ~= "string" or p == "" then
      return
    end
    p = p:gsub("^@", ""):gsub("[/\\]+$", "")
    if not seen[p] then
      seen[p] = true
      cands[#cands + 1] = p
    end
  end

  if type(package) == "table" and type(package.path) == "string" then
    for entry in package.path:gmatch("[^;]+") do
      local dir = entry:match("^(.*)[/\\]%?%.lua$")
      if dir == nil then
        dir = entry:match("^(.*)[/\\]%?[/\\]init%.lua$")
      end
      if dir ~= nil then
        dir = dir:gsub("[/\\]+$", "")
        local parent = dir:match("^(.*)[/\\][^/\\]+$")
        if parent ~= nil then
          add(parent .. "/" .. LEDGER_REL)
        end
        add(dir .. "/ledger/all.lua")      -- 本 mod 恰好就在补丁包里时
      end
    end
  end

  -- CET 的 io.open 沙箱只认「相对本 mod 目录」的路径（裸文件名）。
  -- 实测 CET 里 tostring(package.path) 是空串，上面那套候选一个都拿不到；
  -- 带 mods/ 前缀的相对路径会被沙箱直接拒（Tried to access invalid path）。
  -- 所以裸相对路径排第一，其余留作别的加载方式下的兜底。
  add("ledger/all.lua")
  add("mods/" .. LEDGER_REL)
  add("plugins/cyber_engine_tweaks/mods/" .. LEDGER_REL)
  add("bin/x64/plugins/cyber_engine_tweaks/mods/" .. LEDGER_REL)

  for _, p in ipairs(cands) do
    if readable(p) then
      return p
    end
  end
  return nil
end

---------------------------------------------------------------------------
-- 读账本、建查表
---------------------------------------------------------------------------
local bySource = {}      -- sourceId -> { 原文 = 译文 }
local globalMap = {}     -- 原文 -> 译文（跨模组兜底）
local namesMap = {}      -- sourceId -> 中文显示名（人工整理的优先）
local total = 0

local function addEntry(sourceId, orig, zh)
  if type(orig) ~= "string" or orig == "" then
    return
  end
  if type(zh) ~= "string" or zh == "" or zh == orig then
    return
  end

  globalMap[orig] = zh

  if type(sourceId) == "string" and sourceId ~= "" then
    local m = bySource[sourceId]
    if m == nil then
      m = {}
      bySource[sourceId] = m
    end
    m[orig] = zh
  end

  total = total + 1
end

-- base 为空表示账本走的是沙箱认的裸相对路径（"ledger/all.lua"），
-- 这时同级文件也用裸文件名 —— 不能拼 "./" 或别的目录前缀，沙箱会拒。
local function relPath(base, name)
  if base == nil or base == "" then
    return name
  end
  return base .. "/" .. name
end

-- 模组名单独一张表（names.lua）：人工整理的优先，账本只兜底没有的那些
local function loadNames(base)
  local p = relPath(base, "names.lua")
  local chunk = loadfile(p)

  if not chunk then
    log("没有 names.lua，模组名走账本兜底")
    return
  end

  local ok, data = pcall(chunk)

  if not ok or type(data) ~= "table" then
    log("names.lua 读不进来：" .. tostring(data))
    return
  end

  local n = 0

  for k, v in pairs(data) do
    if type(k) == "string" and type(v) == "string" and v ~= "" then
      namesMap[k] = v
      n = n + 1
    end
  end

  log(string.format("names.lua：%d 条模组名", n))
end

local function lookupName(sourceId)
  if type(sourceId) ~= "string" then
    return nil
  end
  return namesMap[sourceId]
end

local function loadLedger()
  local path = findLedger()

  if path == nil then
    log("找不到账本（" .. LEDGER_REL .. "），本 mod 不生效")
    return false
  end

  local chunk, err = loadfile(path)

  if not chunk then
    log("账本读不进来：" .. tostring(err))
    return false
  end

  local ok, data = pcall(chunk)

  if not ok then
    log("账本执行失败：" .. tostring(data))
    return false
  end

  if type(data) ~= "table" then
    log("账本不是 table")
    return false
  end

  for _, mod in pairs(data) do
    if type(mod) == "table" then
      local ids = mod.sourceIds or {}
      local entries = mod.entries or {}

      for _, e in ipairs(entries) do
        if type(e) == "table" then
          if #ids == 0 then
            addEntry(nil, e.o, e.z)
          else
            for _, sid in ipairs(ids) do
              addEntry(sid, e.o, e.z)
            end
          end
        end
      end
    end
  end

  -- 账本就在补丁包里，顺手把日志也写到那里。
  -- base 是账本所在目录：走裸相对路径时是空串（沙箱基准就是本 mod 目录），
  -- 这时后面的文件也用裸文件名。
  local base = path:match("^(.-)[/\\]?ledger[/\\]all%.lua$")

  if base ~= nil then
    LOG_FILE = relPath(base, "text_override.log")
    loadNames(base)
    -- ImGui 自绘界面（CTO、AMM 那一类）不走 MCM，账本替不到它们。
    -- 单独一个文件，语法错了也只是钩子不生效，这里 pcall 兜着。
    local chunk, lerr = loadfile(relPath(base, "imgui_hook.lua"))
    if chunk then
      local okh, install = pcall(chunk)
      if okh and type(install) == "function" then
        -- 第二个参数是 log：不传的话钩子里的诊断全静默，只剩那句「接管 N 个」
        local ok2, cnt = pcall(install, function(t) return globalMap[t] end, log)
        if ok2 then
          log(string.format("ImGui 钩子：接管 %d 个绘制函数", tonumber(cnt) or 0))
        else
          log("ImGui 钩子没装上：" .. tostring(cnt))
        end
      else
        log("imgui_hook.lua 读不进来：" .. tostring(install))
      end
    else
      log("没有 imgui_hook.lua（ImGui 界面不翻译）：" .. tostring(lerr))
    end
  end

  log(string.format("账本 %s：%d 条", path, total))
  return total > 0
end

local function lookup(sourceId, text)  if type(text) ~= "string" or text == "" then
    return nil
  end

  if type(sourceId) == "string" and bySource[sourceId] ~= nil then
    local hit = bySource[sourceId][text]
    if hit ~= nil then
      return hit
    end
  end

  return globalMap[text]
end

---------------------------------------------------------------------------
-- 打补丁
---------------------------------------------------------------------------
local replaced = 0

local function patchSchema(schema, sourceId)
  if type(schema) ~= "table" then
    return
  end

  local name = lookupName(sourceId) or lookup(sourceId, schema.name)
  if name ~= nil then
    schema.name = name
    replaced = replaced + 1
  end

  local categories = schema.categories

  if type(categories) ~= "table" then
    return
  end

  for _, cat in ipairs(categories) do
    if type(cat) == "table" then
      local cn = lookup(sourceId, cat.name)
      if cn ~= nil then
        cat.name = cn
        replaced = replaced + 1
      end

      -- MCM API 的三个 bridge（native_settings / ms / rcf）都把设置项放在
      -- `settings` 里，老结构才叫 `items`。2026-11-20 之前只认 items，
      -- 于是 Codeware「原生设置」那几十个模组的设置项一条都没替到，
      -- 界面里全是英文（provider 明明挂上了 3/3）。
      local list = cat.settings

      if type(list) ~= "table" then
        list = cat.items
      end

      if type(list) == "table" then
        for _, item in ipairs(list) do
          if type(item) == "table" then
            local ln = lookup(sourceId, item.label)
            if ln ~= nil then
              item.label = ln
              replaced = replaced + 1
            end

            local dn = lookup(sourceId, item.description)
            if dn ~= nil then
              item.description = dn
              replaced = replaced + 1
            end
          end
        end
      end
    end
  end
end

local function patchProvider(provider)
  if type(provider) ~= "table" or provider.__textOverride then
    return false
  end

  local originalList = provider.listMods
  local originalLoad = provider.loadMod
  local wrapped = false

  if type(originalList) == "function" then
    provider.listMods = function(...)
      local snapshot, err = originalList(...)

      if type(snapshot) == "table" and type(snapshot.mods) == "table" then
        for _, mod in ipairs(snapshot.mods) do
          local sid = tostring(mod.id or "")
          local nm = lookupName(sid) or lookup(sid, mod.name)
          if nm ~= nil then
            mod.name = nm
          end
          -- rcf bridge 的模组条目还带 description
          local md = lookup(sid, mod.description)
          if md ~= nil then
            mod.description = md
          end
        end
      end

      return snapshot, err
    end

    wrapped = true
  end

  if type(originalLoad) == "function" then
    provider.loadMod = function(sourceModId, ...)
      local schema, err = originalLoad(sourceModId, ...)

      if type(schema) == "table" then
        patchSchema(schema, tostring(sourceModId or ""))
      end

      return schema, err
    end

    wrapped = true
  end

  if wrapped then
    provider.__textOverride = true
  end

  return wrapped
end

---------------------------------------------------------------------------
-- 主流程
---------------------------------------------------------------------------
local function apply()
  local mcm = GetMod("ModConfigurationMenuAPI")

  if type(mcm) ~= "table" then
    return false, "MCM 未加载"
  end

  if type(mcm.providers) ~= "table" or type(mcm.providers.listProviderIds) ~= "function" then
    return false, "MCM 没有 provider 接口"
  end

  local ok, ids = pcall(mcm.providers.listProviderIds)

  if not ok or type(ids) ~= "table" then
    return false, "listProviderIds 调用失败"
  end

  if #ids == 0 then
    return false, "provider 尚未注册"
  end

  local hooked = 0

  for _, pid in ipairs(ids) do
    local provider = mcm.providers.getProvider(pid)

    if patchProvider(provider) then
      hooked = hooked + 1
    end
  end

  if hooked == 0 then
    return false, "没有可挂接的 provider"
  end

  log(string.format("已挂接 %d/%d 个 provider，查表 %d 条", hooked, #ids, total))

  if type(mcm.refreshIndex) == "function" then
    local rok, rerr = pcall(mcm.refreshIndex, mcm)

    if not rok then
      log("refreshIndex 失败：" .. tostring(rerr))
    end
  end

  return true
end

---------------------------------------------------------------------------
-- 事件
---------------------------------------------------------------------------
local patched = false

registerForEvent("onInit", function()
  log("启动")

  if not loadLedger() then
    patched = true        -- 没账本就不必逐帧重试
    return
  end

  local ok, why = apply()

  if ok then
    patched = true
  else
    log("首次挂接未成功（" .. tostring(why) .. "），改为逐帧重试")
  end
end)

local frames = 0

registerForEvent("onUpdate", function()
  if patched then
    return
  end

  frames = frames + 1

  if frames > RETRY_MAX then
    log(string.format("重试 %d 帧仍失败，放弃", RETRY_MAX))
    patched = true
    return
  end

  local ok, why = apply()

  if ok then
    patched = true
    log(string.format("重试成功，累计替换 %d 处", replaced))
  elseif frames % 180 == 0 then
    log(string.format("第 %d 帧仍未就绪（%s）", frames, tostring(why)))
  end
end)
