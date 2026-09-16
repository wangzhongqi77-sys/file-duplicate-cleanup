# 现场笔记（本环境坑 / 实战结论 / 工具实测）

> 这是 `file-duplicate-cleanup` skill 的参考资料，按需查阅，不必一次读完。

## 本环境坑

- **PowerShell 回显不稳定**：长输出常被吞。写日志再读：
  ```powershell
  $out | Out-File -FilePath "<path>\_check.log" -Encoding UTF8
  ```
  然后用 Read 工具读。
- **Bash 里调 PowerShell 会被安全策略拒**：不要在 Bash 里嵌 powershell，直接用 PowerShell 工具。
- **Bash 临时目录(/tmp)机制损坏**：写脚本用 Write 工具落盘，不要用 heredoc。
- **PowerShell 数组 `+=` 易踩坑**：用 `New-Object System.Collections.ArrayList` + `[void]$arr.Add()`。
- **Git shim 里 `dirname`/`cd`/`cat`/`head`/`tail`/`grep` 全部 command not found**：别用 shell 过滤，用 Python 读文件再判断，或把输出重定向到文件再 Read。

## 实战结论（D:\视频工作 字幕库，2026-09-01）

- 137 个 txt/md，哈希层面**零完全重复**——只有相似度分析才能查出真问题
- 99%：`征途_拍摄脚本.md` ↔ `.txt`（同内容双格式）
- 80-94%：`字幕成品\*.txt` ↔ `字幕索引库\卡片\*.md` 34 组（卡片=成品全文+元信息副本）
- 59%：`字幕开头结尾整理.md` ↔ `开头结尾汇总.txt`
- 80-90% 互相雷同：金艺30秒品牌宣传 15 个文件（5场景×3类型），**模板套的，属不可删**
- 64%：`征途1.6米420工程钢板.md` ↔ `.txt` 实为两版不同文案（钩子不同），非重复
- 给用户解释"源文件 vs 索引卡片"用大白话：txt=散装材料纸，卡片md=装了标签的档案袋，索引库=档案柜

## 工具实测（2026-09-16）

- **自测**：`scripts/selftest.py` **56 项**断言全过（6 扫描模式 + scan-build + clean/clean-paths + 预演/实删 + 中文文件名 + 等价目录删除 + 参数覆盖 + 错误用法）；`selftest_recycle.py` 验证回收站通道（普通路径、隐藏点目录均成功）。
- **性能**：`C:\Users\Administrator`（跳 AppData，媒体/文档 14652 个）**4.5 秒**；命中 349 个 0 字节文件 + 956 个空文件夹。
- **`D:\AI情报自动化`（只读）**：25 组等价目录、涉及 56 个目录；目录对聚合一眼看出 `AI整理word\宣传册_合并源 / 源文件副本 / 车型介绍` 三方互抄 95–115 个文件。
- **踩坑沉淀**：① 交付前必须"预演"，实删前逐个复核存在性；② 单元测试的价值——自测当场抓出真 bug（`clean` 访问不存在的 `--out` 属性而崩溃、嵌套等价目录重复报告、`--skip-paths` 对文件不生效）；③ 夹具里"用于测目录重复的相同目录树"会天然变成文件重复，测重复文件时必须 `--skip-names` 排除它。

### 第二轮：扩到 6 模式 + 构建镜像识别

- 新增 `scan-big`（大文件，`--min-size 100MB`）与 `scan-temp`（编辑器锁文件/下载未完成/临时备份/系统垃圾/pyc/日志，`--min-age-days` 年龄过滤 + `--cats` 分类）；`scan_tree` 加 `file_filter` 省内存。
- 修 bug：`parse_size("10KB")` 崩溃（后缀先命中 `B` 剩 `10K`）——改为先匹配两字符后缀。
- **实跑项目堆（只读）**：
  - `D:\AI情报自动化\神烦老狗项目`：1855 文件 / 62 组"重复"，但 **54 组是 hexo `source↔public` 镜像**，其余是第三方参考项目字体 + LICENSE/样板文件——**没有可安全删的重复**。
  - `D:\Projects`：24172 文件 / 5172 组、报告"可省 1589 MB"，**排除镜像后真实仅 1.69 MB**；真正的空间大户是 Android 构建产物（`棋盘\app\build`+`.gradle`）与 hugo `public`（可重建）。
  - **结论：裸重复报告会严重误导**，必须先做「构建镜像 / 构建产物」分类再下结论。

### 第三轮：真删用户项目堆 + 回收性能实测

- **真删（用户选的 A+C，避开网站 `public`）**：8 个 Android 构建目录（**199.7 MB / 2186 文件**）+ 6 个日志/0 字节文件 + 9 个空目录，全部进系统回收站；`public` 与 `.workbuddy` 主动跳过并校验仍在；项目源码完好。
- **回收性能实测（关键）**：2000 文件 / 39MB 的目录，
  - 系统回收站 `recycle_delete` → **114.6s**（通道 `stage+send2trash`，≈57ms/文件）；
  - 同盘 `move_to_trashdir`（`--trash-dir`）→ **0.002s**。
  → **删整目录/批量文件优先 `--trash-dir`**；用系统回收站时大目录必须后台跑，否则前台命令被 SIGTERM 掐断、只删一半。
- **改 `recycle_delete`**：把旧版"移到 %TEMP%（跨盘复制）再删"改为**先同盘改名到 `<父目录>/fk_stage_<hex>_<名>` 再送回收站**，避免跨盘复制；失败自动还原。
- **踩坑**：`recycle_delete()` 返回元组 `(ok, detail)`，用 `if r:` 判成败恒为真 → 必须取 `r[0]`。
- **新增 `clean-paths`**：吃「路径清单」删整目录/文件（自动去重、去被包含项、`--exclude` 子串排除）。起因：真删时目标不在任何扫描报告里，只能手写驱动脚本——固化成正式子命令。

### 第四轮：新增 scan-build（构建产物分级）

- **动机**：前三轮反复出现同一个问题——"哪些目录是垃圾"。`scan-dupes` 在带构建产物的项目目录里零收获（重复的是镜像，不能删），真正的问题是**构建产物占了几个 G**。于是把"盘构建目录"这个手做的活儿固化成子命令。
- **做法**：遍历时**遇到构建目录就记录体积并停止下钻**（省时）；按两级分类——
  - `safe` = 可重建（`node_modules`/`build`/`.gradle`/`__pycache__`/`.next` 等，装依赖或编译即可再生）；
  - `output` = ⚠ 发布成品（`public`/`dist`/`_site`/`out`，删了站点先打不开，须先重新生成）。
- **配套**：`--emit-paths` 直接吐出可喂给 `clean-paths` 的清单（**默认排除 output 级**）；`clean --report <build.json>` 默认也只删 safe，`--include-output` 才动成品。
- **实测**：`D:\Projects` 1.8 秒 / 3 个构建目录 / 1.19 GB（其中 `public` 1.11 GB 被正确判为发布成品）；`D:\AI情报自动化\神烦老狗项目` 6.6 秒 / **22 个构建目录 / 2.41 GB**（14 个可重建 = **2.34 GB**）——**这些是"重复文件"扫描完全查不到的空间**。
- **同时补测**：`--keep newest/oldest/shortest`、`--strict`、`--all-levels`、`--skip-paths`、`--prefix`、`--batch`、`--include-mirror` 全部纳入自测；补测当场抓出 **`--skip-paths` 只对目录生效、对文件无效** 的真 bug（已修）。
