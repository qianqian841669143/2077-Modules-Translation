# -*- coding: utf-8 -*-
"""
mt_client.py —— 本地部署模型(OpenAI 兼容接口)的最小客户端 + 批量翻译

只依赖标准库, 因此打包体积不变。
支持: Ollama /v1、LM Studio、vLLM、llama.cpp server、one-api 等兼容实现。
"""
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

SYSTEM_PROMPT = (
    "你是《赛博朋克 2077》模组的专业本地化译者。"
    "用户会给你一个 JSON 字符串数组, 请把每个元素翻译成简体中文。\n"
    "规则:\n"
    "1. 只输出 JSON 字符串数组, 不要解释、不要 Markdown 代码块、不要多余文字。\n"
    "2. 输出数组的长度和顺序必须与输入完全一致。\n"
    "3. 占位符必须原样保留: %s %d %q {name} <Rich ...> </> \\n 等。\n"
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
    "占位符(%s %d %q {name} <Rich ...> </>)与换行必须原样保留。"
    "术语沿用游戏官方译法(Quickhack=快速破解, Cyberware=义体, Perk=专长, "
    "Ripperdoc=义体医生, Netrunner=网络黑客, Eddies=欧币)。"
)


class MTError(Exception):
    """机翻过程中的可读错误"""


# 输出长度上限。模型不给上限时会一直往下写：2026-10-31 的日志里, 一批
# 只有 8 条短界面文案的请求生成过 3254 个 token(单条 49.9 秒), 而正常
# 描述同一批内容的响应只要两三百个 token。三条 180 秒超时里两条是这么来的。
_MAX_OUT_TOKENS = 8192


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
_ollama_cache = {}


def _is_ollama(api_base):
    """后端是不是 Ollama —— 只有它认 reasoning_effort 这个开关。

    先看端口(默认 11434), 端口对不上再探一次 /api/version, 结果按地址缓存。
    探不到就当不是, 避免给 LM Studio / vLLM / OpenAI 发它们不认识的字段。
    """
    key = (api_base or "").rstrip("/")
    if key in _ollama_cache:
        return _ollama_cache[key]
    ok = False
    try:
        if urlsplit(key).port == _OLLAMA_PORT:
            ok = True
    except Exception:
        ok = False
    if not ok:
        try:
            with urllib.request.urlopen(_native_base(key) + "/api/version",
                                        timeout=2) as r:
                ok = "version" in json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            ok = False
    _ollama_cache[key] = ok
    return ok


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


def resolve_model(api_base, model="", timeout=10, api_key=""):
    """模型名为空时自动取服务上的第一个模型"""
    if model:
        return model
    ids = list_models(api_base, timeout=timeout, api_key=api_key)
    if not ids:
        raise MTError("服务没有返回任何模型, 请在设置里手动填写模型名称")
    return ids[0]


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
    # 用 OpenAI 标准字段名, Ollama/LM Studio/vLLM 都会映射到各自的原生参数。
    if max_tokens and int(max_tokens) > 0:
        payload["max_tokens"] = int(max_tokens)
    # Ollama 对带 thinking 能力的模型默认开着思考。翻译时思考只烧预算不
    # 出结果: 9B 模型 512 token 的预算会被 reasoning 全部吃掉, content
    # 是空串, 整批被判失败; 27B 也要多花两三百 token。OpenAI 兼容层的
    # reasoning_effort="none" 能关掉它(实测 content 直接出译文)。
    if _is_ollama(api_base):
        payload["reasoning_effort"] = "none"
    data = _post(url, payload, timeout, api_key)
    try:
        msg = data["choices"][0]["message"]
        content = msg.get("content")
    except Exception:
        raise MTError("返回格式不符合 OpenAI 规范: %s" % str(data)[:200])
    if isinstance(content, list):      # 少数实现返回分段内容
        content = "".join(seg.get("text", "") for seg in content
                          if isinstance(seg, dict))
    return (content or "").strip()


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
def translate_one(api_base, model, text, timeout=120, api_key="",
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
    return out.strip().strip('"')


def translate_batch(api_base, model, texts, timeout=180, api_key="", on_retry=None,
                    target="简体中文", glossary="", max_tokens=0):
    """批量翻译; 模型不听话时退回逐条翻译, 保证返回长度一致"""
    if not texts:
        return []
    sys_prompt = SYSTEM_PROMPT.replace("简体中文", target, 1)
    if glossary:
        sys_prompt += "\n\n" + glossary
    payload = json.dumps(texts, ensure_ascii=False)
    budget = int(max_tokens) if max_tokens else output_budget(texts)
    msgs = [{"role": "system", "content": sys_prompt},
            {"role": "user", "content": payload}]
    raw = chat(api_base, model, msgs,
               temperature=0.2, timeout=timeout, api_key=api_key,
               max_tokens=budget)
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
    return out
