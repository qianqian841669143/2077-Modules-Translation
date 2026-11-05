# -*- coding: utf-8 -*-
"""appconfig.py —— 两个前端(Tkinter / pywebview)共用的配置与路径工具"""
import json
import os
import re
import sys

# ---------------------------------------------------------------- json 补丁
# 三次 0xc0000005(2026-11-01) 全落在 CPython _json 的 C 编码器里:
#   * json/encoder.py:256  c_make_encoder(...) 的参数行
#   * json/encoder.py:389  yield _encoder(key) -> c_encode_basestring
# 访问违例在 Python 层接不住(try/except 无效, 进程直接消失), 只能绕开 C 实现,
# 改用 json/encoder.py 自带的纯 Python 版本。代价是序列化慢一点, TM 落盘另有
# 节流(tm.Memory.save), 实际感觉不到。
try:
    import json.encoder as _jenc

    _jenc.c_make_encoder = None
    _jenc.encode_basestring = _jenc.py_encode_basestring
    _jenc.encode_basestring_ascii = _jenc.py_encode_basestring_ascii
except Exception:                      # 老版本 Python 没有这些名字, 忽略即可
    pass

VERSION = "v2.52.1"
APP_DIR_NAME = "2077ModTranslator"

TARGET_LANGS = ["简体中文", "繁體中文", "English", "日本語", "한국어",
                "Русский", "Español", "Deutsch", "Français"]

DEFAULT_SETTINGS = {
    "api_base": "http://127.0.0.1:11434/v1",   # Ollama 默认
    "model": "",                               # 留空 = 自动取 /models 第一个
    "dark_mode": False,                        # 深色界面
    "lang": "",                                # 界面语言, 空 = 跟随系统
    "target_lang": "简体中文",                  # 翻译目标语言
    "use_tm": True,                            # 加载时套用翻译记忆
    # ---- 资源模式（.archive 里的 CR2W 本地化）----
    "game_dir": "",                            # 游戏根目录
    "wkit_path": "",                           # WolvenKit.CLI.exe 完整路径
    # ---- Vortex 活动映射（下载包 → 安装缓存 → 游戏部署）----
    "staging_dir": "",                         # 安装缓存目录（工具实际改写的那一层）
    "downloads_dir": "",                       # 下载包目录（只读，作版本锚）
    # ---- 批量翻译 ----
    # 产物根目录。留空 = 不进行批量翻译（用户明确要求的出口）：
    # 目录写错就会 rmtree 到模组本体，宁可不跑也不要猜一个位置。
    "batch_out_dir": "",
    # 跑完把副本按文件覆盖回原模组目录。默认关：这是会改 standings 里原件的
    # 操作，必须由用户显式打开。判据不满足的情况照覆盖，只在清单里备注。
    "batch_deploy": False,
    # 只算不写：先出一份「会覆盖哪些文件」的清单，确认后再关掉它。
    "batch_deploy_dry": True,
    # 批量面板把「无需翻译」（待译行数 0）的目录折起来：255 个 staging 目录里
    # 只有 127 个有待译行，一屏里近一半是噪音。
    "batch_hide_todo": True,
    "verify_active": True,                     # 加载时提示该目录是否在部署清单里
    "res_source_lang": "en-us",                # 原始语言
    "res_skip_target": True,                   # 目标语言已存在则跳过
    "use_official_tm": True,                   # 套用官方双语语料（基线 TM）
    "official_weak": True,                     # 单词条的官方命中也算（标「待核」）
    "local_model_enabled": True,               # 启用本地模型（停用时立刻卸显存）
    # 关窗时问一句「要停掉本地模型吗」：Ollama 默认 keep_alive，关掉翻译器它
    # 也不会自己退出（9B 常占 6~8 GB、27B 常驻 21 GB）。嫌烦可以关掉这个勾。
    "ask_unload_model": True,
}

