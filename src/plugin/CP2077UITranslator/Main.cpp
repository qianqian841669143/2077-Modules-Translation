// CP2077UITranslator 2.0 —— 按模组分层的运行时文本替换。
//
// 与 1.0 的区别：
//   1. 词典分两层：dict\_global.tsv（兜底）+ dict\<模组键>.tsv（精确）
//      模组键取自调用链上第一个带命名空间的函数全名，例如
//      DigitalVixenCore.DVCStatusWindow::Update -> DigitalVixenCore
//   2. 查找顺序：先全局，未命中再解析调用链查模组级并替换；
//      这样 95% 的调用只有一次哈希查找，不碰调用链。
//   3. 日志默认关闭，由 config.ini 的 verbose 控制。
//
// 词典格式（UTF-8，一行一条）：
//   <英文原文>\t<中文译文>
//   # 开头为注释

#include <RED4ext/RED4ext.hpp>
#include <RED4ext/Relocation.hpp>
#include <RED4ext/Scripting/Functions.hpp>
#include <RED4ext/Scripting/Stack.hpp>
#include <RED4ext/RTTITypes.hpp>
#include <RED4ext/CString.hpp>

#include <Windows.h>

#include <array>
#include <atomic>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

namespace
{
RED4ext::v1::PluginHandle g_handle = 0;
const RED4ext::v1::Sdk* g_sdk = nullptr;
HMODULE g_module = nullptr;

using CallScript_t = bool (*)(RED4ext::IFunction*, RED4ext::IScriptable*,
                              RED4ext::CStackFrame*, void*, void*);
CallScript_t g_orig = nullptr;

constexpr size_t kTextOffset = 0x258;
constexpr size_t kTextIdKeyOffset = 0x278;
constexpr size_t kNativeTypeOffset = 0x30;
// inkTextWidget 上跟「文本」有关的几个 reg。
// 只看 1858（SetText）不够：MCM 的 packed.reds 里标题 / 分类 / 描述走的是
//   inkTextRef.SetLocalizedTextScript(this.m_descriptionText, "LocKey#76949", params)
//   inkTextRef.SetLocalizedText(this.m_text, n"UI-Settings-ButtonMappings-Misc-KeyBind")
// 所以这一组都当文本入口处理，具体是不是 TextWidget 由 nativeType 判断。
constexpr uint32_t kTextRegs[] = {1858, 1859, 1860, 1861, 1863, 1864, 1865, 1835};
constexpr size_t kTextRegCount = sizeof(kTextRegs) / sizeof(kTextRegs[0]);
constexpr int kMaxChain = 8;
constexpr uint32_t kLogDetails = 120;
constexpr size_t kMaxDictBytes = 64u << 20;

// 热路径，每帧可能被调上万次，只做两次整数比较。
// reg 号实测是 1858..1865 连续段（SetText / SetTextDirect / SetLocalizedText /
// SetLocalizedTextString / SetLocalizationKey(2 个) / SetTextFromParts），
// 外加第二套编号里的 1835。
//
// **段里的 1862 必须排除。** 2026-11-21 的崩溃（crash-2026-11-21_130906.txt，
// CP2077UITranslator.dll+0x58AA read at 0x10）就是它：1862 不是文本函数，
// 调它的实例不是 inkTextWidget，+0x258 处不是 CString，长度字段恰好 >= 0x40000000，
// CString::c_str() 于是把那 8 字节当 data 指针取出来，`cmp byte ptr [0x10],0`
// 直接访问违例。以前注释写「放进来无害」，实测有害。
// 掩码 0xEF = 0b1110_1111，bit4 正是 1862。
__forceinline bool IsTextReg(uint32_t aReg)
{
    if (aReg >= 1858 && aReg <= 1865)
    {
        return ((0xEFu >> (aReg - 1858)) & 1u) != 0;
    }
    return aReg == 1835;
}

uintptr_t g_widgetClass = 0;
std::atomic<uint64_t> g_hits{0};
std::atomic<uint64_t> g_missMod{0};
std::atomic<uint64_t> g_totalCalls{0};   // 经过 hook 的全部 redscript 调用
std::atomic<uint64_t> g_textCalls{0};    // 其中 reg 命中文本函数的
uint32_t g_verbose = 0;
uint32_t g_stat = 0;                     // 1 = 每 5 秒写一行频率统计
uint64_t g_lastStat = 0;

// 每一份词典由一段连续内存 + 一个索引构成；string_view 指向这段内存，
// 所以加载完成后绝不能再让 vector 增长。
struct Dict
{
    std::vector<char> blob;
    std::unordered_map<std::string_view, const char*> map;
    uint64_t entries = 0;
};

Dict g_global;
std::vector<std::pair<std::string, Dict*>> g_modDicts;
std::vector<std::string> g_modNames;

void Log(const char* aFormat, ...)
{
    if (g_sdk == nullptr || g_sdk->logger == nullptr)
    {
        return;
    }
    char buffer[2400];
    va_list ap;
    va_start(ap, aFormat);
    const int n = vsnprintf(buffer, sizeof(buffer), aFormat, ap);
    va_end(ap);
    if (n >= 0)
    {
        g_sdk->logger->Info(g_handle, buffer);
    }
}

// 热路径，绝不能带 __try/__except：MSVC 的 SEH 每次进出都要压/弹异常帧，
// InternalExecute 又是所有 redscript 函数调用的入口，每帧要走几千次，
// 累积起来就是「鼠标一动卡一下」。aCtx 是传进来的 this，必然是有效对象，
// 真正的类型判断交给 nativeType 比较即可。
__forceinline uintptr_t ReadNativeType(void* aCtx)
{
    return *reinterpret_cast<uintptr_t*>(reinterpret_cast<uint8_t*>(aCtx) + kNativeTypeOffset);
}

__forceinline const char* ReadWidgetText(void* aCtx)
{
    auto* cs = reinterpret_cast<RED4ext::CString*>(
        reinterpret_cast<uint8_t*>(aCtx) + kTextOffset);
    return cs->c_str();
}

__declspec(noinline) void AssignCString(void* aCs, const char* aText)
{
    *reinterpret_cast<RED4ext::CString*>(aCs) = RED4ext::CString(aText);
}

// inkTextWidget 的 textIdKey(+0x278) 是本地化键。带这个键的控件，游戏每次
// 重绘都会按 key 重新解析文本，把我们写进去的中文覆盖回英文。
// 标题 / 分类 / 下拉选项就是这类控件 —— 实测 "Frame Generation SM86" 日志里
// 命中过、界面上却还是英文。写中文的同时把键清成 0（空 CName）。
__declspec(noinline) void ClearTextIdKey(void* aBase)
{
    *reinterpret_cast<uint64_t*>(reinterpret_cast<uint8_t*>(aBase) + kTextIdKeyOffset) = 0;
}

__declspec(noinline) bool WriteWidgetText(void* aCtx, const char* aText)
{
    if (aCtx == nullptr || aText == nullptr)
    {
        return false;
    }
    __try
    {
        AssignCString(reinterpret_cast<uint8_t*>(aCtx) + kTextOffset, aText);
        ClearTextIdKey(aCtx);
        return true;
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        return false;
    }
}

__declspec(noinline) const char* FuncFullName(RED4ext::IFunction* aFn)
{
    if (aFn == nullptr)
    {
        return nullptr;
    }
    __try
    {
        auto* bf = reinterpret_cast<RED4ext::CBaseFunction*>(aFn);
        const char* full = bf->fullName.ToString();
        return (full != nullptr && full[0] != '\0') ? full : nullptr;
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        return nullptr;
    }
}

__declspec(noinline) RED4ext::CStackFrame* FrameParent(RED4ext::CStackFrame* aFrame)
{
    if (aFrame == nullptr)
    {
        return nullptr;
    }
    __try
    {
        return aFrame->parent;
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        return nullptr;
    }
}

__declspec(noinline) RED4ext::IFunction* FrameFunc(RED4ext::CStackFrame* aFrame)
{
    if (aFrame == nullptr)
    {
        return nullptr;
    }
    __try
    {
        return reinterpret_cast<RED4ext::IFunction*>(aFrame->func);
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        return nullptr;
    }
}

bool HasCjk(const char* aText)
{
    for (const unsigned char* p = reinterpret_cast<const unsigned char*>(aText); *p != 0; ++p)
    {
        if (*p >= 0xE4 && *p <= 0xE9)
        {
            return true;
        }
    }
    return false;
}

// "DigitalVixenCore.DVCStatusWindow::Update;GameInstance" -> "DigitalVixenCore"
// "ME3Bar::Show"                                         -> "ME3Bar"
// 取不到就返回空，调用方当作无模组归属。
bool ModKeyFromFullName(const char* aFull, char* aOut, size_t aCap)
{
    if (aFull == nullptr || aOut == nullptr || aCap == 0)
    {
        return false;
    }
    const char* sep = strstr(aFull, "::");
    if (sep == nullptr || sep == aFull)
    {
        return false;
    }
    const char* dot = static_cast<const char*>(memchr(aFull, '.', static_cast<size_t>(sep - aFull)));
    const char* stop = (dot != nullptr) ? dot : sep;
    const size_t len = static_cast<size_t>(stop - aFull);
    if (len == 0 || len >= aCap)
    {
        return false;
    }
    memcpy(aOut, aFull, len);
    aOut[len] = '\0';
    return true;
}

// 从调用链上找最近一个能解析出模组键的函数。
bool ModKeyFromChain(RED4ext::CStackFrame* aFrame, char* aOut, size_t aCap)
{
    RED4ext::CStackFrame* cur = FrameParent(aFrame);
    for (int i = 0; i < kMaxChain && cur != nullptr; ++i)
    {
        RED4ext::IFunction* f = FrameFunc(cur);
        const char* full = FuncFullName(f);
        if (full != nullptr && ModKeyFromFullName(full, aOut, aCap))
        {
            return true;
        }
        cur = FrameParent(cur);
    }
    return false;
}

const char* Lookup(Dict& aDict, const char* aText)
{
    if (aDict.map.empty())
    {
        return nullptr;
    }
    const auto it = aDict.map.find(std::string_view(aText));
    return (it == aDict.map.end()) ? nullptr : it->second;
}

// ink 的文本里换行是**真字节**（redscript 里 "a\nb" 运行时就是 a + 0x0A + b），
// 而词典的 key 在生成时被压成「空格分隔的单行」—— TSV 一行一条，真换行存不下。
// 两边形态不同，逐字节比较永远不中：全库 249 条带换行的长文本（多行提示、
// 错误信息、BreachRoute 的调试面板）就是这么一直翻不出来的，译文翻得再准
// 也不会显示。
//
// 只在「原样查不到、且串里确实有 CR/LF」时才归一化，普通文本一次都不多扫。
// 规则和 Python 的 _clean 对齐：CR/LF/TAB 变空格、连续空白并成一个、
// 不 trim 首尾（两边都不 trim，才能逐字相同）。
void NormalizeToSpace(const char* aIn, char* aOut, size_t aCap)
{
    size_t n = 0;
    bool prevSpace = false;
    for (const char* p = aIn; *p != '\0' && n + 1 < aCap; ++p)
    {
        char c = *p;
        if (c == '\r' || c == '\n' || c == '\t')
        {
            c = ' ';
        }
        if (c == ' ')
        {
            if (prevSpace)
            {
                continue;
            }
            prevSpace = true;
        }
        else
        {
            prevSpace = false;
        }
        aOut[n++] = c;
    }
    aOut[n] = '\0';
}

const char* LookupText(Dict& aDict, const char* aText)
{
    const char* hit = Lookup(aDict, aText);
    if (hit != nullptr || aText == nullptr)
    {
        return hit;
    }
    if (strchr(aText, '\n') == nullptr && strchr(aText, '\r') == nullptr)
    {
        return nullptr;                          // 没有换行，形态本来就一致
    }
    char buf[1024] = {};
    NormalizeToSpace(aText, buf, sizeof(buf));
    return Lookup(aDict, buf);
}

// 词典里的译文是从 memory 抄来的源文形态，`\n` 还是反斜杠+n 两个字符。
// 写回 inkTextWidget 之前必须还原成真换行，否则多行译文会原样显示 "\n"。
// 就地做：写指针始终不超过读指针，不需要额外缓冲。
char* UnescapeInPlace(char* aText)
{
    char* w = aText;
    for (const char* r = aText; *r != '\0'; ++r)
    {
        if (*r == '\\' && r[1] != '\0')
        {
            char out = 0;
            switch (r[1])
            {
            case 'n': out = '\n'; break;
            case 't': out = '\t'; break;
            case 'r': out = '\r'; break;
            case '"': out = '"'; break;
            case '\\': out = '\\'; break;
            default: break;
            }
            if (out != 0)
            {
                *w++ = out;
                ++r;
                continue;
            }
        }
        *w++ = *r;
    }
    *w = '\0';
    return aText;
}

Dict* FindModDict(const char* aKey)
{
    for (auto& kv : g_modDicts)
    {
        if (kv.first == aKey)
        {
            return kv.second;
        }
    }
    return nullptr;
}

// 只有在 verbose>0 时才会被调用。日志是同步写盘的（red4ext logger 每条都落
// 文件），MCM 界面里鼠标一移动就会刷一批文本，命中一条写一行，I/O 就顶起来了。
// verbose=0（默认）时这条路径完全不执行。
void ReportHit(const char* aSrc, const char* aDst, const char* aScope)
{
    if (g_verbose == 0)
    {
        return;
    }
    const uint64_t n = g_hits.fetch_add(1, std::memory_order_relaxed) + 1;
    if (n <= kLogDetails)
    {
        Log("[hit #%llu %s] \"%s\" -> \"%s\"",
            static_cast<unsigned long long>(n), aScope, aSrc, aDst);
    }
    else if (g_verbose >= 2 && (n % 500) == 0)
    {
        Log("[hits] %llu", static_cast<unsigned long long>(n));
    }
}

// 在 SetText 执行完之后调用：此时 widget->text 才是「这次要显示的那串」。
// 调之前改没有意义 —— SetText 会把参数里的英文重新写回去（1.0 首版的坑）。
//
// 性能红线：这个函数每帧都会被 HUD 调几十次（血量 / 弹药 / 速度全走 SetText），
// 所以里面只做「读串 → 查一张表 → 写回」三件事。爬调用链那套已经全部砍掉，
// 分片在加载时并进总表了，运行时不需要知道模组是谁。
// 对象 +0x30 是它的 nativeType（RED4ext::CClass*），沿 parent 链往上找
// inkTextWidget。**只能 is-a，不能相等**：inkRichTextBox / inkTextRef 这类子类
// 继承同样的字段布局（text 一样在 +0x258），但它们自己的 CClass 不等于
// inkTextWidget —— 老版本用相等比较，把标题/分类整类滤掉了。
//
// 这道闸门 2026-11-21 补回来：此前为了覆盖子类把类型比对整个删掉，结果 reg 1862
// 把非文本对象的 this 送进来，读 +0x258 取出 0x10 当指针，带崩游戏（+0x58AA）。
// g_widgetClass 由 OnPostRegister 填；拿不到就一条都不改 —— 宁可漏翻，不可崩。
__declspec(noinline) bool IsTextWidget(void* aCtx)
{
    if (g_widgetClass == 0)
    {
        return false;
    }
    __try
    {
        auto* cls = reinterpret_cast<RED4ext::CClass*>(ReadNativeType(aCtx));
        for (int i = 0; i < 32 && cls != nullptr; ++i)
        {
            if (reinterpret_cast<uintptr_t>(cls) == g_widgetClass)
            {
                return true;
            }
            cls = cls->parent;
        }
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        return false;
    }
    return false;
}

void ApplyTranslation(void* aCtx)
{
    if (aCtx == nullptr)
    {
        return;
    }
    // 类型闸门。SetText 那一套确实是 inkTextWidget 的方法，但兜底 hook 挂在
    // InternalExecute 这个总入口上，aCtx 是「谁在调」，不先验一下就可能把
    // 别的类的对象当 widget 读。
    if (!IsTextWidget(aCtx))
    {
        return;
    }

    const char* src = ReadWidgetText(aCtx);
    if (src == nullptr || src[0] == '\0' || src[1] == '\0')
    {
        return;                                  // 空串 / 单字符不值得查
    }

    // 不再单独扫一遍 CJK：词典的 key 全是英文，中文串查表必然 miss，
    // 直接由 Lookup 的结果决定要不要写回，少扫一遍整串。
    const char* hit = LookupText(g_global, src);
    if (hit == nullptr)
    {
        return;
    }

    // src 指向 widget->text 的内部缓冲，写回时会被释放并复用，
    // 所以日志用的原文必须先拷出来，否则会出现 "插件 -> 插件" 这种鬼东西。
    // 默认 verbose=0 时这块整个跳过，连 400 字节的栈清零都不做。
    if (g_verbose > 0)
    {
        char srcCopy[400] = {};
        snprintf(srcCopy, sizeof(srcCopy), "%s", src);
        if (WriteWidgetText(aCtx, hit))
        {
            ReportHit(srcCopy, hit, "global");
        }
        return;
    }
    WriteWidgetText(aCtx, hit);
}

// 只在 stat=1 时才会被调用；默认关闭时整个函数不执行。
void StatTick()
{
    const uint64_t n = g_totalCalls.fetch_add(1, std::memory_order_relaxed) + 1;
    if ((n & 0xFFF) != 0)
    {
        return;
    }
    const uint64_t now = GetTickCount64();
    if (now - g_lastStat < 5000)
    {
        return;
    }
    Log("[stat] calls/5s=%llu text/5s=%llu hits=%llu",
        static_cast<unsigned long long>(n),
        static_cast<unsigned long long>(g_textCalls.load(std::memory_order_relaxed)),
        static_cast<unsigned long long>(g_hits.load(std::memory_order_relaxed)));
    g_lastStat = now;
}

// 直接映射缓存：函数对象地址 -> reg 号。
// InternalExecute 每秒被调 200 万次以上，每次去调虚函数 GetRegIndex() 太贵；
// 函数对象是常驻的，地址稳定，缓存下来之后命中路径只剩「一次数组读 + 一次比较」。
// 8192 项 × 12 字节 ≈ 96 KB，常驻没问题。
constexpr size_t kRegCacheSize = 8192;
constexpr size_t kRegCacheMask = kRegCacheSize - 1;
void* g_regCacheKey[kRegCacheSize] = {};
uint32_t g_regCacheVal[kRegCacheSize] = {};

__forceinline uint32_t CachedRegIndex(RED4ext::IFunction* aFn)
{
    const size_t idx = (reinterpret_cast<uintptr_t>(aFn) >> 4) & kRegCacheMask;
    if (g_regCacheKey[idx] == aFn)
    {
        return g_regCacheVal[idx];
    }
    uint32_t reg = 0;
    __try
    {
        reg = aFn->GetRegIndex();
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        reg = 0;
    }
    g_regCacheKey[idx] = aFn;
    g_regCacheVal[idx] = reg;
    return reg;
}

// ---------------------------------------------------------------- 变更点
// 不再 hook CBaseFunction_InternalExecute。
// 那是所有 redscript 函数调用的总入口，实测每秒 211 万次（每帧 1.5 万次），
// 光是 trampoline 的固定开销就吃掉约 10% 的帧时间，内部再怎么优化也救不回来。
//
// 改 hook native 实现本身：SDK 的 ExecuteNative 里是
//     auto func = GetHandler(GetRegIndex());
//     if (func) { func(context, aFrame, resultAddress, resultType); }
// 也就是每个 native 函数的实现地址都在 CBaseFunction_Handlers 这张表里，
// 用 regIndex 直接取。我们只给 inkTextWidget 的那几个文本函数挂 hook，
// 触发频率降到「界面真正设一次文本一次」，从每秒两百万次变成几百次。

using TextHandler_t = void (*)(void*, RED4ext::CStackFrame&, void*, RED4ext::rtti::IType*);

TextHandler_t g_origText[kTextRegCount] = {};

template <size_t IDX>
void HookText(void* aInstance, RED4ext::CStackFrame& aFrame, void* aResult, RED4ext::rtti::IType* aType)
{
    if (g_origText[IDX] != nullptr)
    {
        g_origText[IDX](aInstance, aFrame, aResult, aType);
    }
    // 原函数写完英文之后再改；顺序反了会被覆盖（1.0 首版的坑）。
    ApplyTranslation(aInstance);
}

constexpr TextHandler_t kTextHooks[] = {
    &HookText<0>, &HookText<1>, &HookText<2>, &HookText<3>,
    &HookText<4>, &HookText<5>, &HookText<6>, &HookText<7>,
};

// 兜底方案：handler 表不可用时才挂这条。它对每秒两百万次调用都要付一次
// trampoline 开销，是明显的性能损失，所以只在没有别的办法时启用。
bool HookInternalExecute(RED4ext::IFunction* aFn, RED4ext::IScriptable* aCtx,
                         RED4ext::CStackFrame* aFrame, void* aOut, void* a4)
{
    if (aFn == nullptr || aCtx == nullptr)
    {
        return g_orig(aFn, aCtx, aFrame, aOut, a4);
    }
    if (!IsTextReg(CachedRegIndex(aFn)))
    {
        return g_orig(aFn, aCtx, aFrame, aOut, a4);
    }
    const bool result = g_orig(aFn, aCtx, aFrame, aOut, a4);
    ApplyTranslation(aCtx);
    return result;
}

void LogPath(const char* aLabel, const wchar_t* aPath)
{
    if (aPath == nullptr)
    {
        return;
    }
    char utf8[MAX_PATH * 3] = {};
    if (WideCharToMultiByte(CP_UTF8, 0, aPath, -1, utf8, sizeof(utf8) - 1, nullptr, nullptr) > 0)
    {
        Log("[init] %s=%s", aLabel, utf8);
    }
}

bool ReadWholeFile(const wchar_t* aPath, std::vector<char>& aOut, uint64_t& aBytes)
{
    const HANDLE f = CreateFileW(aPath, GENERIC_READ, FILE_SHARE_READ, nullptr,
                                 OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (f == INVALID_HANDLE_VALUE)
    {
        return false;
    }
    LARGE_INTEGER size{};
    if (!GetFileSizeEx(f, &size) || size.QuadPart <= 0 || size.QuadPart > static_cast<LONGLONG>(kMaxDictBytes))
    {
        CloseHandle(f);
        return false;
    }
    aOut.resize(static_cast<size_t>(size.QuadPart) + 1);
    DWORD read = 0;
    const BOOL ok = ReadFile(f, aOut.data(), static_cast<DWORD>(size.QuadPart), &read, nullptr);
    CloseHandle(f);
    if (!ok || read == 0)
    {
        aOut.clear();
        return false;
    }
    aOut[read] = '\0';
    aBytes = read;
    return true;
}

// 把 <key>\t<value>\n 的内存就地切成两张表，不额外分配字符串。
bool ParseDict(Dict& aDict)
{
    if (aDict.blob.empty())
    {
        return false;
    }
    aDict.map.reserve(4096);

    char* p = aDict.blob.data();
    char* const end = p + aDict.blob.size() - 1;
    while (p < end)
    {
        char* eol = static_cast<char*>(memchr(p, '\n', static_cast<size_t>(end - p)));
        if (eol == nullptr)
        {
            eol = end;
        }
        *eol = '\0';
        if (*p != '#' && *p != '\0')
        {
            char* tab = static_cast<char*>(memchr(p, '\t', static_cast<size_t>(eol - p)));
            if (tab != nullptr)
            {
                *tab = '\0';
                char* dst = UnescapeInPlace(tab + 1);
                if (*p != '\0' && *dst != '\0')
                {
                    aDict.map.emplace(std::string_view(p), dst);
                    ++aDict.entries;
                }
            }
        }
        p = eol + 1;
    }
    return aDict.entries > 0;
}

std::wstring PluginDir()
{
    wchar_t path[MAX_PATH] = {};
    if (g_module == nullptr)
    {
        return {};
    }
    const DWORD n = GetModuleFileNameW(g_module, path, MAX_PATH);
    if (n == 0 || n >= MAX_PATH)
    {
        return {};
    }
    wchar_t* slash = wcsrchr(path, L'\\');
    if (slash == nullptr)
    {
        return {};
    }
    *(slash + 1) = L'\0';
    return std::wstring(path);
}

void ReadConfig(const std::wstring& aDir)
{
    g_verbose = 0;
    g_stat = 0;
    const std::wstring path = aDir + L"config.ini";
    std::vector<char> blob;
    uint64_t bytes = 0;
    if (!ReadWholeFile(path.c_str(), blob, bytes))
    {
        return;
    }
    // 逐行读 key=value：verbose（替换明细）/ stat（每 5 秒一次频率统计）。
    const char* p = blob.data();
    while (p != nullptr && *p != '\0')
    {
        const char* eol = strchr(p, '\n');
        if (strncmp(p, "verbose", 7) == 0 || strncmp(p, "stat", 4) == 0)
        {
            const char* eq = strchr(p, '=');
            if (eq != nullptr)
            {
                const int v = atoi(eq + 1);
                if (p[0] == 'v' && v >= 0 && v <= 2)
                {
                    g_verbose = static_cast<uint32_t>(v);
                }
                else if (p[0] == 's' && v >= 0 && v <= 1)
                {
                    g_stat = static_cast<uint32_t>(v);
                }
            }
        }
        if (eol == nullptr)
        {
            break;
        }
        p = eol + 1;
    }
    g_lastStat = GetTickCount64();
}

void LoadDictFile(const std::wstring& aPath)
{
    Dict dict;
    uint64_t bytes = 0;
    if (!ReadWholeFile(aPath.c_str(), dict.blob, bytes))
    {
        return;
    }
    if (!ParseDict(dict))
    {
        return;
    }
    const std::wstring name = aPath.substr(aPath.find_last_of(L'\\') + 1);
    char utf8[256] = {};
    WideCharToMultiByte(CP_UTF8, 0, name.c_str(), -1, utf8, sizeof(utf8) - 1, nullptr, nullptr);

    if (_stricmp(utf8, "_global.tsv") == 0)
    {
        g_global.blob = std::move(dict.blob);
        g_global.map = std::move(dict.map);
        g_global.entries = dict.entries;
        Log("[init] global entries=%llu bytes=%llu",
            static_cast<unsigned long long>(g_global.entries),
            static_cast<unsigned long long>(bytes));
        return;
    }

    std::string key(utf8);
    if (key.size() > 4 && key.compare(key.size() - 4, 4, ".tsv") == 0)
    {
        key.erase(key.size() - 4);
    }
    auto* stored = new Dict(std::move(dict));
    // 分片同时并入总表：调用链经常取不到模组键（MCM / CET Lua 转发的那一层
    // 只有 McmUi… / $Lua，拿不到 FrameGenSM86），只查分片会全盘落空。
    // 总表里已有的 key 不覆盖，保证 _global 与先加载的分片优先。
    for (const auto& kv : stored->map)
    {
        g_global.map.emplace(kv.first, kv.second);
    }
    g_global.entries += stored->entries;
    g_modDicts.emplace_back(key, stored);
    Log("[init] mod '%s' entries=%llu", key.c_str(),
        static_cast<unsigned long long>(stored->entries));
}

void LoadDictionary(const std::wstring& aDir)
{
    const std::wstring globalPath = aDir + L"dictionary.tsv";
    const std::wstring dictDir = aDir + L"dict";

    // 兼容 1.0 的单文件布局
    {
        std::vector<char> probe;
        uint64_t bytes = 0;
        if (ReadWholeFile(globalPath.c_str(), probe, bytes))
        {
            Dict dict;
            dict.blob = std::move(probe);
            if (ParseDict(dict))
            {
                g_global.blob = std::move(dict.blob);
                g_global.map = std::move(dict.map);
                g_global.entries = dict.entries;
                Log("[init] global entries=%llu bytes=%llu (legacy dictionary.tsv)",
                    static_cast<unsigned long long>(g_global.entries),
                    static_cast<unsigned long long>(bytes));
            }
        }
    }

    // _global.tsv 必须先加载：分片是「emplace 不覆盖」的方式并进总表的，
    // 所以先来的赢。若让它按文件名顺序（Apex… 在 _global 前面）后加载，
    // 它会把已经并好的分片全冲掉。
    LoadDictFile(dictDir + L"\\_global.tsv");

    WIN32_FIND_DATAW fd{};
    const std::wstring pattern = dictDir + L"\\*.tsv";
    const HANDLE h = FindFirstFileW(pattern.c_str(), &fd);
    if (h == INVALID_HANDLE_VALUE)
    {
        Log("[init] no dict\\ directory");
        return;
    }
    do
    {
        if ((fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0)
        {
            continue;
        }
        if (_wcsicmp(fd.cFileName, L"_global.tsv") == 0)
        {
            continue;
        }
        LoadDictFile(dictDir + L"\\" + fd.cFileName);
    } while (FindNextFileW(h, &fd));
    FindClose(h);

    Log("[init] merged entries=%llu",
        static_cast<unsigned long long>(g_global.entries));
}

// 尝试只 hook native 实现（handler 表）。成功返回挂钩数量。
// 返回 0 说明这张表在游戏这个版本里不可用，调用方要退回总入口方案。
int TryHookHandlers()
{
    static RED4ext::UniversalRelocPtr<TextHandler_t*> handlers(
        RED4ext::Detail::AddressHashes::CBaseFunction_Handlers);
    if (handlers == nullptr)
    {
        Log("[h] handler table still null");
        return 0;
    }

    int hooked = 0;
    for (size_t i = 0; i < kTextRegCount; ++i)
    {
        TextHandler_t impl = handlers[kTextRegs[i]];
        if (impl == nullptr)
        {
            continue;
        }
        void* original = nullptr;
        if (!g_sdk->hooking->Attach(g_handle, reinterpret_cast<void*>(impl),
                                    reinterpret_cast<void*>(kTextHooks[i]), &original))
        {
            continue;
        }
        g_origText[i] = reinterpret_cast<TextHandler_t>(original);
        ++hooked;
        Log("[h] reg=%u hooked at 0x%llX", kTextRegs[i],
            static_cast<unsigned long long>(reinterpret_cast<uintptr_t>(impl)));
    }
    Log("[h] hooked %d of %d", hooked, static_cast<int>(kTextRegCount));
    return hooked;
}

// 兜底：挂总入口。static 局部变量（UniversalRelocFunc 的缓存）需要线程安全的
// 初始化守卫，那属于「对象展开」，不能和 OnPostRegister 里的 __try 同函数，
// 所以单独抽出来。
void InstallFallbackHook()
{
    static RED4ext::UniversalRelocFunc<CallScript_t> target(
        RED4ext::Detail::AddressHashes::CBaseFunction_InternalExecute);
    CallScript_t addr = target;
    if (addr == nullptr)
    {
        Log("[h] InternalExecute unresolved");
        return;
    }
    void* original = nullptr;
    const bool ok = g_sdk->hooking->Attach(g_handle, reinterpret_cast<void*>(addr),
                                           reinterpret_cast<void*>(&HookInternalExecute), &original);
    g_orig = reinterpret_cast<CallScript_t>(original);
    Log("[h] fallback to InternalExecute hook: %s", ok ? "OK" : "FAIL");
}

void OnPostRegister()
{
    auto* rtti = RED4ext::CRTTISystem::Get();
    if (rtti == nullptr)
    {
        return;
    }
    RED4ext::CClass* cls = nullptr;
    __try
    {
        cls = rtti->GetClass("inkTextWidget");
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        cls = nullptr;
    }
    g_widgetClass = reinterpret_cast<uintptr_t>(cls);
    Log("[init] inkTextWidget class=0x%llX",
        static_cast<unsigned long long>(g_widgetClass));
}

void Install()
{
    if (g_sdk == nullptr || g_sdk->hooking == nullptr || g_sdk->logger == nullptr)
    {
        return;
    }
    Log("================ CP2077UITranslator 2.1.1 ================");

    // 挂钩一律在 Load 阶段完成。以前把 fallback 放到 OnPostRegister 里做，
    // 那次必然崩溃 —— RTTI 注册回调期间 Attach 钩子不安全。
    if (TryHookHandlers() > 0)
    {
        Log("[h] using per-function hooks (fast path)");
        return;
    }
    InstallFallbackHook();
}
} // namespace

RED4EXT_C_EXPORT bool RED4EXT_CALL Main(RED4ext::v1::PluginHandle aHandle, RED4ext::v1::EMainReason aReason,
                                        const RED4ext::v1::Sdk* aSdk)
{
    if (aReason == RED4ext::v1::EMainReason::Load)
    {
        g_handle = aHandle;
        g_sdk = aSdk;

        HMODULE self = nullptr;
        GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                           reinterpret_cast<LPCWSTR>(&Main), &self);
        g_module = self;

        const std::wstring dir = PluginDir();
        ReadConfig(dir);
        Install();
        LoadDictionary(dir);
        Log("[init] verbose=%u hits=%llu missMod=%llu",
            g_verbose,
            static_cast<unsigned long long>(g_hits.load()),
            static_cast<unsigned long long>(g_missMod.load()));

        auto* rtti = RED4ext::CRTTISystem::Get();
        if (rtti != nullptr)
        {
            rtti->AddPostRegisterCallback(&OnPostRegister);
        }
    }
    return true;
}

RED4EXT_C_EXPORT void RED4EXT_CALL Query(RED4ext::v1::PluginInfo* aInfo)
{
    aInfo->name = L"CP2077UITranslator";
    aInfo->author = L"Module Translation";
    aInfo->version = RED4EXT_V1_SEMVER(2, 0, 0);
    aInfo->runtime = RED4EXT_V1_RUNTIME_VERSION_LATEST;
    aInfo->sdk = RED4EXT_V1_SDK_VERSION_CURRENT;
}

RED4EXT_C_EXPORT uint32_t RED4EXT_CALL Supports()
{
    return RED4EXT_API_VERSION_1;
}
