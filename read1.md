# AI 记忆增强伙伴 — 最终项目架构与功能说明

本文描述当前代码的实际状态：产品定位、完整信息流、每个文件的作用、数据模型、
算法和已知限制。设计依据是根目录的
`AI 记忆增强伙伴：系统设计方案.docx`；操作步骤见 [USAGE.md](USAGE.md)。

---

## 1. 产品定位

这是一个“增强记忆而不是替代记忆”的桌面伙伴：

1. **白天采集**：手动粘贴的文字、当前窗口的屏幕文字、麦克风和系统声音。
2. **夜间整合**：把当天零散片段归纳成长期记忆图中的节点和连线。
3. **次日复习**：桌宠按遗忘规律提问，用户先回忆、再揭晓、最后反馈。
4. **反馈驱动**：记得 / 模糊 / 不记得决定下一次复习时间。

核心原则：

- **先回忆、后揭晓**：主动回忆优先于被动重读。
- **白天零打扰**：采集在后台完成，桌宠只在复习时出现。
- **隐私默认本地**：原始音频、截图、OCR 原文、真实姓名不出本机；云端只收脱敏文本。
- **图是唯一事实来源**：记忆内容和复习状态都在 `node`/`edge` 上。
- **可纠正**：节点保留来源片段，用户可以追查和修改。

---

## 2. 最终产品功能

### 2.1 白天采集

#### 手动文本

```bash
python -m backend.pipeline --text "今天高数课学了洛必达法则……"
python -m backend.pipeline --file notes.txt --date 2026-11-10 --name 张三
```

- 本地脱敏后写入 `segment(source='manual')`。
- 当前 `pipeline` 仍然“写入后立即整合”，用于里程碑 1 的手动通路演示。
- 也可以只落库不整合（后续可加 `--defer`），由 `nightly` 统一处理。

#### 屏幕读取

```bash
python -m backend.setup_local_ocr --check         # 私有引擎是否就绪
python -m backend.local_ocr --check               # 引擎/mkldnn/线程
python -m backend.screen_capture                 # 默认每 2 秒检查前台窗口
python -m backend.screen_capture --ocr-engine paddle
python -m backend.screen_capture --ocr-engine rapid
python -m backend.screen_capture --full-screen   # 整屏（隐私风险更高）
python -m backend.screen_capture --once --dry-run --show-text
```

- 默认只读当前前台窗口的**客户区**，不读整块屏幕。
- 每 2 秒截一张图，缩成 16×16 灰度图做 aHash；变化很小时直接跳过。
- 只有画面明显变化才调用**项目私有的 OCR 引擎**：
  `vendor/ocr_engines/paddle`（PaddleOCR-json，启用 MKL-DNN）或
  `vendor/ocr_engines/rapid`（RapidOCR-json）。引擎由项目自己启动隐藏
  子进程，不占用用户 Umi-OCR 的端口/设置/进程。
- 也支持 `--ocr-backend umi` 使用用户自己的 Umi HTTP 服务；
  `--ocr-backend windows` 回退到 Windows.Media.Ocr / PyWinRT。
- 密码、API key、JWT、私钥、长令牌、卡号等敏感行整行丢弃。
- 其余文本复用 `scrub.mask`：手机号/邮箱/身份证/已知姓名变占位符。
- 截图只在内存中处理，不落盘；OCR 原文不落库。
- 写入 `segment(source='screen', processed=0)`，等 `nightly` 整合。
- 默认排除密码管理器、银行/支付、无痕浏览、登录页等；可用
  `data/screen_exclusions.json` 或命令行追加。
- 支持 `--dry-run`、`--show-text`、`--verbose`、`--self-test` 手动验证。

#### 录音

```bash
python -m backend.audio_capture --list-devices
python -m backend.audio_capture --once --seconds 10
python -m backend.audio_capture --source both    # 麦克风 + 系统声音
python -m backend.audio_capture --source system  # 只看系统声音
```

