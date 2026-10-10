---
title: Template Final Assignment
emoji: 🕵🏻‍♂️
colorFrom: indigo
colorTo: indigo
sdk: gradio
sdk_version: 5.25.2
app_file: app.py
pinned: false
hf_oauth: true
# optional, default duration is 8 hours/480 minutes. Max duration is 30 days/43200 minutes.
hf_oauth_expiration_minutes: 480
---

# GAIA Agent（Hugging Face Agents Course · Unit 4 期末作业）

单 agent 架构：smolagents `CodeAgent` + DeepSeek（`deepseek-flash`，文本和视觉共用）+ 12 个工具。

## 文件结构

| 文件 / 目录 | 作用 |
|---|---|
| `app.py` | Gradio 页面：登录 HF，读取 `answers.json` 缓存，只跑没有答案的题，然后提交 |
| `run_local.py` | 本地开发入口：单题运行、答案缓存、每题日志、本地打分、命令行提交 |
| `agent.py` | `GAIAAgent`：附件预处理 → CodeAgent 循环 → 答案检查、清洗和兜底提取 |
| `tools.py` | 12 个工具：搜索、网页、Wikipedia、文件、Python 执行、音频转写、看图、YouTube、象棋 |
| `prompt.py` | 追加到 CodeAgent 系统提示后面的做题规则和答案格式规则 |
| `requirements.txt` | 依赖 |
| `.env.example` | 环境变量模板（复制成 `.env` 再填写，`.env` 不会上传） |
| `questions.json` | 20 道题的缓存 |
| `answers.json` | 每题答案的缓存（提交时用） |
| `attachments/` | 题目附件（mp3 / xlsx / png / py） |
| `logs/` | 每题的完整运行日志（不上传） |
| `stockfish/` | 象棋引擎，本地使用（不上传） |

## 运行

```
pip install -r requirements.txt
copy .env.example .env          # 填入 DEEPSEEK_API_KEY

python run_local.py --list                  # 查看每题当前的答案
python run_local.py --task 8e867cd7         # 单独跑一题（task_id 前 8 位）
python run_local.py                         # 跑所有还没有答案的题
python run_local.py --force                 # 全部重跑
python app.py                               # 网页登录后提交
```

YouTube 题需要代理：`set HTTPS_PROXY=http://127.0.0.1:<端口>`。DeepSeek 的请求会自动绕过代理直连。

## 设计要点

- **附件预处理：** 音频先转写成文字，图片直接附上，代码和表格先放预览，模型第一步就拿到全部信息。
- **专用工具：** 模型的弱项交给专用工具。象棋图切成 64 格分别识别、3 遍投票后得到 FEN，走法交给 Stockfish 计算。
- **答案兜底：** 模型交出非答案会被打回重做；交上来的答案统一清洗格式；步数用完时，从工具输出里再提取一次答案。
- **防泄露：** 搜索时过滤 benchmark 数据集、别人的运行记录和用搜索词生成的垃圾页，禁止直接搜索题目答案。