# 显示用的路径前缀裁剪 (Excel 里仍保存完整相对路径)
STRIP_PREFIXES = (
    "bin/x64/plugins/cyber_engine_tweaks/mods/",
    "bin/x64/plugins/cyber_engine_tweaks/",
    "bin/x64/plugins/",
    "r6/scripts/",
    "r6/input/",
    "r6/",
    "archive/pc/mod/",
    "red4ext/plugins/",
)


def app_root():
    """程序所在目录：打包后是 exe 所在目录，源码运行时是仓库根。

    源码运行时若旁边有打包好的 dist\\2077ModTranslator.exe，就跟它共用一份
    数据目录——否则自测与实际使用读写的是两套记忆，排查问题时很容易看错。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dist = os.path.join(root, "dist")
    if os.path.isfile(os.path.join(dist, "2077ModTranslator.exe")):
        return dist
    return root


def data_dir():
    """用户数据根目录：设置 / 记忆 / 语料 / 解包中间产物都在这儿。

    默认跟着程序走（便携，整个目录可以拷走），可用环境变量 MT_DATA_DIR 指到别处。
    以前固定写 %APPDATA%\\2077ModTranslator：官方语料 + 解包产物能把 C 盘吃掉 700 MB。
    """
    env = (os.environ.get("MT_DATA_DIR") or "").strip()
    if env:
        return os.path.abspath(env)
    return os.path.join(app_root(), "userdata")


def legacy_config_dir():
    """旧位置，只用于迁移与提示。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_DIR_NAME)


def config_dir():
    return data_dir()


def config_file():
    return os.path.join(config_dir(), "settings.json")


def load_settings():
    """读取设置; 不存在或损坏时返回默认值"""
    cfg = dict(DEFAULT_SETTINGS)
    raw = None
    try:
        with open(config_file(), "r", encoding="utf-8-sig") as f:   # 容忍 BOM
            raw = json.load(f)
        if isinstance(raw, dict):
            for k, default in DEFAULT_SETTINGS.items():
                if isinstance(raw.get(k), type(default)):
                    cfg[k] = raw[k]
    except Exception:
        raw = None
    # v2.43.0 把批量输出目录改成可配置项。老配置里没有这个键，补上旧路径
    # （userdata\batch\out），升级后第一次点「开始」不会被空白挡住；用户主动
    # 清空之后才是真的「空 = 不跑」，这里不再自动补回去。
    if not isinstance(raw, dict) or "batch_out_dir" not in raw:
        cfg["batch_out_dir"] = os.path.join(config_dir(), "batch", "out")
    return cfg


def legacy_batch_out_dir():
    """v2.43.0 之前的固定批量输出目录。迁移与「盖章」都要认它。"""
    return os.path.join(config_dir(), "batch", "out")


def save_settings(cfg):
    d = config_dir()
    os.makedirs(d, exist_ok=True)
    path = config_file()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def normalize_api_base(url):
    """补 http:// 前缀、去掉末尾斜杠"""
    u = (url or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "http://" + u
    return u.rstrip("/")


def shorten_path(p):
    for pre in STRIP_PREFIXES:
        if p.startswith(pre):
            return p[len(pre):]
    return p


def disp_path(p, root=""):
    """文件列的显示路径：从模组目录下一级开始，什么前缀都不吃。

    以前用 shorten_path 砍静态前缀（archive/pc/mod/、r6/scripts/ …），砍完
    只剩文件名附近的一截，同一个 mod 里两个语言文字包看起来一模一样，分不清
    谁是谁。现在原样给出相对模组根的完整地址，归档内路径（资源层）也是它自己
    的相对地址，本来就没什么可砍的。

    绝对路径（老数据、手工填的）落在 root 下时转成相对；其余一律原样返回。
    """
    p = str(p or "")
    if not p:
        return p
    if root and os.path.isabs(p):
        try:
            rel = os.path.relpath(p, root)
        except Exception:
            return p
        if rel and not rel.startswith(".."):
            return rel.replace("\\", "/")
    return p