- 使用 `soundcard` 同时监听默认麦克风和默认扬声器的 WASAPI 回环。
- 音频以 16 kHz 单声道 float 读入，转成 16-bit PCM 喂给 Vosk。
- Vosk 自带端点检测，负责切句和跳过静音；不额外引入 VAD。
- 同一个声音被麦克风和系统回环同时录到时，按文本相似度去重。
- 转写文本合并中文之间空格，再做敏感行丢弃和 `scrub.mask`。
- 写入 `segment(source='microphone'/'system', processed=0)`。
- 音频只在内存中，转写完成后丢弃，不保存 WAV。
- 默认模型 `models/vosk-model-small-cn-0.22`，可用 `--model-path` 换更大的
  Vosk 模型；模型目录被 gitignore。

### 2.2 夜间整合

```bash
python -m backend.nightly
python -m backend.nightly --date 2026-11-13   # 把 11-13 当作“当晚”，卡片生成到 11-14
python -m backend.nightly --offline           # 跳过 LLM 整合，只用本地模板出题
```

`nightly` 做两件事：

1. 找出所有 `segment.processed=0` 的日期，逐日调用 `memory.consolidate`。
2. 为“次日”准备复习卡片，写入 `daily_card`。

整合过程（`backend/memory.py`）：

1. 取当天未处理片段，按时间排序。
2. 第一次 LLM 调用：抽取候选记忆项（事实 / 事件 / 人物 / 概念 / 待办），
   给出 0–1 重要度。
3. 本地为每个候选检索 top-5 相似节点（当前是字符 bigram Jaccard）。
4. 第二次 LLM 调用：对每个候选决定 `create / merge / link / ignore / supersede`。
5. 在一个事务里写 `node`、`edge`、`daily_summary`，并把片段标为 `processed=1`。
6. 合并类操作记一次“自然复习”；新节点 `step=0`，次日到期。

整合失败时整体回滚，片段保持未处理，下一晚重试。

### 2.3 次日复习

```bash
python -m backend.desktop_pet
python -m backend.review_cli
```

- 桌宠优先读 `daily_card` 中今天到期的缓存卡片；缓存完整时不调用云端，启动
  是毫秒级。
- 缓存缺失或节点 `step`/内容变化时，先显示本地模板，再后台请求云端补题；
  用户已经开始作答就不会中途换题。
- 复习交互：看“类型 · 主题”标签和具体情境题 → “显示答案” → “记得 / 模糊 / 不记得” → 自动下一题。
- LLM 出题强调具体细节（人物/时间/步骤/条件/因果），避免“谈谈你对 X 的理解”式抽象问题。
- 每题下方有“重要度”：`很重要 / 重要 / 一般 / 不重要`，直接更新 `node.importance`。
- 底部“修改”按钮打开编辑窗口：标题/内容/类型/重要度，支持填写要求后 AI 改写，确认后保存。
- 选“不重要”：`due_at` 设为 `NULL`，移出复习；随后询问是否删除该节点。
- 删除节点会级联删除它的边、复习记录和缓存卡片，`daily_summary` 中的 id 也会清理。
- `review_cli` 提供同样的命令行流程，按 `1/2/3` 反馈，`s` 跳过，`q` 退出。
- 反馈写回 `node` 的复习字段，并写入 `review_log`。

### 2.4 复习算法

间隔阶梯（`backend/review.py`）：

```
step:      0   1   2   3    4    5    6
间隔(天):  1   3   7   14   30   60   120
```

反馈：

| 反馈 | step 变化 | 下次复习 | 其他 |
|---|---|---|---|
| 记得 | +1 | 新 step 对应间隔 | reps +1 |
| 模糊 | 不变 | 当前间隔的一半（至少 1 天） | 下次给更多线索 |
| 不记得 | −2（最低 0） | 次日 | lapses +1 |

每日队列（`backend/scheduler.py`）：

- 最多 8 张（可调）。
- 优先级 = `(逾期天数 / 当前间隔) × (0.5 + 重要度) × (1 + 0.2 × lapses)`。
- 题型随 step 升级：0–2 线索回忆，3 填空，4–5 情景重现，6 关联提问。
- `due_at` 只对可复习节点设置；`todo`、`person`、`day` 和重要度低于 0.3 的
  节点不进入复习。
- 新节点 `step=0`，次日到期。

### 2.5 卡片缓存

`daily_card` 是本地预生成队列：

