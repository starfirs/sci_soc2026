# 完整使用说明

本文是 AI 记忆增强伙伴的操作手册。项目简介和速查见 [README.md](README.md)；
架构和开发约束见 [CLAUDE.md](CLAUDE.md)。

## 1. 当前能力

已经实现：

- 手动文本 → 本地脱敏 → 云端 LLM 整合 → SQLite 记忆图
- 间隔复习 + 三按钮反馈（记得 / 模糊 / 不记得）
- 夜间整合未处理片段 + 预生成次日卡片到本地 `daily_card`
- PyQt6 桌宠：显示题目、翻答案、收集反馈
- 屏幕读取基础版：读取前台窗口，项目私有 PaddleOCR-json / RapidOCR-json（Paddle 支持 MKL-DNN），脱敏后写入 `segment`
- 录音链路基础版：麦克风 + 系统声音回环，Vosk 本地转写，脱敏后写入 `segment`
- 本地 SQLite 存储、占位符映射、复习日志
- 节点查看/编辑：直接改标题、内容、类型、重要度、状态，或让 LLM 按自然语言改写

还没实现：

- 更高准确率的 Whisper 后端（当前用 Vosk 小模型）
- 对话模式 `memory.query`
- 本地 embedding（当前相似检索用字符 bigram Jaccard）
- 托盘/开机自启、自动夜间定时、屏幕采集指示灯/暂停快捷键
- todo 截止提醒、节点编辑界面

## 2. 安装

要求 Windows 10/11、Python 3.10+（当前开发环境为 3.14）。

```bash
python -m pip install -r requirements.txt
```

如果直连 PyPI 出现 SSL EOF 错误，使用清华镜像：

```bash
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

屏幕读取默认使用**项目私有的 OCR 引擎**，不需要启动 Umi-OCR。私有引擎
位于 `vendor/ocr_engines/`（已 gitignore），从 Umi-OCR 的插件目录复制：

1. 下载 Umi-OCR Paddle 版和/或 Rapid 版：
   <https://sourceforge.net/projects/umi-ocr/files/v2.1.5/>
2. 解压后找到插件目录：
   - Paddle：`UmiOCR-data/plugins/win7_x64_PaddleOCR-json`
   - Rapid：`UmiOCR-data/plugins/win7_x64_RapidOCR-json`
3. 复制到项目：
   ```bash
   python -m backend.setup_local_ocr \
     --paddle-from "D:/Umi-OCR_Paddle/UmiOCR-data/plugins/win7_x64_PaddleOCR-json" \
     --rapid-from "D:/Umi-OCR_Rapid/UmiOCR-data/plugins/win7_x64_RapidOCR-json"
   ```
4. 检查：
   ```bash
   python -m backend.setup_local_ocr --check
   python -m backend.local_ocr --check
   ```

私有引擎直接由项目启动隐藏子进程，不占用用户 Umi-OCR 的端口/设置/进程；
用户自己的 Umi-OCR 可以照常使用。Paddle 默认 `enable_mkldnn=true`，Rapid
不支持 mkldnn。

如果仍想使用用户自己的 Umi-OCR HTTP 服务：

- 启动 Umi-OCR，打开 `全局设置 → 高级`，启用 HTTP 服务（默认
  `127.0.0.1:1224`）
- 屏幕读取加 `--ocr-backend umi`
- 验证：
  ```bash
  python -m backend.umi_ocr --check
  ```

也可以用 `--ocr-backend windows` 回退到 Windows 自带 OCR；这需要系统里有
中文 OCR 语言包：`设置 → 时间和语言 → 语言和区域 → 添加语言 → 中文（简体）`，
并安装 OCR 组件。

录音使用 Vosk 本地模型，需要先下载中文小模型（约 42 MB，`models/` 已被
gitignore）：

```bash
mkdir -p models
curl -L -o models/vosk-model-small-cn-0.22.zip https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip
python -X utf8 -c "import zipfile; zipfile.ZipFile('models/vosk-model-small-cn-0.22.zip').extractall('models')"
```

小模型准确率有限；需要更好效果时可下载更大的 Vosk 模型，用
`--model-path` 指向它。

## 3. 配置

复制 `.env.example` 为 `.env`：

```bash
cp .env.example .env
```

填写：

```ini
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_AUTH_TOKEN=sk-你的key
DEEPSEEK_MODEL=deepseek-flash

