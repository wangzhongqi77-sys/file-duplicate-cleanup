#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest_recycle.py — 验证「系统回收站」通道（send2trash / ctypes SHFileOperationW）。

注意：本测试会把两个临时文件真正送进系统回收站（可手动还原），故单独成一个脚本、不并进 selftest。
覆盖：① 普通路径；② 隐藏点目录（.hd/）内的文件（历史上 send2trash 对此类路径会失败，考验兜底）。
用法：<venv-python> selftest_recycle.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FCLEAN = os.path.join(HERE, "fclean.py")
PY = sys.executable


def main():
    base = tempfile.mkdtemp(prefix="fclean_recycle_")
    try:
        p1 = os.path.join(base, "r1.txt")
        with open(p1, "w", encoding="utf-8") as f:
            f.write("recycle me")
        os.makedirs(os.path.join(base, ".hd"), exist_ok=True)
        p2 = os.path.join(base, ".hd", "r2.txt")   # 隐藏点目录内的文件
        with open(p2, "w", encoding="utf-8") as f:
            f.write("hidden dot dir file")

        rep = os.path.join(base, "rep.json")
        with open(rep, "w", encoding="utf-8") as f:
            json.dump({"mode": "empty", "files": [{"path": p1}, {"path": p2}]}, f)

        r = subprocess.run([PY, FCLEAN, "clean", "--report", rep, "--apply"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(r.stdout)
        if r.returncode != 0:
            print(r.stderr[-1500:])

        ok1 = not os.path.exists(p1)
        ok2 = not os.path.exists(p2)
        print("普通文件进回收站: %s" % ("PASS" if ok1 else "FAIL"))
        print("隐藏点目录文件进回收站: %s" % ("PASS" if ok2 else "FAIL"))
        return 0 if (ok1 and ok2) else 1
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
