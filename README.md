---
title: Douyin Video Diagnose
emoji: 🎬
colorFrom: indigo
colorTo: pink
sdk: docker
app_port: 7860
pinned: false
---

# Douyin Video Diagnose API

FastAPI + SSE (first 3s frames). `GET /health`, `POST /diagnose`.

Stages: `parsing` → `experts` → `debate` → `judge` → `done` or `error`.
System prompts load from `prompts/content.md`, `visual.md`, `growth.md`, `debate.md`, and `judge.md`. A missing file falls back to a short inline prompt. Responses do not include model names.

Set LLM keys in the environment (see `.env.example`). Never commit secrets.

| Variable | Role |
|---|---|
| `OPENAI_API_KEY` | OpenAI-compatible key. Wins when set. |
| `OPENAI_BASE_URL` / `OPENAI_MODEL` | Optional. Defaults: `https://api.openai.com/v1`, `gpt-4o-mini`. |
| `DEEPSEEK_API_KEY` | Used when `OPENAI_API_KEY` is empty. Host `https://api.deepseek.com`, model `deepseek-chat`, unless the `OPENAI_*` overrides above are set. |
| `JUDGE_OPENAI_API_KEY` / `JUDGE_OPENAI_BASE_URL` / `JUDGE_MODEL` | Optional judge client. `JUDGE_OPENAI_MODEL` is an alias for the model. Unset means the judge reuses the primary client. |

With no key, the API uses a deterministic mock and the same SSE report shape.

Frame extraction uses `ffmpeg` on `PATH` when present, otherwise the `imageio-ffmpeg` binary.

```bash
python tests/smoke_diagnose.py
```