# 可选
# DEEPSEEK_MAX_TOKENS=4096
# DEEPSEEK_TIMEOUT=30
```

注意：

- 模型必须是这个 key 能访问的；本项目使用 `deepseek-flash`。
- `.env` 已被 gitignore，不要提交真实 key。
- 数据库默认在 `data/memory.db`，可用 `MEMORY_DB` 或命令行的 `--db` 覆盖。
- 屏幕排除规则可放在 `data/screen_exclusions.json`（格式见第 6 节）。

查看当前 key 可用模型：

```bash
python -X utf8 -c "
import json, os, urllib.request
from dotenv import load_dotenv
load_dotenv('.env')
req = urllib.request.Request(
    os.environ['ANTHROPIC_BASE_URL'].rstrip('/') + '/v1/models',
    headers={'x-api-key': os.environ['ANTHROPIC_AUTH_TOKEN'],
             'anthropic-version': '2023-06-01'},
)
print([m['id'] for m in json.load(urllib.request.urlopen(req))['data']])
"
```

## 4. 快速开始

### 4.1 一键启动

双击仓库根目录的 `start.bat`：

1. 启动桌宠（真实数据）
2. 演示模式（自动生成 3 张今天的示例卡片）
3. 启动屏幕读取
4. 启动录音
5. 运行测试
6. 打开本说明

只想立即看桌宠效果，双击 `start_demo.bat`。它只重建
`data/demo_start.db`，不会修改真实 `data/memory.db`。

### 4.2 命令行

```bash
# 1. 粘贴一段今天的笔记，脱敏后整合进记忆图
python -m backend.pipeline --text "今天高数课学了洛必达法则，它是求解极限的重要方法。"

# 2. 查看记忆图
python -m backend.pipeline --list-nodes

# 3. 晚上预生成明天的复习卡片
python -m backend.nightly

# 4. 第二天打开桌宠复习
python -m backend.desktop_pet
```

如果只是先看复习界面，可以在 `data/manual_test.db` 这样的沙盒库里测试，
不要直接改真实库。

## 5. 每日使用顺序

1. **白天（可选）**：后台运行屏幕读取和录音
   ```bash
   python -m backend.screen_capture
   python -m backend.audio_capture
   ```
   屏幕默认每 2 秒检查一次前台窗口；画面没变时只做哈希，不做 OCR；变化时
   才识别、脱敏并写入 `segment(source='screen')`。
   录音默认同时监听麦克风和系统声音；音频只在内存中，转写后脱敏写入
   `segment(source='microphone'/'system')`。

2. **复习**：打开桌宠
   ```bash
   python -m backend.desktop_pet
   ```
   先自己回忆 → 点“显示答案” → 选“记得 / 模糊 / 不记得”。
   反馈会写回节点并安排下次复习时间。

   每题下方还有“重要度”选项：`很重要 / 重要 / 一般 / 不重要`。
   选择“不重要”会把该节点移出复习队列，并询问是否删除这条记忆；
   删除会级联删除相关边、复习记录和缓存卡片，不可恢复。

   底部“修改”按钮会打开编辑窗口：可以直接改标题/内容/类型/重要度，
   也可以填写自然语言要求后点“AI 改写”，确认无误再“保存”。

3. **晚上**：整合当天片段并生成明天的卡片
   ```bash
   python -m backend.nightly
   ```
   它会处理所有 `processed=0` 的片段（手动文本和屏幕片段都会处理），
   然后把明天到期的卡片写入本地 `daily_card`。

4. **第二天**：桌宠优先读 `daily_card`，缓存完整时不调用云端，启动是
   毫秒级；缓存缺失才回退到本地模板并在后台补题。

## 6. 屏幕读取

### 6.1 基本命令

```bash
# 检查项目私有引擎（默认 local）
python -m backend.setup_local_ocr --check
python -m backend.local_ocr --check

