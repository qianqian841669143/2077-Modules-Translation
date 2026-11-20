-- imgui_hook.lua —— 把 CET 的 ImGui 界面文字换成中文（2026-11-20）
--
-- 为什么需要它：不少模组的设置页是自己用 ImGui 画的（ImGui.Checkbox /
-- ImGui.Button / ImGui.Text …），根本不经过 MCM，账本替不到它们。实例：
-- Complete Third Person Overhaul —— 539 条译文全在账本里，界面上却一个都
-- 不显示，因为它的 UI 全是 ImGui 直绘（init.lua 的 M.UI.check 就是
-- ImGui.Checkbox）。init.lua 只挂 MCM 的 provider，那个界面它永远够不着。
--
-- 做法：在调用之前把「文本参数」那一格换成译文。只换账本里**整串**有的，
-- 不认识的原样传给 ImGui，所以最坏情况是漏译，不会把界面搞乱。
--
-- 由 init.lua 在读完账本之后 loadfile + 调用，返回接管了几个函数。
-- 单独一个文件是为了让 init.lua 的改动只有三行；这里语法错了也只是钩子不生效，
-- init.lua 那边是 pcall 包的。

local unpack = unpack or table.unpack   -- LuaJIT 是 5.1，只有全局 unpack

-- 取元表，保护 metatable（__metatable 字段）和非 table 返回值。
local function mtOf(v)
  local ok, mt = pcall(getmetatable, v)
  if not ok or mt == nil or type(mt) ~= "table" then
    return nil
  end
  return mt
end

-- 能不能当函数调用。Lua 函数直接是；sol2 绑出来的东西是 userdata / table，
-- 靠 __call 元方法调用 —— CET 的 ImGui 成员就是这种。
-- 2026-11-20 13:40 那次日志「读到 0 个绘制函数」，就是因为这里只认
-- type(v) == "function"，24 个名字全被跳过，一次赋值都没试过。
local function isCallable(v)
  local t = type(v)
  if t == "function" then
    return true
  end
  if t ~= "userdata" and t ~= "table" then
    return false
  end
  local mt = mtOf(v)
  return mt ~= nil and mt.__call ~= nil
end

return function(lookup, log)
  local kind = type(ImGui)

  -- CET 的 ImGui 是 sol2 绑定的 **userdata**，type() 返回 "userdata"。
  -- 2026-11-20 第一版只判 "table"，于是每次启动都停在「接管 0 个绘制函数」，
  -- CTO 那类自绘设置页一个字都没换（日志里就是这个 0）。
  if kind ~= "table" and kind ~= "userdata" then
    if log then
      log(string.format("ImGui 钩子：ImGui 的类型是 %s，没法接管", kind))
    end
    return 0
  end

  -- 同一串每帧都会来问一遍：查过一次就记住。
  -- 存 false 表示「账本里没有」，跟「没查过」（nil）区分开。
  local memo, memoN = {}, 0

  -- 诊断：ImGui 本身、它的成员、元表各是什么。只看「接管 N 个」分不清
  -- 是成员读不到、不是函数，还是写不回去。
  if log then
    local okp, probe = pcall(function() return ImGui.Text end)
    local mt = mtOf(ImGui)
    log(string.format(
      "ImGui 钩子：ImGui=%s，ImGui.Text=%s，metatable=%s，__index=%s，__newindex=%s",
      kind, okp and type(probe) or "读取报错",
      mt and "有" or "无",
      mt and type(mt.__index) or "-",
      mt and type(mt.__newindex) or "-"))
  end

  local function tr(s)
    if type(s) ~= "string" or s == "" or #s > 200 then
      return nil
    end
    local hit = memo[s]
    if hit ~= nil then
      return hit or nil
    end
    if memoN > 20000 then          -- 换模组/换场景之后旧的没用了，别无限长
      memo, memoN = {}, 0
    end
    local zh = lookup(s)
    memoN = memoN + 1
    if type(zh) == "string" and zh ~= "" and zh ~= s then
      memo[s] = zh
      return zh
    end
    memo[s] = false
    return nil
  end

  local wrapped = 0
  local seen = 0        -- 读到过几个绘制函数：用来区分「读不到」和「写不回去」
  local writeErr = nil  -- 第一次写回去的报错原文（原来被 pcall 吞掉了）
  local failed = {}     -- 有、但写不回去的名字

  -- 第一个参数就是显示文字的：签名不变，只是把那一格换掉再转给原函数。
  -- 不用 {...} + unpack 转发：ImGui.Text 一帧要调几百次，别每次都建表。
  local function wrapFirst(name)
    local okr, f = pcall(function() return ImGui[name] end)
    if not okr or not isCallable(f) then
      return
    end
    seen = seen + 1
    local ok, err = pcall(function()
      ImGui[name] = function(label, ...)
        if type(label) == "string" then
          local zh = tr(label)
          if zh then
            label = zh
          end
        end
        return f(label, ...)
      end
    end)
    if ok then
      wrapped = wrapped + 1
    else
      failed[#failed + 1] = name
      if writeErr == nil then
        writeErr = tostring(err)
      end
    end
  end

  -- TextColored 的文字在最后一位（r,g,b,a,text）
  local function wrapTextColored()
    local okr, f = pcall(function() return ImGui.TextColored end)
    if not okr or not isCallable(f) then
      return
    end
    seen = seen + 1
    local ok, err = pcall(function()
      ImGui.TextColored = function(r, g, b, a, t)
        if type(t) == "string" then
          local zh = tr(t)
          if zh then
            t = zh
          end
        end
        return f(r, g, b, a, t)
      end
    end)
    if ok then
      wrapped = wrapped + 1
    else
      failed[#failed + 1] = "TextColored"
      if writeErr == nil then
        writeErr = tostring(err)
      end
    end
  end

  -- 只收「第一个参数是给人看的文字」的那些；Begin / End / BeginChild / PushID
  -- 之类传的是 id，改了会串窗口，一律不碰。
  local first = {
    "Text", "TextWrapped", "TextDisabled", "TextUnformatted", "BulletText",
    "Checkbox", "Button", "SmallButton", "Selectable", "MenuItem",
    "SliderFloat", "SliderInt", "SliderAngle", "DragFloat", "DragInt",
    "Combo", "BeginCombo", "CollapsingHeader", "BeginTabItem", "TreeNode",
    "RadioButton", "InputText", "InputTextMultiline", "SetTooltip",
  }
  for i = 1, #first do
    wrapFirst(first[i])
  end
  wrapTextColored()

  if log and wrapped == 0 then
    if seen == 0 then
      log(string.format(
        "ImGui 钩子：%d 个名字里一个都没读到可调用对象（成员类型见上面那行诊断）",
        #first + 1))
    else
      log(string.format("ImGui 钩子：读到 %d 个绘制函数，但一个都写不回去：%s",
        seen, tostring(writeErr)))
    end
  elseif log and #failed > 0 then
    log(string.format("ImGui 钩子：%d 个里有 %d 个写不回去（%s…）",
      seen, #failed, table.concat(failed, "/", 1, math.min(#failed, 6))))
  end
  return wrapped
end
