# sci_soc2026

AI 记忆增强伙伴（课程项目）。完整设计见 `AI 记忆增强伙伴：系统设计方案.docx`。

> 完整安装、配置、每日流程、命令参考和故障排查见 [USAGE.md](USAGE.md)。  
> 最终项目架构、详细数据流、各文件职责和产品功能见 [read1.md](read1.md)。

## 当前进度

- **里程碑 1（已实现）**：手动粘贴文字 → 本地脱敏 → DeepSeek 整合 → 写入 SQLite 记忆图。
- **里程碑 2 核心（已实现）**：复习队列、云端出题、三按钮反馈、间隔调度与 `review_log`。
- **重要度与删除（已实现）**：复习时可标记“很重要 / 重要 / 一般 / 不重要”；标为不重要会移出复习，并可确认删除该记忆。
- **节点编辑接口（已实现）**：桌宠卡片下方有“修改”按钮，可直接改标题/内容/类型/重要度，也可以填写要求让 LLM 改写后提交；输入输出都经过本地脱敏。
- **次日卡片预生成（已实现）**：夜间整合未处理片段并把明天的题写入本地 `daily_card`，桌宠次日直接读缓存。
- **屏幕读取基础版（已实现）**：默认只读当前前台窗口，项目私有 PaddleOCR-json/RapidOCR-json 引擎（Paddle 启用 MKL-DNN）、变化检测、敏感行丢弃，脱敏后写入 `segment(source='screen')`；不需要用户运行 Umi-OCR。
- **录音链路基础版（已实现）**：麦克风 + 系统声音回环 → Vosk 本地转写 → 脱敏 → 写入 `segment(source='microphone'/'system')`，音频只在内存中。
- **简易 PyQt6 桌宠（已实现）**：无边框置顶窗口、状态小脸、提问气泡与三按钮反馈。
- 旧原型 `backend/memory_graph.py`（networkx + JSON）仅作参考，schema 与设计文档不一致。

## 安装

```bash
python -m pip install -r requirements.txt
```

若直连 PyPI 出现 SSL 错误，改用镜像：

```bash
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

## 配置

复制 `.env.example` 为 `.env`，填入 DeepSeek API key（`.env` 已被 gitignore）：

```
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_AUTH_TOKEN=sk-...
DEEPSEEK_MODEL=deepseek-flash
```

## 运行

```bash
# 粘贴一段文字，脱敏后交给云端整合
python -m backend.pipeline --text "今天高数课学了洛必达法则……"

# 指定日期、从文件读取、顺便脱敏已知姓名
python -m backend.pipeline --file notes.txt --date 2026-11-10 --name 张三

# 查看当前记忆图
python -m backend.pipeline --list-nodes

# 查看/编辑记忆节点
python -m backend.node_edit --list
python -m backend.node_edit --id 3 --show
python -m backend.node_edit --id 3 --title "新标题" --content "新内容"
python -m backend.node_edit --id 3 --instruction "把这条记忆改得更简洁"

# 今日复习：先自己想，按回车看答案，再按 1/2/3 反馈
python -m backend.review_cli
python -m backend.review_cli --list       # 只看题目和答案
python -m backend.review_cli --offline    # 本地模板出题，不调用云端

# 每晚运行：整合未处理片段，并预生成明天的复习卡片
python -m backend.nightly
python -m backend.nightly --date 2026-11-13  # 指定“当晚”，卡片生成到 11-14
python -m backend.nightly --offline           # 跳过整合，只用本地模板出题

# 桌宠界面（可拖动，右键菜单可重新加载/退出）
# 昨晚已生成卡片时直接读本地缓存，不再等云端；缓存缺失才后台补题
python -m backend.desktop_pet
python -m backend.desktop_pet --offline   # 不调用云端
python -m backend.desktop_pet --smoke-test --screenshot shot.png

# 自动读屏（默认项目私有 OCR 引擎；截图只在内存，不落盘）
python -m backend.setup_local_ocr --check           # 检查私有引擎是否就绪
python -m backend.local_ocr --check                 # 查看引擎/mkldnn/线程
python -m backend.screen_capture --self-test        # 自检截图和 OCR
python -m backend.screen_capture --once --dry-run   # 只识别一次，不写库
python -m backend.screen_capture                    # 持续读取，默认每 2 秒检查画面
python -m backend.screen_capture --ocr-engine rapid # 指定 Rapid
python -m backend.screen_capture --ocr-backend umi  # 可选：用户自己的 Umi HTTP
python -m backend.screen_capture --ocr-backend windows  # 备用 Windows OCR
python -m backend.screen_capture --full-screen      # 整屏读取（隐私风险更高）

