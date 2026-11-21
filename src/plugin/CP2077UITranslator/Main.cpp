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
#include <RED4ext/CNamePool.hpp>
#include <RED4ext/CString.hpp>
#include <RED4ext/Containers/DynArray.hpp>
#include <RED4ext/Handle.hpp>
#include <RED4ext/ResourcePath.hpp>
#include <RED4ext/Scripting/Natives/Generated/localization/PersistenceOnScreenEntries.hpp>

#include <Windows.h>

#include <array>
#include <atomic>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <string>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
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
// 诊断上限。只影响 verbose>0 的 [hit] / [miss] 明细；verbose=0（默认）时
// 两条路径都在第一行返回，所以放大这两个数字对正常运行零开销。
// 原来 120 撑不过十几秒：MCM 面板一开就把名额吃光，进义体界面时已经什么都不记了。
constexpr uint32_t kLogDetails = 2000;
constexpr uint32_t kMissDetails = 2000;
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
uint32_t g_onscreens = 0;                // 0=只打印地址不挂 hook；1=只调原函数；2=遍历不写；3=改写
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

// 图标字体落在 Unicode 私用区，翻译无从下手，记进 miss 只会白占名额。
// 按 UTF-8 前缀卡范围，不解码整串：
//   EE 80..BF        U+E000..U+EFFF
//   EF 80..A3        U+F000..U+F8FF
//   F3 B0..BF        U+F0000 以上
// 全角标点从 EF BC 起、CJK 兼容区从 EF A4 起，都不会被误伤。
bool HasPrivateUse(const char* aText)
{
    if (aText == nullptr)
    {
        return false;
    }
    const unsigned char* p = reinterpret_cast<const unsigned char*>(aText);
    for (; p[0] != 0; ++p)
    {
        if (p[0] == 0xEE && p[1] >= 0x80 && p[1] <= 0xBF)
        {
            return true;
        }
        if (p[0] == 0xEF && p[1] >= 0x80 && p[1] <= 0xA3)
        {
            return true;
        }
        if (p[0] == 0xF3 && p[1] >= 0xB0 && p[1] <= 0xBF)
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

// 找下一个「标签 / 值」分界，返回分隔符起点。
// 只认两种：`<标签>: <值>` 和 `<标签>   |   <值>`。
const char* NextLabelSep(const char* p)
{
    for (; *p != '\0'; ++p)
    {
        if (p[0] == ':' && p[1] == ' ')
        {
            return p;
        }
        if (p[0] == ' ' && p[1] == '|' && p[2] == ' ')
        {
            return p;                            // 留住竖线前面的空格，输出时错位不至于挤在一起
        }
    }
    return nullptr;
}

// 整串查不到时的兜底：界面上一大批文本是「标签 + 运行时值」拼出来的
// （`Frontal Cortex: 6/6`、`Holsters: melee large (blades): < 0 >`、
// `Total installed: 40`、`Arms: 1/1`）。值在运行时才算出来，静态提取永远
// 抓不到；但标签本身早就在词典里了（`Frontal Cortex` 额皮质、
// `Holsters: ranged small (pistols)` 枪套：远程小型 (手枪)）。
//
// 所以按分隔符从左往右回溯：每个分界处切一刀，取前缀查表，第一个命中的
// 就用「译文 + 原文余部」。`Holsters: ... (blades): < 0 >` 这种两层冒号的，
// 第一刀切出 `Holsters` 查不到，第二刀切出整段标签就命中了。
//
// 风控四条，缺一不可：
//   1. 只认 `: ` 和 ` | `，别的分隔形态一概不猜；
//   2. 标签 3~48 字节，太短容易撞上 `HP` / `A` 这种，太长多半不是标签；
//   3. 标签必须含英文字母（挡掉 `12: 34` 这种纯数字时间）；
//   4. 标签必须是词典里真有的 key —— 不做任何二次猜测或机器翻译。
const char* LookupByPrefix(Dict& aDict, const char* aText, char* aOut, size_t aCap)
{
    if (aText == nullptr || aOut == nullptr || aCap < 8)
    {
        return nullptr;
    }
    char key[64] = {};
    const char* from = aText;
    for (;;)
    {
        const char* sep = NextLabelSep(from);
        if (sep == nullptr)
        {
            return nullptr;
        }
        const size_t n = static_cast<size_t>(sep - aText);
        if (n >= 3 && n < 48 && n + 1 < sizeof(key))
        {
            memcpy(key, aText, n);
            key[n] = '\0';
            for (size_t i = n; i > 0 && key[i - 1] == ' '; --i)
            {
                key[i - 1] = '\0';
            }
            bool hasAlpha = false;
            for (size_t i = 0; key[i] != '\0'; ++i)
            {
                if ((key[i] >= 'A' && key[i] <= 'Z') || (key[i] >= 'a' && key[i] <= 'z'))
                {
                    hasAlpha = true;
                    break;
                }
            }
            // 值部分的英文词数：超过 2 个说明后面跟的是一整句话
            // （`LEGEND:  Precision rifle headshots deal +25% damage`、
            // `BOSS  |  Killed by V`）。这种拼出来是半中半英，比完全不翻更糟，
            // 而且会干扰判断 —— 留给整句词典，这里一律不动。
            size_t words = 0;
            for (const char* q = sep; *q != '\0'; ++q)
            {
                const bool alpha = (*q >= 'A' && *q <= 'Z') || (*q >= 'a' && *q <= 'z');
                if (!alpha)
                {
                    continue;
                }
                const char prev = (q == sep) ? '\0' : q[-1];
                const bool prevAlpha = (prev >= 'A' && prev <= 'Z') || (prev >= 'a' && prev <= 'z');
                if (!prevAlpha)
                {
                    ++words;
                }
            }
            if (hasAlpha && words <= 2)
            {
                const char* hit = Lookup(aDict, key);
                if (hit != nullptr && snprintf(aOut, aCap, "%s%s", hit, sep) > 0)
                {
                    return aOut;
                }
            }
        }
        from = sep + 1;
    }
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
//
// 明细按**原文**去重，每个不同的字符串只记一次。ApexGrapple 的调参面板每帧
// 重绘一次就把 "Figure size" / "ADS FOV" 重设一遍，AudioXL 的 30 条音量滑块
// 同理；2026-11-21 那次 1,979 行明细里只有 50 条唯一原文，97.4% 的行是重复，
// 2000 个名额 8 秒就被 5 个字符串吃光，进义体界面时已经什么都记不到了。
// 去重之后名额的含义变成「2000 条不同原文」。
void ReportHit(const char* aSrc, const char* aDst, const char* aScope)
{
    if (g_verbose == 0)
    {
        return;
    }
    const uint64_t n = g_hits.fetch_add(1, std::memory_order_relaxed) + 1;
    static std::unordered_set<std::string> seen;
    static uint32_t logged = 0;
    if (logged < kLogDetails && seen.insert(std::string(aSrc)).second)
    {
        ++logged;
        Log("[hit #%u %s] \"%s\" -> \"%s\"", logged, aScope, aSrc, aDst);
    }
    else if (g_verbose >= 2 && (n % 500) == 0)
    {
        Log("[hits] %llu", static_cast<unsigned long long>(n));
    }
}

// 只在 verbose>=2 时工作：把「查不到」的文本记下来，去重、限 kMissDetails 条。
//
// 用途是诊断 —— 界面上一片英文时，光看 hit 日志分不清是「词典里没有」还是
// 「词典里有但形态对不上」（大小写 / 换行 / 富文本标签 / 运行时占位符被替换成
// 数字）。把真实的 src 打出来，一眼就能比对。
//
// 只记「像句子」的：长度 8~300 且含空格，单字和纯数字不占名额。
// verbose=0/1 时第一行就返回，热路径上没有额外开销。
void ReportMiss(const char* aText)
{
    if (g_verbose < 2 || aText == nullptr)
    {
        return;
    }
    static std::unordered_set<std::string> seen;
    static uint32_t count = 0;
    if (count >= kMissDetails || aText[0] == '\0')
    {
        return;
    }
    // 已经是中文的串不必记：词典 key 全是英文，中文串查不到是必然的。
    // 2026-11-21 那份 318 条 miss 里 111 条（35%）是 `负重上限` / `重命名` /
    // `等级 46` 这种，把名额挤掉了真正要看的英文。图标字符同理。
    if (HasCjk(aText) || HasPrivateUse(aText))
    {
        return;
    }
    const size_t len = std::strlen(aText);
    // verbose>=3 是「全量采集」模式：连不含空格的单词也记（Kerenzikov 这种
    // 单词名字以前被这条空格规则整个漏掉）。
    if (len < 8 || len > 300 || (g_verbose < 3 && std::strchr(aText, ' ') == nullptr))
    {
        return;
    }
    // 去重键把数字串抹成 '#'。DVCore 的战斗追踪每挨一下就换一串
    // "Head   x17" / "Hits landed:   33"，不归一化的话名额几分钟就被计数器吃光
    // （2026-11-21 那次 242 条 miss 里 100 多条全是这种），真正要看的义体文本
    // 一条也记不上。日志里打的仍然是原文。
    std::string key(aText);
    for (size_t i = 0; i < key.size();)
    {
        if (key[i] >= '0' && key[i] <= '9')
        {
            size_t j = i;
            while (j < key.size() && key[j] >= '0' && key[j] <= '9')
            {
                ++j;
            }
            key.replace(i, j - i, "#");
            i += 1;
        }
        else
        {
            ++i;
        }
    }
    // 打字机特效会把同一句逐字长出来（"已拾取: 稳" → "已拾取: 稳中求胜"），
    // 已经记过它的前缀就不再重复占名额。
    for (const auto& prev : seen)
    {
        if (key.size() > prev.size() && key.compare(0, prev.size(), prev) == 0)
        {
            return;
        }
    }
    if (!seen.insert(key).second)
    {
        return;
    }
    ++count;
    Log("[miss #%u] \"%s\"", count, aText);
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

// 判断「这个**函数**是不是 inkTextWidget 的方法」。
//
// 关键：看函数挂在哪个类上（RTI 里的 CClass），**完全不碰实例内存**。
// 2026-11-21 15:41 的崩溃（CP2077UITranslator.dll+0x6EB3，mov rcx,[rcx+10h]
// 读 0xFFFFFFFFFFFFFFFF）就是因为上一版拿每个调用的 aCtx 去读 +0x30 当
// nativeType、再顺着 CClass::parent 爬 —— 一百多万次/分钟，撞上一个
// 不是 IScriptable 的 this 就是野指针。函数归属是反射数据，安全得多。
//
// 缓存按函数对象地址（只有几百个，不是几百万），命中路径只剩一次数组读。
constexpr size_t kFnCacheSize = 4096;
constexpr size_t kFnCacheMask = kFnCacheSize - 1;
void* g_fnCacheKey[kFnCacheSize] = {};
uint8_t g_fnCacheVal[kFnCacheSize] = {};   // 0=未填 1=是 2=否

__declspec(noinline) bool IsTextWidgetFunction(RED4ext::IFunction* aFn)
{
    if (aFn == nullptr || g_widgetClass == 0)
    {
        return false;
    }
    const size_t idx = (reinterpret_cast<uintptr_t>(aFn) >> 4) & kFnCacheMask;
    if (g_fnCacheKey[idx] == aFn)
    {
        return g_fnCacheVal[idx] == 1;
    }
    bool ok = false;
    __try
    {
        auto* cls = aFn->GetParent();
        for (int i = 0; i < 32 && cls != nullptr; ++i)
        {
            if (reinterpret_cast<uintptr_t>(cls) == g_widgetClass)
            {
                ok = true;
                break;
            }
            cls = cls->parent;
        }
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        ok = false;
    }
    g_fnCacheKey[idx] = aFn;
    g_fnCacheVal[idx] = ok ? 1 : 2;
    return ok;
}

// 只读 textIdKey(+0x278) 那 8 字节哈希。带 __try 的这个函数里不碰任何
// 需要「静态初始化守卫」的东西 —— 和 OnPostRegister 同理，混在一起不能展开。
__declspec(noinline) uint64_t ReadTextIdHash(void* aCtx)
{
    __try
    {
        return *reinterpret_cast<const uint64_t*>(
            reinterpret_cast<const uint8_t*>(aCtx) + kTextIdKeyOffset);
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        return 0;
    }
}

// CName 只是一个哈希，名字得问 CNamePool。地址解析不出来时 UniversalRelocFunc
// 给的是 0（InstallFallbackHook 里也是这么判的），直接调就是调 0 地址。
using CNameGet_t = const char* (*)(const RED4ext::CName&);

const char* NameToString(const RED4ext::CName& aName)
{
    static CNameGet_t getter = []() -> CNameGet_t {
        RED4ext::UniversalRelocFunc<CNameGet_t> target(
            RED4ext::Detail::AddressHashes::CNamePool_Get);
        return target;
    }();
    if (getter == nullptr)
    {
        return nullptr;
    }
    const char* text = getter(aName);
    return (text != nullptr && text[0] != '\0') ? text : nullptr;
}

// 只在 verbose>=2 时记，按键名去重，名额和 miss 共用一套上限。
void ReportKey(const char* aKey)
{
    if (g_verbose < 2 || aKey == nullptr)
    {
        return;
    }
    static std::unordered_set<std::string> seen;
    static uint32_t count = 0;
    if (count >= kMissDetails)
    {
        return;
    }
    if (!seen.insert(std::string(aKey)).second)
    {
        return;
    }
    ++count;
    Log("[key #%u] \"%s\"", count, aKey);
}

void ReportKeyOf(void* aCtx)
{
    const uint64_t hash = ReadTextIdHash(aCtx);
    if (hash == 0)
    {
        return;
    }
    ReportKey(NameToString(RED4ext::CName(hash)));
}

// ---------------------------------------------------------------- 全路径探针
//
// verbose>=3 用，默认路径一行都不执行。
//
// 不再猜「义体走哪个函数」：凡是 aCtx 确实是 inkTextWidget（含子类）的
// InternalExecute 调用，不管 reg 是不是那 8 个文本函数，都把 reg 号、
// 函数全名、当时的 text、textIdKey 记一条（按 reg+text+key 去重）。
//
//   * 义体走的是没进白名单的函数  -> 这里会直接打出那个 reg 和函数名
//   * 这里一条都不出             -> 只剩「C++ 直写 widget，不经过脚本 VM」
//                                   一种解释，那就得从 localization 那一层下手
//
// 另外每 10 万次调用打一行累计，用来区分「零命中」和「根本没调用」。
std::atomic<uint64_t> g_probeCalls{0};
std::atomic<uint64_t> g_probeWidgets{0};

void SafeCopy(char* aOut, size_t aCap, const char* aIn)
{
    if (aOut == nullptr || aCap == 0)
    {
        return;
    }
    size_t i = 0;
    for (; aIn != nullptr && aIn[i] != '\0' && i + 1 < aCap; ++i)
    {
        const char c = aIn[i];
        aOut[i] = (c == '\n' || c == '\r' || c == '\t') ? ' ' : c;
    }
    aOut[i] = '\0';
}

// 读 widget->text(+0x258) 也要带保护：CString::c_str() 遇到坏的长度字段会
// 把那 8 字节当数据指针解引用。ApplyTranslation 那条路上有 reg 闸门兜着，
// 探针这条路闸门更宽，读之前必须先确认这次读不会崩。
__declspec(noinline) void CopyWidgetText(void* aCtx, char* aOut, size_t aCap)
{
    if (aOut == nullptr || aCap == 0)
    {
        return;
    }
    aOut[0] = '\0';
    __try
    {
        SafeCopy(aOut, aCap, ReadWidgetText(aCtx));
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        aOut[0] = '\0';
    }
}

void ProbeCall(RED4ext::IFunction* aFn, uint32_t aReg, void* aCtx)
{
    const uint64_t n = g_probeCalls.fetch_add(1, std::memory_order_relaxed) + 1;
    if (!IsTextWidgetFunction(aFn))
    {
        if ((n % 100000) == 0)
        {
            Log("[probe] InternalExecute=%llu textWidget=%llu",
                static_cast<unsigned long long>(n),
                static_cast<unsigned long long>(g_probeWidgets.load(std::memory_order_relaxed)));
        }
        return;
    }
    g_probeWidgets.fetch_add(1, std::memory_order_relaxed);

    static std::unordered_set<std::string> seen;
    static std::unordered_map<uint32_t, std::string> names;
    static uint32_t count = 0;
    if (count >= kMissDetails)
    {
        return;
    }

    char text[200] = {};
    CopyWidgetText(aCtx, text, sizeof(text));

    char key[160] = {};
    const uint64_t hash = ReadTextIdHash(aCtx);
    if (hash != 0)
    {
        SafeCopy(key, sizeof(key), NameToString(RED4ext::CName(hash)));
    }

    std::string sig;
    sig.reserve(sizeof(aReg) + sizeof(text) + sizeof(key));
    sig.append(reinterpret_cast<const char*>(&aReg), sizeof(aReg));
    sig.push_back('|');
    sig.append(text);
    sig.push_back('|');
    sig.append(key);
    if (!seen.insert(sig).second)
    {
        return;
    }

    std::string& fn = names[aReg];
    if (fn.empty())
    {
        const char* full = FuncFullName(aFn);
        fn = (full != nullptr) ? full : "?";
    }
    ++count;
    Log("[probe #%u reg=%u %s] text=\"%s\" key=\"%s\"",
        count, aReg, fn.c_str(), text, key);
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

    // 带本地化键的控件：键才是文本真正的来源。义体名/描述这类控件在游戏调
    // SetLocalizationKey 的那一刻 text 还是空的，真正的中英文要等渲染时按
    // textIdKey 解析出来 —— 所以我们 hook 到的永远是个空串，下面那条早退
    // 把日志里唯一能看见的东西也吞了。这里先把键名记下来。
    ReportKeyOf(aCtx);

    const char* src = ReadWidgetText(aCtx);
    if (src == nullptr || src[0] == '\0' || src[1] == '\0')
    {
        return;                                  // 空串 / 单字符不值得查
    }

    // 不再单独扫一遍 CJK：词典的 key 全是英文，中文串查表必然 miss，
    // 直接由 Lookup 的结果决定要不要写回，少扫一遍整串。
    // joined 故意不初始化：只有整串查不到时才由 LookupByPrefix 写满，热路径
    // 上不为它清一次 1 KB 的栈。它指向的串在这一层用完就丢，不逃逸。
    char joined[1024];
    const char* scope = "global";
    const char* hit = LookupText(g_global, src);
    if (hit == nullptr)
    {
        // 整串没有，试「标签 + 运行时值」的拼法（`Frontal Cortex: 6/6`）。
        hit = LookupByPrefix(g_global, src, joined, sizeof(joined));
        scope = "prefix";
    }
    if (hit == nullptr)
    {
        ReportMiss(src);                         // verbose<2 时立刻返回
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
            ReportHit(srcCopy, hit, scope);
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
    const uint32_t reg = CachedRegIndex(aFn);
    const bool isText = IsTextReg(reg);
    // verbose<3 时行为和以前一模一样：不是文本函数就直接透传，连计数都不碰。
    if (!isText && g_verbose < 3)
    {
        return g_orig(aFn, aCtx, aFrame, aOut, a4);
    }
    const bool result = g_orig(aFn, aCtx, aFrame, aOut, a4);
    if (isText)
    {
        ApplyTranslation(aCtx);
    }
    if (g_verbose >= 3)
    {
        ProbeCall(aFn, reg, aCtx);               // 全路径观察，只读不写
    }
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
        if (strncmp(p, "verbose", 7) == 0 || strncmp(p, "stat", 4) == 0 || strncmp(p, "onscreens", 9) == 0)
        {
            const char* eq = strchr(p, '=');
            if (eq != nullptr)
            {
                const int v = atoi(eq + 1);
                // 上限必须跟着 kProbeVerbose 走：加 verbose=3 时忘了放宽这里，
                // 2026-11-21 15:35 那次整场日志只有 3 行，探针一个都没开起来
                // ——配置写的是 3，被 v <= 2 挡掉后回落成 0。
                if (p[0] == 'v' && v >= 0 && v <= 3)
                {
                    g_verbose = static_cast<uint32_t>(v);
                }
                else if (p[0] == 's' && v >= 0 && v <= 1)
                {
                    g_stat = static_cast<uint32_t>(v);
                }
                else if (p[0] == 'o' && v >= 0 && v <= 3)
                {
                    g_onscreens = static_cast<uint32_t>(v);
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

// ============================================================================
// onscreens 语言表覆盖
//
// 坐标：Localization_LoadOnScreens，RED4ext 地址库 hash 3550098299
// （与 Codeware 的 src/Red/Shared/AddressLib.hpp 同一个值），原型
// uint64_t(Handle<Entries>&, ResourcePath)。Codeware 和 ArchiveXL 都挂这里：
// 前者把 provider 的 en-us fallback 写进表，后者写 .xl 里的条目。
//
// 谁最后执行谁说了算。red4ext 按插件目录名字母序加载，本插件
// CP2077UITranslator 排在 Codeware 之后，同址 hook 时后加载者位于最外层、
// 回调最后跑，所以这里写进去的中文不会再被 provider 的 en-us 覆盖。
// 不依赖 Cosmopolitan、redscript、TweakXL 任何一个第三方模组。
// ============================================================================
// 类型必须用 SDK 里由游戏 Reflection 数据生成的那一份，不能自己手写。
// 2026-11-21 16:52 那次闪退就是这么来的：我照着自己猜的布局写了个
// { DynArray entries; }（entries 放在偏移 0），而真正的
// localizationPersistenceOnScreenEntries 继承 ISerializable，entries 在 0x30，
// 结构体总长 0x40。结果 Size() 读成 498（ISerializable 里的字段），
// entry[0] 的 secondaryKey 是个野指针，游戏当场 0xC0000005。
// SDK 那份带 RED4EXT_ASSERT_SIZE，布局由编译器替我们盯着。
using OnScreenEntries = RED4ext::localization::PersistenceOnScreenEntries;
using OnScreenEntry = RED4ext::localization::PersistenceOnScreenEntry;

using LoadOnScreens_t = uint64_t (*)(RED4ext::Handle<OnScreenEntries>&, RED4ext::ResourcePath);

LoadOnScreens_t g_loadOnScreensOrig = nullptr;

// 只在 __try 外面调：本函数里有 CString 临时对象要展开，和 __try 同处一个
// 函数体 MSVC 会直接报 C2712，所以遍历改写必须拆成独立的 noinline 函数。
__declspec(noinline) uint32_t RewriteOnScreens(OnScreenEntries* aTable, bool aWrite)
{
    auto& list = aTable->entries;
    const uint32_t count = list.Size();
    Log("[onscreens] entries=%u", count);
    if (count > 200000)
    {
        // 布局对不上时 Size() 读出来是垃圾，别拿着它去遍历野内存
        Log("[onscreens] implausible count, layout mismatch?");
        return 0;
    }

    uint32_t replaced = 0;
    for (uint32_t i = 0; i < count; ++i)
    {
        auto& entry = list[i];
        const char* key = entry.secondaryKey.c_str();
        if (i < 3)
        {
            // 前三条先把 key 原样打出来：key 是 GP_Herald_Mesh-Name 这种形状
            // 就说明 secondaryKey@0x08 的偏移是对的，后面的赋值才敢下笔。
            Log("[onscreens] entry[%u] pk=%llu key='%s'", i,
                static_cast<unsigned long long>(entry.primaryKey), key == nullptr ? "(null)" : key);
        }
        if (key == nullptr || key[0] == '\0')
        {
            continue;
        }
        const char* zh = Lookup(g_global, key);
        if (zh == nullptr)
        {
            continue;
        }
        ++replaced;
        if (aWrite)
        {
            entry.femaleVariant = RED4ext::CString(zh);
            entry.maleVariant = RED4ext::CString(zh);
        }
    }
    return replaced;
}

uint64_t LoadOnScreensDetour(RED4ext::Handle<OnScreenEntries>& aOut, RED4ext::ResourcePath aPath)
{
    // 这行必须在调原函数之前。16:43 那次闪退，原函数一进就炸，而 "called"
    // 当初写在调用之后，结果一行证据都没留下，白丢一次启动。
    Log("[onscreens] enter mode=%u path=%llx", g_onscreens,
        static_cast<unsigned long long>(aPath.hash));

    const uint64_t result = g_loadOnScreensOrig(aOut, aPath);
    Log("[onscreens] orig ret=%llu inst=%p",
        static_cast<unsigned long long>(result), static_cast<void*>(aOut.instance));

    if (g_onscreens < 2 || aOut.instance == nullptr || g_global.map.empty())
    {
        return result;
    }

    // 结构布局（每条 0x68，femaleVariant@0x28、maleVariant@0x48）是从
    // localizationPersistenceOnScreenEntry 反推的，一旦对不上，遍历就是读野内存。
    // 用 SEH 兜住：最坏也只崩这一次回调，不让整个游戏陪葬。
    uint32_t replaced = 0;
    __try
    {
        replaced = RewriteOnScreens(aOut.instance, g_onscreens >= 3);
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        Log("[onscreens] EXCEPTION 0x%08X during rewrite",
            static_cast<unsigned>(GetExceptionCode()));
        return result;
    }

    Log("[onscreens] replaced=%u", replaced);
    return result;
}

void InstallOnScreensHook()
{
    static RED4ext::UniversalRelocFunc<LoadOnScreens_t> target(3550098299u);
    LoadOnScreens_t fn = target;
    Log("[onscreens] addr=%p mode=%u", reinterpret_cast<void*>(fn), g_onscreens);
    if (fn == nullptr)
    {
        Log("[onscreens] address unresolved");
        return;
    }
    if (g_onscreens == 0)
    {
        // 只报告地址，不挂 hook：上一次直接在没验证地址的情况下 Attach，
        // 16:43 那次启动崩了。先确认解析出来的地址落在主模块里再开。
        const auto base = reinterpret_cast<uintptr_t>(GetModuleHandleW(nullptr));
        Log("[onscreens] probe addr=%p base=%p delta=%lld",
            reinterpret_cast<void*>(fn),
            reinterpret_cast<void*>(base),
            static_cast<long long>(reinterpret_cast<intptr_t>(fn) - static_cast<intptr_t>(base)));
        return;
    }
    if (!g_sdk->hooking->Attach(g_handle, reinterpret_cast<void*>(fn),
                                reinterpret_cast<void*>(&LoadOnScreensDetour),
                                reinterpret_cast<void**>(&g_loadOnScreensOrig)))
    {
        Log("[onscreens] Attach failed");
        return;
    }
    Log("[onscreens] hooked");
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
        InstallOnScreensHook();
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
