---
name: file-duplicate-cleanup
description: 扫描并清理目录里的垃圾文件——重复文件（完全重复/双格式/版本冗余/**构建镜像**）/ 0 字节空文件 / 空文件夹 / 大文件 / 临时垃圾 / **构建产物（node_modules/build/.gradle 等，按可重建与发布成品分级）**。内置零依赖工具 fclean.py，默认只扫不动，删除一律走回收站。适用于"找重复文件/清理多余文件/清空文件/0KB 文件/空文件夹/大文件占空间/临时垃圾/清理构建产物/清理垃圾"类需求。
agent_created: true
version: 1.3.0
---

# 文件清理（重复文件 / 空文件·空文件夹 / 大文件 / 临时垃圾 / 构建产物）

## 何时用

用户说"找重复文件""哪些文件是多余的""清理重复""清空文件 / 0KB 文件 / 空文件夹 / 清理垃圾"时用。**先查后动**——除非用户明确授权删除，否则只出清单不动手。

## ⚠ 与 `file-organizer` 的分工（别混）

两个 skill 名字像、都碰"重复文件/空目录"，但**干的是两件事**：

| 你要干的活 | 用哪个 |
|---|---|
| **删掉**重复/空文件/空文件夹/大文件/临时垃圾/构建产物，腾空间 | **本 skill（file-duplicate-cleanup）** |
| 把散落文件**搬对位置**、按主题分类、改可读名、PDF/OCR 重命名 | `file-organizer` |

一句话：**我这边"扔垃圾"，那边"归位"**。两者是前后工序（先整理、后清理），本 skill 不负责分类 / 重命名 / OCR。

## 工具：`scripts/fclean.py`（已实测，56 项自测全过）

零依赖单文件工具，默认**只扫不动**。推荐解释器（含 Send2Trash，回收站更稳）：
`C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe`

```bash
PY="C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
FC="<skill目录>/scripts/fclean.py"

# 空文件(0字节) + 空文件夹
"$PY" "$FC" scan-empty --root "C:/Users/Administrator" --out out_empty.json

# 重复文件（大小分组->inode去重->前缀哈希->全量哈希，fclones 式）
"$PY" "$FC" scan-dupes --root "<dir>" --ext md,py,json --out out_dupes.json

# 目录级重复（重复文件夹 + 目录对聚合）
"$PY" "$FC" scan-dirdupes --root "<dir>" --out out_dir.json

# 文本相似度（查版本冗余）
"$PY" "$FC" scan-similar --root "<dir>" --threshold 0.8 --out out_sim.json

# 大文件（占空间大户）
"$PY" "$FC" scan-big --root "<dir>" --min-size 100MB --top 50 --out out_big.json

# 临时/垃圾文件（编辑器锁文件/下载未完成/临时备份/系统垃圾/pyc/日志，带年龄过滤）
"$PY" "$FC" scan-temp --root "<dir>" --min-age-days 30 --out out_temp.json

# 构建产物（node_modules/build/.gradle/__pycache__ 等，按可重建 / ⚠发布成品 两级分）
"$PY" "$FC" scan-build --root "<dir>" --out out_build.json --emit-paths build_safe.txt

# 删除（默认预演；--apply 才动）
"$PY" "$FC" clean --report out_empty.json                        # 预演，不改动
"$PY" "$FC" clean --report out_empty.json --apply \
       --with-empty-dirs --trash-dir "D:/.../__回收站__" --use 清理   # 项目内->回收目录
"$PY" "$FC" clean --report out_dupes.json --apply                 # 散落文件->系统回收站，每组留1份
"$PY" "$FC" clean --report out_dir.json --apply --delete-identical-dirs
"$PY" "$FC" clean --report out_build.json --apply --trash-dir "<同盘目录>/__回收站__"
       # 构建产物：默认只删「可重建」，⚠发布成品需 --include-output 才动

# 按「路径清单」删整目录/文件（构建产物等不在扫描报告里的目标；清单一行一个、# 开头为注释）
"$PY" "$FC" clean-paths --list paths.txt --exclude "public,\.workbuddy"            # 预演
"$PY" "$FC" clean-paths --list paths.txt --exclude "public" --apply \
       --trash-dir "<同盘目录>/__回收站__" --use 清理                                # 快通道
```

- 自测：`"$PY" "<skill目录>/scripts/selftest.py"`（56 项，连跑 3 轮稳定）；回收站通道：`selftest_recycle.py`。
- **`clean-paths`**（按清单删整目录）：吃一份路径清单（`--list` 一行一个，或 `--paths` 逗号分隔），自动**去重 + 去掉被其它目标包含的项**，支持 `--exclude`（子串排除，如 `public,\.workbuddy`）。用于删构建产物这种"不在扫描报告里"的目标——比手工逐条删更安全可控。
- **`scan-build` 构建产物分级（找空间大户最有效）**：遍历时遇到构建目录就记录体积并**停止下钻**（省时），按两级分——`safe`=可重建（`node_modules`/`build`/`.gradle`/`__pycache__`/`.next`…，装依赖或编译即可再生）、`output`=⚠发布成品（`public`/`dist`/`_site`/`out`，删了站点先打不开）。
  - `--emit-paths build_safe.txt` 直接吐出可喂 `clean-paths` 的清单，**默认排除 output 级**；要连成品一起写加 `--include-output`。
  - `clean --report out_build.json` 默认也只删 safe，`--include-output` 才动成品。
  - 实测：某项目堆 **22 个构建目录 / 2.41 GB**（可重建 2.34 GB）——**这是"重复文件"扫描完全查不到的空间**。
- **构建镜像识别（关键）**：工具自动把「源码↔产物」重复（`source`/`static`/`themes` ↔ `public`/`dist`/`_site`，去标记后后缀相同）标为 `build-mirror`，汇总里单列**真实可省（排除镜像）**；`clean` **默认不删镜像组**（要删加 `--include-mirror`）。实测某项目虚报可省 1589 MB，排除镜像后真实仅 1.69 MB——没有这层识别，报告会严重误导。
- 关键参数：`--keep first|newest|oldest|shortest`、`--min-size`、`--skip-names`、`--skip-paths`、`--strict`（目录指纹纳入内容哈希）、`--all-levels`、`--exclude-mirror`、`--min-age-days`、`--cats`、`--tail-size`、`--cache`、`--link`。
- **学自 fclones 的三处优化（v1.3.0）**：
  - **末尾哈希剪枝**（`--tail-size`，默认 4096，`0`=关闭）：开头相同但结尾不同的文件在这层被淘汰，不用整篇重读。
  - **哈希缓存**（`--cache <db>`，**默认关闭**）：SQLite 存哈希，只要文件的 size+mtime 没变就复用。**实测边界（重要，别乱开）**：大文件/慢盘收益巨大（2×200MB 相同文件 `0.70s → 0.00s`）；但**小目录反而亏**——只有 2 组重复时 `0.0s → 0.1s`（查库开销 > 重算哈希）。→ **只在反复扫同一批大文件时开**。
  - **硬链接去重**（`clean --link hard`）：副本路径全保留、磁盘只占一份（`os.link()`，零依赖）。**⚠ 硬链接不是备份**——改其中一个另一个也跟着变；**跨驱动器必然失败**（实测 `WinError 17`），工具会检测并跳过、不动原文件。
- 删除通道：给了 `--trash-dir` 就移进该目录的 `<use>_<日期>/`（**同盘改名，秒完成、可还原**）；否则走**系统回收站**（send2trash → 同盘改名到非隐藏临时名再删 → ctypes `SHFileOperationW` 兜底，失败自动还原）。
- **返回值是元组**：`recycle_delete(p)` 返回 `(ok, detail)`（如 `(True,'stage+send2trash')`）。调用方判成功必须取 `r[0]`，**不要 `if r:`**（元组非空恒为真，会把失败当成功）。
- **性能红线（2026-09-16 实测）**：系统回收站对**文件数多**的目录极慢——2000 文件/39MB 耗时 **114.6s**（≈57ms/文件，Windows 逐个入 $Recycle.Bin）；而**同盘 `--trash-dir` 移动同样的目录只要 0.002s**（差 5.7 万倍）。
  → **删整目录 / 批量 >200 文件：优先 `--trash-dir`（同盘改名）；若坚持进系统回收站，务必 `run_in_background` 且别设短超时**（前台易被 SIGTERM 打断，只删一半）。

## 核心原则

1. **分层下结论**：不要只靠哈希。哈希只能查出一模一样的，往往 0 结果就误判"没有重复"。真正的重复多在「同内容双格式」和「版本冗余」。
2. **隔离不硬删**：本环境 `Remove-Item` 会被 safe-delete 守卫拦截（exit 1、不真删）。统一 `Move-Item` 到 `__回收站__\<用途>_<日期>\`（同盘移动=改名，秒完成、可恢复）。
3. **成品不删**：先问清哪些是"成品/资产"、哪些是"中间产物"，只清中间产物；拿不准的一律列出来让用户拍板。
4. **临时文件自己清**：扫描用的脚本/日志，跑完必须移进回收站，别留在项目根目录污染目录。

## 四层扫描法

### 第1层：哈希查完全重复（快，常为 0）

```powershell
$groups = @{}
Get-ChildItem -Path "<root>" -Recurse -File -Include *.txt,*.md |
  Where-Object { $_.FullName -notmatch '__回收站__|\.workbuddy' } |
  ForEach-Object {
    $h = (Get-FileHash -LiteralPath $_.FullName -Algorithm MD5).Hash
    if (-not $groups.ContainsKey($h)) { $groups[$h] = New-Object System.Collections.ArrayList }
    [void]$groups[$h].Add($_.FullName)
  }
# 输出 Count -gt 1 的组
```

### 第2层：文件名查双格式/版本冗余
按「同名不同扩展名」和「同前缀 + `_定稿`/`_草稿`/`_旧`/`-2`」分组。

### 第3层：正文相似度（最关键，能查出版本重复）
对字幕/文档类文件，先剥离格式外壳再比对：

```python
import re
from difflib import SequenceMatcher
TIME_PAT = re.compile(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*")   # 时间码
MD_PAT   = re.compile(r"^[#>\-\*\s`\|]+")                     # md 装饰
META_PAT = re.compile(r"^(\*\*(原始文件|分类|行数|版本|备注|说明|来源|用途)\*\*.*|←.*|分类：.*|行数：.*)")
# 逐行剥离后拼接，两两 SequenceMatcher(None, a, b).ratio()
# 阈值：>=0.9 铁定重复；0.8-0.9 高度雷同待判；0.45-0.8 同类话术/模板雷同
```

### 第4层：人工分类定性（不可省）
相似度只是线索，必须抽查文件内容确认关系：

| 关系 | 判定 | 处置 |
|---|---|---|
| 同内容双格式（txt/md、txt/docx） | 真重复 | 二选一 |
| 存档副本（源文件 vs 索引卡片） | 真重复 | 看用户从哪边查，问清留哪边 |
| 版本迭代（草稿→定稿） | 版本冗余 | 定稿留、草稿可清，但需用户确认 |
| 模板雷同（不同车型/场景套同一模板） | **不是重复** | 保留，删了素材就没了 |
| 同类话术共享（同品牌不同车型字幕） | **不是重复** | 保留 |

## 模式二：空文件 / 空文件夹清理（0 字节）

适用"全盘清空文件 / 0KB 文件"体检。两个空维度：**空文件**（`st_size == 0`，图片/视频/文档）与**空文件夹**（无任何子项）。

两次实战（2026-09-15/16，C 盘）沉淀的要点：

1. **别一口气全盘 walk**：机械盘全盘 `os.walk` 跑 1h53m 都没扫完（600 万文件）。改**分目录段扫**（每个顶层目录单独扫），并**跳过 `AppData`**（低价值+巨慢）；单段 5 秒出结果。
2. **Windows junction 会自指**：`Application Data`/`Local Settings`/`AppData\Local\Application Data` 等兼容性 junction 会让 naive 遍历无限递归、同一文件被计数几十次。把这些名字加进跳过名单，并对路径 normalize 折叠。实测 原始 840 条 → 真实 450 条。
3. **严格判空**：`st_size == 0`。0 字节文件**不占数据簇**（只占 MFT 记录），删它省不了空间——如实告诉用户，别夸大。
4. **删除走安全通道**：0 字节文件散落全盘，不适合 Move 进项目回收站。走 `recycle_delete`（系统回收站）；它对 `.workbuddy` 这类**隐藏点目录会失败**，此时工具会**先同盘改名到 `<父目录>/fk_stage_<hex>_<名>` 再送回收站**（秒完成，避免跨盘复制），失败自动还原。批量 ≤10，逐批核。
5. **清单会漂移**：隔天再删**必须重扫**（WorkBuddy 会话自清会让旧清单里一堆文件已消失），旧清单只作参考。
6. **AppData 不碰**：系统/应用缓存里的 0 字节文件，风险高收益低，默认排除。

## 模式三：目录级重复（重复文件夹）

Czkawka 没有、**dupeGuru 有 `FOLDERS` 扫描类型**——把一个文件夹当整体比对，找出"整目录是另一目录的副本"。对"命名重叠、备份散落"的项目群特别有用。

零依赖、纯 Python 两种画法：

1. **目录指纹** = 递归相对路径列表 + 各文件 size（可选 md5）。指纹相同的两目录 = 完全副本。适合查"整份复制/备份出来的目录"。
2. **目录级聚合**（更实用）：把模式一第 1 层查出的重复文件按**父目录**归组，统计"目录 A ↔ 目录 B 之间重复文件数"，直接暴露"这两个文件夹在互相抄"，比孤立看文件更能定性该删哪个目录。

判断口径：**整目录完全等价**才建议删；只要有一处差异，一律列入"待人工确认"，不自动动。

## 能力边界（学自 Czkawka / rmlint / dupeGuru / fclones）

覆盖：重复文件｜重复文件夹｜空文件｜空文件夹｜大文件｜临时垃圾｜构建产物。
**故意不做**：重复音乐、相似图片/视频、坏扩展名——"相似 ≠ 重复"，自动删风险大于收益。
完整对照表与"借来的巧思"见 `references/cleanup-dimensions.md`。

## 执行模板

1. 四层扫描 → 出清单
2. 清单分「真重复可清」/「版本冗余待定」/「看着像但不能清」三类
3. **列清单给用户确认**（用 AskUserQuestion 给选项，不要自作主张删）
4. 确认后 `Move-Item` 到 `__回收站__\<用途>_<日期>\`
5. 移动后校验：目标文件数 == 源文件数，成品目录文件数不变
6. 清理自己的临时脚本/日志

## 本环境坑（高频，留摘要）

- **PowerShell 长输出常被吞** → 先把结果写进日志文件，再用 Read 工具读；**不要在 Bash 里嵌 PowerShell**（会被安全策略拒）。
- **Bash 临时目录机制损坏，`cat`/`head`/`tail`/`grep`/`dirname` 全不可用** → 用 Write 落盘脚本、用 Python 或 Read 处理文本。

## 按需参考

- `references/field-notes.md` —— 本环境坑详版 + **四轮实战记录**（字幕库相似度 / 项目堆实跑 / 真删 A+C 与回收性能实测 / scan-build 分级）+ 工具实测数据。
- `references/cleanup-dimensions.md` —— 能力维度对照表、从 Czkawka / rmlint / dupeGuru / fclones 借来的巧思、以及"哪些维度故意不做"的理由。