- 每晚 `prepare_daily_cards` 为次日到期节点生成 `question`/`answer`。
- 每行记录生成时的 `step` 和 `content_hash`（标题 + 内容 sha256）。
- 第二天读取时，只有 `step` 和 `content_hash` 仍匹配才使用缓存。
- 节点被修改、合并或反馈推进后，旧缓存自动失效，回退到本地模板或后台补题。
- 同一天重复运行 `nightly` 会替换该日期的缓存，不会重复累积。
- LLM 出题失败时写入本地模板卡片，保证第二天仍有题可复习。

### 2.6 节点编辑

桌宠卡片底部的“修改”按钮会打开 `EditNodeDialog`，可以直接编辑并保存，
也可以在“AI 改写”输入框里填写要求，点击后由 LLM 改写并回填，再提交。

用户也可以通过命令行或 Python API 主动修改记忆节点：

```bash
python -m backend.node_edit --list
python -m backend.node_edit --id 3 --show
python -m backend.node_edit --id 3 --title "新标题" --content "新内容"
python -m backend.node_edit --id 3 --importance 0.9 --type concept
python -m backend.node_edit --id 3 --instruction "把这条记忆改得更简洁"
python -m backend.node_edit --id 3 --delete
```

- 直接修改：标题、内容、类型、重要度、状态。
- LLM 修改：用户给自然语言要求，LLM 返回完整替换节点；节点和指令都会先
  本地脱敏，占位符保持不变。
- 修改标题/内容/类型会让该节点的 `daily_card` 缓存失效。
- 重要度 < 0.3 或状态非 `active` 会清除 `due_at`，移出复习。
- 删除节点会级联删除边、复习记录和缓存卡片；`daily_summary.node_ids` 同步清理。
- Python 接口：`backend.node_edit.edit_node` / `update_node` /
  `edit_node_with_llm`。

### 2.7 手动截图与文档导入

- 桌宠运行时按 `Ctrl+Alt+E` 对当前前台窗口做一次手动 OCR，走屏幕读取的
  排除规则、本地 OCR、脱敏后写入 `segment(source='screen')`。
- 右键桌宠也有“手动截图”菜单项；快捷键被占用时仍可用菜单。
- 支持导入 `.pdf`、`.docx`、`.txt`、`.md`：
  - 文件可以位于任意目录；`--pick` 打开系统文件选择器。
  - 文本 PDF 用 `pypdf` 提取；扫描版 PDF 没有文本层会提示先 OCR。
  - 文档按约 4000 字符切块，每块走脱敏 → segment → 整合。
  - 导入专用云端超时为至少 90 秒，避免长公式页在 30 秒默认值下超时。
  - 单块整合失败不会中断整份文档：失败块保留为 `processed = 0`，
    后续块继续导入，最后由 `nightly` 重试。
  - `--defer` 只写入 segment，交给 `nightly`。
  - `--dry-run` 只提取/切块，不写库、不调用 LLM。

### 2.8 控制中心

`python -m backend.control_center` 或双击 `start_center.bat` 打开集中界面：

- **概览**：节点/到期/待整合/卡片统计；打开桌宠、手动输入、导入、nightly。
- **记忆库**：搜索、表格、详情、编辑/删除；复用 AI 改写对话框。
- **采集**：屏幕读取启停（引擎/间隔/整屏）、录音启停、手动截图、音频设备。
- **功能说明**：集成能力与使用顺序。

屏幕/录音在控制中心里作为后台 `QProcess` 运行，关闭窗口会停止。

---

## 3. 总体架构

### 3.1 三段式

