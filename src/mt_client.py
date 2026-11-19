# -*- coding: utf-8 -*-
"""
mt_client.py —— 本地部署模型(OpenAI 兼容接口)的最小客户端 + 批量翻译

只依赖标准库, 因此打包体积不变。
支持: Ollama /v1、LM Studio、vLLM、llama.cpp server、one-api 等兼容实现。
"""
import json
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

SYSTEM_PROMPT = (
    "你是《赛博朋克 2077》模组的专业本地化译者。"
    "用户会给你一个 JSON 字符串数组, 请把每个元素翻译成简体中文。\n"
    "规则:\n"
    "1. 只输出 JSON 字符串数组, 不要解释、不要 Markdown 代码块、不要多余文字。\n"
    "2. 输出数组的长度和顺序必须与输入完全一致。\n"
    "3. 占位符必须原样保留: %s %d %q {name} \\n 等。\n"
    "   <Rich ...> 和 </> 只是样式标记: 标签本身原样保留, 但标签之间的文字是正文,"
    "必须翻译成中文, 不能因为它在标签里就保留英文(品牌名和标语也要翻)。\n"
    "   占位符的**顺序**也不能变: 程序按位置取参数, 把 %s 和 %d 换个位置"
    "会直接报错崩掉。中文语序别扭就换个说法, 别让占位符换位。\n"
    "4. 不要给译文额外加引号, 不要翻译纯数字/纯符号/资源路径。\n"
    "5. 术语沿用游戏官方译法: Quickhack=快速破解, Cyberware=义体, "
    "Breach Protocol=破解协议, Perk=专长, Ripperdoc=义体医生, "
    "Netrunner=网络黑客, Braindance=超梦, Fixer=中间人, Eddies=欧币, "
    "MCM/Mod Settings=模组设置。\n"
    "6. 已经是中文的条目原样返回。"
)

SINGLE_PROMPT = (
    "你是《赛博朋克 2077》模组的专业本地化译者。"
    "把用户给出的一句话翻译成简体中文, 只输出译文本身, 不要解释、不要加引号。"
    "占位符(%s %d %q {name})与换行必须原样保留, 顺序也不能变"
    "(程序按位置取参数, 换位会崩)。"
    "<Rich ...> 和 </> 是样式标记: 标签本身保留, 标签之间的文字必须翻译成中文。"
    "术语沿用游戏官方译法(Quickhack=快速破解, Cyberware=义体, Perk=专长, "
    "Ripperdoc=义体医生, Netrunner=网络黑客, Eddies=欧币)。"
)


