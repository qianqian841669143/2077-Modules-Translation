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

return function(lookup, log)
  if type(ImGui) ~= "table" then
    return 0
  end

  -- 同一串每帧都会来问一遍：查过一次就记住。
  -- 存 false 表示「账本里没有」，跟「没查过」（nil）区分开。
  local memo, memoN = {}, 0

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

  -- 第一个参数就是显示文字的：签名不变，只是把那一格换掉再转给原函数。
  -- 不用 {...} + unpack 转发：ImGui.Text 一帧要调几百次，别每次都建表。
  local function wrapFirst(name)
    local f = ImGui[name]
    if type(f) ~= "function" then
      return
    end
    local ok = pcall(function()
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
    end
  end

  -- TextColored 的文字在最后一位（r,g,b,a,text）
  local function wrapTextColored()
    local f = ImGui.TextColored
    if type(f) ~= "function" then
      return
    end
    local ok = pcall(function()
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
    log("ImGui 钩子：一个函数都没接上（CET 的 ImGui 表是只读的？）")
  end
  return wrapped
end