```
┌──────────────────────────── 白天（本机） ────────────────────────────┐
│  手动文本       屏幕截图          麦克风/系统声音                     │
│     │              │                    │                            │
│     ▼              ▼                    ▼                            │
│  pipeline     screen_capture       audio_capture                     │
│     │              │                    │                            │
│     └──────────────┴──────────┬─────────┘                            │
│                                ▼                                     │
│                         capture_text                                 │
│              （归一化 / 敏感行丢弃 / 脱敏 / 去重）                    │
│                                │                                     │
│                                ▼                                     │
│                     segment(processed=0)                             │
└────────────────────────────────┼──────────────────────────────────────┘
                                 │
┌──────────────────────────── 夜间（本机 + 云端） ─────────────────────┐
│                                ▼                                     │
│                           nightly                                     │
│                ┌───────────────┴───────────────┐                     │
│                ▼                               ▼                     │
│       memory.consolidate               prepare_daily_cards           │
│   （2 次 LLM + 本地 top-5 检索）       （1 次 LLM + 本地兜底）        │
│                │                               │                     │
│                ▼                               ▼                     │
│        node / edge / day                 daily_card（次日）           │
└────────────────┼───────────────────────────────┼─────────────────────┘
                 │                               │
┌────────────────┼──────────── 次日（本机交互） ─┼─────────────────────┐
│                ▼                               ▼                     │
│          复习调度 scheduler  ←──────── 桌宠 / review_cli              │
│                │                               │                     │
│                ▼                               ▼                     │
│           node.review 字段                 review_log                 │
└──────────────────────────────────────────────────────────────────────┘
```

### 3.2 进程与线程

| 组件 | 运行方式 | 说明 |
|---|---|---|
| `screen_capture` | 独立进程，主循环 | 每 2 秒截屏+哈希；变化时才 OCR |
| `audio_capture` | 独立进程 | 每个音源一个线程；Vosk 识别在当前线程；主线程负责入库 |
| `nightly` | 一次性 CLI | 逐日整合，再生成次日卡片 |
| `desktop_pet` | Qt 主线程 | 界面渲染与交互 |
| `CardWorker` | 一次性 QThread | 只在缓存缺失时后台补题；缓存完整时不做网络请求 |
| `review_cli` | 一次性 CLI | 同步逐题交互 |

### 3.3 隐私边界

```
本机                                     云端
────────────────────────────────────────────────────────
原始音频/截图/OCR 原文/转写原文   ──X──   不上传
真实姓名、手机号、邮箱、身份证    ──X──   不上传（只在本机 placeholder_map）
脱敏后的 segment 文本             ────→   nightly 整合时发送
到期节点的脱敏内容                ────→   出题时发送
```

---

## 4. 详细信息流

### 4.1 手动文本

```
命令行文字/文件
   │
   ▼
pipeline.ingest_text
   │
   ├─ scrub.mask(text, known_names, existing)
   │     ├─ 身份证/手机/邮箱 → [身份证1]/[手机1]/[邮箱1]
   │     ├─ 银行卡号 → 直接删除
   │     └─ 已知姓名 → [人物1]
   │
   ├─ save_entity_map → placeholder_map（只在本机）
   ├─ INSERT segment(source='manual', processed=0)
   └─ memory.consolidate(date)  ← 当前立即整合
```

### 4.2 屏幕

```
每 2 秒
   │
   ▼
foreground_window_info
   ├─ GetForegroundWindow / GetClientRect / ClientToScreen
   ├─ 窗口标题 + 进程名
   └─ 命中排除规则？──是──→ 跳过
   │否
   ▼
截取整块屏幕 → 裁剪到前台窗口客户区
   │
   ▼
16×16 aHash 与上一帧比较
   ├─ 差异 ≤ 阈值 → 跳过，不 OCR
   └─ 差异大
        │
        ▼
私有 PaddleOCR-json（mkldnn）/ RapidOCR-json 子进程
        │
        ▼
normalize_captured_text（去中文间空格）
        │
        ▼
drop_sensitive_lines（密码/key/JWT/卡号整行丢弃）
        │
        ▼
scrub.mask（手机/邮箱/身份证/姓名 → 占位符）
        │
        ▼
与最近 5 条屏幕片段做 bigram 相似度去重
        │
        ▼
INSERT segment(source='screen', processed=0)
```

### 4.3 录音

```
麦克风 ──┐
         ├─ soundcard 16 kHz 单声道 float
系统回环 ─┘
         │
         ▼
每 0.1 秒 PCM（int16）
         │
         ▼
Vosk KaldiRecognizer（每个音源一个）
         │
         ├─ 端点检测切句
         └─ Result()/FinalResult() → 文本
         │
         ▼
normalize_captured_text
         │
         ▼
drop_sensitive_lines
         │
         ▼
scrub.mask
         │
         ▼
与最近 5 条 microphone/system 片段去重
         │
         ▼
INSERT segment(source='microphone'/'system', processed=0)
```