# ------------------------------------------------ 品牌 / 系列署名的固定译名
#
# SenSen 系列的每件装备描述末尾都有一段「厂家署名」，形如
#   <Rich color="MainColors.Gold">SENSATION TECHNOLOGY · Augur Collection</>
# 提示词里写着「品牌名和标语也要翻」，但模型只在**同一批请求内**保持一致：
# 实测译文库里同时存在四种品牌写法（SENSATION TECHNOLOGY / 感官科技 /
# 森森科技 / 感觉科技）和三种系列写法（先知套装 / 先知系列 / 先兆系列）。
# 16:46 那一批二十多条全部原样保留英文，更早的批次又全部译成「感官科技」。
# prompt 管不住跨批一致性，所以这里改成确定性的字面替换：模型给英文就换
# 成中文，模型给错的中文写法也一并纠正。
#
# 名字是用户拍板的：品牌统一「感官科技」，Herald 用「先驱」而不是「使者/
# 信使」。顺序由代码按字面长度降序排，长串永远先命中，避免「SENSATION
# TECHNOLOGY · Augur Collection」被「Augur Collection」那条先吃掉。
_TERM_FIXES_RAW = [
    # —— 整条署名（最长，优先级最高）
    # 译名来自 Nexus 36646 的社区中文包（不是游戏官方）：品牌、遗珍、美德标语、
    # 人名音译都跟它对齐；只有 Augur 例外，用户不喜欢「占兆」，仍用「先知」。
    ("SENSATION TECHNOLOGY · Heirloom · worn with any Collection",
     "感官科技 · 遗珍 · 可与任意套装混搭"),
    ("SENSATION TECHNOLOGY · Virtue Collection · Move like a Ghost.",
     "感官科技 · 美德套装 · 如幽灵般行动。"),
    ("SENSATION TECHNOLOGY · Index Relic · struck for Ilo Acaidia",
     "感官科技 · 索引遗物 · 为伊洛·阿凯迪亚铸造"),
    ("SENSATION TECHNOLOGY · Solar Relic · struck for Azula Zaidi",
     "感官科技 · 太阳遗物 · 为阿祖拉·扎伊迪铸造"),
    ("SENSATION TECHNOLOGY · Grace Relic · struck for Akima Ellan Grace",
     "感官科技 · 恩典遗物 · 为阿基玛·艾兰·恩典铸造"),
    ("SENSATION TECHNOLOGY · Augur Collection", "感官科技 · 先知套装"),
    ("SENSATION TECHNOLOGY · Herald Collection", "感官科技 · 先驱套装"),
    # SENSEN 是作者自己在署名里写的缩写形态（大字标用全大写，描述末尾
    # 用 SENSATION TECHNOLOGY）。同一套装备两种写法属于作者的疏漏，这里
    # 一并归到「感官科技」——用户 2026-11-14 定下的口径：整个模组的署名
    # 只认一个品牌名。
    ("SENSEN · AUGUR COLLECTION", "感官科技 · 先知套装"),
    ("SENSEN · HERALD COLLECTION", "感官科技 · 先驱套装"),
    ("SENSEN · VIRTUE COLLECTION", "感官科技 · 美德套装"),
    # GP-Signed：每件 5++ 阶装备的签名落款，社区包的译法是「天台铸造」。
    ("Struck in Tentai. People over power.", "天台铸造。以人为本，而非权力。"),
    ("刻于天胎。人定胜天。", "天台铸造。以人为本，而非权力。"),
    # —— 已经译成中文、但系列名用了「系列」的旧缓存
    ("感官科技 · 先兆系列", "感官科技 · 先知套装"),
    ("感官科技 · 先知系列", "感官科技 · 先知套装"),
    ("感官科技 · 美德系列", "感官科技 · 美德套装"),
    ("感官科技 · 先驱系列", "感官科技 · 先驱套装"),
    # —— 上一版口径留下的三种中文写法，跟着社区包改过来
    ("传家宝", "遗珍"),
    ("如幽灵般移动。", "如幽灵般行动。"),
    # —— 产品名前缀：社区包把 `SenSen Holdfast Berserk` 译成
    # 「感官科技 坚守狂暴」，说明作者名 SenSen 在产品名里也当作品牌处理。
    # 只收「SenSen + 空格」这种前缀形态，SenSenDeckMirror_Herald 这类
    # 驼峰内部标识没有空格，不会被碰到。
    ("SenSen ", "感官科技 "),
    ("森森 ", "感官科技 "),
    # —— The House：SenSen 那个拟人化的主理方，既是组织也是宅邸的意象。
    # 只收带定冠词或固定搭配的形态，不做 `House` 的裸词映射 —— 原版任务名里
    # 也有 House（`House on a Hill`、`Operation: House Accounts`），那些得留给
    # 模型按上下文处理，一刀切成「宅邸」会改掉游戏本体的术语。
    ("Sensation Technology's House", "感官科技的宅邸"),
    ("House release fee", "宅邸放行费"),
    ("House crew", "宅邸成员"),
    ("The House", "宅邸"),
    # 上面几组的老机翻残留（「房屋」是 House 被当普通名词的产物）
    ("房屋发布费", "宅邸放行费"),
    ("房屋释放费", "宅邸放行费"),
    ("房屋工作人员", "宅邸成员"),
    ("房管团队", "宅邸成员"),
    ("房屋拒绝升级", "宅邸拒绝升级"),
    # —— 英文残留兜底：模型只译了半句时，剩下的单词也拉回固定译名
    ("worn with any Collection", "可与任意套装混搭"),
    ("Move like a Ghost.", "如幽灵般行动。"),
    ("Index Relic", "索引遗物"),
    ("Solar Relic", "太阳遗物"),
    ("Grace Relic", "恩典遗物"),
    ("Augur Collection", "先知套装"),
    ("Herald Collection", "先驱套装"),
    ("Virtue Collection", "美德套装"),
    # MCM 分类名是复数（`@runtimeProperty("ModSettings.mod", "ST Heirlooms")`），
    # \bHeirloom\b 匹配不到它，得单列一条；排在单数前面由长度排序保证。
    ("Heirlooms", "遗珍"),
    ("Heirloom", "遗珍"),
    ("SENSATION TECHNOLOGY", "感官科技"),
    # 只收「SENSEN 带间隔号」的署名形态：裸词 SENSEN 在 "SenSen Pax Grips"
    # 这类前缀里也出现，那条由上面的「SenSen 」规则统一处理。
    ("SENSEN ·", "感官科技 · "),
    ("Augur", "先知"),
    ("Herald", "先驱"),
    ("Virtue", "美德"),
]


def _term_rx(s):
    """把署名字面量转成正则：分隔点号和空白都放宽一档。

    模型抄署名时不一定用同一个间隔号（· U+00B7 / ・ U+30FB / • U+2022
    都见过），空格数也随手变。只对这些点号与空白放宽，其余字符仍然按
    字面量逐字转义，不会误伤正文里的别的词。
    """
    out = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "·":
            out.append(r"\s*[·・•∙]\s*")
            i += 1
        elif ch == " ":
            while i < n and s[i] == " ":
                i += 1
            out.append(r"\s+")
        else:
            j = i
            while j < n and s[j] not in "· ":
                j += 1
            out.append(re.escape(s[i:j]))
            i = j
    return "".join(out)


def _term_pattern(s):
    """编译一条规则；英文词首尾补 \\b，免得 "Augur" 命中 "Augury" 这类子串。

    中文规则不加边界（\\b 对 CJK 没有意义，加了反而可能不匹配）。
    """
    rx = _term_rx(s)
    head, tail = s[:1], s[-1:]
    if head.isascii() and head.isalpha():
        rx = r"\b" + rx
    if tail.isascii() and tail.isalpha():
        rx = rx + r"\b"
    return re.compile(rx, re.IGNORECASE)


_TERM_PATTERNS = [
    (_term_pattern(a), b)
    for a, b in sorted(_TERM_FIXES_RAW, key=lambda p: -len(p[0]))
]

# 品牌名的错译要在主表**之前**先纠正，否则永远晚一步：
# 「感觉科技 · 美德系列」要是先跑主表，长串规则「感官科技 · 美德系列」
# 匹配不上（当时还是「感觉科技」），等短规则把品牌改过来时，系列规则
# 那一趟已经跑完了，结果只剩品牌被修正、系列仍停在「系列」。
_TERM_BRAND_FIXES = [
    ("感觉科技", "感官科技"),
    ("森森科技", "感官科技"),
]
_TERM_BRAND_PATTERNS = [
    (_term_pattern(a), b) for a, b in _TERM_BRAND_FIXES
]

# 替换结束后可能留下「先驱 套装」这种中文夹空格：整串规则没命中时是先
# 把 Herald 换成「先驱」、套装还是英文时又换成「套装」，中间那个空格就
# 落在了两个中文之间。
_TERM_GLUE_RE = re.compile(r"(先驱|先知|美德|遗珍|森森)\s+(?=套装)")

