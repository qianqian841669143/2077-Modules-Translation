-- zzz_CP2077_CETZh / init.lua
--
-- CET 界面的翻译入口：把 ImGui 的文本函数包一层，用词典替换。
--
-- 为什么需要它：随包安装的 CP2077UITranslator.dll 挂的是 inkTextWidget 的文本
-- 函数，管得到游戏原生 UI 和 MCM 桥接到 ink 的那部分；但 CET 自己的浮层
-- （nativeSettings 面板、各 mod 的设置窗口、MCM 的 ImGui 分支）走的是
-- ImGui::Text 这一套，一个字节都不经过 ink，dll 再准也碰不到。
--
-- 这里不做任何永久修改：只在内存里给 ImGui 的函数加一层转发，原函数照旧调用，
-- 词典里没有的串原样放行。停用这个 mod 立刻完全复原。

local DICT_NAME = "dict.tsv"
local MOD_DIR = "bin/x64/plugins/cyber_engine_tweaks/mods/zzz_CP2077_CETZh/"

local dict = {}
local hits, misses = 0, 0

local function loadDict()
    local f = io.open(DICT_NAME, "r")
    if not f then
        -- CET 的 cwd 是 mod 目录（AppearanceMenuMod 就是这么读自己文件的）；
        -- 万一换版本改了工作目录，再按游戏根 + mods 路径拼一次。
        f = io.open(MOD_DIR .. DICT_NAME, "r")
    end
    if not f then
        print("[CETZh] 找不到 " .. DICT_NAME)
        return 0
    end
    local n = 0
    for line in f:lines() do
        local s, d = line:match("^(.-)\t(.+)$")
        if s and d and #s > 0 and s ~= d and dict[s] == nil then
            dict[s] = d
            n = n + 1
        end
    end
    f:close()
    return n
end

-- (函数名, 文案参数位置)。只包「某个参数是纯文案」的函数；
-- TextColored 例外（颜色在前，文本在第 5 个）。
local TARGETS = {
    Text = 1, TextWrapped = 1, TextDisabled = 1, BulletText = 1,
    TextUnformatted = 1, LabelText = 1, TextColored = 5,
    Selectable = 1, MenuItem = 1, Button = 1, SmallButton = 1,
    Checkbox = 1, RadioButton = 1,
    TreeNode = 1, TreeNodeEx = 1, CollapsingHeader = 1,
    Begin = 1, BeginChild = 1, BeginMenu = 1, BeginTabItem = 1,
    BeginCombo = 1, Combo = 1,
    InputText = 1, InputInt = 1, InputFloat = 1,
    SliderFloat = 1, SliderInt = 1, DragFloat = 1, DragInt = 1,
}

local patched = 0
local failed = 0

local function makeWrapper(orig, idx)
    return function(...)
        local a = { ... }
        local s = a[idx]
        if type(s) == "string" and #s > 2 then
            -- "##id" 后缀是 ImGui 的内部标识，翻掉会毁掉控件身份，
            -- 所以只在它的前半段上查词典，后缀原样拼回去。
            local cut = s:find("##", 1, true)
            if cut then
                local head = s:sub(1, cut - 1)
                local c = dict[head]
                if c then a[idx] = c .. s:sub(cut) end
            else
                local c = dict[s]
                if c then a[idx] = c end
            end
        end
        return orig(unpack(a))
    end
end

local function patch()
    if type(ImGui) ~= "table" then
        print("[CETZh] ImGui 不是 table（" .. type(ImGui) .. "），无法挂载")
        return
    end
    for name, idx in pairs(TARGETS) do
        local orig = ImGui[name]
        if type(orig) == "function" and not rawget(_G, "__cetzh_" .. name) then
            local wrapper = makeWrapper(orig, idx)
            -- CET 的 ImGui 表正常是可写的；万一哪天加了保护，
            -- 这里只留一条日志，不把 onInit 整个打断。
            local ok, err = pcall(function() ImGui[name] = wrapper end)
            if ok and ImGui[name] == wrapper then
                rawset(_G, "__cetzh_" .. name, true)
                patched = patched + 1
            else
                failed = failed + 1
                print("[CETZh] 挂载失败 " .. name .. ": " .. tostring(err))
            end
        end
    end
end

registerForEvent("onInit", function()
    local n = loadDict()
    print(string.format("[CETZh] 词典 %d 条", n))
end)

-- ImGui 的表不是 mod 加载时就填好的：实测 onInit 里它已经是 table，但
-- Text/Button 这些函数一个都还没有（挂载 0 个）。所以真正的挂载推迟到第一次
-- 绘制，并且带一次结构探测，日志里能看出这一版 CET 把函数放在哪。
local retry = 0
local probed = false

local function probe()
    local names = { "Text", "TextWrapped", "TextColored", "Button", "Checkbox",
                    "Selectable", "Begin", "BeginCombo", "InputText", "MenuItem" }
    local parts = {}
    for _, n in ipairs(names) do
        parts[#parts + 1] = n .. ":" .. type(ImGui[n])
    end
    return string.format("ImGui=%s {%s}", type(ImGui), table.concat(parts, " "))
end

registerForEvent("onDraw", function()
    if patched > 0 then
        return
    end
    if not probed then
        probed = true
        print("[CETZh] onDraw " .. probe())
        patch()
        print(string.format("[CETZh] onDraw 挂载 %d 个 / 失败 %d 个", patched, failed))
        return
    end
    -- 第一次没挂上：隔 60 帧再试一次，最多试 20 次，别每帧刷日志。
    retry = retry + 1
    if retry % 60 == 0 then
        patch()
        if retry >= 1200 then
            print("[CETZh] 挂载一直失败，放弃重试")
        end
    end
end)