### 4.4 夜间整合

```
nightly.run_nightly
   │
   ├─ SELECT DISTINCT date FROM segment WHERE processed=0
   │
   ├─ 对每个日期 memory.consolidate(date)
   │     ├─ 取未处理片段
   │     ├─ LLM #1：抽取候选（type/title/content/importance）
   │     ├─ 本地相似节点 top-5（bigram Jaccard）
   │     ├─ LLM #2：create / merge / link / ignore / supersede
   │     ├─ 事务写 node / edge / daily_summary
   │     ├─ segment.processed=1
   │     └─ 新节点 step=0，次日到期
   │
   └─ prepare_daily_cards(base_date + 1)
         ├─ select_due(次日)
         ├─ LLM：为到期节点生成题目/答案
         ├─ 失败则 _fallback 本地模板
         └─ 事务 DELETE + INSERT daily_card
```

### 4.5 次日复习

```
desktop_pet / review_cli
   │
   ▼
get_today_queue(today)
   │
   ├─ select_due(today)：status=active、due_at<=today、排除 day/todo
   ├─ _load_cached_cards(daily_card)
   │     └─ 只接受 step 和 content_hash 匹配的行
   ├─ 缓存缺失 → LLM 只补缺失节点（桌面端先显示本地模板）
   └─ 返回 Card 列表（source=cache/llm/local）
   │
   ▼
用户：看题 → 显示答案 → 反馈
   │
   ▼
submit_feedback(node_id, card_type, feedback)
   ├─ review.apply_feedback
   ├─ UPDATE node（step/interval/due_at/reps/lapses）
   └─ INSERT review_log
```

---

## 5. 数据模型

数据库默认 `data/memory.db`，SQLite WAL，外键开启。

### 5.1 `segment` — 当日片段 / 待整合文本

| 字段 | 说明 |
|---|---|
| `id` | 主键 |
| `ts_start`, `ts_end` | 片段起止时间 |
| `text_masked` | 脱敏后的文本 |
| `source` | `manual` / `screen` / `microphone` / `system` |
| `processed` | 0 未整合，1 已整合 |

### 5.2 `node` — 记忆节点

| 字段 | 说明 |
|---|---|
| `type` | `fact` / `event` / `person` / `concept` / `todo` / `day` |
| `title`, `content` | 节点内容（脱敏） |
| `importance` | 0–1 重要度 |
| `status` | `active` / `archived` / `superseded` |
| `source_segment_ids` | 来源片段，便于溯源 |
| `step` | 复习阶段 0–6 |
| `interval_days` | 当前间隔 |
| `due_at` | 下次到期日；不可复习节点为 NULL |
| `last_reviewed_at`, `reps`, `lapses` | 复习统计 |

### 5.3 其他表

| 表 | 作用 |
|---|---|
| `edge` | `related` / `part_of` / `causes` / `same_as` |
| `daily_summary` | 每天摘要、主题、节点 id |
| `review_log` | 每次反馈：card_type、feedback、step_before/after |
| `placeholder_map` | 占位符 ↔ 真实值，只在本机 |
| `consolidation_report` | 每晚整合报告 |
| `daily_card` | 预生成的次日卡片（review_date + node_id 主键） |

### 5.4 图不变量

- 每个日期一个 `day` 节点；所有记忆节点都有 `part_of` 边连到当天 day。
- 只有 `active` 节点参与检索和复习。
- `todo`、`person`、`day` 和重要度 < 0.3 的节点不设 `due_at`。
- 新可复习节点 `step=0`，次日到期。
- 合并、删改都在事务中完成；失败回滚，片段保持未处理。

---

## 6. 各个文件的作用

### 6.1 `backend/` 核心模块