# Heirloom 那句话的译文有三种残尾：「可与任何套装搭配」（少了「穿戴」）、
# 「可与任何收藏搭配穿戴」、「可与任何收藏品搭配佩戴」。整串规则只认得
# 英文原文，这一条把已经译成中文的残尾也拉回社区包的统一写法。
_TERM_HEIRLOOM_RE = re.compile(r"可与任何(?:收藏品?|套装)搭配(?:穿戴|佩戴)?")

# 早先跑过的缓存里已经存成了「森森 · 先知套装」这种半成品：规则表里的英文
# SENSEN 那时候还在，但当时映射的是「森森」。现在口径改成全模组只认一个
# 品牌名，所以中文形态也要再收一次。只认「森森 + 间隔号 + 已知系列名」，
# 不动「森森镜像先知」这类 MCM 内部标识（那里 SenSen 是作者名不是署名）。
_TERM_SENSEN_ZH_RE = re.compile(r"森森\s*·\s*(?=(?:先知|先驱|美德|遗珍))")


def normalize_terms(text):
    """把品牌与系列署名强制统一到固定译名，其余正文原样不动。

    纯字面替换，不碰 <Rich ...> 标签和 {int_0} 这类占位符 —— 规则表里没有
    任何以 < { } 开头或结尾的串，所以标签结构不会被破坏。
    对已经正确的译文是空操作（规则里的英文/错译都已经不存在）。
    """
    if not text:
        return text
    out = str(text)
    for pat, rep in _TERM_BRAND_PATTERNS:
        out = pat.sub(rep, out)
    for pat, rep in _TERM_PATTERNS:
        out = pat.sub(rep, out)
    out = _TERM_GLUE_RE.sub(r"\1", out)
    out = _TERM_SENSEN_ZH_RE.sub("感官科技 · ", out)
    return _TERM_HEIRLOOM_RE.sub("可与任意套装混搭", out)


class MTError(Exception):
    """机翻过程中的可读错误"""


# 输出长度上限。模型不给上限时会一直往下写：2026-10-31 的日志里, 一批
# 只有 8 条短界面文案的请求生成过 3254 个 token(单条 49.9 秒), 而正常
# 描述同一批内容的响应只要两三百个 token。三条 180 秒超时里两条是这么来的。
#
# 2026-11-09 从 8192 提到 16384：实测 Qwen3.5-9B(Q4_K_M) 的英文↔中文换算约
# 5.4 字符/token, 8192 只够 44000 字符, 长文档必然在中间被砍掉, 而 chat()
# 以前不检查 finish_reason, 半截结果会被当成翻译成功。16384 对应约 88000
# 字符, 配合 translate_batch 的 300 秒超时(85 tok/s 下 16384 token ≈ 193 秒)。
_MAX_OUT_TOKENS = 16384


def output_budget(texts):
    """按这批原文估一个「够用但不会失控」的输出 token 上限。

    英文源译成中文后字符数大致减半, 中文约 1 token/字, 再加上 JSON 的
    引号逗号和换行开销。系数 0.9 是实测偏松一档: 正常批次碰不到上限,
    只有模型开始复读时才会被截断。截断不是灾难 —— 解析失败会退化成逐条
    翻译, 那条路径已经存在, 只是慢, 但不会挂起 3 分钟。
    """
    if isinstance(texts, str):
        texts = [texts]
    chars = 0
    for t in (texts or []):
        chars += len(str(t or ""))
    n = len(texts or [])
    return max(512, min(_MAX_OUT_TOKENS, 128 + 4 * n + int(chars * 0.9)))


def _native_base(api_base):
    """把 OpenAI 兼容地址还原成 Ollama 原生地址(去掉结尾的 /v1)。"""
    b = (api_base or "").rstrip("/")
    if b.endswith("/v1"):
        b = b[:-3]
    return b.rstrip("/")


_OLLAMA_PORT = 11434
_backend_cache = {}


def _port_open(api_base, timeout=0.35):
    """本地端口有没有在监听。比发 HTTP 请求快一个数量级。

    服务没起时 urllib 在这台机器上要磨 6 秒才放弃（实测 6.07s：/api/version
    2s + /props 2s + /models 3s，串行三次）。这个探测挂在「关窗口之前」，
    于是「本地模型没跑时点关闭要卡一会儿」—— 卡的就是这 6 秒。
    """
    try:
        u = urlsplit(api_base or "")
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "https" else 80)
        if not host or not port:
            return False
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _probe_llamacpp(key):
    """是不是 llama.cpp 的 server。

    它的 /props 同时带 model_path 和 total_slots, 是这套接口独有的。老版本没有
    /props 时退一步看 OpenAI 模型列表里的 owned_by。探不到就当不是。
    """
    try:
        with urllib.request.urlopen(_native_base(key) + "/props", timeout=2) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        if isinstance(d, dict) and ("model_path" in d or "total_slots" in d):
            return True
    except Exception:
        pass
    try:
        with urllib.request.urlopen(key + "/models", timeout=3) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        for it in (d.get("data") or []):
            if (isinstance(it, dict)
                    and str(it.get("owned_by") or "").lower() == "llamacpp"):
                return True
    except Exception:
        pass
    return False