# 选择私有引擎（Paddle 默认启用 mkldnn）
python -m backend.screen_capture --ocr-engine paddle
python -m backend.screen_capture --ocr-engine rapid

# 可选：改用你自己的 Umi-OCR HTTP 服务
python -m backend.screen_capture --ocr-backend umi
python -m backend.umi_ocr --check

# 自检：用一个自绘窗口验证截图 + OCR，不读真实屏幕
python -m backend.screen_capture --self-test

# 只读一次，不写数据库
python -m backend.screen_capture --once --dry-run

# 只读一次并显示脱敏后的文本
python -m backend.screen_capture --once --dry-run --show-text

# 持续读取（默认每 2 秒检查一次）
python -m backend.screen_capture

# 更省资源
python -m backend.screen_capture --interval 5

# 整屏读取（会把背景窗口也读进来，隐私风险更高）
python -m backend.screen_capture --full-screen

# 没有 Umi-OCR 时回退 Windows OCR
python -m backend.screen_capture --ocr-backend windows

# 查看跳过原因
python -m backend.screen_capture --verbose
```

### 6.2 读取逻辑

- 默认只读**当前前台窗口的客户区**，不读整块屏幕。
- 每 2 秒截一次图，缩成 16×16 灰度图做 aHash；和上一帧差异很小时直接跳过。
- 只有画面明显变化时才调用本地 OCR：默认启动项目私有的
  PaddleOCR-json / RapidOCR-json 子进程（stdin/stdout JSON 管道）；也可用
  `--ocr-backend umi` 改走 Umi-OCR 的本地 HTTP。
- OCR 文本先去掉中文之间的空格，再做相似度去重（最近 5 条屏幕片段）。
- 密码、API key、JWT、私钥、长令牌、银行卡号等敏感行**整行丢弃**。
- 其余文本复用 `scrub.mask`：手机号/邮箱/身份证/已知姓名变成占位符，
  银行卡号丢弃。
- 截图只在内存里处理，不保存文件；OCR 原文不落库，只保存脱敏文本。
- 写入 `segment(source='screen', processed=0)`，不会立即整合；等 `nightly`。

### 6.3 排除规则

默认排除密码管理器、银行/支付、无痕/隐私浏览、登录页等。可以在
`data/screen_exclusions.json` 追加：

```json
{
  "title_keywords": ["公司内部系统", "我的私人页面"],
  "processes": ["someapp.exe", "another.exe"]
}
```

也可以临时追加：

```bash
python -m backend.screen_capture --exclude "私人页面" --exclude-process someapp.exe
```

标题/进程名按“包含”匹配，大小写不敏感。窗口标题或进程命中规则时，
这一轮完全不截图。

### 6.4 限制

- 默认需要先把 OCR 引擎插件复制到 `vendor/ocr_engines/`；缺失时会明确报错。
  如果不想复制，可用 `--ocr-backend umi`（需要启动 Umi-OCR）或
  `--ocr-backend windows`。
- 私有引擎由项目自己启动/关闭，不占用用户 Umi 的端口和设置；两者可以同时
  使用，但会各自占一份 CPU/内存。
- Paddle 只有 `enable_mkldnn=true` 才明显更快，但内存占用更高；Rapid 不
  支持 mkldnn，适合低配机器。可用 `--no-mkldnn` 关闭 Paddle 加速。
- 密码框、验证码、无痕浏览只能按标题/进程尽力排除，不能保证 100%。
- OCR 质量受字体、字号、DPI 和 Umi-OCR 模型影响；必要时用
  `--dry-run --show-text` 检查识别结果。
- 桌宠是置顶窗口，如果挡住前台窗口，可能被截进画面。
- 默认只读前台窗口；`--full-screen` 会读到背景内容，只在你明确需要时用。
- 目前没有托盘开关、采集指示灯、暂停快捷键；用 `Ctrl+C` 停止。

## 7. 录音链路

### 7.1 基本命令

```bash
# 列出麦克风、扬声器和回环设备
python -m backend.audio_capture --list-devices

