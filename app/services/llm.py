"""LLM client interface — OpenAI-compatible; deterministic mock without a key."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional, Protocol
from urllib.parse import urlparse

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
# DeepSeek's OpenAI-compatible API. The SDK posts to {base}/chat/completions,
# which this host serves (a /v1 suffix also works if set via OPENAI_BASE_URL).
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"

_ROLE_MARKERS = (
    "content_expert",
    "visual_expert",
    "growth_expert",
    "cross_critique",
    "judge",
)
_ROLE_LINE = re.compile(
    r"Role marker:\s*(content_expert|visual_expert|growth_expert|cross_critique|judge)\b"
)


class LLMClient(Protocol):
    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.3,
    ) -> dict[str, Any]:
        ...


@dataclass(frozen=True)
class LLMEndpoint:
    api_key: str
    base_url: str
    model: str

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


def _clean(value: str) -> str:
    return (value or "").strip()


def resolve_primary_endpoint(settings: Settings) -> LLMEndpoint:
    """Pick key, base URL, and model.

    OPENAI_API_KEY wins when it is set. DEEPSEEK_API_KEY is the alias used only
    when the OpenAI key is empty, with DeepSeek's host and deepseek-chat unless
    OPENAI_BASE_URL / OPENAI_MODEL are set.
    """
    openai_key = _clean(settings.openai_api_key)
    deepseek_key = _clean(settings.deepseek_api_key)
    base_override = _clean(settings.openai_base_url).rstrip("/")
    model_override = _clean(settings.openai_model)

    if openai_key:
        return LLMEndpoint(
            api_key=openai_key,
            base_url=base_override or OPENAI_DEFAULT_BASE_URL,
            model=model_override or OPENAI_DEFAULT_MODEL,
        )
    if deepseek_key:
        return LLMEndpoint(
            api_key=deepseek_key,
            base_url=base_override or DEEPSEEK_BASE_URL,
            model=model_override or DEEPSEEK_DEFAULT_MODEL,
        )
    return LLMEndpoint(
        api_key="",
        base_url=base_override or OPENAI_DEFAULT_BASE_URL,
        model=model_override or OPENAI_DEFAULT_MODEL,
    )


def resolve_judge_endpoint(settings: Settings, primary: LLMEndpoint) -> Optional[LLMEndpoint]:
    """Return a separate judge endpoint, or None to reuse `primary`.

    JUDGE_MODEL wins over JUDGE_OPENAI_MODEL when both are set.
    A model/base override without any API key stays on the mock client.
    """
    judge_key = _clean(settings.judge_openai_api_key)
    judge_base = _clean(settings.judge_openai_base_url).rstrip("/")
    judge_model = _clean(settings.judge_model) or _clean(settings.judge_openai_model)
    if not judge_key and not judge_base and not judge_model:
        return None

    api_key = judge_key or primary.api_key
    if not api_key:
        return None

    base_url = judge_base or primary.base_url
    model = judge_model or primary.model
    if (
        primary.enabled
        and api_key == primary.api_key
        and base_url.rstrip("/") == primary.base_url.rstrip("/")
        and model == primary.model
    ):
        return None
    return LLMEndpoint(api_key=api_key, base_url=base_url, model=model)


class OpenAICompatibleClient:
    """Thin async wrapper around the OpenAI Python SDK (compatible base_url)."""

    def __init__(self, *, api_key: str, base_url: str, model: str) -> None:
        from openai import AsyncOpenAI

        self.model = model
        self.base_url = base_url.rstrip("/")
        self._client = AsyncOpenAI(api_key=api_key, base_url=self.base_url)

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.3,
    ) -> dict[str, Any]:
        resp = await self._client.chat.completions.create(
            model=self.model,
            temperature=temperature,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        raw = resp.choices[0].message.content or "{}"
        return json.loads(raw)


def _role_from_system(system: str) -> str:
    """Prefer the explicit Role marker line (last one wins)."""
    found = _ROLE_LINE.findall(system)
    if found:
        return found[-1]
    for marker in _ROLE_MARKERS:
        if marker in system:
            return marker
    return "generic"


class MockLLMClient:
    """Deterministic mock that still returns schema-shaped JSON for UI wiring."""

    def __init__(self, seed: str = "mvp") -> None:
        self._seed = seed

    def _digest(self, text: str) -> int:
        h = hashlib.sha256(f"{self._seed}:{text}".encode()).hexdigest()
        return int(h[:8], 16)

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.3,
    ) -> dict[str, Any]:
        role = _role_from_system(system)

        score_base = 55 + (self._digest(user) % 30)  # 55–84

        if role == "content_expert":
            return {
                "score": float(score_base),
                "findings": [
                    {
                        "claim": "钩子不够具体，前三秒信息密度偏低",
                        "evidence": "文案开头偏口号，缺少可感知的场景/数字",
                        "severity": "warn",
                    },
                    {
                        "claim": "主题清晰，适合垂直赛道沉淀",
                        "evidence": "caption 中出现明确主题词",
                        "severity": "info",
                    },
                ],
            }
        if role == "visual_expert":
            return {
                "score": float(score_base - 3),
                "findings": [
                    {
                        "claim": "封面主体可读性一般，标题区对比不足",
                        "evidence": "封面存在文字叠图风险（mock 推断）",
                        "severity": "warn",
                    },
                    {
                        "claim": "情绪色调与赛道匹配",
                        "evidence": "暖色/冷色倾向与 caption 情绪一致（mock）",
                        "severity": "info",
                    },
                ],
            }
        if role == "growth_expert":
            return {
                "score": float(score_base + 2),
                "findings": [
                    {
                        "claim": "完播驱动点不足：缺少中段反转预告",
                        "evidence": "文案未给出「看到最后」的具体利益点",
                        "severity": "warn",
                    },
                    {
                        "claim": "话题标签可再聚焦 1–2 个核心词",
                        "evidence": "extra / caption 中话题较散（若有）",
                        "severity": "info",
                    },
                ],
            }
        if role == "cross_critique":
            return {
                "critiques": [
                    {
                        "from_expert": "growth",
                        "to_expert": "content",
                        "point": "内容钩子若更具体，增长侧完播预期会更稳",
                    },
                    {
                        "from_expert": "visual",
                        "to_expert": "growth",
                        "point": "封面信息若过载，会削弱标题点击后的预期管理",
                    },
                ],
                "disagreements": [
                    {
                        "topic": "是否应强化标题党式数字",
                        "sides": {
                            "content": "反对过度数字堆砌，伤害信任",
                            "growth": "适度数字可提升点击，但需可兑现",
                        },
                    }
                ],
            }
        if role == "judge":
            return {
                "overall_score": float(score_base),
                "one_liner": "信息清楚但钩子与封面对比偏弱，优先改前三秒与封面对比度。",
                "verdict": "可发，但建议先改 Top3 再放大投放。",
                "final_scores": {},  # filled by orchestrator if empty
                "top3_fixes": [
                    {
                        "title": "重写前三秒钩子",
                        "why": "当前开头偏抽象，完播与停留受限",
                        "how": "用具体场景+数字开场，3 秒内抛出冲突或结果",
                        "expected_effect": "前3秒留存与完播率提升",
                    },
                    {
                        "title": "提高封面文字对比",
                        "why": "信息密度与可读性不足时点击犹豫",
                        "how": "主体裁切更紧，标题区加半透明底或描边",
                        "expected_effect": "封面点击率提升",
                    },
                    {
                        "title": "文案末尾给明确行动点",
                        "why": "互动与关注转化缺少触发",
                        "how": "用一个具体问题收尾，引导评论区回答",
                        "expected_effect": "评论率与关注转化提升",
                    },
                ],
                "do_not": [
                    "不要为了点击伪造未兑现的结果承诺",
                    "MVP 不做全视频逐帧分析结论，避免过度解读",
                ],
                "dimension_overrides": {},
            }

        return {"note": "unrecognized role", "echo": user[:120]}


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc or url
    except Exception:
        return "unknown"


def _client_for(endpoint: LLMEndpoint, *, role: str) -> LLMClient:
    if not endpoint.enabled:
        logger.warning("No LLM API key set — using deterministic MockLLMClient (%s)", role)
        return MockLLMClient()
    logger.info(
        "Using OpenAI-compatible LLM for %s (host=%s model=%s)",
        role,
        _host(endpoint.base_url),
        endpoint.model,
    )
    return OpenAICompatibleClient(
        api_key=endpoint.api_key,
        base_url=endpoint.base_url,
        model=endpoint.model,
    )


def get_llm_clients(settings: Optional[Settings] = None) -> tuple[LLMClient, LLMClient]:
    """Return (primary, judge). Judge is the same object when no override is set."""
    settings = settings or get_settings()
    primary_ep = resolve_primary_endpoint(settings)
    primary = _client_for(primary_ep, role="primary")
    judge_ep = resolve_judge_endpoint(settings, primary_ep)
    if judge_ep is None:
        return primary, primary
    return primary, _client_for(judge_ep, role="judge")


def get_llm_client(settings: Optional[Settings] = None) -> LLMClient:
    primary, _judge = get_llm_clients(settings)
    return primary