def detect_backend(api_base):
    """判断后端是哪一种: "ollama" / "llamacpp" / "openai"。按地址缓存。

    两个后端都有「默认开着思考」的模型, 但关掉思考的参数不一样:
      * Ollama   —— reasoning_effort="none"（OpenAI 兼容层映射）
      * llama.cpp —— jinja 模板里的 enable_thinking=false（chat_template_kwargs）
    发错了不会报错, 只会让思考把 max_tokens 全部吃掉、content 留空, 表面症状是
    「连得上但一条都翻不出来」。所以必须先认准后端再决定发哪个字段。
    """
    key = (api_base or "").rstrip("/")
    if not key:
        return "openai"
    if key in _backend_cache:
        return _backend_cache[key]
    # 端口上没人监听就别发 HTTP 了（见 _port_open 的注释）。这里**不写缓存**：
    # 服务可能下一秒就起来，缓存一个「不是 llama.cpp」会让之后每个请求都按
    # openai 发，thinking 开关发不对、正文全空。
    if not _port_open(key):
        return "openai"
    kind = "openai"
    try:
        if urlsplit(key).port == _OLLAMA_PORT:
            kind = "ollama"
    except Exception:
        pass
    if kind == "openai":
        try:
            with urllib.request.urlopen(_native_base(key) + "/api/version",
                                        timeout=2) as r:
                if "version" in json.loads(r.read().decode("utf-8", "replace")):
                    kind = "ollama"
        except Exception:
            pass
    if kind == "openai" and _probe_llamacpp(key):
        kind = "llamacpp"
    _backend_cache[key] = kind
    return kind


def _is_ollama(api_base):
    """后端是不是 Ollama —— 只有它认 reasoning_effort 这个开关。"""
    return detect_backend(api_base) == "ollama"


def _is_llamacpp(api_base):
    """后端是不是 llama.cpp 的 server。"""
    return detect_backend(api_base) == "llamacpp"


def warmup(api_base, model, keep_alive="2h", timeout=300):
    """提前把模型载入并延长保活, 返回 (ok, 说明)。

    首次载入 27B 要 35-80 秒(实测日志: 空闲超时后的第一个请求 1m30s / 45s,
    大部分花在从盘上读 17 GB 权重)。这段时间以前算在第一个批次的 180 秒
    超时里, 是最冤的一种失败。

    只有 Ollama 原生 /api/chat 认 keep_alive, OpenAI 兼容层不认。LM Studio /
    vLLM 没有这个端点, 会 404 —— 那不算错误, 调用方照常走兼容路径。
    """
    if not api_base:
        return False, "没有填写 API 地址"
    if _is_llamacpp(api_base):
        # llama.cpp 起服务时就把权重载进显存了, 既没有 keep_alive 也没有
        # /api/chat; 打过去只会拿一个 404。这里发的意义只有一个: 用一次最小
        # 生成把 CUDA kernel 和 prompt cache 烫起来, 省掉首个批次的冷启动。
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "max_tokens": 1,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            _post(api_base.rstrip("/") + "/chat/completions", payload, timeout)
        except Exception as ex:
            return False, str(ex)
        return True, str(model)
    url = _native_base(api_base) + "/api/chat"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
        "keep_alive": keep_alive,
        "options": {"num_predict": 1},
    }
    try:
        data = _post(url, payload, timeout)
    except Exception as ex:
        return False, str(ex)
    if not isinstance(data, dict):
        return False, "接口返回的不是 JSON 对象"
    return True, str(data.get("model") or model)


def unload(api_base, model="", timeout=20, api_key=""):
    """让 Ollama 立刻把模型从显存里卸掉，返回 (ok, 说明)。

    27B 常驻要占 21 GB，翻译完不卸，游戏和别的程序就一直在抢显存。
    Ollama 的做法是发一个 keep_alive=0 的请求：不生成任何 token，直接卸载。
    非 Ollama 后端没有这个概念，如实返回 False 而不是假装成功。
    """
    if not api_base:
        return False, "没有填写 API 地址"
    if _is_llamacpp(api_base):
        # llama.cpp 没有卸载端点: 权重是 llama-server 进程本身的显存, 只能
        # 停进程（见 kill_llamacpp）。如实说明, 不要把「停不掉」伪装成「不用停」。
        return False, ("llama.cpp 的模型常驻在 llama-server 进程里，没有卸载接口；"
                       "要释放显存请结束那个进程（关闭软件时会问一次）")
    if not _is_ollama(api_base):
        return False, "当前后端不是 Ollama，不需要释放显存"
    mdl = (model or "").strip()
    if not mdl:
        try:
            names = list_models(api_base, api_key=api_key)
        except Exception:
            names = []
        mdl = names[0] if names else ""
    if not mdl:
        return False, "没有可卸载的模型"
    payload = {"model": mdl, "keep_alive": 0, "stream": False,
               "options": {"num_predict": 1}}
    try:
        _post(_native_base(api_base) + "/api/generate", payload, timeout, api_key)
    except Exception as ex:
        return False, str(ex)
    return True, mdl


# ---------------------------------------------------------------- 进程
# llama-server 没有卸载接口: 权重就是那个进程自己的显存, 想释放只能结束进程。
# 下面这几个函数只在本机、且端口上的进程名确实是 llama-server 时才动手。

def _run_quiet(args, timeout=10):
    """跑一个控制台程序, 返回 (输出文本, 退出码)。不弹黑框。"""
    try:
        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = 0x08000000        # CREATE_NO_WINDOW
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, **kw)
        txt = (p.stdout or b"").decode("utf-8", "replace")
        if not txt.strip():
            txt = (p.stderr or b"").decode("utf-8", "replace")
        return txt, p.returncode
    except Exception as ex:
        return str(ex), -1


def _resolve_host_port(api_base):
    """把 api_base 拆成 (host, port)；拆不出来时 port 为 None。"""
    try:
        parts = urlsplit(api_base or "")
        return (parts.hostname or "").lower(), parts.port
    except Exception:
        return "", None


