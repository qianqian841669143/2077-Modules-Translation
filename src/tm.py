# -*- coding: utf-8 -*-
"""
tm.py —— 翻译记忆(Translation Memory)

同一句原文只翻一次: 机翻前先查记忆, 命中就直接复用;
机翻结果自动写回记忆, 下次(甚至别的模组)直接命中。

存储: <程序目录>/userdata/memory.json（可用 MT_DATA_DIR 改到别处）
"""
import hashlib
import json
import os
import threading
import time

import appconfig


def _app_dir():
    return appconfig.data_dir()


# 按文件路径共享的保存锁。锁挂在实例上是不够的：批量在 S4 里会同时有两个
# 会话在跑（一个加载下一个模组、一个在机翻当前模组），各自构造自己的
# Memory，于是有两把互不相干的锁，两边同时写同一份 memory.json 就成了
# 坏档。这里按规范化后的绝对路径取同一把锁。
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _path_lock(path):
    key = os.path.normcase(os.path.abspath(path))
    with _LOCKS_GUARD:
        lk = _LOCKS.get(key)
        if lk is None:
            lk = _LOCKS[key] = threading.Lock()
        return lk


# 来源优先级：手改 > 官方 > 官方待核 > 机翻。数字越大越优先。
# 与 baseline.ORIGIN_RANK 保持一致；这里自带一份，避免 tm 反向依赖 baseline。
# official-weak 必须在这张表里：漏掉的话它的 rank 会算成 0，既覆盖不了机翻
# 记忆，又会被后来的机翻结果顶掉——官方命中反而比机翻还不受保护。
_ORIGIN_RANK = {"manual": 4, "official": 3, "official-weak": 2,
                "memory": 2, "machine": 1, "": 0}


def _rank(origin):
    return _ORIGIN_RANK.get(origin or "", 0)


def _poisoned(src, text):
    """记忆里这条译文是不是「模型跑飞」的产物：同一段连写几十遍 / 占位符爆炸。

    实测两条：`GR-MsgPromoted` 存成「你已晋升为 %s 级 %d 级 %q 级 …」×42、
    `g:ghxst_boss_warn_02` 存成「…%d 个感动，%d 个震撼」×33。这类译文一旦被
    记忆反复喂回表格，每次应用都写一串重复数字进模组，还会 `format` 崩。
    取出来之前先认一下，命中就当没有这条（并顺手从记忆里删掉）。
    """
    try:
        import mod_text_engine as E
    except Exception:       # noqa: BLE001  导入失败就别拦
        return False
    try:
        if E.repeat_run_guard(src, text)[0]:
            return True
        return bool(E.placeholder_explosion(src, text)[0])
    except Exception:       # noqa: BLE001
        return False


