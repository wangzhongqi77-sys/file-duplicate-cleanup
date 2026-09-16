#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fclean.py — 文件清理工具箱（默认只扫不动；--apply 才真正删除）

三个模式：
  scan-empty    空文件（0 字节）+ 空文件夹
  scan-dupes    重复文件（fclones 式流水线：大小分组 -> inode 去重 -> 前缀哈希 -> 全量哈希）
  scan-dirdupes 目录级重复（整目录指纹 = 重复文件夹；+ 目录对聚合）
  scan-similar  文本相似度（先剥格式外壳再比对，能查出版本冗余）

一个动作：
  clean         按报告安全删除（项目内 move 到 __回收站__；散落文件进系统回收站）

设计参考（2026-09-16）：Czkawka / rmlint / dupeGuru(FOLDERS) / fclones（前缀哈希 / dry-run / 分组-删除分离）

零第三方依赖；回收站优先用 send2trash（若已装），否则用 ctypes 调 SHFileOperationW 兜底。
建议解释器：C:\\Users\\Administrator\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe
"""
import argparse
import csv
import fnmatch
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime

VERSION = "1.3.0"

# ---------------------------------------------------------------- 扩展名分类
IMG = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.heic', '.heif',
       '.tif', '.tiff', '.svg', '.ico', '.raw', '.cr2', '.nef', '.arw', '.avif'}
VID = {'.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.webm', '.m4v',
       '.mpg', '.mpeg', '.3gp', '.rmvb', '.ts', '.vob', '.mts', '.m2ts'}
DOC = {'.pdf', '.doc', '.docx', '.docm', '.dot', '.dotx', '.wps',
       '.xls', '.xlsx', '.xlsm', '.xlt', '.xltx', '.csv', '.et',
       '.ppt', '.pptx', '.pps', '.ppsx', '.pot', '.potx', '.dps',
       '.odt', '.ods', '.odp', '.rtf', '.txt', '.md'}
KIND = {}
for _e in IMG:
    KIND[_e] = '图片'
for _e in VID:
    KIND[_e] = '视频'
for _e in DOC:
    KIND[_e] = '文档'
ALL_EXT = IMG | VID | DOC

TEXT_EXT = {'.txt', '.md', '.srt', '.lrc', '.csv', '.json', '.py', '.js', '.html', '.xml'}

# Windows 兼容 junction / 噪声目录：遍历时跳过，避免自我递归与无意义开销
DEFAULT_SKIP_NAMES = {
    '$recycle.bin', 'system volume information', 'winsxs',
    'node_modules', '.git', '__pycache__', 'venv', '.venv', 'site-packages',
    '.next', '.nuxt', '.cache', '.parcel-cache', '.turbo', '.svelte-kit',
    '.gradle', '.kotlin', '.astro', '.contentlayer',
    'cache', 'cachestorage', 'code cache', 'gpu cache', 'shader cache',
    'crashpad', 'service worker', 'blob_storage', 'session storage',
    'appdata', 'application data', 'local settings', 'my documents',
    'recent', 'cookies', 'nethood', 'printhood', 'sendto', 'templates',
    '「开始」菜单', 'my music', 'my pictures', 'my videos',
    '__回收站__',
}

REPARSE_FLAG = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT（symlink / junction / mount point）


def is_reparse(st):
    return bool(getattr(st, "st_file_attributes", 0) & REPARSE_FLAG)


def norm_skip_paths(raw):
    """分号分隔的绝对路径 -> 规范化（转 \\\\、去尾分隔符、小写）"""
    out = []
    for p in (raw or "").split(';'):
        p = p.strip()
        if not p:
            continue
        q = os.path.abspath(p.replace('/', os.sep))
        out.append(os.path.normcase(q).rstrip(os.sep))
    return tuple(out)


# ---------------------------------------------------------------- 扫描核心
class FRec:
    __slots__ = ("path", "size", "mtime", "dev", "ino", "ext", "kind")

    def __init__(self, path, st):
        self.path = path
        self.size = st.st_size
        self.mtime = st.st_mtime
        self.dev = getattr(st, 'st_dev', 0)
        self.ino = getattr(st, 'st_ino', 0)
        self.ext = os.path.splitext(path)[1].lower()
        self.kind = KIND.get(self.ext, '其他')

    def as_dict(self):
        return {
            "path": self.path, "size": self.size, "ext": self.ext,
            "kind": self.kind,
            "mtime": datetime.fromtimestamp(self.mtime).strftime('%Y-%m-%d %H:%M'),
        }


def scan_tree(root, skip_names, skip_paths, follow=False, ext_filter=None,
              progress_every=20000, file_filter=None, need_dir_map=True):
    """一次遍历，同时收集：文件、目录、空目录、目录->直接子文件、目录->子目录。

    返回 dict。跳过 junction/符号链接（防自指递归）、跳过 skip 名单。
    file_filter: 可选 callable(FRec)->bool，只保留返回 True 的文件（省内存）。
    need_dir_map: 是否构建「目录->子文件」映射（只有 scan-dirdupes 需要）。
    """
    root = os.path.abspath(root)
    files, dirs, empty_dirs = [], [], []
    dir_files = defaultdict(list)
    dir_subdirs = defaultdict(list)
    st_out = {"scanned": 0, "skipped": 0, "seen": 0}
    stack = [root]
    visited = set()
    while stack:
        d = stack.pop()
        try:
            key = os.path.normcase(os.path.abspath(d))
            if key in visited:
                continue
            visited.add(key)
            entries = list(os.scandir(d))
        except (PermissionError, FileNotFoundError, OSError):
            continue
        dirs.append(d)
        n_children = 0
        for e in entries:
            n_children += 1
            try:
                st = e.stat(follow_symlinks=follow)
            except OSError:
                continue
            if is_reparse(st):
                continue  # 不进入、不记录（但计为子项，父目录不算空）
            is_dir = e.is_dir(follow_symlinks=follow)
            if skip_paths:
                fp = os.path.normcase(os.path.abspath(e.path))
                if any(fp == s or fp.startswith(s + os.sep) for s in skip_paths):
                    st_out["skipped"] += 1
                    continue
            if is_dir:
                if e.name.lower() in skip_names:
                    st_out["skipped"] += 1
                    continue
                dir_subdirs[d].append(e.path)
                stack.append(e.path)
            else:
                st_out["seen"] += 1
                fr = FRec(e.path, st)
                if ext_filter and fr.ext not in ext_filter:
                    continue
                if file_filter is not None and not file_filter(fr):
                    continue
                files.append(fr)
                if need_dir_map:
                    dir_files[d].append((e.name, st.st_size))
                st_out["scanned"] += 1
                if progress_every and st_out["seen"] % progress_every == 0:
                    sys.stderr.write('\r  已扫 %d 个文件...' % st_out["seen"])
                    sys.stderr.flush()
        if n_children == 0:
            empty_dirs.append(d)
    sys.stderr.write('\r')
    return {"root": root, "files": files, "dirs": dirs, "empty_dirs": empty_dirs,
            "dir_files": dir_files, "dir_subdirs": dir_subdirs, "stats": st_out}


# ---------------------------------------------------------------- 哈希
def file_hash(path, limit=None, chunk=1 << 20, algo="md5"):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        remaining = limit
        while True:
            n = chunk if remaining is None else min(chunk, remaining)
            if n <= 0:
                break
            b = f.read(n)
            if not b:
                break
            h.update(b)
            if remaining is not None:
                remaining -= len(b)
    return h.hexdigest()


def file_tail_hash(path, tail=1 << 12, algo="md5"):
    """读文件**末尾** tail 字节算哈希。

    学自 fclones：两个文件「开头前缀」相同不代表内容相同（日志文件追加、
    尾部元数据不同的图片/视频都属此类）。先看一眼尾巴，尾巴不同就直接淘汰，
    省掉整个文件的全量哈希 —— 对 GB 级大文件特别值。
    """
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        f.seek(max(0, pos - tail), os.SEEK_SET)
        while True:
            b = f.read(min(1 << 20, tail))
            if not b:
                break
            h.update(b)
    return h.hexdigest()


class HashCache:
    """扫描哈希的持久化缓存（学自 fclones / Czkawka）。

    以 (路径, 阶段, 算法) 为主键缓存哈希；**命中条件是该文件的 size 与 mtime
    都没变过**，变了就当作失效重新计算。这样第二次扫同一目录基本不用再读文件内容。
    用的是 Python 标准库 sqlite3 —— 不引入第三方依赖。
    """

    def __init__(self, path):
        import sqlite3  # 标准库，按需导入：不用缓存时完全不碰它
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS hash_cache ("
            " path TEXT NOT NULL, stage TEXT NOT NULL, algo TEXT NOT NULL,"
            " size INTEGER NOT NULL, mtime REAL NOT NULL, hash TEXT NOT NULL,"
            " PRIMARY KEY (path, stage, algo))")
        self.hits = 0
        self.misses = 0
        self._pending = []

    def get(self, path, stage, algo, size, mtime):
        row = self.conn.execute(
            "SELECT hash FROM hash_cache WHERE path=? AND stage=? AND algo=?"
            " AND size=? AND mtime=?", (path, stage, algo, size, mtime)).fetchone()
        if row is None:
            self.misses += 1
            return None
        self.hits += 1
        return row[0]

    def put(self, path, stage, algo, size, mtime, digest):
        self._pending.append((path, stage, algo, size, mtime, digest))
        if len(self._pending) >= 500:
            self.flush()

    def flush(self):
        if not self._pending:
            return
        self.conn.executemany(
            "INSERT OR REPLACE INTO hash_cache VALUES (?,?,?,?,?,?)", self._pending)
        self._pending = []
        self.conn.commit()

    def close(self):
        try:
            self.flush()
            self.conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------- 模式二：空文件/空目录
def cmd_scan_empty(args):
    ext_filter = ALL_EXT if not args.any else None
    if args.ext:
        ext_filter = {("." + e.lower().lstrip('.')) for e in args.ext.split(',') if e.strip()}
    skip = set(DEFAULT_SKIP_NAMES) | {x.strip().lower() for x in args.skip_names.split(',') if x.strip()}
    skip_paths = norm_skip_paths(args.skip_paths)
    t0 = time.time()
    data = scan_tree(args.root, skip, skip_paths, ext_filter=ext_filter)
    zero = [fr for fr in data["files"] if fr.size == 0]
    empty_dirs = [] if args.no_dirs else data["empty_dirs"]
    payload = {
        "mode": "empty", "root": data["root"], "generated": datetime.now().isoformat(timespec='seconds'),
        "scanned": data["stats"]["scanned"], "skipped_dirs": data["stats"]["skipped"],
        "seconds": round(time.time() - t0, 1),
        "count": len(zero), "dir_count": len(empty_dirs),
        "files": [fr.as_dict() for fr in zero],
        "empty_dirs": [{"path": d} for d in empty_dirs],
    }
    write_report(args, payload, zero, empty_dirs)
    return payload


# ---------------------------------------------------------------- 模式一：重复文件
# 构建产物的常见目录标记：这些目录里的文件通常是另一侧“源目录”生成出来的镜像
PUB_MARKERS = ('\\public\\', '\\_site\\', '\\dist\\', '\\out\\')
SRC_MARKERS = ('\\source\\', '\\themes\\', '\\static\\', '\\content\\', '\\src\\', '\\assets\\')


def is_build_mirror(paths):
    """判断一组重复文件是否为「源码 -> 构建产物」镜像（如 Hexo/Hugo 的 source/static ↔ public）。

    判据：组内既有落在产物目录、又有落在源目录的文件，且去掉标记后的相对后缀有重合。
    这类重复**不能删**——删了要么产物失效，要么下次构建又生成。
    """
    def suffix(p, markers):
        for m in markers:
            i = p.find(m)
            if i >= 0:
                return p[i + len(m):]
        return None

    pub, src = set(), set()
    for p in (x.lower() for x in paths):
        s = suffix(p, PUB_MARKERS)
        if s is not None:
            pub.add(s)
        t = suffix(p, SRC_MARKERS)
        if t is not None:
            src.add(t)
    return bool(pub & src)


def find_dupes(files, min_size=1, prefix=1 << 16, algo="md5",
               tail=1 << 12, cache=None):
    """fclones 式流水线（含「末尾哈希」剪枝 + 可选哈希缓存）。返回 (group 列表, 跳过的硬链接数)。

    阶段：按大小分组 -> inode 去重 -> 开头前缀哈希 -> **末尾哈希剪枝** -> 全量哈希。
    cache 给定时，每阶段的哈希先查缓存（命中条件：size 与 mtime 都没变），省掉读文件。
    """
    head_stage = "head:%d" % prefix
    tail_stage = "tail:%d" % tail

    def H(fr, stage, fn):
        """带缓存的哈希：size+mtime 未变则直接复用上次结果，不读文件内容。"""
        if cache is not None:
            got = cache.get(fr.path, stage, algo, fr.size, fr.mtime)
            if got is not None:
                return got
        v = fn()
        if cache is not None:
            cache.put(fr.path, stage, algo, fr.size, fr.mtime, v)
        return v

    by_size = defaultdict(list)
    seen_inode = set()
    dup_hardlinks = 0
    for fr in files:
        if fr.size < min_size:
            continue
        if fr.ino:
            key = (fr.dev, fr.ino)
            if key in seen_inode:
                dup_hardlinks += 1
                continue
            seen_inode.add(key)
        by_size[fr.size].append(fr)
    groups = []
    for size, recs in by_size.items():
        if len(recs) < 2:
            continue
        pre = defaultdict(list)
        for fr in recs:
            try:
                pre[H(fr, head_stage,
                      lambda p=fr.path: file_hash(p, limit=prefix, algo=algo))].append(fr)
            except OSError:
                continue
        for h, rs in pre.items():
            if len(rs) < 2:
                continue
            if size <= prefix:  # 前缀已覆盖全文
                groups.append({"size": size, "hash": h, "stage": "prefix",
                               "paths": [r.path for r in rs]})
                continue
            # 末尾哈希剪枝：尾巴不在前缀范围内时先看尾巴，不同就在这层淘汰，省掉全量读
            buckets = [rs]
            if tail and size > prefix + tail:
                tails = defaultdict(list)
                for fr in rs:
                    try:
                        tails[H(fr, tail_stage,
                                lambda p=fr.path: file_tail_hash(p, tail=tail, algo=algo))].append(fr)
                    except OSError:
                        continue
                buckets = [v for v in tails.values() if len(v) >= 2]
                if not buckets:
                    continue
            for rs2 in buckets:
                full = defaultdict(list)
                for fr in rs2:
                    try:
                        full[H(fr, "full",
                               lambda p=fr.path: file_hash(p, algo=algo))].append(fr)
                    except OSError:
                        continue
                for fh, fs in full.items():
                    if len(fs) >= 2:
                        groups.append({"size": size, "hash": fh, "stage": "full",
                                       "paths": [r.path for r in fs]})
    for g in groups:
        g["kind"] = "build-mirror" if is_build_mirror(g["paths"]) else "normal"
    groups.sort(key=lambda g: (-g["size"], g["paths"][0].lower()))
    return groups, dup_hardlinks


def cmd_scan_dupes(args):
    ext_filter = None
    if args.ext:
        ext_filter = {("." + e.lower().lstrip('.')) for e in args.ext.split(',') if e.strip()}
    skip = set(DEFAULT_SKIP_NAMES) | {x.strip().lower() for x in args.skip_names.split(',') if x.strip()}
    skip_paths = norm_skip_paths(args.skip_paths)
    cache = HashCache(args.cache) if getattr(args, "cache", None) else None
    t0 = time.time()
    data = scan_tree(args.root, skip, skip_paths, ext_filter=ext_filter)
    try:
        all_groups, hardlinks = find_dupes(data["files"], min_size=args.min_size,
                                           prefix=args.prefix, algo=args.hash,
                                           tail=getattr(args, "tail_size", 1 << 12),
                                           cache=cache)
    finally:
        if cache is not None:
            cache.close()
    mirror = [g for g in all_groups if g["kind"] == "build-mirror"]
    groups = ([g for g in all_groups if g["kind"] != "build-mirror"]
              if args.exclude_mirror else all_groups)

    def waste(gs):
        return sum(g["size"] * (len(g["paths"]) - 1) for g in gs)

    payload = {
        "mode": "dupes", "root": data["root"], "generated": datetime.now().isoformat(timespec='seconds'),
        "scanned": data["stats"]["scanned"], "seconds": round(time.time() - t0, 1),
        "group_count": len(groups),
        "dup_file_count": sum(len(g["paths"]) for g in groups),
        "wasted_bytes": waste(groups),
        "mirror_group_count": len(mirror),
        "wasted_bytes_mirror": waste(mirror),
        "wasted_bytes_excluding_mirror": waste([g for g in all_groups if g["kind"] != "build-mirror"]),
        "exclude_mirror": bool(args.exclude_mirror),
        "hardlink_skipped": hardlinks,
        "cache_enabled": cache is not None,
        "cache_hits": (cache.hits if cache is not None else 0),
        "cache_misses": (cache.misses if cache is not None else 0),
        "groups": groups,
    }
    print_summary_dupes(payload)
    dump_json(args.out, payload)
    return payload


# ---------------------------------------------------------------- 模式三：目录级重复
def dir_signatures(dirs, dir_files, dir_subdirs, strict=False, algo="md5"):
    """自底向上算每个目录的指纹串。strict=True 时把文件内容哈希也纳入。"""
    sig = {}
    for d in sorted(dirs, key=lambda x: x.count(os.sep), reverse=True):
        parts = []
        for name, size in dir_files.get(d, []):
            h = ""
            if strict:
                try:
                    h = file_hash(os.path.join(d, name), algo=algo)
                except OSError:
                    h = "?"
            parts.append("f|%s|%d|%s" % (name, size, h))
        for sub in dir_subdirs.get(d, []):
            parts.append("d|%s|%s" % (os.path.basename(sub), sig.get(sub, "")))
        parts.sort()
        sig[d] = "\x1e".join(parts)
    return sig


def prune_nested(groups):
    """若某等价组的所有目录都嵌在另一等价组的目录之下，则剔除该组（只留最外层）。"""
    keep = []
    norms = [set(os.path.normcase(os.path.abspath(d)) for d in g) for g in groups]
    for i, g in enumerate(groups):
        gi = norms[i]
        nested = False
        for j, other in enumerate(norms):
            if i == j:
                continue
            if all(any(d != x and d.startswith(x + os.sep) for x in other) for d in gi):
                nested = True
                break
        if not nested:
            keep.append(g)
    return keep


def cmd_scan_dirdupes(args):
    skip = set(DEFAULT_SKIP_NAMES) | {x.strip().lower() for x in args.skip_names.split(',') if x.strip()}
    skip_paths = norm_skip_paths(args.skip_paths)
    t0 = time.time()
    data = scan_tree(args.root, skip, skip_paths)
    sig = dir_signatures(data["dirs"], data["dir_files"], data["dir_subdirs"],
                         strict=args.strict, algo=args.hash)
    buckets = defaultdict(list)
    for d, s in sig.items():
        if s:
            buckets[s].append(d)
    raw = [sorted(v) for v in buckets.values() if len(v) >= 2]
    raw.sort(key=lambda v: v[0].lower())
    identical = raw if args.all_levels else prune_nested(raw)

    # 目录对聚合：哪些目录之间重复文件最多（用重复文件结果算）
    groups, _ = find_dupes(data["files"], min_size=1, prefix=args.prefix, algo=args.hash)
    pair = Counter()
    for g in groups:
        parents = sorted({os.path.dirname(p) for p in g["paths"]})
        if len(parents) < 2:
            continue
        n = len(g["paths"])
        for i in range(len(parents)):
            for j in range(i + 1, len(parents)):
                pair[(parents[i], parents[j])] += n
    pairs = [{"dir_a": a, "dir_b": b, "shared_dup_files": c}
             for (a, b), c in pair.most_common(args.top_pairs)]

    payload = {
        "mode": "dirdupes", "root": data["root"], "generated": datetime.now().isoformat(timespec='seconds'),
        "seconds": round(time.time() - t0, 1),
        "identical_dir_groups": len(identical), "identical_dir_count": sum(len(v) for v in identical),
        "identical": identical, "dir_pairs": pairs,
    }
    print("\n[目录级重复] 完全等价目录组 %d 个（涉及 %d 个目录）；目录对（重复文件最多）Top %d："
          % (len(identical), payload["identical_dir_count"], len(pairs)))
    for v in identical[:10]:
        print("  ✓ 等价: " + "  ==  ".join(v))
    for p in pairs[:10]:
        print("  ↔ %d 个重复文件  %s  ||  %s" % (p["shared_dup_files"], p["dir_a"], p["dir_b"]))
    dump_json(args.out, payload)
    return payload


# ---------------------------------------------------------------- 模式四：文本相似度
import re
TIME_PAT = re.compile(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*")
MD_PAT = re.compile(r"^[#>\-\*\s`\|]+")
META_PAT = re.compile(r"^(\*\*(原始文件|分类|行数|版本|备注|说明|来源|用途)\*\*.*|←.*|分类：.*|行数：.*)")


def norm_text(path, max_bytes=400000):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read(max_bytes)
    except OSError:
        return ""
    lines = []
    for ln in raw.splitlines():
        ln = TIME_PAT.sub("", ln)
        ln = MD_PAT.sub("", ln)
        if META_PAT.match(ln):
            continue
        ln = ln.strip()
        if ln:
            lines.append(ln)
    return "\n".join(lines)


def cmd_scan_similar(args):
    from difflib import SequenceMatcher
    exts = {("." + e.lower().lstrip('.')) for e in
            (args.ext or "txt,md,srt,lrc").split(',') if e.strip()}
    skip = set(DEFAULT_SKIP_NAMES) | {x.strip().lower() for x in args.skip_names.split(',') if x.strip()}
    skip_paths = norm_skip_paths(args.skip_paths)
    t0 = time.time()
    data = scan_tree(args.root, skip, skip_paths, ext_filter=exts)
    cand = [fr for fr in data["files"] if 0 < fr.size <= 2_000_000][:args.cap]
    texts = {fr.path: norm_text(fr.path) for fr in cand}
    pairs = []
    n = len(cand)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = texts[cand[i].path], texts[cand[j].path]
            if not a or not b:
                continue
            # 快筛：长度差距过大直接跳过
            if min(len(a), len(b)) / max(len(a), len(b)) < 0.5:
                continue
            r = SequenceMatcher(None, a, b).quick_ratio()
            if r < args.threshold - 0.05:
                continue
            r = SequenceMatcher(None, a, b).ratio()
            if r >= args.threshold:
                pairs.append({"ratio": round(r, 3), "a": cand[i].path, "b": cand[j].path})
    pairs.sort(key=lambda x: -x["ratio"])
    payload = {
        "mode": "similar", "root": data["root"], "generated": datetime.now().isoformat(timespec='seconds'),
        "seconds": round(time.time() - t0, 1), "candidates": n, "pair_count": len(pairs),
        "threshold": args.threshold, "pairs": pairs,
    }
    print("\n[文本相似] 候选 %d 个，≥%.2f 的相似对 %d 组："
          % (n, args.threshold, len(pairs)))
    for p in pairs[:20]:
        print("  %.3f  %s  ||  %s" % (p["ratio"], p["a"], p["b"]))
    dump_json(args.out, payload)
    return payload


# ---------------------------------------------------------------- 模式五：大文件
def parse_size(s):
    """支持纯数字（字节）或 100MB / 1.5GB / 500K / 2G。"""
    if isinstance(s, int):
        return s
    t = str(s).strip().upper()
    mult = 1
    for suf, m in (('TB', 1 << 40), ('GB', 1 << 30), ('MB', 1 << 20), ('KB', 1 << 10),
                   ('T', 1 << 40), ('G', 1 << 30), ('M', 1 << 20), ('K', 1 << 10), ('B', 1)):
        if t.endswith(suf):
            mult = m
            t = t[:-len(suf)]
            break
    return int(float(t) * mult)


def human(n):
    for u, m in (('TB', 1 << 40), ('GB', 1 << 30), ('MB', 1 << 20), ('KB', 1 << 10)):
        if n >= m:
            return "%.2f %s" % (n / m, u)
    return "%d B" % n


def cmd_scan_big(args):
    min_size = parse_size(args.min_size)
    skip = set(DEFAULT_SKIP_NAMES) | {x.strip().lower() for x in args.skip_names.split(',') if x.strip()}
    skip_paths = norm_skip_paths(args.skip_paths)
    t0 = time.time()
    data = scan_tree(args.root, skip, skip_paths, need_dir_map=False,
                     file_filter=lambda fr: fr.size >= min_size)
    recs = sorted(data["files"], key=lambda fr: -fr.size)
    if args.top:
        recs = recs[:args.top]
    payload = {
        "mode": "big", "root": data["root"], "generated": datetime.now().isoformat(timespec='seconds'),
        "seconds": round(time.time() - t0, 1), "min_size": min_size,
        "scanned": data["stats"]["scanned"], "count": len(recs),
        "total_bytes": sum(fr.size for fr in recs),
        "files": [fr.as_dict() for fr in recs],
    }
    print("\n[大文件] 阈值 %s，命中 %d 个，合计 %s，耗时 %.1f 秒"
          % (human(min_size), len(recs), human(payload["total_bytes"]), payload["seconds"]))
    for fr in recs[:20]:
        print("  %10s  %s" % (human(fr.size), fr.path))
    dump_json(args.out, payload)
    return payload


# ---------------------------------------------------------------- 模式六：临时/垃圾文件
TEMP_CATS = {
    "编辑器/Office锁文件": ["~$*", "*.swp", "*.swo", ".#*", "*~"],
    "下载未完成": ["*.crdownload", "*.part", "*.partial", "*.download", "*.!ut", "*.opdownload"],
    "临时/备份": ["*.tmp", "*.temp", "*.bak", "*.old", "*.orig", "*.dmp"],
    "系统垃圾": ["thumbs.db", "desktop.ini", ".ds_store"],
    "Python字节码": ["*.pyc", "*.pyo"],
    "日志": ["*.log"],
}


def _match_temp(name, enabled):
    ln = name.lower()
    for cat, pats in TEMP_CATS.items():
        if cat not in enabled:
            continue
        for p in pats:
            if fnmatch.fnmatch(ln, p):
                return cat
    return None


def cmd_scan_temp(args):
    enabled = {c.strip() for c in args.cats.split(',') if c.strip()} if args.cats else set(TEMP_CATS)
    unknown = enabled - set(TEMP_CATS)
    if unknown:
        sys.stderr.write("未知分类 %s（可选：%s）\n" % (unknown, ",".join(TEMP_CATS)))
    now = time.time()
    age = args.min_age_days * 86400
    skip = set(DEFAULT_SKIP_NAMES) | {x.strip().lower() for x in args.skip_names.split(',') if x.strip()}
    skip_paths = norm_skip_paths(args.skip_paths)
    t0 = time.time()

    def keep(fr):
        if _match_temp(os.path.basename(fr.path), enabled) is None:
            return False
        return (now - fr.mtime) >= age

    data = scan_tree(args.root, skip, skip_paths, need_dir_map=False, file_filter=keep)
    by_cat = defaultdict(list)
    for fr in data["files"]:
        by_cat[_match_temp(os.path.basename(fr.path), enabled)].append(fr)
    files = sorted(data["files"], key=lambda fr: -fr.size)
    payload = {
        "mode": "temp", "root": data["root"], "generated": datetime.now().isoformat(timespec='seconds'),
        "seconds": round(time.time() - t0, 1), "min_age_days": args.min_age_days,
        "scanned": data["stats"]["scanned"], "count": len(files),
        "total_bytes": sum(fr.size for fr in files),
        "by_cat": {k: len(v) for k, v in by_cat.items()},
        "files": [fr.as_dict() for fr in files],
    }
    print("\n[临时/垃圾文件] 年龄≥%d 天，命中 %d 个，合计 %s，耗时 %.1f 秒"
          % (args.min_age_days, len(files), human(payload["total_bytes"]), payload["seconds"]))
    for cat, lst in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        print("  %-18s %d 个" % (cat, len(lst)))
    for fr in files[:15]:
        print("  %10s  %s" % (human(fr.size), fr.path))
    dump_json(args.out, payload)
    return payload


# ---------------------------------------------------------------- 模式七：构建产物
# 两级分开：safe = 可重建（装依赖/编译即可再生）；output = ⚠ 发布成品（删了站点先打不开，须先重新生成）
ARTIFACT_SAFE = {
    "依赖目录": ["node_modules", "bower_components", "vendor", "site-packages",
                 ".venv", "venv", ".tox", "pods"],
    "编译产物": ["build", "target", "cmake-build-debug", "cmake-build-release",
                 ".gradle", ".kotlin", ".cxx", ".ninja_deps", "CMakeFiles"],
    "框架/打包缓存": [".next", ".nuxt", ".svelte-kit", ".astro", ".turbo", ".parcel-cache",
                     ".contentlayer", ".docusaurus", ".vite", ".angular", ".cache",
                     ".webpack", ".rollup.cache"],
    "Python 缓存": ["__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                    ".eggs", ".nox", "htmlcov", ".hypothesis"],
    "基础设施缓存": [".terraform", ".serverless", ".aws-sam"],
}
ARTIFACT_OUTPUT = ["public", "_site", "dist", "out", ".output", "gh-pages", "build-output"]
ARTIFACT_SUFFIX_OUTPUT = []

_ARTIFACT_MAP = {}      # 目录名(小写) -> (tier, 分类)
for _cat, _names in ARTIFACT_SAFE.items():
    for _n in _names:
        _ARTIFACT_MAP[_n.lower()] = ("safe", _cat)
for _n in ARTIFACT_OUTPUT:
    _ARTIFACT_MAP.setdefault(_n.lower(), ("output", "⚠ 发布成品"))


def classify_artifact(dirname):
    """返回 (tier, 分类) 或 None。tier: safe=可重建 / output=⚠发布成品。"""
    return _ARTIFACT_MAP.get(dirname.lower())


def _tree_size_count(d):
    """递归量一个目录的字节数与文件数；跳过 junction/符号链接（防自指死循环）。"""
    s = c = 0
    stack = [d]
    seen = set()
    while stack:
        cur = stack.pop()
        try:
            key = os.path.normcase(os.path.abspath(cur))
            if key in seen:
                continue
            seen.add(key)
            entries = list(os.scandir(cur))
        except (PermissionError, FileNotFoundError, OSError):
            continue
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if is_reparse(st):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                else:
                    s += st.st_size
                    c += 1
            except OSError:
                continue
    return s, c


def scan_build_dirs(root, extra_skip, skip_paths, follow=False):
    """遍历树；**遇到构建目录就记录并停止下钻**（省时）。返回 (hits, scanned)。

    hits: [{path,name,cat,tier,size,files}, ...]
    """
    root = os.path.abspath(root)
    hits = []
    stack = [root]
    visited = set()
    scanned = 0
    while stack:
        d = stack.pop()
        try:
            key = os.path.normcase(os.path.abspath(d))
            if key in visited:
                continue
            visited.add(key)
            entries = list(os.scandir(d))
        except (PermissionError, FileNotFoundError, OSError):
            continue
        for e in entries:
            try:
                st = e.stat(follow_symlinks=follow)
            except OSError:
                continue
            if is_reparse(st):
                continue
            if not e.is_dir(follow_symlinks=follow):
                scanned += 1
                continue
            name = e.name
            low = name.lower()
            if low in extra_skip:
                continue
            full = os.path.normcase(os.path.abspath(e.path)).rstrip(os.sep)
            if any(full == q or full.startswith(q + os.sep) for q in skip_paths):
                continue
            hit = classify_artifact(low)
            if hit:
                size, cnt = _tree_size_count(e.path)
                hits.append({"path": e.path, "name": name, "cat": hit[1],
                             "tier": hit[0], "size": size, "files": cnt})
            else:
                stack.append(e.path)   # 没命中才继续下钻
    return hits, scanned


def cmd_scan_build(args):
    # 注意：这里**不**用 DEFAULT_SKIP_NAMES（那套会把 node_modules/.gradle 等挡掉），只用系统级噪声名单
    sys_skip = {'$recycle.bin', 'system volume information', 'winsxs', '__回收站__',
                'appdata', 'application data', 'local settings', 'my documents'}
    skip = sys_skip | {x.strip().lower() for x in args.skip_names.split(',') if x.strip()}
    skip_paths = norm_skip_paths(args.skip_paths)
    t0 = time.time()
    hits, scanned = scan_build_dirs(args.root, skip, skip_paths)
    if args.min_size:
        lo = parse_size(args.min_size)
        hits = [h for h in hits if h["size"] >= lo]
    hits.sort(key=lambda h: -h["size"])

    safe = [h for h in hits if h["tier"] == "safe"]
    outp = [h for h in hits if h["tier"] == "output"]
    by_cat = defaultdict(lambda: [0, 0])
    for h in hits:
        by_cat[h["cat"]][0] += 1
        by_cat[h["cat"]][1] += h["size"]

    payload = {
        "mode": "build", "root": os.path.abspath(args.root),
        "generated": datetime.now().isoformat(timespec='seconds'),
        "seconds": round(time.time() - t0, 1), "scanned": scanned,
        "count": len(hits),
        "safe_bytes": sum(h["size"] for h in safe),
        "safe_count": len(safe),
        "output_bytes": sum(h["size"] for h in outp),
        "output_count": len(outp),
        "total_bytes": sum(h["size"] for h in hits),
        "by_cat": {k: {"count": v[0], "bytes": v[1]} for k, v in by_cat.items()},
        "dirs": hits,
    }

    print("\n[构建产物] 命中 %d 个构建目录，合计 %s，耗时 %.1f 秒（构建目录内部只量体积、不再下钻）"
          % (len(hits), human(payload["total_bytes"]), payload["seconds"]))
    print("  ✅ 可重建（装依赖/编译即可再生）：%d 个，%s" % (len(safe), human(payload["safe_bytes"])))
    print("  ⚠ 发布成品（删了站点先打不开，须重新生成）：%d 个，%s"
          % (len(outp), human(payload["output_bytes"])))
    for cat in sorted(by_cat, key=lambda k: -by_cat[k][1]):
        c, s = by_cat[cat]
        print("  %-14s %3d 个  %10s" % (cat, c, human(s)))
    print("  体积 Top:")
    for h in hits[:15]:
        mark = "⚠" if h["tier"] == "output" else " "
        print("  %s %10s %6d文件  %s" % (mark, human(h["size"]), h["files"], h["path"]))

    dump_json(args.out, payload)
    print("  报告: %s" % args.out)

    if args.emit_paths:
        sel = [h for h in hits if args.include_output or h["tier"] != "output"]
        with open(args.emit_paths, "w", encoding="utf-8") as f:
            for h in sel:
                f.write(h["path"] + "\n")
        print("  清单: %s（%d 行%s）→ 交给 clean-paths 删"
              % (args.emit_paths, len(sel), "，含 ⚠发布成品" if args.include_output else "，已排除发布成品"))
    return payload


# ---------------------------------------------------------------- 输出
def write_report(args, payload, zero_recs, empty_dirs):
    dump_json(args.out, payload)
    csv_path = os.path.splitext(args.out)[0] + ".csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["类型", "路径", "大小(字节)", "分类", "修改时间"])
        for fr in zero_recs:
            w.writerow(["0字节文件", fr.path, fr.size, fr.kind,
                        datetime.fromtimestamp(fr.mtime).strftime('%Y-%m-%d %H:%M')])
        for d in empty_dirs:
            w.writerow(["空文件夹", d, "", "", ""])
    print("\n[空文件/空目录] 共扫 %d 个文件，命中 0 字节文件 %d 个、空文件夹 %d 个，耗时 %.1f 秒"
          % (payload["scanned"], payload["count"], payload["dir_count"], payload["seconds"]))
    for k in ('图片', '视频', '文档', '其他'):
        c = sum(1 for h in payload["files"] if h["kind"] == k)
        if c:
            print("  %s: %d" % (k, c))
    print("  报告: %s" % args.out)
    print("  清单: %s" % csv_path)


def print_summary_dupes(payload):
    mb = payload["wasted_bytes"] / 1048576
    print("\n[重复文件] 扫 %d 个文件，重复组 %d，涉及 %d 个文件，可省 %.2f MB，耗时 %.1f 秒（硬链跳过 %d）"
          % (payload["scanned"], payload["group_count"], payload["dup_file_count"],
             mb, payload["seconds"], payload["hardlink_skipped"]))
    if payload.get("mirror_group_count") and not payload.get("exclude_mirror"):
        print("  ⚠ 其中 %d 组是「源码↔构建产物」镜像（%.2f MB）——默认不建议删（删了产物失效 / 下次构建又生成）"
              % (payload["mirror_group_count"], payload["wasted_bytes_mirror"] / 1048576))
        print("    真实可省（排除镜像）：%.2f MB" % (payload["wasted_bytes_excluding_mirror"] / 1048576))
    for g in payload["groups"][:15]:
        tag = " ⚠构建镜像" if g.get("kind") == "build-mirror" else ""
        print("  %8d B × %d  [%s]%s" % (g["size"], len(g["paths"]), g["stage"], tag))
        print("      %s" % g["paths"][0])
        for p in g["paths"][1:]:
            print("      └ %s" % p)


def dump_json(path, payload):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- 删除（安全通道）
def _send2trash(path):
    import send2trash  # 延迟导入
    send2trash.send2trash(path)


def _shfileop_delete_one(path):
    """ctypes 调 SHFileOperationW 送回收站（stdlib 兜底）。返回 True/False。"""
    if os.name != 'nt':
        return False
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", wintypes.WORD), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE = 3
    FOF_ALLOWUNDO = 0x40
    FOF_NOCONFIRMATION = 0x10
    FOF_SILENT = 0x4
    FOF_NOERRORUI = 0x400
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = os.path.abspath(path) + "\0\0"
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    try:
        ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except Exception:
        return False
    return not os.path.exists(path)  # 返回码不可靠，用是否消失为准


def recycle_delete(path):
    """把单个文件/目录送进系统回收站（可还原）。返回 (ok, detail)。

    注意：返回的是**元组**；调用方判成功请用 `ok = r[0]`，不要直接 `if r:`（元组恒为真）。
    目录直连 send2trash 常失败（尤其隐藏点目录），主通道是「同盘改名到非隐藏临时名再送回收站」——
    同盘改名=瞬间完成，避免旧版移到 %TEMP% 造成的跨盘复制（大目录会慢到被中断）。
    """
    if not os.path.exists(path):
        return False, "已不存在"
    orig = path
    # 1) 直接 send2trash（文件多半走这条）
    try:
        _send2trash(path)
        if not os.path.exists(path):
            return True, "send2trash"
    except Exception:
        pass

    # 2) 同盘改名到非隐藏临时名，再送回收站（目录/隐藏点目录的主通道）
    base = os.path.basename(path.rstrip("\\/")) or "item"
    parent = os.path.dirname(os.path.abspath(path.rstrip("\\/")))
    stage = None
    if os.path.isdir(parent):
        cand = os.path.join(parent, "fk_stage_%s_%s" % (uuid.uuid4().hex[:8], base))
        try:
            shutil.move(path, cand)          # 同盘 = 改名，秒完成
            stage = cand
        except Exception:
            stage = None
    if stage is None:                        # 跨盘/权限问题 → 退回系统临时目录
        cand = os.path.join(tempfile.gettempdir(),
                            "fk_stage_%s_%s" % (uuid.uuid4().hex[:8], base))
        try:
            shutil.move(path, cand)
            stage = cand
        except Exception:
            stage = None

    if stage is not None:
        for fn, tag in ((_send2trash, "stage+send2trash"),
                        (_shfileop_delete_one, "stage+SHFileOperation")):
            try:
                fn(stage)
            except Exception:
                pass
            if not os.path.exists(stage):
                return True, tag
        # 都失败 → 还原保命
        try:
            shutil.move(stage, orig)
        except Exception:
            pass
        return False, "回收站失败，已还原"

    # 3) 直接 ctypes（最后兜底）
    if _shfileop_delete_one(orig):
        return True, "SHFileOperation"
    return False, "回收站均失败"


def move_to_trashdir(path, trashdir, use, date):
    """把文件/目录移动到 <trashdir>/<use>_<date>/ 下（同盘移动=改名，秒完成）。返回 (ok, detail)。"""
    dest_root = os.path.join(trashdir, "%s_%s" % (use, date))
    name = os.path.basename(path.rstrip("\\/")) or "item"
    dest = os.path.join(dest_root, name)
    if os.path.exists(dest):
        dest = os.path.join(dest_root, "%s_%s" % (uuid.uuid4().hex[:6], name))
    try:
        os.makedirs(dest_root, exist_ok=True)
        shutil.move(path, dest)
        return True, dest
    except Exception as e:
        return False, str(e)


def safe_mtime(p):
    try:
        return os.path.getmtime(p)
    except OSError:
        return 0.0


def choose_keeper(paths, keep):
    if keep == "newest":
        return max(paths, key=safe_mtime)
    if keep == "oldest":
        return min(paths, key=safe_mtime)
    if keep == "shortest":
        return min(paths, key=len)
    return paths[0]  # first


def select_dupe_targets(groups, keep="first"):
    """每个重复组保留一份（keep 策略），其余作为删除目标。返回 (targets, keepers)。"""
    targets, keepers = [], []
    for g in groups:
        paths = list(g.get("paths", []))
        if len(paths) < 2:
            continue
        k = choose_keeper(paths, keep)
        keepers.append(k)
        targets += [p for p in paths if p != k]
    return targets, keepers


def select_dir_targets(identical_groups):
    """完全等价目录组：每组保留第一个，其余为删除目标。嵌套目标自动去重（只留最外层）。"""
    targets, keepers = [], []
    for grp in identical_groups:
        if len(grp) < 2:
            continue
        keepers.append(grp[0])
        targets += grp[1:]
    out = []
    for t in targets:
        nt = os.path.normcase(t)
        if any(nt != os.path.normcase(u) and nt.startswith(os.path.normcase(u) + os.sep)
               for u in targets):
            continue  # 已被另一个目标目录包含，跳过冗余操作
        out.append(t)
    return out, keepers


def dupe_link_pairs(groups, keep="first"):
    """为「硬链接去重」生成 (保留文件路径, 待处理文件路径) 配对列表。"""
    pairs = []
    for g in groups:
        paths = list(g.get("paths", []))
        if len(paths) < 2:
            continue
        k = choose_keeper(paths, keep)
        for p in paths:
            if p != k:
                pairs.append((k, p))
    return pairs


def hardlink_replace(keeper, target, dry=True):
    """把 target 换成指向 keeper 的硬链接：路径还在、看着没变，但磁盘只占一份数据。

    ⚠ 硬链接**不是备份**：改其中任何一个，另一个也跟着变。

    采用「先建后删」的安全顺序：先在同目录建好链接（临时名）并校验 inode，
    确认无误后才把原文件送进回收站，最后让链接版本补上名字。
    任一步出错都会清掉临时文件，原文件不会被碰。

    返回 (结果, 说明)：True=成功/可成功，False=失败，None=有意跳过。
    """
    try:
        ks, ts = os.stat(keeper), os.stat(target)
    except OSError as e:
        return False, "读文件信息失败: %s" % e.strerror
    if getattr(ks, "st_dev", 0) != getattr(ts, "st_dev", 0):
        return None, "跨盘/跨卷（Windows 不支持跨驱动器硬链接）"
    if ks.st_ino == ts.st_ino:
        return None, "已经是同一份数据，无需处理"
    tmp = target + ".fclean_link_tmp"
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    try:
        os.link(keeper, tmp)
        if os.stat(tmp).st_ino != ks.st_ino:
            raise OSError("链接校验失败")
        if dry:
            os.remove(tmp)
            return True, "可链接（inode=%d）" % ks.st_ino
        ok, detail = recycle_delete(target)
        if not ok:
            raise OSError("原文件未能安全移走: %s" % detail)
        os.replace(tmp, target)
        return True, "已指向同一份数据（原文件进回收站，可还原）"
    except OSError as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False, str(e).replace("[Errno ", "")[:120]


def cmd_clean(args):
    with open(args.report, encoding="utf-8") as f:
        rep = json.load(f)
    mode = rep.get("mode")
    files, dirs, keepers = [], [], []
    if mode == "dupes":
        gs = rep.get("groups", [])
        if not args.include_mirror:
            kept = [g for g in gs if g.get("kind") != "build-mirror"]
            skipped = len(gs) - len(kept)
            if skipped:
                print("已跳过 %d 组「构建镜像」重复（默认不删；确实要删加 --include-mirror）" % skipped)
            gs = kept
        files, keepers = select_dupe_targets(gs, args.keep)
    elif mode == "dirdupes" and args.delete_identical_dirs:
        files, keepers = select_dir_targets(rep.get("identical", []))
    elif mode == "build":
        ds = rep.get("dirs", [])
        outp = [d for d in ds if d.get("tier") == "output"]
        if outp and not args.include_output:
            print("已跳过 %d 个「⚠发布成品」目录（删了站点先打不开，须重新生成；确实要删加 --include-output）"
                  % len(outp))
        files = [d["path"] for d in ds
                 if args.include_output or d.get("tier") != "output"]
    else:
        files = [x["path"] for x in rep.get("files", [])]
    if args.with_empty_dirs and mode == "empty":
        dirs = [x["path"] for x in rep.get("empty_dirs", [])]
        dirs.sort(key=lambda d: d.count(os.sep), reverse=True)  # 深的先删

    if mode == "dupes" and keepers:
        print("重复清理：%d 组，每组保留 1 份（策略=%s），将处理 %d 个文件"
              % (len(keepers), args.keep, len(files)))
        for k in keepers[:5]:
            print("  留: %s" % k)
    if mode == "dirdupes" and keepers:
        print("!! 等价目录清理：每组保留第一个，将删除 %d 个整目录（可还原，务必先看清单）" % len(files))

    if mode == "dupes" and getattr(args, "link", "none") == "hard":
        pairs = dupe_link_pairs(gs, args.keep)
        print("模式: 硬链接去重 | 动作: %s | 配对: %d 个（保留策略=%s）"
              % ("实做" if args.apply else "预演(不动)", len(pairs), args.keep))
        print("注意：硬链接不是备份——改其中任意一个，另一个也跟着变；"
              "跨驱动器 / 不支持硬链接的文件系统会自动跳过，原文件不动。")
        c = Counter()
        fails = []
        for keep_path, tgt in pairs:
            if not os.path.exists(tgt):
                c["missing"] += 1
                continue
            r, msg = hardlink_replace(keep_path, tgt, dry=not args.apply)
            if r is True:
                c["linked"] += 1
            elif r is None:
                c["skipped"] += 1
            else:
                c["failed"] += 1
                fails.append((tgt, msg))
            done_n = c["linked"] + c["skipped"] + c["failed"]
            if done_n % max(1, args.batch) == 0:
                print("  [%d/%d] 成功 %d | 跳过 %d | 失败 %d"
                      % (done_n, len(pairs), c["linked"], c["skipped"], c["failed"]))
            if c["failed"]:
                print("  !! 出现失败，停止后续")
                break
        print("\n=== %s ===" % ("硬链接结果" if args.apply else "预演结果"))
        for kk in ("linked", "skipped", "missing", "failed"):
            if c[kk]:
                print("  %-8s %d" % (kk, c[kk]))
        for fp, fe in fails[:30]:
            print("  FAIL %s => %s" % (fp, fe))
        return {"stats": dict(c), "failures": fails}

    channel = "trashdir" if args.trash_dir else "recycle"
    date = datetime.now().strftime("%Y%m%d")
    do = args.apply
    stats = Counter()
    failures = []
    batch = max(1, args.batch)

    def handle(paths, is_dir):
        for i, p in enumerate(paths, 1):
            if not os.path.exists(p):
                stats["missing"] += 1
                continue
            if is_dir:
                try:
                    if any(os.scandir(p)):
                        stats["skip_nonempty"] += 1
                        continue
                except OSError:
                    stats["failed"] += 1
                    failures.append((p, "无法读取"))
                    continue
            if not do:
                stats["planned"] += 1
                continue
            if channel == "trashdir":
                ok, detail = move_to_trashdir(p, args.trash_dir, args.use, date)
            else:
                ok, detail = recycle_delete(p)
            if ok:
                stats["done"] += 1
                if do:
                    print("  ok [%s]  %s" % (detail, p))
            else:
                stats["failed"] += 1
                failures.append((p, detail))
            if i % batch == 0:
                print("  [%d/%d] 完成 %d | 失败 %d | 已不存在 %d"
                      % (i, len(paths), stats["done"], stats["failed"], stats["missing"]))
                if stats["failed"]:
                    print("  !! 出现失败，停止后续")
                    return False
        return True

    print("模式: %s | 通道: %s | 目标: %d 个文件%s"
          % ("实删" if do else "预演(不动)", channel, len(files),
             " + %d 个空目录" % len(dirs) if dirs else ""))
    if not handle(files, False):
        pass
    elif dirs:
        handle(dirs, True)

    print("\n=== %s ===" % ("实删结果" if do else "预演结果"))
    for k in ("done", "planned", "missing", "skip_nonempty", "failed"):
        if stats[k]:
            print("  %-8s %d" % (k, stats[k]))
    for p, e in failures[:30]:
        print("  FAIL %s => %s" % (p, e))
    return {"stats": dict(stats), "failures": failures}


# ---------------------------------------------------------------- clean-paths
def read_path_list(path):
    """读路径清单：一行一个；空行与 # 开头忽略。返回路径列表。"""
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            s = line.strip().strip('"')
            if not s or s.startswith("#"):
                continue
            out.append(s)
    return out


