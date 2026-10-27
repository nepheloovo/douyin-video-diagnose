# Douyin Video Diagnose API

FastAPI + SSE：抖音视频前 3 秒抽帧与文案诊断。托管在 Render。

公开 API base：https://douyin-video-diagnose.onrender.com

- `GET /health`
- `POST /diagnose` 为 **multipart/form-data**（不是 JSON），立即返回 `job_id`
- SSE：`GET /diagnose/{job_id}`（`parsing` → `experts` → `debate` → `judge` → `done` 或 `error`）

系统提示词来自 `prompts/content.md`、`visual.md`、`growth.md`、`debate.md`、`judge.md`。缺文件时用短内置提示。响应不含模型名。

密钥只放环境变量或本地 `.env`（样例见 `.env.example`）。**不要提交密钥。**

| 变量 | 作用 |
|---|---|
| `OPENAI_API_KEY` | OpenAI 兼容密钥。非空时优先于 `DEEPSEEK_API_KEY`。 |
| `DEEPSEEK_API_KEY` | 仅当 `OPENAI_API_KEY` 为空时使用。默认主机 `https://api.deepseek.com`，模型 `deepseek-chat`。 |
| `OPENAI_BASE_URL` / `OPENAI_MODEL` | 可选覆盖。DeepSeek 示例：`https://api.deepseek.com`、`deepseek-chat`。未设置时 OpenAI 默认为 `https://api.openai.com/v1`、`gpt-4o-mini`。 |
| `JUDGE_OPENAI_API_KEY` / `JUDGE_OPENAI_BASE_URL` / `JUDGE_MODEL` | 可选评审客户端。`JUDGE_OPENAI_MODEL` 是模型别名。未设置则评审复用主客户端。 |

未设置密钥时使用确定性 mock，SSE 报告结构相同。抽帧需要 **ffmpeg**（优先 `PATH`，否则用 `imageio-ffmpeg` 自带二进制）。

## 本机两步启动

1. 安装依赖（需要 ffmpeg）：

```bash
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

2. 设置环境变量后启动。密钥用 `OPENAI_API_KEY` 或 `DEEPSEEK_API_KEY`。走 DeepSeek 时可选 `OPENAI_BASE_URL=https://api.deepseek.com`、`OPENAI_MODEL=deepseek-chat`，然后：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

冒烟：`python tests/smoke_diagnose.py`