# 转写一个 16 kHz 单声道 WAV，不写数据库
python -m backend.audio_capture --wav data/asr_test.wav --dry-run --show-text

# 只录 10 秒，转写并写入数据库
python -m backend.audio_capture --once --seconds 10

# 持续监听麦克风 + 系统声音（Ctrl+C 停止）
python -m backend.audio_capture --source both

# 只监听系统声音
python -m backend.audio_capture --source system

# 检查模型和测试音频
python -m backend.audio_capture --self-test
```

### 7.2 读取逻辑

- 默认 `--source both`：麦克风和默认扬声器的 WASAPI 回环同时监听。
- 音频以 16 kHz 单声道 float 读入，转成 16-bit PCM 后喂给 Vosk。
- Vosk 自带的端点检测负责切句和跳过静音；没有单独的 VAD 依赖。
- 同一个声音同时被麦克风和系统回环录到时，按字符 bigram 相似度去重。
- 转写文本合并中文之间的空格，再做敏感行丢弃和 `scrub.mask` 脱敏。
- 写入 `segment(source='microphone'/'system', processed=0)`，由 `nightly` 整合。
- 音频只在内存中；转写完成后立即丢弃，不保存 WAV。

### 7.3 模型与设备

默认模型：`models/vosk-model-small-cn-0.22`。下载步骤见第 2 节。
如果默认设备不对：

```bash
python -m backend.audio_capture --list-devices
python -m backend.audio_capture --mic "麦克风阵列" --speaker "扬声器"
```

`--mic` / `--speaker` 都是“名称包含”匹配。没有找到系统回环时会打印警告，
但仍然可以只录麦克风。

### 7.4 限制

- Vosk 小模型准确率有限，专有名词、口音和远场麦克风容易出错；可以换更大的
  Vosk 模型（`--model-path`），或后续接 Whisper。
- 说话没有停顿时，Vosk 可能要到停止时才输出整段文本。
- 目前是独立 CLI 进程，没有托盘、录音指示灯、暂停快捷键或开机自启。
- 系统声音回环依赖 Windows WASAPI；没有音频设备时会直接报错。
- 麦克风可能录到周围其他人的声音；首版不区分说话人。

## 8. 命令参考

### 8.1 手动文本整合

```bash
python -m backend.pipeline --text "……"
python -m backend.pipeline --file notes.txt --date 2026-11-10 --name 张三
python -m backend.pipeline --list-nodes
```

| 参数 | 说明 |
|---|---|
| `--text` | 直接粘贴的文字 |
| `--file` | 从 UTF-8 文件读取 |
| `--date` | 归入哪一天，默认今天 |
| `--name` | 需要脱敏的已知姓名，可重复 |
| `--db` | 数据库路径 |
| `--list-nodes` | 只打印当前记忆图 |

注意：`pipeline` 仍是“写入后立即整合”。屏幕片段和以后接入的录音片段
会留在 `processed=0`，由 `nightly` 统一整合。

### 8.2 复习命令行

```bash
python -m backend.review_cli
python -m backend.review_cli --list
python -m backend.review_cli --offline
```

| 参数 | 说明 |
|---|---|
| `--limit` | 最多显示几张，默认 8 |
| `--list` | 只看题目和答案 |
| `--offline` | 不调用云端，只用本地模板 |
| `--db` | 数据库路径 |

交互键：回车显示答案，`1` 记得，`2` 模糊，`3` 不记得，`s` 跳过，`q` 退出。

### 8.3 桌宠

```bash
python -m backend.desktop_pet
python -m backend.desktop_pet --offline
python -m backend.desktop_pet --db data/manual_test.db
python -m backend.desktop_pet --smoke-test --screenshot shot.png
```

| 参数 | 说明 |
|---|---|
| `--offline` | 不调用云端 |
| `--db` | 数据库路径 |
| `--limit` | 最多几张 |
| `--smoke-test` | 强制离线并自动退出，用于 UI 验证 |
| `--screenshot` | 保存窗口截图 |

桌宠优先读 `daily_card`；缓存完整时不会发网络请求。缓存缺失时先显示
本地模板，再后台请求云端；你已经开始作答就不会中途换题。

复习卡片下方有“重要度”四个按钮。设置为“不重要”后节点 `due_at` 变为
`NULL`，不再进入复习；随后可以确认删除该节点。

### 8.4 夜间任务

```bash
python -m backend.nightly
python -m backend.nightly --date 2026-11-13
python -m backend.nightly --offline
```

| 参数 | 说明 |
|---|---|
| `--date` | 把哪一天当作“当晚”，卡片生成到次日；默认今天 |
| `--offline` | 跳过 LLM 整合，只用本地模板准备卡片 |
| `--limit` | 次日最多几张，默认 8 |
| `--db` | 数据库路径 |

幂等：同一天重复运行会替换该日期的 `daily_card`，不会重复累积。
LLM 出题失败时会写入本地模板卡片，并在输出的 `errors` 里给出 warning。

### 8.5 屏幕读取

见第 6 节。核心参数：

| 参数 | 说明 |
|---|---|
| `--interval` | 画面检查间隔，默认 2 秒 |
| `--once` | 只读一次 |
| `--dry-run` | 不写数据库 |
| `--show-text` | 打印脱敏文本 |
| `--full-screen` | 整屏读取 |
| `--verbose` | 打印跳过原因 |
| `--exclude` | 追加标题排除词，可重复 |
| `--exclude-process` | 追加进程名，可重复 |
| `--exclusions` | 自定义排除规则 JSON |
| `--name` | 额外脱敏姓名，可重复 |
| `--ocr-backend` | `local`（默认）/ `umi` / `windows` |
| `--ocr-engine` | `auto` / `paddle` / `rapid` |
| `--ocr-engine-dir` | 私有引擎目录 |
| `--no-mkldnn` | 本地 Paddle 不启用 mkldnn |
| `--cpu-threads` | 本地 OCR 线程数 |
| `--umi-url` | Umi-OCR 基础地址（`--ocr-backend umi`） |
| `--umi-paddle-url` / `--umi-rapid-url` | 双实例时各自的地址 |
| `--umi-home` | Umi-OCR 根目录，用于核对 mkldnn |
| `--umi-timeout` | Umi-OCR HTTP 超时 |
| `--no-require-mkldnn` | 允许 Umi Paddle 不启用 mkldnn |
| `--min-chars` / `--max-chars` | 最短/最长入库字符数 |
| `--self-test` | 截图 + OCR 自检 |

### 8.6 测试

```bash
python -m pytest -q
```

测试使用假 LLM / 假 Umi-OCR 服务 / 临时引擎目录，不消耗 API。当前 102 个测试。

### 8.7 节点编辑

桌宠里也可以直接点卡片底部的“修改”按钮；下面是命令行/脚本接口。

```bash
# 查看节点列表
python -m backend.node_edit --list