# 录音链路（本地 Vosk；需先按 USAGE.md 下载模型）
python -m backend.audio_capture --list-devices
python -m backend.audio_capture --wav data/asr_test.wav --dry-run --show-text
python -m backend.audio_capture --once --seconds 10
python -m backend.audio_capture --source both       # 麦克风 + 系统声音
python -m backend.audio_capture --source system     # 只看系统声音

# 运行测试（测试使用假 LLM，不消耗 API）
python -m pytest -q
```

数据库默认位于 `data/memory.db`，可用 `MEMORY_DB` 或 `--db` 覆盖。

## 一键启动

双击 `start.bat`，会显示菜单：

1. 启动桌宠（真实数据）
2. 演示模式（自动生成 3 张今天的示例卡片）
3. 启动屏幕读取
4. 启动录音
5. 运行测试
6. 打开使用说明

只想快速看效果，直接双击 `start_demo.bat`：它会重建
`data/demo_start.db`（不会碰真实库）并打开桌宠。

## 每日使用顺序

1. 白天可让 `python -m backend.screen_capture` 和 `python -m backend.audio_capture` 在后台运行：屏幕默认每 2 秒检查一次前台窗口，录音同时监听麦克风和系统声音；两者都只在内存中处理，变化/说话时才转写并脱敏写入 `segment`。
2. 打开桌宠，复习昨晚生成的卡片；缓存完整时不会调用云端。
3. 晚上运行 `python -m backend.nightly`：整合当天未处理片段（手动文本 + 屏幕片段 + 录音片段），并生成第二天的卡片。
4. 第二天重复第 2 步。若某天忘记运行夜间任务，桌宠会自动回退到本地模板，并在后台尝试补题。

## 屏幕读取说明

- 默认使用项目私有的 **PaddleOCR-json / RapidOCR-json**（`vendor/ocr_engines/`），由本项目的隐藏子进程运行；不占用用户 Umi-OCR 的端口、进程或设置。
- Paddle 默认 `enable_mkldnn=true`；Rapid 更省内存但不支持 mkldnn。用 `--ocr-engine paddle|rapid` 选择。
- 把 Umi-OCR 的插件目录复制为私有引擎：
  ```bash
  python -m backend.setup_local_ocr \
    --paddle-from "D:/Umi-OCR_Paddle/UmiOCR-data/plugins/win7_x64_PaddleOCR-json" \
    --rapid-from "D:/Umi-OCR_Rapid/UmiOCR-data/plugins/win7_x64_RapidOCR-json"
  ```
  `vendor/` 已被 gitignore；引擎为 MIT 许可。
- 也可以改用用户自己的 Umi HTTP 服务：`--ocr-backend umi`；`python -m backend.umi_ocr --check` 查看引擎/mkldnn。
- 没有私有引擎或 Umi 时可用 `--ocr-backend windows` 回退到 Windows 自带 OCR。
- **默认只读当前前台窗口**，不读整块屏幕；窗口标题/进程命中排除规则时直接跳过。
- 默认排除密码管理器、银行/支付、无痕/隐私浏览、登录页等；可在 `data/screen_exclusions.json` 中追加：
  ```json
  {"title_keywords": ["我的私人页面"], "processes": ["someapp.exe"]}
  ```
- 密码、API key、JWT、私钥、长令牌、银行卡号等敏感行整行丢弃；其余文本复用 `scrub.mask` 脱敏。
- 截图只在内存中处理，不保存文件；OCR 原文不落库，只有脱敏文本写入 `segment`。
- `--full-screen` 会读到背景窗口，隐私风险更高，仅在你明确需要时使用。
- 目前没有桌宠录音/采集指示灯，按 `Ctrl+C` 停止读取。

## 录音说明

- 需要先下载 Vosk 中文小模型到 `models/`，步骤见 [USAGE.md](USAGE.md)。
- 默认 `--source both`：麦克风和系统声音回环同时监听；同一个声音被两边录到时按文本相似度去重。
- Vosk 使用 16 kHz 单声道 PCM；音频只存在内存，转写后丢弃，不落盘。
- 转写文本复用与屏幕相同的脱敏/敏感行丢弃规则。
- 小模型准确率有限；可用 `--model-path` 换成更大的 Vosk 模型。

## 隐私边界

原始音频、原始转写、屏幕截图、OCR 原文、真实姓名均留在本机。云端只会收到由
`backend/scrub.py` 生成的脱敏文本；`[人物1]` 等占位符与真实值的映射只写入本机
`placeholder_map` 表。