def cmd_clean_paths(args):
    """按「路径清单」安全删除整目录/文件（用于构建产物等不在扫描报告里的目标）。"""
    paths = []
    if args.list:
        paths = read_path_list(args.list)
    if args.paths:
        paths += [p.strip() for p in args.paths.replace(";", ",").split(",") if p.strip()]
    paths = list(dict.fromkeys(paths))  # 去重保序

    excl = [s.strip() for s in (args.exclude or "").split(",") if s.strip()]
    kept, skipped = [], []
    for p in paths:
        if any(e in p for e in excl):
            skipped.append(p)
        else:
            kept.append(p)

    # 去「被另一个目标目录包含」的项，避免重复计数/重复删
    norm = [os.path.normcase(os.path.normpath(p)) for p in kept]
    final = [p for p, n in zip(kept, norm)
             if not any(n != q and n.startswith(q + os.sep) for q in norm)]

    date = datetime.now().strftime("%Y%m%d")
    channel = "trashdir" if args.trash_dir else "recycle"
    print("目标 %d 项（去重/去包含后）| 已排除 %d 项 | 通道 %s | %s"
          % (len(final), len(skipped), channel, "实删" if args.apply else "预演(不动)"))
    tot = 0; dirs_n = 0; files_n = 0
    for p in final:
        if not os.path.exists(p):
            print("  [缺失] %s" % p)
            continue
        if os.path.isdir(p):
            s, c = _tree_size_count(p)
            dirs_n += 1; tot += s
            print("  [D] %9s %5d 文件  %s" % (human(s), c, p))
        else:
            try:
                s = os.path.getsize(p)
            except OSError:
                s = 0
            files_n += 1; tot += s
            print("  [F] %9s %-8s  %s" % (human(s), "", p))
    print("  ---- 合计 %s / %d 目录 / %d 文件 ----" % (human(tot), dirs_n, files_n))
    for p in skipped:
        print("  [已排除] %s" % p)

    if not args.apply:
        print("\n[干跑] 未做任何改动。加 --apply 执行。")
        return {"stats": {"planned": len(final)}}

    if channel == "recycle" and files_n + dirs_n and (dirs_n or files_n > 200):
        print("  提示：目标含整目录/多文件，系统回收站较慢；要「秒完成」可改用 --trash-dir <同盘目录>")

    done = failed = 0
    failures = []
    batch = max(1, args.batch)
    for i, p in enumerate(final, 1):
        if not os.path.exists(p):
            continue
        if channel == "trashdir":
            ok, detail = move_to_trashdir(p, args.trash_dir, args.use, date)
        else:
            ok, detail = recycle_delete(p)
        if ok:
            done += 1
            print("  ok [%s]  %s" % (detail, p))
        else:
            failed += 1
            failures.append((p, detail))
        if i % batch == 0:
            print("  [%d/%d] 完成 %d | 失败 %d" % (i, len(final), done, failed))
            if failed:
                print("  !! 出现失败，停止后续")
                break

    print("\n=== 实删结果 ===")
    print("  done     %d" % done)
    print("  failed   %d" % failed)
    for p, e in failures[:30]:
        print("  FAIL %s => %s" % (p, e))
    return {"stats": {"done": done, "failed": failed}, "failures": failures}