# 查看某个节点的详细信息（含边）
python -m backend.node_edit --id 3 --show

# 直接修改字段
python -m backend.node_edit --id 3 --title "新标题" --content "新内容"
python -m backend.node_edit --id 3 --importance 0.9 --type concept
python -m backend.node_edit --id 3 --status archived

# 从文件读取长内容
python -m backend.node_edit --id 3 --content-file notes.txt

# LLM 按自然语言改写
python -m backend.node_edit --id 3 --instruction "把这条记忆改得更简洁，并补充适用条件"

# 删除节点（级联删除边、复习记录和缓存卡片）
python -m backend.node_edit --id 3 --delete
```

Python 接口：

```python
from backend.node_edit import edit_node, update_node

# 直接修改
update_node(conn, 3, title="新标题", content="新内容")

# 统一入口；有 instruction 时走 LLM
edit_node(conn, 3, instruction="改得更简洁", llm=client)
```

规则：

- 直接修改的标题/内容也会先经过 `scrub.mask`，真实姓名/电话不会写进节点。
- LLM 只看到脱敏后的节点和指令；返回的占位符保持不变。
- 修改标题/内容/类型会让该节点的 `daily_card` 缓存失效，下次重新出题。
- 重要度低于 0.3 或状态不是 `active` 时自动移出复习。
- 删除节点会级联删除相关边、复习记录和缓存卡片，不可恢复。

## 9. 数据与隐私

数据库默认 `data/memory.db`。主要表：

- `segment`：待整合文本；`source` 可为 `manual` / `screen` / `microphone` / `system`；`processed=1` 表示已整合
- `node`：记忆节点和复习字段
- `edge`：节点关系
- `daily_summary`：每日摘要
- `review_log`：每次反馈
- `placeholder_map`：占位符 ↔ 真实值，只在本机
- 删除节点会级联删除它的 `edge`、`review_log`、`daily_card`；`daily_summary.node_ids` 会同步清理
- `daily_card`：预生成的次日卡片
- `consolidation_report`：每晚整合报告

本地保存、不会上传的内容：

- 原始截图、原始 OCR 文本
- 原始音频、原始转写文本（音频只在内存，转写后丢弃）
- 真实姓名、手机号、邮箱、身份证号等真实值（只在 `placeholder_map`）
- `.env`、`data/`、`models/`、`vendor/`、`*.db` 全部在 `.gitignore` 中

会上传的内容：

- `nightly` 整合时发送脱敏后的片段和相关节点摘要
- 出题时发送到期节点的脱敏内容
- 屏幕读取（私有引擎或本地 Umi HTTP）和录音本身不联网

## 10. 手动测试与沙盒

不要直接拿真实库测试反馈。先复制一份：

```bash
python -X utf8 -c "import sqlite3; s=sqlite3.connect('data/memory.db'); d=sqlite3.connect('data/manual_test.db'); s.backup(d); s.close(); d.close()"
```

然后：

```bash
# 打开沙盒桌宠
python -m backend.desktop_pet --db data/manual_test.db