| 文件 | 作用 | 关键内容 |
|---|---|---|
| `__init__.py` | Python 包标记 | — |
| `config.py` | 运行时配置 | 加载 `.env`；`LLMConfig`；`get_db_path()`；`configure_stdio()`；`ROOT_DIR` |
| `db.py` | SQLite 存储 | `SCHEMA`；`connect()`；`init_db()`；WAL/外键；所有表 |
| `scrub.py` | 本地脱敏 | `mask()`：身份证/邮箱/银行卡/手机/已知姓名 |
| `capture_text.py` | 采集文本共享清洗 | `normalize_captured_text`；`drop_sensitive_lines`；`text_similarity`；`ingest_captured_text` |
| `pipeline.py` | 手动文本入口 | `ingest_text`；`load_entity_map`/`save_entity_map`；`--text/--file/--list-nodes` |
| `memory.py` | 夜间整合核心 | `consolidate`；两次 LLM 调用；`Candidate`/`Decision`/`ConsolidationReport`；图写入；自然复习 |
| `review.py` | 间隔算法 | `INTERVALS`；`apply_feedback`；`ReviewUpdate`；`natural_review_update` |
| `scheduler.py` | 复习调度 + 卡片缓存 | `select_due`；`prepare_daily_cards`；`get_today_queue`；`submit_feedback`；`set_node_importance`；`delete_node`；`DueNode`/`Card`/`PreparationReport` |
| `nightly.py` | 夜间编排 | `run_nightly`；逐日整合；生成次日卡片；`NightlyReport` |
| `review_cli.py` | 命令行复习 | 题目 → 答案 → 1/2/3 反馈；`--list`/`--offline` |
| `pet_controller.py` | 桌宠纯状态机 | `PetController.load/current/reveal/feedback/skip/set_importance/delete_current/progress`，不依赖 Qt |
| `desktop_pet.py` | PyQt6 复习伙伴 | `CardWorker`；`EditWorker`；`EditNodeDialog`；`HotkeyWorker`；`ScreenshotWorker`；`DesktopPet`；540×600、日期栏、主题标签、控制中心风格卡片；缓存优先、后台补题；重要度/修改/删除/手动截图 |
| `demo.py` | 演示数据 | 重建 `data/demo_start.db`，写入 3 个今日到期节点和卡片 |
| `screen_capture.py` | 屏幕读取 | 前台窗口客户区；aHash；私有 OCR（Umi/Windows 备用）；排除规则；`ScreenReader`；CLI |
| `local_ocr.py` | 私有 OCR 引擎 | 启动 PaddleOCR-json/RapidOCR-json 子进程；stdin/stdout JSON；mkldnn；CLI |
| `setup_local_ocr.py` | 私有引擎安装 | 从 Umi 插件目录复制到 `vendor/ocr_engines/`；`--check` |
| `umi_ocr.py` | 可选 Umi 客户端 | HTTP `/api/ocr`；引擎检测/选择；mkldnn 读取与写入；CLI |
| `audio_capture.py` | 录音链路 | `soundcard` 麦克风/回环；Vosk；`CaptureWorker`；`transcribe_wav`；CLI |
| `node_edit.py` | 节点编辑接口 | `update_node`；`edit_node_with_llm`；`edit_node`；`node_detail`；CLI |
| `import_doc.py` | 文档导入 | `extract_text`；`split_text`；`import_file`；PDF/DOCX/TXT/MD |
| `control_center.py` | 集中控制中心 | 概览统计、记忆库、采集进程、导入、nightly、功能说明 |
| `llm.py` | 云端 LLM | `DeepSeekClient`（stdlib `urllib` HTTP）；`LLMError`；`parse_json` |
| `memory_graph.py` | 旧原型（不要扩展） | networkx + JSON，schema 与设计不一致 |
| `test.py` | 旧原型脚本（不要扩展） | 旧版演示 |

### 6.2 测试

| 文件 | 覆盖 |
|---|---|
| `tests/conftest.py` | 临时 SQLite `conn` fixture |
| `test_scrub.py` | 脱敏、占位符稳定性 |
| `test_db.py` | schema、幂等初始化 |
| `test_review.py` | 间隔阶梯、三种反馈 |
| `test_llm.py` | JSON 解析容错 |
| `test_memory.py` | 抽取/决策/建图/合并/自然复习 |
| `test_pipeline.py` | 手动文本入库、映射持久化 |
| `test_scheduler.py` | 到期选择、优先级、卡片缓存、部分缓存、LLM 补题、重要度、节点删除 |
| `test_node_edit.py` | 直接编辑、脱敏、LLM 改写、缓存失效、节点详情 |
| `test_import_doc.py` | PDF/DOCX/TXT 提取、切块、defer/LLM 导入 |
| `test_pet_controller.py` | 桌宠状态机、重要度与删除当前卡片 |
| `test_nightly.py` | 夜间整合 + 次日卡片、离线兜底、幂等 |
| `test_screen_capture.py` | 归一化、敏感行、排除规则、屏幕入库/去重 |
| `test_audio_capture.py` | 录音文本归一化、脱敏、跨音源去重、dry-run |