# ---------------------------------------------------------------- CLI
def build_parser():
    ap = argparse.ArgumentParser(prog="fclean", description="文件清理工具箱（默认只扫不动）")
    ap.add_argument("--version", action="version", version="fclean " + VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p, default_ext_any=False):
        p.add_argument("--root", required=True, help="扫描根目录")
        p.add_argument("--out", default=None, help="JSON 报告路径（默认 <root>_fclean_<mode>.json）")
        p.add_argument("--skip-names", default="", help="额外跳过的目录名，逗号分隔")
        p.add_argument("--skip-paths", default="", help="额外跳过的绝对路径，分号分隔")

    p = sub.add_parser("scan-empty", help="空文件(0字节)+空文件夹")
    add_common(p)
    p.add_argument("--ext", default="", help="只查这些扩展名，逗号分隔")
    p.add_argument("--any", action="store_true", help="不限扩展名（默认只查 图片/视频/文档）")
    p.add_argument("--no-dirs", action="store_true", help="不查空文件夹")
    p.set_defaults(func=cmd_scan_empty, mode="empty")

    p = sub.add_parser("scan-dupes", help="重复文件")
    add_common(p)
    p.add_argument("--ext", default="", help="只查这些扩展名，逗号分隔")
    p.add_argument("--min-size", type=int, default=1, help="最小参与字节数（默认 1）")
    p.add_argument("--prefix", type=int, default=1 << 16, help="前缀哈希长度（默认 65536）")
    p.add_argument("--hash", default="md5", help="哈希算法（默认 md5）")
    p.add_argument("--tail-size", type=int, default=1 << 12,
                   help="末尾哈希字节数（默认 4096）。在全量读之前先看一眼尾巴做剪枝，"
                        "开头相同但结尾不同的文件会在这层被淘汰，不必整个重读。0=关闭该剪枝")
    p.add_argument("--cache", default="",
                   help="哈希缓存的 SQLite 文件路径（**给了才启用**）。第二次扫同一目录时，"
                        "只要文件的大小和修改时间没变就直接复用上次的哈希，几乎不用再读文件内容")
    p.add_argument("--exclude-mirror", action="store_true",
                   help="排除「源码↔构建产物」镜像重复（推荐，避免误报）")
    p.set_defaults(func=cmd_scan_dupes, mode="dupes")

    p = sub.add_parser("scan-dirdupes", help="目录级重复（重复文件夹+目录对聚合）")
    add_common(p)
    p.add_argument("--strict", action="store_true", help="指纹纳入文件内容哈希（更严）")
    p.add_argument("--prefix", type=int, default=1 << 16)
    p.add_argument("--hash", default="md5")
    p.add_argument("--top-pairs", type=int, default=50)
    p.add_argument("--all-levels", action="store_true", help="连嵌套的等价子目录一起报（默认只报最外层）")
    p.set_defaults(func=cmd_scan_dirdupes, mode="dirdupes")

    p = sub.add_parser("scan-similar", help="文本相似度")
    add_common(p)
    p.add_argument("--ext", default="txt,md,srt,lrc", help="参与比对的扩展名")
    p.add_argument("--threshold", type=float, default=0.8, help="相似阈值（默认 0.8）")
    p.add_argument("--cap", type=int, default=400, help="最多比对多少个文件")
    p.set_defaults(func=cmd_scan_similar, mode="similar")

    p = sub.add_parser("scan-big", help="大文件（占用空间大户）")
    add_common(p)
    p.add_argument("--min-size", default="100MB", help="最小体积，支持 100MB/1GB/500K")
    p.add_argument("--top", type=int, default=200, help="最多列出多少个（默认 200）")
    p.set_defaults(func=cmd_scan_big, mode="big")

    p = sub.add_parser("scan-temp", help="临时/垃圾文件")
    add_common(p)
    p.add_argument("--min-age-days", type=int, default=7, help="只报这么多天前的（默认 7）")
    p.add_argument("--cats", default="", help="分类，逗号分隔，默认全部")
    p.set_defaults(func=cmd_scan_temp, mode="temp")

    p = sub.add_parser("scan-build", help="构建产物（build/node_modules 等，按可重建/发布成品分级）")
    add_common(p)
    p.add_argument("--min-size", default="", help="只报大于该体积的目录，如 10MB（默认不限）")
    p.add_argument("--emit-paths", default="", help="把要删的目录写成清单文件，可直接喂 clean-paths")
    p.add_argument("--include-output", action="store_true",
                   help="清单里连「⚠发布成品」一起写（默认排除，如网站 public/dist）")
    p.set_defaults(func=cmd_scan_build, mode="build")

    p = sub.add_parser("clean", help="按报告安全删除")
    p.add_argument("--report", required=True, help="scan 生成的 JSON 报告")
    p.add_argument("--apply", action="store_true", help="真正删除（默认预演）")
    p.add_argument("--trash-dir", default="", help="移动到该目录下的 <use>_<date>/（默认走系统回收站）")
    p.add_argument("--use", default="fclean", help="回收目录用途名（默认 fclean）")
    p.add_argument("--with-empty-dirs", action="store_true", help="连带空文件夹（仅 empty 报告）")
    p.add_argument("--keep", choices=["first", "newest", "oldest", "shortest"], default="first",
                   help="重复组保留哪份（默认 first）")
    p.add_argument("--include-mirror", action="store_true",
                   help="连「源码↔构建产物」镜像也一起删（默认跳过，危险）")
    p.add_argument("--include-output", action="store_true",
                   help="配合 scan-build 报告：连「⚠发布成品」目录也一起删（默认跳过）")
    p.add_argument("--delete-identical-dirs", action="store_true",
                   help="配合 scan-dirdupes：删掉完全等价目录组里除第一个外的整目录")
    p.add_argument("--link", choices=["none", "hard"], default="none",
                   help="none=删掉多余副本（默认，走回收站可还原）；"
                        "hard=把多余副本换成硬链接：路径都还在、磁盘只占一份。"
                        "注意硬链接不是备份，改一个另一个也变；跨驱动器会自动跳过")
    p.add_argument("--batch", type=int, default=10, help="每批数量，失败即停（默认 10）")
    p.set_defaults(func=cmd_clean, mode="clean")

    p = sub.add_parser("clean-paths", help="按路径清单安全删除整目录/文件（如构建产物）")
    p.add_argument("--list", default="", help="路径清单文件，一行一个（# 开头为注释）")
    p.add_argument("--paths", default="", help="直接给路径，逗号/分号分隔")
    p.add_argument("--exclude", default="", help="含这些子串的路径一律跳过，逗号分隔（如 public,\\.workbuddy）")
    p.add_argument("--apply", action="store_true", help="真正删除（默认预演）")
    p.add_argument("--trash-dir", default="", help="移动到该目录下的 <use>_<date>/（同盘秒移；默认走系统回收站）")
    p.add_argument("--use", default="fclean", help="回收目录用途名（默认 fclean）")
    p.add_argument("--batch", type=int, default=10, help="每批数量，失败即停（默认 10）")
    p.set_defaults(func=cmd_clean_paths, mode="clean-paths")
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.mode not in ("clean", "clean-paths") and getattr(args, "out", None) is None:
        args.out = "%s_fclean_%s.json" % (os.path.abspath(args.root).rstrip("\\/"), args.mode)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