# 查看沙盒今天的卡片
python -m backend.review_cli --list --db data/manual_test.db

# 在沙盒里预生成
python -m backend.nightly --date 2026-11-14 --db data/manual_test.db

# 录音转写测试（需要先下载模型和一个 16 kHz 单声道 WAV）
python -m backend.audio_capture --wav data/asr_test.wav --dry-run --show-text --db data/manual_test.db
```

想重置沙盒，重新执行上面的复制命令即可。

## 11. 故障排查

**PyPI 安装报 SSL EOF**
使用清华镜像，见第 2 节。

**安装 srt 时报 `pkgutil.ImpImporter`**
Anaconda Python 3.12 的用户目录里可能有旧的 `setuptools 58`。先升级：
```bash
python -m pip install --user --upgrade "setuptools>=68" wheel
python -m pip install --user -r requirements.txt
```

**PyQt6 报 `DLL load failed while importing QtCore`**
Anaconda 的 Python 3.12 与 PyQt6 6.12 的 Qt DLL 不兼容。项目的
`requirements.txt` 已对 Python < 3.14 固定 `PyQt6<6.9`；重新安装即可：
```bash
python -m pip install --user -r requirements.txt
```
验证：`python -c "import PyQt6.QtCore; print(PyQt6.QtCore.QT_VERSION_STR)"`。

**LLM 返回 403 key_model_access_denied**
`DEEPSEEK_MODEL` 不是这个 key 能访问的模型。用第 3 节的命令列出可用模型，
改成可用的那个。

**桌宠打开很慢**
正常路径应该读 `daily_card`，缓存完整时是毫秒级。检查：
```bash
python -X utf8 -c "import sqlite3; c=sqlite3.connect('data/memory.db'); c.row_factory=sqlite3.Row; [print(dict(r)) for r in c.execute(\"SELECT review_date,node_id,source FROM daily_card\")]"
```
如果缓存为空，先运行 `python -m backend.nightly`。

**提示“今天没有到期的记忆”**
今天没有节点到期，或者你已经复习完了。用
`python -m backend.pipeline --list-nodes` 看 `due` 字段，或等第二天。

**屏幕读取一直跳过**
加 `--verbose` 看原因：窗口被排除、锁屏、画面不变、内容太短、内容全是
敏感行。用 `--dry-run --show-text` 检查 OCR 结果。

**私有 OCR 引擎缺失**
运行 `python -m backend.setup_local_ocr --check`。如果显示缺失，按第 2 节
把 Umi 的 PaddleOCR-json / RapidOCR-json 插件目录复制到
`vendor/ocr_engines/paddle|rapid`。也可以临时改用
`--ocr-backend umi` 或 `--ocr-backend windows`。

**Umi-OCR 连接失败（可选后端）**
先启动 Umi-OCR，确认 `全局设置 → 高级 → 启用 HTTP 服务`，然后运行
`python -m backend.umi_ocr --check`。默认地址是 `http://127.0.0.1:1224`。

