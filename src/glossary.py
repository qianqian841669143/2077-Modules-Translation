# -*- coding: utf-8 -*-
"""
glossary.py —— 术语表

把"原文术语 → 译文术语"喂给模型, 保证同一个人名/名词在整份翻译里一致。
存储: <程序目录>/userdata/glossary.json
"""
import json
import os
import re

# 赛博朋克 2077 常用术语(可删可改)
DEFAULT_TERMS = [
    ("Quickhack", "快速破解", "游戏内快捷破解"),
    ("Breach Protocol", "破解协议", ""),
    ("Cyberware", "义体", ""),
    ("Ripperdoc", "义体医生", ""),
    ("Ripperware", "义体插件", ""),
    ("Netrunner", "网络黑客", ""),
    ("Netwatch", "网络监察", ""),
    ("Braindance", "超梦", ""),
    ("Cyberpsycho", "赛博精神病", ""),
    ("Cyberdeck", "赛博组件", ""),
    ("Sandevistan", "桑德万斯坦", ""),
    ("Blackwall", "黑墙", ""),
    ("Perk", "专长", ""),
    ("Attribute", "属性", ""),
    ("Eddies", "欧币", ""),
    ("Fixer", "中间人", ""),
    ("Night City", "夜之城", ""),
    ("Badlands", "恶土", ""),
    ("NCPD", "夜之城警局", ""),
    ("Trauma Team", "创伤小组", ""),
    ("Militech", "军用科技", ""),
    ("Arasaka", "荒坂", ""),
    ("Kang Tao", "康陶", ""),
    ("Afterlife", "来生", ""),
    ("Watson", "沃森", ""),
    ("Japantown", "日本街", ""),
    ("Mod Settings", "模组设置", ""),
    ("MCM", "模组配置菜单", ""),
]


def _app_dir():
    import appconfig
    return appconfig.data_dir()


class Glossary(object):
    def __init__(self, path=None):
        self.path = path or os.path.join(_app_dir(), "glossary.json")
        self.terms = []
        self.load()

    # ------------------------------------------------------------ 磁盘
    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            items = data.get("terms") if isinstance(data, dict) else data
            out = []
            for it in (items or []):
                if isinstance(it, dict) and it.get("src"):
                    out.append({"src": str(it["src"]), "dst": str(it.get("dst", "")),
                                "note": str(it.get("note", ""))})
            self.terms = out
        except Exception:
            self.terms = []
        if not self.terms:
            self.reset_defaults(save=False)
        return self

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "terms": self.terms}, f,
                          ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------ 维护
    def reset_defaults(self, save=True):
        self.terms = [{"src": s, "dst": d, "note": n} for s, d, n in DEFAULT_TERMS]
        if save:
            self.save()

    def add(self, src, dst, note=""):
        self.terms.append({"src": src.strip(), "dst": dst.strip(), "note": note.strip()})

    def update(self, idx, src, dst, note=""):
        if 0 <= idx < len(self.terms):
            self.terms[idx] = {"src": src.strip(), "dst": dst.strip(),
                               "note": note.strip()}

    def remove(self, idx):
        if 0 <= idx < len(self.terms):
            del self.terms[idx]

    # ------------------------------------------------------------ 用于提示词
    def relevant(self, texts, limit=40):
        """挑出在当前这批原文里出现过的术语, 避免提示词过长"""
        blob = "\n".join(texts).lower()
        hit = []
        for t in self.terms:
            src = (t.get("src") or "").strip()
            if not src or not (t.get("dst") or "").strip():
                continue
            if src.lower() in blob:
                hit.append(t)
            if len(hit) >= limit:
                break
        return hit

    @staticmethod
    def prompt_block(terms):
        if not terms:
            return ""
        lines = ["术语对照(必须遵守, 保持与原文一致):"]
        for t in terms:
            lines.append("- %s = %s" % (t["src"], t["dst"]))
        return "\n".join(lines)

    # ------------------------------------------------------------ 导入导出
    def export_txt(self, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 每行一条: 原文=译文   (# 开头为注释)\n")
            for t in self.terms:
                f.write("%s=%s\n" % (t["src"], t["dst"]))

    def import_txt(self, path, merge=True):
        if not merge:
            self.terms = []
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                src, dst = line.split("=", 1)
                src, dst = src.strip(), dst.strip()
                if src and dst:
                    self.add(src, dst)
        return True
