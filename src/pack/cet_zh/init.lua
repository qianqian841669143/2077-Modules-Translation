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
local MISS_NAME = "miss.tsv"
local MOD_DIR = "bin/x64/plugins/cyber_engine_tweaks/mods/zzz_CP2077_CETZh/"

local dict = {}
local hits, misses = 0, 0

-- 运行时收集：凡是流经 ImGui、词典里又没有的串，攒起来定期落到 miss.tsv。
-- 靠提取器穷举 CET 生态的文案不现实 —— 三元表达式、拼出来的串、别的 mod 自己
-- 封装的按钮函数都会漏（`ImGui.Button(Duty.onDuty and 'Go off duty' or 'Go on
-- duty')`、`controls.DynamicButton("All LUTs", ...)`）。与其在静态提取里一条条
-- 猜，不如让它跑起来自己报，下一轮照着 miss.tsv 补。
local missSeen, missList = {}, {}
local missCount = 0
local function noteMiss(s)
    -- 回车和换行都得挡掉：tsv 是按行读的，串里带裸 \r 会让下游把一条撕成
    -- 好几条（2026-11-21 那份 miss.tsv 里有 216 行带裸 \r，解析出来一堆
    -- 像 `#` / `#Window Manager` 这种半截货）。
    s = s:gsub("[\r\n]", " ")
    if #s < 3 or #s > 300 or missSeen[s] then
        return
    end
    if s:find("^%s*$") or s:find("^%d+$") then
        return
    end
    -- 已经是中文的不用再收（UTF-8 汉字首字节 0xE4-0xE9 = 228-233）。
    -- 之前 `已暂停` 这种也进了 miss.tsv，白占待译名额。
    if s:find("[\228-\233]") then
        return
    end
    -- 图标字体在 Unicode 私用区，翻译无从下手：EE 80.. 是 U+E000 段，
    -- EF 80..A3 是 U+F000-U+F8FF，F3 B0..BF 再往上。全角标点从 EF BC 起、
    -- CJK 兼容区从 EF A4 起，都不会被误伤。
    if s:find("[\238][\128-\191]") or s:find("\239[\128-\163]") or s:find("\243[\176-\191]") then
        return
    end
    -- 实时读数每帧都在变：`VRAM: 6.4 / 21.3 (22GB)`、`Textures: 4.4 GB`。
    -- 收进来会变成成百个数字变体，翻出来也用不上。数字多的直接放过。
    local digits = 0
    for i = 1, #s do
        local b = s:byte(i)
        if b >= 48 and b <= 57 then
            digits = digits + 1
            if digits >= 4 then
                return
            end
        end
    end
    missSeen[s] = true
    missList[#missList + 1] = s
    missCount = missCount + 1
end

local function flushMiss()
    if #missList == 0 then
        return
    end
    local f = io.open(MISS_NAME, "a")
    if not f then
        f = io.open(MOD_DIR .. MISS_NAME, "a")
    end
    if not f then
        return
    end
    for _, s in ipairs(missList) do
        f:write(s, "\n")
    end
    f:close()
    missList = {}
end

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

-- (函数名, 文案参数位置)。只包「某个参数是纯文案」的函数。
-- 位置特殊的三类：
--   TextColored / ProgressBar —— 颜色或数值在前，文案在后面；
--   LabelText —— 第 1 个参数是 ID（不显示），显示的是第 2 个；
--   InputTextWithHint —— 第 1 个是标签、第 2 个才是框里的占位提示
--     （LUTSwitcher 的 `Search LUTs...` 就在这一位）。
-- OpenPopup / BeginPopup / PushID / InvisibleButton 这类名字纯当 ID 用，绝不能翻，
-- 翻了控件就对不上号，所以完全不进表。
local TARGETS = {
    Text = 1, TextWrapped = 1, TextDisabled = 1, BulletText = 1,
    TextUnformatted = 1, LabelText = 2, TextColored = 5, SetTooltip = 1,
    Selectable = 1, MenuItem = 1, Button = 1, SmallButton = 1,
    Checkbox = 1, RadioButton = 1, ProgressBar = 4,
    TreeNode = 1, TreeNodeEx = 1, CollapsingHeader = 1,
    Begin = 1, BeginChild = 1, BeginMenu = 1, BeginTabItem = 1,
    BeginCombo = 1, Combo = 1, BeginPopupModal = 1,
    InputText = 1, InputTextWithHint = 2, InputInt = 1, InputFloat = 1,
    SliderFloat = 1, SliderInt = 1, DragFloat = 1, DragInt = 1,
    TableSetupColumn = 1, TableHeader = 1,
}

-- 每帧自愈要遍历这张表，先把 pairs 结果缓存成数组，免得每帧都重新遍历哈希表。
local ORDER = {}
for name, idx in pairs(TARGETS) do
    ORDER[#ORDER + 1] = { name, idx }
end

local patched = 0
local failed = 0

-- CET 1.38 把 ImGui 的每个函数暴露成 userdata（带 __call），不是 function。
-- 只认 function 的话，28 个目标一个都进不了挂载分支，日志里就是
-- 「挂载 0 个 / 失败 0 个」—— 这个判断让整个 CET 入口静默失效了一整轮。
local function isCallable(v)
    local t = type(v)
    return t == "function" or t == "userdata"
end

local function makeWrapper(orig, idx)
    return function(...)
        local n = select("#", ...)
        local a = { ... }
        local s = a[idx]
        if type(s) == "string" and #s > 2 then
            -- "##id" 后缀是 ImGui 的内部标识，翻掉会毁掉控件身份，
            -- 所以只在它的前半段上查词典，后缀原样拼回去。
            local cut = s:find("##", 1, true)
            local head = cut and s:sub(1, cut - 1) or s
            local c = dict[head]
            if c then
                hits = hits + 1
                a[idx] = cut and (c .. s:sub(cut)) or c
            else
                misses = misses + 1
                if not cut then
                    noteMiss(s)
                end
            end
        end
        -- 带上实参个数，别让中间的 nil 被 unpack(a) 截掉。
        return orig(unpack(a, 1, n))
    end
end

-- name -> 我们装上去的 wrapper。每帧拿它和 ImGui 表里的现值比一比：相等说明
-- 还是我们的；不等说明 CET 重建过表，或者压根还没填，那就重新装一次。
local installed = {}
local reported = false

local function patch()
    if type(ImGui) ~= "table" then
        print("[CETZh] ImGui 不是 table（" .. type(ImGui) .. "），无法挂载")
        return
    end
    local fresh, dead = {}, 0
    for i = 1, #ORDER do
        local name, idx = ORDER[i][1], ORDER[i][2]
        local cur = rawget(ImGui, name)
        if cur == nil then
            cur = ImGui[name]   -- 万一函数是 __index 提供的，rawget 看不见
        end
        if cur ~= installed[name] then
            if isCallable(cur) then
                local wrapper = makeWrapper(cur, idx)
                -- rawset 绕开可能存在的 __newindex，再回读校验一次：
                -- 写不进去就留一条日志，不把 onDraw 整个打断。
                local ok, err = pcall(rawset, ImGui, name, wrapper)
                if ok and rawget(ImGui, name) == wrapper then
                    if installed[name] == nil then
                        patched = patched + 1
                    end
                    installed[name] = wrapper
                    fresh[#fresh + 1] = name .. ":" .. type(cur)
                else
                    failed = failed + 1
                    print("[CETZh] 挂载失败 " .. name .. ": " .. tostring(err))
                end
            else
                dead = dead + 1
            end
        end
    end
    if not reported and (#fresh > 0 or dead > 0) then
        reported = true
        print(string.format("[CETZh] 挂载 %d 个（本帧新装 %d: %s）；表里还空着 %d 个",
              patched, #fresh, table.concat(fresh, " "), dead))
    end
end

registerForEvent("onInit", function()
    local n = loadDict()
    print(string.format("[CETZh] 词典 %d 条", n))
end)

-- ImGui 的表不是 mod 加载时就填好的：onInit 里它已经是 table，但 Text/Button
-- 这些成员一个都还没有。所以挂载推迟到 onDraw，并且每帧都过一遍 —— 既是首次
-- 重试，也是自愈：CET 若重建了表，下一帧就装回去。
local frame = 0
local lastReport = 0

registerForEvent("onDraw", function()
    patch()
    frame = frame + 1
    if frame - lastReport >= 3600 then
        lastReport = frame
        if hits + misses > 0 then
            flushMiss()
            print(string.format("[CETZh] 命中 %d / 未命中 %d / 已收集待译 %d 条到 %s",
                  hits, misses, missCount, MISS_NAME))
        end
    end
end)