当前共 **108** 个测试，全部使用 FakeLLM / 假 Umi 服务 / 临时数据库，不访问网络。

### 6.3 文档与配置

| 文件 | 作用 |
|---|---|
| `README.md` | 项目简介、进度、速查命令、一键启动、每日流程 |
| `USAGE.md` | 完整安装/配置/命令/屏幕/录音/隐私/故障排查 |
| `start.bat` | Windows 一键菜单：桌宠/演示/采集/导入/记忆库/nightly/控制中心 |
| `start_demo.bat` | 直接重建演示库并打开桌宠 |
| `start_center.bat` | 直接打开集中控制中心 |
| `read1.md` | 本文：最终架构、信息流、文件和功能说明 |
| `CLAUDE.md` | 给 Claude Code 的开发约束、命令和架构摘要 |
| `requirements.txt` | Python 依赖：dotenv、networkx、pytest、PyQt6、WinRT OCR、soundcard/numpy/vosk |
| `.env.example` | DeepSeek key/模型/超时示例 |
| `.gitignore` | 忽略 `.env`、`data/`、`models/`、`vendor/`、`*.db` 等 |
| `pytest.ini` | `pythonpath=.`、`testpaths=tests` |
| `AI 记忆增强伙伴：系统设计方案.docx` | 权威产品/架构设计文档 |

### 6.4 本地目录

| 目录 | 内容 |
|---|---|
| `data/` | `memory.db`、沙盒库、测试 WAV、屏幕排除规则（gitignored） |
| `models/` | Vosk 中文模型（gitignored） |
| `vendor/ocr_engines/` | 项目私有 Paddle/Rapid OCR 引擎（gitignored，MIT） |
| `memory_graph.py/` | 历史空目录，与 `backend/memory_graph.py` 无关 |

---

## 7. 关键算法

### 7.1 相似检索

当前没有本地 embedding，使用字符 bigram Jaccard：

```
A = {A[i:i+2]}
B = {B[i:i+2]}
similarity = |A ∩ B| / |A ∪ B|
```

用途：

- 夜间整合为每个候选找 top-5 相似旧节点。
- 屏幕/录音片段去重（最近 5 条）。
- 麦克风和系统声音的同一句话去重。

设计文档中的本地 embedding 模型尚未接入，是后续替换点。

### 7.2 图像变化检测

- 截屏缩放到 16×16 灰度。
- 以平均亮度为阈值生成 256 位 aHash。
- 两帧汉明距离 ≤ 10 视为没变化，跳过 OCR。
- 只有变化明显才 OCR，降低 CPU 占用。
- 即使图像变化，文本相似度去重还能挡住光标、时间等细小变化。

### 7.3 卡片缓存有效性

```
cached.step == node.step
AND cached.content_hash == sha256(node.title + "\n" + node.content)
```

任一不满足就视为失效，重新生成或回退本地模板。

### 7.4 复习反馈

```
remembered: step += 1; interval = INTERVALS[step]; reps += 1
fuzzy:      step 不变; interval = max(1, interval // 2)
forgot:     step = max(0, step - 2); interval = 1; due = 明天; lapses += 1
```

---

## 8. 运行与验证

### 8.1 安装

