-- ##000_CETHotkeysZh / init.lua
--
-- 只干一件事：把 CET 热键窗口里每个 mod 的热键显示名换成中文。
--
-- 为什么目录名长这样：CET 按 mods 目录名的字母序加载 init.lua，而各 mod 都是在
-- **文件顶层**就调用 registerHotkey（WeatherSwitcher/init.lua:87 的
-- Hotkeys.registerHotkeys()、##GoreRunners/init.lua:37 的 registerHotkey(...)
-- 都在顶层，不在 onInit 里）。所以包裹必须抢在任何一个 mod 的 init.lua 之前执行。
-- '#'(0x23) < '0'(0x30) < 'A'，'##000_' 排在最前 —— 连 ##GoreRunners 都在我们
-- 后面（第三字符 '0' < 'G'）。
--
-- 管不到的："Hotkeys" / "Unbound" / "Save" / "Reset changes" 这几个是 CET 自己
-- 的标签，写在 src/overlay/widgets/Bindings.cpp 里，是 ImGui::Button("Save", ...)
-- 这种 C++ 字面量，Lua 层没有任何入口。要动它们只能改 CET 源码重编译。

local DICT_NAME = "hotkeys.tsv"
local MISS_NAME = "hotkeys_miss.tsv"
-- CET 的 cwd 一般是游戏根；万一换版本改了，再按 mod 目录拼一次。
local MOD_DIR = "bin/x64/plugins/cyber_engine_tweaks/mods/##000_CETHotkeysZh/"

local dict = {}
local missSeen, missList = {}, {}
local hits, misses = 0, 0

local function openInDir(name)
    local f = io.open(name, "r")
    if not f then
        f = io.open(MOD_DIR .. name, "r")
    end
    return f
end

local function loadDict()
    local f = openInDir(DICT_NAME)
    if not f then
        print("[CETHotkeysZh] 找不到 " .. DICT_NAME)
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

-- 运行时补漏：静态扫 Lua 源码只能抓到「两个字面量参数」的调用，像 NCPDFR 那种
-- 套了一层局部函数（hotkey(cetId, label, action)）、WeatherSwitcher 那种从
-- toggleMap 表里取名字的，静态提取会漏。让没命中的名字自己报上来，
-- 下一轮照着 hotkeys_miss.tsv 补。
local function noteMiss(s)
    if #s < 3 or #s > 200 or missSeen[s] then
        return
    end
    missSeen[s] = true
    missList[#missList + 1] = s
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

local loaded = loadDict()

-- 顶层立刻接管：这时其它 mod 的 init.lua 一个都还没执行。
local orig = registerHotkey
if orig == nil then
    print("[CETHotkeysZh] registerHotkey 还不存在，本层失效")
else
    registerHotkey = function(id, name, ...)
        if type(name) == "string" then
            local zh = dict[name]
            if zh then
                hits = hits + 1
                name = zh
            else
                misses = misses + 1
                noteMiss(name)
            end
        end
        return orig(id, name, ...)
    end
    print(string.format("[CETHotkeysZh] 词典 %d 条，registerHotkey 已接管", loaded))
end

registerForEvent("onDraw", function()
    if #missList >= 20 then
        flushMiss()
    end
end)

registerForEvent("onShutdown", function()
    flushMiss()
    print(string.format("[CETHotkeysZh] 热键名命中 %d / 未命中 %d", hits, misses))
end)