def _listening_pids(port):
    """本机在 port 上处于 LISTENING 的 PID 列表。"""
    out, _rc = _run_quiet(["netstat", "-ano", "-p", "TCP"])
    pids = []
    for line in out.splitlines():
        f = line.split()
        if len(f) < 5:
            continue
        if f[0].upper() != "TCP" or f[3].upper() != "LISTENING":
            continue
        if not f[1].endswith(":" + str(port)):
            continue
        try:
            pids.append(int(f[4]))
        except ValueError:
            pass
    return pids


def _proc_name(pid):
    """进程名（小写、带 .exe）；取不到返回空串。"""
    out, _rc = _run_quiet(["tasklist", "/FI", "PID eq %d" % pid,
                           "/FO", "CSV", "/NH"])
    line = out.strip().splitlines()[0] if out.strip() else ""
    if not line.startswith('"'):
        return ""
    return line.split('","')[0].strip('"').strip().lower()


def llamacpp_pids(api_base):
    """本机 api_base 端口上监听的 llama-server PID 列表。

    名字不是 llama-server 的一律排除 —— 8080 太常见, 不能见到端口就下手。
    """
    host, port = _resolve_host_port(api_base)
    if host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0") or not port:
        return []
    return [pid for pid in _listening_pids(port)
            if _proc_name(pid) in ("llama-server.exe", "llama-server")]


def kill_llamacpp(api_base, timeout=15):
    """结束本机的 llama-server 进程，返回 (ok, 说明)。

    只认本机地址 + 只认 llama-server 进程名, 两条任一不满足都只报错不动手。
    """
    host, port = _resolve_host_port(api_base)
    if host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        return False, "只支持结束本机的 llama-server"
    if not port:
        return False, "API 地址里没有端口号"
    listening = _listening_pids(port)
    if not listening:
        return False, "端口 %d 上没有正在监听的进程" % port
    mine = llamacpp_pids(api_base)
    if not mine:
        others = "；".join("%d(%s)" % (pid, _proc_name(pid) or "进程名未知")
                          for pid in listening)
        return False, "端口 %d 上不是 llama-server：%s" % (port, others)
    done = []
    for pid in mine:
        _out, rc = _run_quiet(["taskkill", "/PID", str(pid), "/F", "/T"],
                              timeout=timeout)
        if rc == 0:
            done.append(pid)
    if not done:
        return False, "taskkill 失败（可能需要管理员权限）"
    return True, "已结束 llama-server（PID %s）" % "、".join(str(x) for x in done)


# ---------------------------------------------------------------- HTTP
def _post(url, payload, timeout, api_key=""):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", "Bearer " + api_key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as ex:
        body = ""
        try:
            body = ex.read()[:400].decode("utf-8", "replace")
        except Exception:
            pass
        raise MTError("HTTP %s %s %s" % (ex.code, ex.reason, body.strip()))
    except urllib.error.URLError as ex:
        raise MTError("连接失败: %s" % (ex.reason,))
    except Exception as ex:
        raise MTError(str(ex))
    try:
        return json.loads(raw)
    except Exception:
        raise MTError("返回不是合法 JSON: %s" % raw[:200])