```bash
python -m pip install -r requirements.txt
# SSL 失败时：
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 8.2 模型

- 屏幕 OCR：项目私有 PaddleOCR-json（启用 MKL-DNN）或 RapidOCR-json；
  用 `python -m backend.setup_local_ocr --check` 检查。也可用
  `--ocr-backend umi` 或 `--ocr-backend windows`。
- 录音 ASR：Vosk 中文小模型，下载步骤见 [USAGE.md](USAGE.md)。

### 8.3 最小验证

```bash
python -m backend.pipeline --text "今天高数课学了洛必达法则……"
python -m backend.pipeline --list-nodes
python -m backend.nightly --offline --db data/manual_test.db
python -m backend.screen_capture --self-test
python -m backend.audio_capture --wav data/asr_test.wav --dry-run --show-text
python -m backend.desktop_pet --db data/manual_test.db
python -m pytest -q
```

### 8.4 端到端验证结果

- 手动文本：脱敏 → 整合 → 建节点 → 次日卡片。
- 屏幕：受控窗口截图 → 私有 PaddleOCR-json/RapidOCR-json → 脱敏 →
  `segment(source='screen')` → `nightly` 建节点 → 生成卡片。
- 录音：TTS WAV → Vosk 转写 → 脱敏 → `segment(source='microphone')`
  → `nightly` 建节点 → 生成卡片。
- 桌宠：缓存完整时约 12 ms 加载，无云端请求；缓存缺失时本地模板先显示。
- 测试：**108 passed**。

---

## 9. 当前能力边界

### 9.1 已实现

- 手动文本整合
- 屏幕读取基础版（前台窗口、2 秒轮询、私有 Paddle/Rapid 引擎、排除规则）
- 录音基础版（麦克风 + 系统回环、Vosk、端点检测、跨音源去重）
- 夜间整合（抽取 → 本地检索 → 决策 → 写图）
- 次日卡片预生成（`daily_card`）
- 间隔复习、三按钮反馈、`review_log`
- 重要度标记（很重要/重要/一般/不重要）与“不重要”节点删除
- 节点编辑接口：直接字段修改 + LLM 自然语言改写
- 手动截图快捷键与 PDF/DOCX/TXT/Markdown 导入
- 集中式 PyQt6 控制中心
- PyQt6 复习伙伴（缓存优先、后台补题、日期栏、控制中心风格卡片）
- 本地脱敏、占位符映射、敏感行丢弃
- 108 个离线测试

### 9.2 未实现 / 后续路线

1. **Whisper / faster-whisper**：替换 Vosk 小模型，提高专有名词和口音准确率。
2. **spaCy 实体识别**：目前只识别已知姓名和正则实体，地点/机构未覆盖。
3. **本地 embedding**：替换 bigram Jaccard，提高相似检索质量。
4. **`memory.query` 对话模式**：自然语言查询、引用节点、编辑/删除。
5. **托盘/开机自启/自动夜间任务**：目前 `nightly` 需要手动运行。
6. **采集暂停指示**：屏幕/录音目前是独立 CLI，没有桌宠指示灯或暂停快捷键。
7. **todo 截止提醒**：`todo` 节点已存储，但不进入复习，也没有提醒。
8. **多说话人区分**：录音不区分说话人。
9. **OCR 引擎部署**：默认私有引擎需要从 Umi-OCR 的插件目录复制到
   `vendor/ocr_engines/`（`vendor/` 已 gitignore）。Umi HTTP 后端是可选
   兼容模式：它的 HTTP 接口不能按请求换引擎，单实例要在 GUI 里切换插件，
   双实例要两个端口；Paddle 的 mkldnn 需要在插件设置或
   `UmiOCR-data/.settings` 中开启（默认 true，可 `--set-mkldnn` 写入）。
10. **屏幕采集增强**：默认只读前台窗口；桌宠置顶可能被截入画面；`--full-screen`
    会读背景窗口。

---

## 10. 完成度总结

| 模块 | 状态 |
|---|---|
| 里程碑 1：手动文本 → 脱敏 → 整合 → 图 | 完成 |
| 里程碑 2：复习闭环 + 桌宠 | 完成 |
| 次日卡片预生成 / 本地缓存 | 完成 |
| 屏幕读取基础版 | 完成 |
| 录音链路基础版 | 完成 |
| 语音/桌宠状态指示、自动夜间任务 | 未完成 |
| Whisper、spaCy、本地 embedding、对话模式 | 未完成 |

一句话总结：**采集（手动/屏幕/录音）→ 本地脱敏 → 夜间整合 → 次日预生成卡片 → 桌宠复习反馈** 的主闭环已经贯通；当前主要短板是 ASR 准确率、实体识别和自动化调度。