**Umi-OCR 引擎不对（可选后端）**
`--ocr-engine paddle` 但端口上跑的是 Rapid 时会明确报错。单实例先在
Umi-OCR 里切换插件；双实例把 Paddle/Rapid 分别放在 1224/1225，并配置
`UMI_OCR_PADDLE_URL` / `UMI_OCR_RAPID_URL`。

**Paddle mkldnn 未开启**
私有引擎默认 `--enable_mkldnn=true`，可用 `--no-mkldnn` 关闭。Umi 后端
可运行 `python -m backend.umi_ocr --set-mkldnn --home "Umi-OCR目录"`，
然后重启 Umi-OCR。Rapid 不支持 mkldnn。

**录音提示找不到模型**
按第 2 节下载 `models/vosk-model-small-cn-0.22`，或用 `--model-path` 指定
已有的 Vosk 模型目录。

**没有找到麦克风/系统回环**
先运行 `python -m backend.audio_capture --list-devices`。系统声音需要默认
扬声器有可用的 WASAPI 回环；没有回环时可以先用 `--source microphone`。

**录音转写不准确**
Vosk 小模型对专有名词、口音和远场麦克风效果有限。可以换更大的 Vosk 模型，
或后续接入 Whisper。录音时尽量靠近麦克风。

**控制台中文乱码**
在命令前加 `python -X utf8`；项目的 CLI 也会尝试把 stdio 设为 UTF-8。

## 12. 目录结构

```
backend/
  config.py          .env、数据库路径、UTF-8 stdio
  db.py              SQLite schema 与连接
  scrub.py           本地脱敏
  capture_text.py    采集文本共享清洗/脱敏/入库
  pipeline.py        手动文本入口
  memory.py          云端整合与图写入
  review.py          间隔阶梯与反馈
  scheduler.py       到期队列、卡片缓存、反馈写回
  nightly.py         夜间整合 + 预生成次日卡片
  review_cli.py      命令行复习
  pet_controller.py  与 Qt 无关的桌宠状态机
  desktop_pet.py     PyQt6 桌宠
  screen_capture.py  屏幕读取 + 私有 OCR/Umi/Windows OCR
  local_ocr.py       私有 PaddleOCR-json / RapidOCR-json 进程管理
  setup_local_ocr.py 把 Umi 插件复制为项目私有引擎
  umi_ocr.py         Umi-OCR HTTP 客户端、引擎选择、mkldnn 检查
  audio_capture.py   麦克风/系统声音 + Vosk 转写
  node_edit.py       节点查看、直接编辑、LLM 自然语言改写
  llm.py             DeepSeek HTTP 客户端
tests/               102 个测试，使用 FakeLLM / 假 Umi 服务
data/                本地数据库、屏幕排除规则（gitignored）
models/              本地 Vosk 模型（gitignored）
```
