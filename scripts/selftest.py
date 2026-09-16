#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest.py — fclean.py 自测：合成夹具 + 断言，可重复跑。

用法：
  <venv-python> selftest.py
退出码 0 = 全部通过；非 0 = 有失败。
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

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [PASS] %s" % name)
    else:
        FAIL.append((name, detail))
        print("  [FAIL] %s  %s" % (name, detail))


def run(*args):
    r = subprocess.run([PY, FCLEAN] + list(args), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print("  !! 命令失败：", " ".join(args))
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
    return r


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_fixture(base):
    fx = os.path.join(base, "fixture")
    os.makedirs(fx, exist_ok=True)
    j = lambda *p: os.path.join(fx, *p)

    # 重复组1：同内容三份，含不同扩展名（11 字节，走前缀哈希）
    os.makedirs(j("sub1"), exist_ok=True)
    os.makedirs(j("sub2"), exist_ok=True)
    for p in ("a.txt", "sub1/b.txt", "sub2/c_copy.md"):
        with open(j(*p.split("/")), "w", encoding="utf-8") as f:
            f.write("hello world")

    # 重复组2：1MB 相同二进制（超过前缀长度，必须走全量哈希）
    blob = bytes(range(256)) * 4096  # 1 MiB
    with open(j("big1.bin"), "wb") as f:
        f.write(blob)
    with open(j("bigdup.bin"), "wb") as f:
        f.write(blob)
    with open(j("unique.bin"), "wb") as f:
        f.write(b"X" + blob[1:])  # 与组2不同

    # 0 字节文件
    for p in ("empty1.png", "empty2.pdf"):
        open(j(p), "wb").close()

    # 空文件夹（两个）
    os.makedirs(j("emptyfolder"), exist_ok=True)
    os.makedirs(j("nested", "inner_empty"), exist_ok=True)

    # 完全相同的目录树
    for root in ("treeA", "treeA_copy"):
        os.makedirs(j(root, "y"), exist_ok=True)
        with open(j(root, "x.txt"), "w", encoding="utf-8") as f:
            f.write("tree x")
        with open(j(root, "y", "z.txt"), "w", encoding="utf-8") as f:
            f.write("tree z")

    # 相似文本
    base_lines = ["金艺锐虎自卸 500锰钢厢体", "厢底四道梁加固", "锰钢门芯 钢板底",
                  "1.8米车厢 工程钢板", "皮实耐造 靠得住"]
    with open(j("sim1.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(base_lines))
    with open(j("sim2.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(base_lines[:4]) + "\n锰钢门芯 钢板底\n皮实耐造 很省心")
    return fx


def main():
    base = tempfile.mkdtemp(prefix="fclean_test_")
    try:
        fx = build_fixture(base)
        out = lambda m: os.path.join(base, m + ".json")

        print("\n=== 1) scan-empty ===")
        run("scan-empty", "--root", fx, "--out", out("empty"))
        rep = load(out("empty"))
        paths = {h["path"] for h in rep["files"]}
        check("0字节命中=2", rep["count"] == 2, "got %d" % rep["count"])
        check("0字节含 empty1.png/empty2.pdf",
              {os.path.basename(p) for p in paths} == {"empty1.png", "empty2.pdf"}, str(paths))
        edirs = {os.path.basename(d["path"]) for d in rep["empty_dirs"]}
        check("空目录含 emptyfolder 和 inner_empty",
              {"emptyfolder", "inner_empty"} <= edirs, str(edirs))
        check("nested 不算空（含子目录）", "nested" not in edirs, str(edirs))

        print("\n=== 2) scan-dupes ===")
        run("scan-dupes", "--root", fx, "--out", out("dupes"), "--ext", "txt,md,bin",
            "--skip-names", "treeA,treeA_copy")
        rep = load(out("dupes"))
        check("重复组=2", rep["group_count"] == 2, "got %d" % rep["group_count"])
        check("涉及文件=5", rep["dup_file_count"] == 5, "got %d" % rep["dup_file_count"])
        sizes = sorted(g["size"] for g in rep["groups"])
        check("组大小 = [11, 1048576]", sizes == [11, 1048576], str(sizes))
        stages = {g["stage"] for g in rep["groups"]}
        check("小文件走 prefix、大文件走 full", stages == {"prefix", "full"}, str(stages))
        expect_waste = 11 * 2 + 1048576 * 1
        check("可省字节正确", rep["wasted_bytes"] == expect_waste,
              "%d vs %d" % (rep["wasted_bytes"], expect_waste))

        print("\n=== 3) scan-dirdupes ===")
        run("scan-dirdupes", "--root", fx, "--out", out("dirdupes"), "--top-pairs", "20")
        rep = load(out("dirdupes"))
        groups = [set(os.path.basename(d) for d in v) for v in rep["identical"]]
        check("treeA 与 treeA_copy 被识别为等价目录",
              any({"treeA", "treeA_copy"} <= g for g in groups), str(groups))
        pairset = {(p["dir_a"], p["dir_b"]) for p in rep["dir_pairs"]}
        check("目录对里有 sub1<->sub2", any(
            os.path.basename(a) == "sub1" and os.path.basename(b) == "sub2"
            for a, b in pairset), str(list(pairset)[:3]))

        print("\n=== 4) scan-similar ===")
        run("scan-similar", "--root", fx, "--out", out("similar"), "--threshold", "0.8")
        rep = load(out("similar"))
        hit = any({os.path.basename(p["a"]), os.path.basename(p["b"])} == {"sim1.txt", "sim2.txt"}
                  for p in rep["pairs"])
        check("sim1.txt 与 sim2.txt 被判为相似", hit, "pairs=%d" % rep["pair_count"])

        print("\n=== 4b) scan-big ===")
        run("scan-big", "--root", fx, "--min-size", "100KB", "--out", out("big"))
        rep = load(out("big"))
        check("大文件命中=3", rep["count"] == 3, "got %d" % rep["count"])
        names = {os.path.basename(f["path"]) for f in rep["files"]}
        check("含 big1.bin/bigdup.bin/unique.bin",
              names == {"big1.bin", "bigdup.bin", "unique.bin"}, str(names))
        check("大文件合计≥3MB", rep["total_bytes"] >= 3 * 1048576, str(rep["total_bytes"]))

        print("\n=== 4c) scan-temp ===")
        old_bak = os.path.join(fx, "old.bak")
        with open(old_bak, "w", encoding="utf-8") as f:
            f.write("old backup")
        new_tmp = os.path.join(fx, "new.tmp")
        with open(new_tmp, "w", encoding="utf-8") as f:
            f.write("fresh temp")
        os.utime(old_bak, (0, 0))  # 设为 1970，确保“够旧”
        run("scan-temp", "--root", fx, "--min-age-days", "7", "--out", out("temp"))
        rep = load(out("temp"))
        names = {os.path.basename(f["path"]) for f in rep["files"]}
        check("临时文件：含够旧的 old.bak", "old.bak" in names, str(names))
        check("临时文件：不含刚建的 new.tmp", "new.tmp" not in names, str(names))
        run("scan-temp", "--root", fx, "--min-age-days", "7", "--cats", "系统垃圾", "--out", out("temp2"))
        rep2 = load(out("temp2"))
        check("分类过滤（系统垃圾）命中=0", rep2["count"] == 0, str(rep2["count"]))

        print("\n=== 4d) 构建镜像识别与排除 ===")
        site = os.path.join(fx, "site")
        for sub in ("source", "public"):
            os.makedirs(os.path.join(site, sub), exist_ok=True)
            with open(os.path.join(site, sub, "a.txt"), "w", encoding="utf-8") as f:
                f.write("mirror content 123")
        run("scan-dupes", "--root", site, "--out", out("mirror"), "--ext", "txt")
        rm = load(out("mirror"))
        check("镜像组被标记 build-mirror",
              rm["group_count"] == 1 and rm["groups"][0]["kind"] == "build-mirror",
              str(rm.get("mirror_group_count")))
        check("镜像可省字节被单列", rm["wasted_bytes_mirror"] > 0, str(rm["wasted_bytes_mirror"]))
        run("scan-dupes", "--root", site, "--out", out("mirror2"), "--ext", "txt", "--exclude-mirror")
        rm2 = load(out("mirror2"))
        check("--exclude-mirror 后组数=0", rm2["group_count"] == 0, str(rm2["group_count"]))

        print("\n=== 5) clean 预演不动文件 ===")
        before = sorted(os.listdir(fx))
        run("clean", "--report", out("empty"))
        after = sorted(os.listdir(fx))
        check("预演后目录未变", before == after, str(after))

        print("\n=== 6) clean --apply 走 trash-dir ===")
        td = os.path.join(base, "__回收站__")
        run("clean", "--report", out("empty"), "--apply", "--trash-dir", td,
            "--use", "selftest", "--with-empty-dirs")
        check("empty1.png 已移走", not os.path.exists(os.path.join(fx, "empty1.png")))
        check("empty2.pdf 已移走", not os.path.exists(os.path.join(fx, "empty2.pdf")))
        check("非目标文件仍在", os.path.exists(os.path.join(fx, "a.txt")))
        # 回收目录里应有 0 字节文件 + 空目录
        moved = []
        for r, _d, fs in os.walk(td):
            moved += [os.path.join(r, x) for x in fs]
        check("回收目录含 2 个文件", len(moved) == 2, str(moved))
        check("回收目录含空目录名", any("emptyfolder" in r for r, _d, _f in os.walk(td)),
              str(list(os.walk(td))[:3]))

        print("\n=== 6b) clean --apply 清理重复文件（保留 first）===")
        rep = load(out("dupes"))
        td2 = os.path.join(base, "__回收站2__")
        run("clean", "--report", out("dupes"), "--apply", "--trash-dir", td2, "--use", "dup")
        removed = 0
        for g in rep["groups"]:
            alive = [p for p in g["paths"] if os.path.exists(p)]
            removed += len(g["paths"]) - len(alive)
            check("重复组(%d B) 仅保留 1 份" % g["size"], len(alive) == 1, "alive=%d" % len(alive))
        check("共删除 3 个重复文件", removed == 3, "removed=%d" % removed)

        print("\n=== 6c) 边界：中文文件名 + 等价目录删除 ===")
        uni = os.path.join(fx, "uni")
        os.makedirs(uni, exist_ok=True)
        for n in ("重复_中文.txt", "重复_中文_副本.txt"):
            with open(os.path.join(uni, n), "w", encoding="utf-8") as f:
                f.write("相同内容 unicode")
        run("scan-dupes", "--root", uni, "--out", out("uni"), "--ext", "txt")
        ru = load(out("uni"))
        check("中文文件名重复可识别", ru["group_count"] == 1 and ru["dup_file_count"] == 2,
              "g=%d f=%d" % (ru["group_count"], ru["dup_file_count"]))
        repd = load(out("dirdupes"))
        check("嵌套等价组折叠为最外层（=1 组）", len(repd["identical"]) == 1, str(repd["identical"]))
        td3 = os.path.join(base, "__回收站3__")
        run("clean", "--report", out("dirdupes"), "--apply", "--delete-identical-dirs",
            "--trash-dir", td3, "--use", "dir")
        ok = True
        for grp in repd["identical"]:
            if len([d for d in grp if os.path.isdir(d)]) != 1:
                ok = False
        check("每个等价目录组各保留 1 份", ok, str(repd["identical"]))

        print("\n=== 6d) clean-paths：按清单删整目录 ===")
        cp = os.path.join(fx, "cp")
        outdir = os.path.join(cp, "outdir")
        os.makedirs(outdir, exist_ok=True)
        for n in ("a.bin", "b.bin"):
            with open(os.path.join(outdir, n), "wb") as f:
                f.write(b"z" * 64)
        single = os.path.join(cp, "single.log")
        with open(single, "w", encoding="utf-8") as f:
            f.write("log")
        pub = os.path.join(cp, "public")
        os.makedirs(pub, exist_ok=True)
        with open(os.path.join(pub, "p.html"), "w", encoding="utf-8") as f:
            f.write("site")
        cp_list = os.path.join(base, "cp_list.txt")
        with open(cp_list, "w", encoding="utf-8") as f:
            f.write("\n".join(["# 清单", outdir, single, pub, os.path.join(outdir, "a.bin")]))

        r = run("clean-paths", "--list", cp_list, "--exclude", "public")
        check("clean-paths 去包含后 目标=2", "目标 2 项" in r.stdout, r.stdout[:200])
        check("clean-paths 预演不动", os.path.isdir(outdir) and os.path.exists(single))
        td4 = os.path.join(base, "__回收站4__")
        run("clean-paths", "--list", cp_list, "--exclude", "public", "--apply",
            "--trash-dir", td4, "--use", "cp")
        check("clean-paths 实删：outdir 消失", not os.path.isdir(outdir))
        check("clean-paths 实删：single.log 消失", not os.path.exists(single))
        check("clean-paths --exclude：public 保留", os.path.isdir(pub))

        print("\n=== 7) 错误用法 ===")
        r = run("scan-empty", "--root", os.path.join(base, "no_such_dir"), "--out", out("nope"))
        check("不存在的根目录不崩溃（退出码0）", r.returncode == 0, str(r.returncode))

        print("\n=== 8) 参数覆盖：--keep / --strict / --all-levels / --skip-paths / --prefix / --batch / --include-mirror ===")
        p8 = os.path.join(base, "p8")

        def make_keep_fixture(sub):
            d = os.path.join(p8, sub)
            os.makedirs(d, exist_ok=True)
            for n, mt in (("a.txt", 1000000000), ("b.txt", 1700000000), ("c.txt", 1400000000)):
                p = os.path.join(d, n)
                with open(p, "w", encoding="utf-8") as f:
                    f.write("keep-test-content")
                os.utime(p, (mt, mt))
            return d

        d1 = make_keep_fixture("keep_newest")
        run("scan-dupes", "--root", d1, "--out", out("k1"), "--ext", "txt")
        run("clean", "--report", out("k1"), "--apply", "--keep", "newest",
            "--trash-dir", os.path.join(base, "__r8a__"))
        alive = [n for n in ("a.txt", "b.txt", "c.txt") if os.path.exists(os.path.join(d1, n))]
        check("--keep newest 保留最新的 b.txt", alive == ["b.txt"], str(alive))

        d2 = make_keep_fixture("keep_oldest")
        run("scan-dupes", "--root", d2, "--out", out("k2"), "--ext", "txt")
        run("clean", "--report", out("k2"), "--apply", "--keep", "oldest",
            "--trash-dir", os.path.join(base, "__r8b__"))
        alive2 = [n for n in ("a.txt", "b.txt", "c.txt") if os.path.exists(os.path.join(d2, n))]
        check("--keep oldest 保留最旧的 a.txt", alive2 == ["a.txt"], str(alive2))

        d3 = make_keep_fixture("keep_shortest")
        run("scan-dupes", "--root", d3, "--out", out("k3"), "--ext", "txt")
        run("clean", "--report", out("k3"), "--apply", "--keep", "shortest",
            "--trash-dir", os.path.join(base, "__r8c__"))
        n_alive = sum(1 for n in ("a.txt", "b.txt", "c.txt") if os.path.exists(os.path.join(d3, n)))
        check("--keep shortest 只剩 1 份", n_alive == 1, str(n_alive))

        p8d = os.path.join(p8, "nested")
        for root in ("A", "B"):
            os.makedirs(os.path.join(p8d, root, "sub"), exist_ok=True)
            with open(os.path.join(p8d, root, "f.txt"), "w", encoding="utf-8") as f:
                f.write("same")
            with open(os.path.join(p8d, root, "sub", "g.txt"), "w", encoding="utf-8") as f:
                f.write("same2")
        run("scan-dirdupes", "--root", p8d, "--out", out("d8"), "--strict")
        r8 = load(out("d8"))
        check("--strict 仍识别 A<->B 等价",
              any({"A", "B"} <= {os.path.basename(x) for x in g} for g in r8["identical"]),
              str(r8["identical"])[:160])
        run("scan-dirdupes", "--root", p8d, "--out", out("d8b"), "--all-levels")
        r8b = load(out("d8b"))
        check("--all-levels 组数多于默认（含嵌套）",
              len(r8b["identical"]) > len(r8["identical"]),
              "%d vs %d" % (len(r8b["identical"]), len(r8["identical"])))

        sp = os.path.join(p8, "skippath")
        os.makedirs(sp, exist_ok=True)
        for n in ("zero1.png", "zero2.png"):
            open(os.path.join(sp, n), "wb").close()
        run("scan-empty", "--root", sp, "--out", out("e8"),
            "--skip-paths", os.path.join(sp, "zero1.png"))
        re8 = load(out("e8"))
        names = {os.path.basename(f["path"]) for f in re8["files"]}
        check("--skip-paths 排除指定文件", names == {"zero2.png"}, str(names))

        pf = os.path.join(p8, "prefix")
        os.makedirs(pf, exist_ok=True)
        for n in ("x.txt", "y.txt"):
            with open(os.path.join(pf, n), "w", encoding="utf-8") as f:
                f.write("prefix-test")
        run("scan-dupes", "--root", pf, "--out", out("p8"), "--ext", "txt", "--prefix", "8")
        rp = load(out("p8"))
        check("--prefix 8 仍查出重复", rp["group_count"] == 1, str(rp["group_count"]))

        run("clean", "--report", out("e8"), "--apply", "--batch", "1",
            "--trash-dir", os.path.join(base, "__r8d__"))
        check("--batch 1 可用（zero2 已删）", not os.path.exists(os.path.join(sp, "zero2.png")))

        rm = load(out("mirror"))
        run("clean", "--report", out("mirror"), "--apply", "--include-mirror",
            "--trash-dir", os.path.join(base, "__r8e__"))
        mirror_ok = True
        for g in rm["groups"]:
            if len([p for p in g["paths"] if os.path.exists(p)]) != 1:
                mirror_ok = False
        check("--include-mirror 后镜像组仅剩 1 份", mirror_ok, str(rm["groups"])[:160])

        print("\n=== 9) scan-build 构建产物分级 ===")
        p9 = os.path.join(base, "p9")
        for n, sz in (("node_modules", 5000), (os.path.join("app", "build"), 20000),
                      (".gradle", 3000), (os.path.join("src", "__pycache__"), 800)):
            d = os.path.join(p9, n)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "x.bin"), "wb") as f:
                f.write(b"x" * sz)
        for n, sz in (("public", 9000), ("dist", 4000)):
            d = os.path.join(p9, n)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
                f.write("a" * sz)
        os.makedirs(os.path.join(p9, "src"), exist_ok=True)
        with open(os.path.join(p9, "src", "main.py"), "w", encoding="utf-8") as f:
            f.write("print(1)")

        plist = os.path.join(base, "p9_paths.txt")
        run("scan-build", "--root", p9, "--out", out("b9"), "--emit-paths", plist)
        rb = load(out("b9"))
        check("scan-build 可重建=4", rb["safe_count"] == 4, str(rb["safe_count"]))
        check("scan-build 发布成品=2", rb["output_count"] == 2, str(rb["output_count"]))
        tiers = {d["name"]: d["tier"] for d in rb["dirs"]}
        check("public/dist 归为 output（发布成品）",
              tiers.get("public") == "output" and tiers.get("dist") == "output", str(tiers))
        check("node_modules/build 归为 safe（可重建）",
              tiers.get("node_modules") == "safe" and tiers.get("build") == "safe", str(tiers))
        check("源码目录 src 未被误判为构建产物", "src" not in tiers, str(tiers))
        plines = [l.strip() for l in open(plist, encoding="utf-8") if l.strip()]
        check("--emit-paths 默认排除发布成品",
              len(plines) == 4 and all("public" not in l and "dist" not in l for l in plines),
              str(plines))
        run("clean", "--report", out("b9"))
        check("clean(build) 预演不改动",
              os.path.isdir(os.path.join(p9, "node_modules")) and os.path.isdir(os.path.join(p9, "public")))
        run("clean", "--report", out("b9"), "--apply", "--trash-dir", os.path.join(base, "__r9__"))
        check("clean(build) 默认删 safe、保留 public",
              not os.path.isdir(os.path.join(p9, "node_modules")) and os.path.isdir(os.path.join(p9, "public")),
              "safe 删除/成品保留失败")
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print("\n" + "=" * 50)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for n, d in FAIL:
        print("  FAIL: %s  %s" % (n, d))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