class Memory(object):
    def __init__(self, path=None, limit=200000):
        self.path = path or os.path.join(_app_dir(), "memory.json")
        self.limit = limit
        self.entries = {}
        self._dirty = False
        self._last_save = 0.0
        # 读档/存档失败要给界面一个能说清话的地方，别再静默当好事发生过。
        self.load_error = ""
        self.save_error = ""
        self.load()

    # ------------------------------------------------------------ 磁盘
    def load(self):
        """读盘。坏档不再静默清空——改名留证，并把原因记在 load_error 里。

        以前这里是 `except Exception: self.entries = {}`：一旦 save() 把半截
        文件 os.replace 成了正式档，下次启动就把整份记忆悄悄丢掉，用户只会
        发现「怎么全都要重翻」，磁盘上连个痕迹都没有（2026-11-05 丢过一份
        5.4 MB / 十几万条的记忆）。
        """
        self.load_error = ""
        if not os.path.exists(self.path):
            return self
        try:
            with open(self.path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if not (isinstance(data, dict) and isinstance(data.get("entries"), dict)):
                raise ValueError("memory.json 结构不对（缺 entries）")
            self.entries = data["entries"]
        except Exception as ex:     # noqa: BLE001  坏档不能挡住程序启动
            self.entries = {}
            self.load_error = str(ex) or ex.__class__.__name__
            self._quarantine()
        return self

    def _quarantine(self):
        """把读不动的记忆档改名留证，别让下一次 save() 直接覆盖掉现场。"""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dst = "%s.bad-%s" % (self.path, stamp)
        try:
            os.replace(self.path, dst)
            self.load_error += "（原文件已备份为 %s）" % os.path.basename(dst)
        except OSError:
            pass

    def save(self, force=False):
        if not self._dirty and not force:
            return
        # json 改走纯 Python 编码器之后(appconfig 里的补丁), 17 万条要将近 1 秒,
        # 而 _run_translate 每补翻一条就 save 一次 —— 必须节流, 否则时间全花在
        # 反复序列化同一份字典上。force=True(显式保存/退出)不受限制。
        now = time.time()
        if not force and now - self._last_save < 3.0:
            return
        self._last_save = now
        # 先在内存里序列化出完整一份，再一次写完，最后核对长度才 os.replace。
        # 边生成边写的时候进程一旦死在中间（2026-11-05 连崩 4 次，每次都留下
        # 半截 memory.json.tmp：1.97 MB / 完整档 3.17 MB），磁盘上就只剩半截
        # 档。整份写 + 核对 + 原子替换：落盘要么完整覆盖，要么什么都不动。
        try:
            try:
                payload = json.dumps({"version": 1, "entries": self.entries},
                                     ensure_ascii=False)
            except RuntimeError:
                # 边序列化边被别的线程改了字典：拷一份快照再序列化。
                payload = json.dumps({"version": 1, "entries": dict(self.entries)},
                                     ensure_ascii=False)
            blob = payload.encode("utf-8")
            with _path_lock(self.path):
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                # 每个写入者用自己的临时名：万一真并发了，也是各写各的完整
                # 文件，不会把两段 json 交错灌进同一个 .tmp。
                tmp = "%s.%d.%d.tmp" % (self.path, os.getpid(),
                                        threading.get_ident())
                try:
                    with open(tmp, "wb") as f:
                        f.write(blob)
                        f.flush()
                        os.fsync(f.fileno())
                    if os.path.getsize(tmp) != len(blob):
                        raise IOError("临时文件长度不对（%d/%d）"
                                      % (os.path.getsize(tmp), len(blob)))
                    os.replace(tmp, self.path)
                except Exception:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    raise
            self._dirty = False
            self.save_error = ""
        except Exception as ex:     # noqa: BLE001  存不下也不能把应用带走
            self.save_error = str(ex)

    # ------------------------------------------------------------ 查询
    @staticmethod
    def _key(src, tgt):
        raw = (tgt or "") + "\x00" + (src or "")
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def get(self, src, tgt="简体中文"):
        if not src:
            return None
        k = self._key(src, tgt)
        e = self.entries.get(k)
        if e and e.get("t"):
            if _poisoned(src, e["t"]):
                # 坏条目留在记忆里只会反复喂回表格：直接扔掉（下次机翻重来）。
                self.entries.pop(k, None)
                self._dirty = True
                return None
            e["n"] = int(e.get("n", 0)) + 1
            e["ts"] = int(time.time())
            self._dirty = True
            return e["t"]
        return None

    def get_entry(self, src, tgt="简体中文"):
        """返回 {"text": 译文, "origin": 来源}，没命中返回 None。

        与 get() 的区别：只读、不动计数——批量填充时每行都会问一次，
        没必要为此反复改内存和标脏。
        """
        if not src:
            return None
        k = self._key(src, tgt)
        e = self.entries.get(k)
        if not e or not e.get("t"):
            return None
        if _poisoned(src, e["t"]):
            self.entries.pop(k, None)
            self._dirty = True
            return None
        return {"text": e["t"], "origin": e.get("o") or ""}

    def put(self, src, tgt, text, origin="machine"):
        """写入记忆。

        origin 用来实现「手改 > 官方 > 机翻」：已经存着更高优先级的译文时，
        低优先级的写入不会把它顶掉，只刷新命中次数。否则机翻一遍就会把
        用户手工改过的译文冲掉，这是最难发现的一种数据丢失。
        """
        if not src or not text or src.strip() == text.strip():
            return
        k = self._key(src, tgt)
        old = self.entries.get(k)
        if old and _rank(old.get("o")) > _rank(origin):
            old["n"] = int(old.get("n", 0)) + 1
            old["ts"] = int(time.time())
            self._dirty = True
            return
        self.entries[k] = {
            "s": src, "t": text, "g": tgt, "o": origin,
            "n": int(old.get("n", 0)) + 1 if old else 0,
            "ts": int(time.time()),
        }
        self._dirty = True
        if len(self.entries) > self.limit:
            self._trim()

    def forget(self, src, tgt="简体中文", origins=("machine", "memory", "")):
        """删掉一条记忆，返回是否真的删了。

        用途是「用户否定了这条译文」：清空某行后重翻，如果记忆里还留着上次
        的结果，_baseline_fill 会立刻把同一句原样填回来，看起来就像没翻。

        只删机翻/记忆来源的条目。手改和官方语料是更可靠的资产，不能因为
        用户清空了一次显示就跟着丢——那两种来源本来也不该出现在这里。
        """
        if not src:
            return False
        k = self._key(src, tgt)
        e = self.entries.get(k)
        if not e:
            return False
        if (e.get("o") or "") not in origins:
            return False
        del self.entries[k]
        self._dirty = True
        return True

    def _trim(self):
        items = sorted(self.entries.items(),
                       key=lambda kv: (kv[1].get("ts", 0), kv[1].get("n", 0)),
                       reverse=True)
        self.entries = dict(items[:int(self.limit * 0.8)])

    # ------------------------------------------------------------ 维护
    def count(self):
        return len(self.entries)

    def origin_counts(self):
        """按来源分类统计，给设置页显示「记忆里有多少是手改/官方/机翻」。"""
        out = {}
        for e in self.entries.values():
            k = e.get("o") or "machine"
            out[k] = out.get(k, 0) + 1
        out["total"] = len(self.entries)
        return out

    def mark_manual(self, src, tgt="简体中文", text=None):
        """把一条记忆标成「用户手改」。

        手改优先于官方与机翻，所以只提升不降低：已经更高级别的不动。
        """
        if not src:
            return False
        k = self._key(src, tgt)
        e = self.entries.get(k)
        if not e:
            return False
        if text:
            e["t"] = text
        if _rank(e.get("o")) <= _rank("manual"):
            e["o"] = "manual"
            e["ts"] = int(time.time())
            self._dirty = True
            return True
        return False

    def clear(self):
        self.entries = {}
        self._dirty = True
        self.save(force=True)

    def file_size(self):
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0