def list_models(api_base, timeout=6, api_key=""):
    """返回模型 id 列表"""
    url = api_base.rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if api_key:
        req.add_header("Authorization", "Bearer " + api_key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as ex:
        raise MTError("HTTP %s %s" % (ex.code, ex.reason))
    except urllib.error.URLError as ex:
        raise MTError("连接失败: %s" % (ex.reason,))
    except Exception as ex:
        raise MTError(str(ex))
    ids = []
    if isinstance(data, dict):
        for it in (data.get("data") or []):
            if isinstance(it, dict) and it.get("id"):
                ids.append(str(it["id"]))
        if not ids:
            for it in (data.get("models") or []):
                if isinstance(it, dict) and it.get("name"):
                    ids.append(str(it["name"]))
    return ids


def list_model_details(api_base, timeout=6, api_key=""):
    """列出服务上的模型及其容量信息，给设置页的下拉用。取不到的一律留空。

    各家字段不统一：
      * llama.cpp —— data[].meta 带 n_ctx / ftype / size，
        architecture.input_modalities 里出现 image/video 才算多模态
      * Ollama    —— /api/tags 的 details 带 parameter_size / quantization_level
      * 其它      —— 往往只有 id
    """
    url = api_base.rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if api_key:
        req.add_header("Authorization", "Bearer " + api_key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        data = {}
    out = []
    if isinstance(data, dict):
        for it in (data.get("data") or []):
            if not isinstance(it, dict) or not it.get("id"):
                continue
            meta = it.get("meta") or {}
            arch = it.get("architecture") or {}
            details = it.get("details") or {}
            mods = [str(m).lower() for m in (arch.get("input_modalities") or [])]
            try:
                ctx = int(meta.get("n_ctx") or 0)
            except Exception:
                ctx = 0
            try:
                size = int(meta.get("size") or 0)
            except Exception:
                size = 0
            out.append({
                "id": str(it["id"]),
                "ctx": ctx,
                "quant": str(meta.get("ftype")
                             or details.get("quantization_level") or ""),
                "size": size,
                "params": str(details.get("parameter_size") or ""),
                "multimodal": any(m in ("image", "video", "audio") for m in mods),
            })
    if not out and isinstance(data, dict):
        for it in (data.get("models") or []):
            if isinstance(it, dict) and it.get("name"):
                out.append({"id": str(it["name"]), "ctx": 0, "quant": "",
                            "size": 0, "params": "", "multimodal": False})
    return out


def resolve_model(api_base, model="", timeout=10, api_key=""):
    """模型名为空时自动取服务上的第一个模型"""
    if model:
        return model
    ids = list_models(api_base, timeout=timeout, api_key=api_key)
    if not ids:
        raise MTError("服务没有返回任何模型, 请在设置里手动填写模型名称")
    return ids[0]


def _message_text(data):
    """从 OpenAI 规范的返回里取 (正文, 思考内容)。

    llama.cpp / vLLM / DeepSeek 都把思考放在 reasoning_content（少数用
    reasoning）里, 正文才是 content。两者要分开取, 否则「思考把预算吃光、
    正文是空串」这件事会被当成翻译成功。
    """
    try:
        msg = data["choices"][0]["message"]
    except Exception:
        raise MTError("返回格式不符合 OpenAI 规范: %s" % str(data)[:200])
    content = msg.get("content")
    if isinstance(content, list):      # 少数实现返回分段内容
        content = "".join(seg.get("text", "") for seg in content
                          if isinstance(seg, dict))
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    if not isinstance(reasoning, str):
        reasoning = ""
    return (content or ""), reasoning


def _finish_reason(data):
    """取 choices[0].finish_reason；结构异常时返回 None。"""
    try:
        choices = (data or {}).get("choices") or []
        return choices[0].get("finish_reason") if choices else None
    except Exception:
        return None


def _finish_truncated(data):
    """输出是否被 max_tokens 截断（OpenAI 兼容层统一用 length 表示）。"""
    return (_finish_reason(data) or "").lower() == "length"


def _check_truncated(data, max_tokens):
    """截断时抛错，不再把半截结果当成翻完了。

    以前 chat() 完全不看 finish_reason：长文档译到一半被 8192 token 砍断，
    半截正文照样返回，上层解析不出 JSON 就退化成逐条、逐条不行就整批失败，
    而单条长文本这条路径会把前半段直接写回去——用户看到的就是「后半段没了」。
    """
    if _finish_truncated(data):
        raise MTError(
            "模型输出被截断（finish_reason=length，上限 %s token），后半段会丢失。"
            "请减小单批文本量，或调高 mt_client._MAX_OUT_TOKENS。"
            % (max_tokens or "未指定"))


def chat(api_base, model, messages, temperature=0.2, timeout=120, api_key="",
         max_tokens=0):
    url = api_base.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    # 不传 max_tokens 时模型可以一直写到上下文上限, 这是超时的主要来源。
    # 用 OpenAI 标准字段名, Ollama/LM Studio/vLLM/llama.cpp 都会映射到原生参数。
    if max_tokens and int(max_tokens) > 0:
        payload["max_tokens"] = int(max_tokens)
    # 带 thinking 能力的模型默认开着思考, 而翻译时思考只烧预算不出结果:
    # 9B 模型 512 token 的预算会被 reasoning 全吃光, content 是空串, 整批
    # 被判失败。两个后端的关闭开关不一样, 发错了不报错、只是继续空转:
    #   Ollama   —— reasoning_effort="none"（OpenAI 兼容层映射）
    #   llama.cpp —— jinja 模板的 enable_thinking=false（chat_template_kwargs）
    backend = detect_backend(api_base)
    if backend == "ollama":
        payload["reasoning_effort"] = "none"
    elif backend == "llamacpp":
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    data = _post(url, payload, timeout, api_key)
    content, reasoning = _message_text(data)
    if not content.strip() and reasoning:
        # 后端没认上面那个开关（版本偏旧 / 模板不认 enable_thinking）时的兜底:
        # 换成 OpenAI 标准的 reasoning_effort 再发一次, 两套开关总有一套生效。
        retry = dict(payload)
        retry.pop("chat_template_kwargs", None)
        retry["reasoning_effort"] = "none"
        try:
            data = _post(url, retry, timeout, api_key)
            content, reasoning2 = _message_text(data)
            reasoning = reasoning2 or reasoning
        except Exception:
            pass
    out = content.strip()
    if not out:
        # 以前这里返回空串, 上层会把它当成「已翻译」写进文件, 结果是一批
        # 空译文。空正文必须是错误, 由调用方记为失败并重试。
        if reasoning:
            raise MTError(
                "模型只输出了思考过程，正文为空（思考 %d 字）。"
                "后端疑似开着思考模式，请在服务端关闭它（llama.cpp: "
                "--reasoning-budget 0 或 chat_template_kwargs.enable_thinking=false）"
                % len(reasoning))
        raise MTError("模型返回了空正文")
    _check_truncated(data, max_tokens)
    return out


# ---------------------------------------------------------------- 解析
def _strip_fence(s):
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def parse_json_array(text, expect_len):
    """从模型输出里抠出 JSON 数组; 失败返回 None"""
    s = _strip_fence(text)
    try:
        val = json.loads(s)
        if isinstance(val, list) and len(val) == expect_len:
            return [str(x) for x in val]
    except Exception:
        pass
    a, b = s.find("["), s.rfind("]")
    if a >= 0 and b > a:
        try:
            val = json.loads(s[a:b + 1])
            if isinstance(val, list) and len(val) == expect_len:
                return [str(x) for x in val]
        except Exception:
            pass
    return None


def _parse_lines(text, expect_len):
    lines = [l.strip().strip('",') for l in _strip_fence(text).splitlines()]
    lines = [l for l in lines if l]
    if len(lines) == expect_len:
        return lines
    return None


# ---------------------------------------------------------------- 翻译
def translate_one(api_base, model, text, timeout=240, api_key="",
                  target="简体中文", glossary="", max_tokens=0):
    sys_prompt = SINGLE_PROMPT.replace("简体中文", target, 1)
    if glossary:
        sys_prompt += "\n\n" + glossary
    budget = int(max_tokens) if max_tokens else output_budget([text])
    out = chat(api_base, model,
               [{"role": "system", "content": sys_prompt},
                {"role": "user", "content": text}],
               temperature=0.2, timeout=timeout, api_key=api_key,
               max_tokens=budget)
    return normalize_terms(out.strip().strip('"'))


def _split_long_text(text, max_chars=8000):
    """把一条超长文本切成若干段，优先按行/句末切，尽量不切断句子。

    只在输出被截断时才会走到这里：一条 5 万字符的长文档靠一次请求翻完是
    不可能的（16384 token ≈ 88000 字符的上限也会被超时吃掉），切成 8000
    字符左右的块逐块翻、再拼回去才稳。
    """
    text = str(text or "")
    if len(text) <= max_chars:
        return [text]
    chunks, cur = [], ""
    for line in re.split(r"(?<=\n)", text):
        if len(cur) + len(line) <= max_chars:
            cur += line
            continue
        if cur:
            chunks.append(cur)
            cur = ""
        while len(line) > max_chars:
            cut = max(line.rfind("。", 0, max_chars),
                      line.rfind("！", 0, max_chars),
                      line.rfind("？", 0, max_chars),
                      line.rfind(". ", 0, max_chars))
            if cut < max_chars // 3:
                cut = max_chars - 1
            chunks.append(line[:cut + 1])
            line = line[cut + 1:]
        cur = line
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


def translate_batch(api_base, model, texts, timeout=300, api_key="", on_retry=None,
                    target="简体中文", glossary="", max_tokens=0):
    """批量翻译; 模型不听话时退回逐条翻译, 保证返回长度一致

    输出被截断（finish_reason=length）时不再硬顶：多条就拆成两半分别翻，
    单条太长就按段落切开逐段翻再拼回去——否则长文档的后半段会静默丢失。
    """
    if not texts:
        return []
    sys_prompt = SYSTEM_PROMPT.replace("简体中文", target, 1)
    if glossary:
        sys_prompt += "\n\n" + glossary
    payload = json.dumps(texts, ensure_ascii=False)
    budget = int(max_tokens) if max_tokens else output_budget(texts)
    msgs = [{"role": "system", "content": sys_prompt},
            {"role": "user", "content": payload}]
    try:
        raw = chat(api_base, model, msgs,
                   temperature=0.2, timeout=timeout, api_key=api_key,
                   max_tokens=budget)
    except MTError as ex:
        if "finish_reason=length" not in str(ex):
            raise
        if len(texts) > 1:
            # 多条：对半拆，两段各自走完整流程（含自己的逐条兜底）
            mid = len(texts) // 2
            head = translate_batch(api_base, model, texts[:mid], timeout=timeout,
                                   api_key=api_key, target=target,
                                   glossary=glossary, max_tokens=max_tokens)
            tail = translate_batch(api_base, model, texts[mid:], timeout=timeout,
                                   api_key=api_key, target=target,
                                   glossary=glossary, max_tokens=max_tokens)
            return head + tail
        # 单条：按段落切开逐段翻，再拼回去
        parts = _split_long_text(texts[0])
        if len(parts) <= 1:
            raise
        if on_retry:
            on_retry()
        done = [translate_one(api_base, model, p, timeout=timeout,
                              api_key=api_key, target=target,
                              glossary=glossary)
                for p in parts]
        return ["\n\n".join(done)]
    out = parse_json_array(raw, len(texts))
    if out is None:
        out = _parse_lines(raw, len(texts))
    if out is None:
        # 整批解析不出来（多数是输出被截断）时，先原样重试一次再退化成逐条。
        # 逐条重发是 8 次请求，整批重试是 1 次，代价差一个数量级。
        try:
            raw = chat(api_base, model, msgs,
                       temperature=0.2, timeout=timeout, api_key=api_key,
                       max_tokens=max(budget, output_budget(texts) + 256))
            out = parse_json_array(raw, len(texts))
            if out is None:
                out = _parse_lines(raw, len(texts))
        except Exception:
            out = None
    if out is None:
        if on_retry:
            on_retry()
        # 逐条兜底时每条都带自己的预算, 否则一条长文本就能再挂 3 分钟。
        return [translate_one(api_base, model, t, timeout=timeout, api_key=api_key,
                              target=target, glossary=glossary)
                for t in texts]
    # 品牌/系列署名在这里统一：批量这条路径是机翻的主干，单条兜底走
    # translate_one（那边已经规范化过，重复调用是幂等的空操作）。
    return [normalize_terms(x) for x in out]


# ------------------------------------------------------ 本地 llama-server 管理
# 设置页「启用当前设置模型」用：按 api_base + model 让本机 llama-server
# 正跑着那份权重。已经在跑别的权重（Q8 ↔ Q4）时先结束再换，不要求用户
# 自己去命令行敲脚本。
DEFAULT_LLAMA_EXE = r"D:\llama\llama-server.exe"
DEFAULT_MODEL_DIR = r"D:\Models\Qwen3.5-9B"
DEFAULT_CONTEXT = 262144


def loaded_model(api_base, timeout=4, api_key=""):
    """服务当前加载的权重 id（llama.cpp 返回的是 .gguf 全路径）。"""
    try:
        ids = list_models(api_base, timeout=timeout, api_key=api_key)
    except Exception:
        return ""
    return ids[0] if ids else ""


def pick_weights(model, model_dir=""):
    """按设置里的模型名挑一份 GGUF；返回 (路径, 标签) 或 (None, 错误说明)。

    模型名是我们自己填的标签（qwen3.5-9b / qwen3.5-9b-q4），llama-server
    其实不校验它，所以这里只拿它当「想要哪档量化」的开关：名字里有 q4 就
    选 Q4_K_M，有 q8 就选 Q8_0，都没有就取目录里体积最大的那份。
    """
    import os
    d = (model_dir or DEFAULT_MODEL_DIR).strip()
    if not os.path.isdir(d):
        return None, "模型目录不存在：%s" % d
    ggufs = [f for f in os.listdir(d)
             if f.lower().endswith(".gguf") and "mmproj" not in f.lower()]
    if not ggufs:
        return None, "模型目录里没有 .gguf：%s" % d
    low = (model or "").lower()
    want = None
    if "q4" in low:
        want = "Q4_K_M"
    elif "q8" in low:
        want = "Q8_0"
    if want:
        hits = [f for f in ggufs if want.lower() in f.lower()]
        if hits:
            return os.path.join(d, hits[0]), want
    # 名字里没写量化（或没对上）：取体积最大的，通常是 Q8
    ggufs.sort(key=lambda f: os.path.getsize(os.path.join(d, f)), reverse=True)
    return os.path.join(d, ggufs[0]), os.path.splitext(ggufs[0])[0]


def find_mmproj(model_dir=""):
    """同目录下的视觉投影文件（没有就返回空串，纯文本也能跑）。"""
    import os
    d = (model_dir or DEFAULT_MODEL_DIR).strip()
    try:
        for f in os.listdir(d):
            if f.lower().endswith(".gguf") and f.lower().startswith("mmproj"):
                return os.path.join(d, f)
    except Exception:
        pass
    return ""


def start_llamacpp(exe="", weights="", mmproj="", host="127.0.0.1", port=8080,
                   ctx=DEFAULT_CONTEXT, extra_args=None):
    """以脱离父进程的方式启动 llama-server，返回 (ok, 说明)。

    必须 detached：放在 DSH/终端作业对象里的子进程会随宿主退出被回收，
    这里用 DETACHED_PROCESS 让它挂到独立进程组，翻译器关掉也不影响它。
    """
    import os
    exe = (exe or DEFAULT_LLAMA_EXE).strip()
    if not os.path.isfile(exe):
        return False, "找不到 llama-server：%s" % exe
    if not weights or not os.path.isfile(weights):
        return False, "找不到权重文件：%s" % weights
    args = [exe, "-m", weights,
            "-c", str(int(ctx)), "-ngl", "99", "-fa", "on",
            "-ctk", "q8_0", "-ctv", "q8_0",
            "--host", host, "--port", str(int(port)), "-np", "1",
            "--jinja", "--no-webui"]
    if mmproj and os.path.isfile(mmproj):
        args += ["--mmproj", mmproj, "--image-min-tokens", "1024"]
    if extra_args:
        args += list(extra_args)
    flags = 0
    if os.name == "nt":
        flags = (subprocess.DETACHED_PROCESS |
                 subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        subprocess.Popen(args, cwd=os.path.dirname(exe), creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True)
    except Exception as ex:
        return False, "启动失败：%s" % ex
    return True, "已启动 llama-server（%s）" % os.path.basename(weights)


def wait_ready(api_base, want="", timeout=240, interval=1.0, on_progress=None):
    """等到服务就绪且加载的正是 want 那份权重；返回 (ok, 当前/最后错误)。"""
    import os
    import time
    want_norm = os.path.normcase(os.path.normpath(want)) if want else ""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            ids = list_models(api_base, timeout=3)
            if ids:
                cur = ids[0]
                if not want_norm:
                    return True, cur
                if os.path.normcase(os.path.normpath(cur)) == want_norm:
                    return True, cur
                last = "服务在跑，但加载的是 %s" % os.path.basename(cur)
        except Exception as ex:
            last = str(ex)
        if on_progress:
            try:
                on_progress("等待模型加载…")
            except Exception:
                pass
        time.sleep(interval)
    return False, last or "等待超时"


def ensure_model(api_base, model="", model_dir="", llama_exe="",
                 ctx=DEFAULT_CONTEXT, on_progress=None, timeout=300):
    """确保 api_base 上跑着设置里指定的那份权重。

    - 已经在跑目标权重：直接返回，不动它
    - 在跑别的权重：结束 llama-server，再用目标 GGUF 重启并等就绪
    - 没在跑：直接启动
    只处理本机 llama.cpp；Ollama / 远端服务只提示不自作主张。
    """
    import os
    import time
    say = on_progress or (lambda _m: None)

    host, port = _resolve_host_port(api_base)
    if host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0") or not port:
        return False, "只支持本机地址（例如 http://127.0.0.1:8080/v1）"

    target, label = pick_weights(model, model_dir)
    if not target:
        return False, label

    current = loaded_model(api_base)
    if current:
        try:
            be = detect_backend(api_base)
        except Exception:
            be = ""
        if be == "ollama":
            return False, "检测到 Ollama（%s），本功能只切换 llama.cpp" % os.path.basename(current)
        if os.path.normcase(os.path.normpath(current)) == os.path.normcase(os.path.normpath(target)):
            return True, "已在运行：%s" % os.path.basename(current)
        say("当前是 %s，切换中…" % os.path.basename(current))

    # 先停掉本机 llama-server（不动别的进程）
    if llamacpp_pids(api_base):
        ok, msg = kill_llamacpp(api_base)
        if not ok:
            return False, msg
        say(msg)
        deadline = time.time() + 20
        while time.time() < deadline:
            if not _listening_pids(port):
                break
            time.sleep(0.3)

    ok, msg = start_llamacpp(llama_exe or DEFAULT_LLAMA_EXE, target,
                             find_mmproj(model_dir), host, port, ctx)
    if not ok:
        return False, msg
    say("已启动，等待 %s 加载完成…" % os.path.basename(target))

    ok, info = wait_ready(api_base, want=target, timeout=timeout,
                          on_progress=say)
    if ok:
        return True, "已启用：%s" % os.path.basename(info)
    return False, "加载失败或超时：%s" % info
