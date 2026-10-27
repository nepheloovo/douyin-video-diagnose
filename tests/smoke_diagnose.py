"""Smoke: mock diagnose (no API key), prompt loading, DeepSeek alias, ffmpeg resolver.

Run from the repo root:

    python tests/smoke_diagnose.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings, get_settings  # noqa: E402
from app.schemas.request import DiagnoseRequest  # noqa: E402
from app.services.frames import resolve_ffmpeg  # noqa: E402
from app.services.llm import (  # noqa: E402
    DEEPSEEK_BASE_URL,
    DEEPSEEK_DEFAULT_MODEL,
    OPENAI_DEFAULT_BASE_URL,
    OPENAI_DEFAULT_MODEL,
    MockLLMClient,
    OpenAICompatibleClient,
    get_llm_clients,
)
from app.services.pipeline import JobInputs  # noqa: E402
from app.services.prompts import PROMPT_FILES, load_system_prompt, role_marker_line  # noqa: E402
from app.services import prompts as prompts_mod  # noqa: E402
from app.services.store import JobStatus, job_store  # noqa: E402

_LLM_ENV = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "DEEPSEEK_API_KEY",
    "JUDGE_OPENAI_API_KEY",
    "JUDGE_OPENAI_BASE_URL",
    "JUDGE_OPENAI_MODEL",
    "JUDGE_MODEL",
)

_PHRASES = {
    "content_expert": "内容专家",
    "visual_expert": "视觉专家",
    "growth_expert": "增长专家",
    "cross_critique": "书面辩论",
    "judge": "最终裁判",
}

_MODEL_LEAKS = ("deepseek-chat", "deepseek-reasoner", "gpt-4o", "gpt-4o-mini")


def check(cond: bool, message: str) -> None:
    if not cond:
        raise SystemExit(f"FAIL: {message}")


def settings_from(**env: str) -> Settings:
    """Build Settings from an explicit env, ignoring ambient keys and the cache."""
    saved = {key: os.environ.get(key) for key in _LLM_ENV}
    try:
        for key in _LLM_ENV:
            os.environ.pop(key, None)
        for key, value in env.items():
            os.environ[key] = value
        get_settings.cache_clear()
        return Settings()
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        get_settings.cache_clear()


def _key_of(client: OpenAICompatibleClient) -> str:
    return str(client._client.api_key)


def test_prompts_load_five_files() -> None:
    for marker, filename in PROMPT_FILES.items():
        path = prompts_mod.PROMPTS_DIR / filename
        check(path.is_file(), f"missing prompt file {path}")
        loaded = load_system_prompt(marker, "FALLBACK_SHOULD_NOT_BE_USED")
        check("FALLBACK_SHOULD_NOT_BE_USED" not in loaded, f"{marker} used fallback")
        check(_PHRASES[marker] in loaded, f"{marker} missing phrase from {filename}")
        check(role_marker_line(marker) in loaded.splitlines(), f"{marker} missing role line")
        # Debate markdown mentions judge_resolution; the explicit line must still win.
        routed = _role(loaded)
        check(routed == marker, f"{marker} routed as {routed}")


def test_missing_prompt_falls_back() -> None:
    saved = prompts_mod.PROMPTS_DIR
    try:
        with tempfile.TemporaryDirectory() as tmp:
            prompts_mod.PROMPTS_DIR = Path(tmp)
            fallback = "You are the content expert. Role marker: content_expert. inline"
            loaded = load_system_prompt("content_expert", fallback)
            check(loaded == fallback, "missing file did not return the inline fallback")
    finally:
        prompts_mod.PROMPTS_DIR = saved


def _role(system: str) -> str:
    from app.services.llm import _role_from_system

    return _role_from_system(system)


class _Spy(MockLLMClient):
    def __init__(self) -> None:
        super().__init__()
        self.systems: list[str] = []

    async def complete_json(self, *, system: str, user: str, temperature: float = 0.3) -> dict:
        self.systems.append(system)
        return await super().complete_json(system=system, user=user, temperature=temperature)


async def _run_cover_caption(spy: _Spy | None = None):
    import app.services.pipeline as pipeline

    original = pipeline.get_llm_clients
    if spy is not None:
        pipeline.get_llm_clients = lambda settings=None: (spy, spy)
    try:
        # No keys: even if the patch is absent, the client must be the mock.
        get_settings.cache_clear()
        job = job_store.create()
        req = DiagnoseRequest(
            caption="前三秒还在讲背景，没有给出结果。今天分享一个方法。",
            cover_url="https://example.com/cover.jpg",
        )
        with tempfile.TemporaryDirectory() as tmp:
            await pipeline.run_diagnose_job(
                job,
                JobInputs(request=req, work_dir=Path(tmp)),
            )
        return job
    finally:
        pipeline.get_llm_clients = original


def test_mock_pipeline_loads_prompts() -> None:
    spy = _Spy()
    job = asyncio.run(_run_cover_caption(spy))
    check(job.status == JobStatus.DONE, f"job status {job.status} error={job.error}")
    check(job.report is not None, "missing report")
    report = job.report
    assert report is not None
    phases = [event.phase.value for event in job.events]
    for phase in ("parsing", "experts", "debate", "judge", "done"):
        check(phase in phases, f"missing SSE phase {phase}; got {phases}")
    check(phases[-1] == "done", f"terminal phase {phases[-1]}")
    check(report.mode.value == "cover_caption", f"mode {report.mode}")
    check(len(report.top3_fixes) == 3, f"top3_fixes {len(report.top3_fixes)}")
    check(bool(report.one_liner), "empty one_liner")
    check("钩子与封面对比偏弱" in report.one_liner, "judge mock text missing")
    names = {dim.name: dim for dim in report.dimensions}
    for name in ("content", "visual", "growth"):
        check(name in names and not names[name].skipped, f"{name} skipped")
        check(bool(names[name].findings), f"{name} findings empty")
    check(len(report.disagreements) == 1, "debate disagreements missing — role routing failed")
    check(len(spy.systems) == 5, f"expected 5 LLM calls, got {len(spy.systems)}")
    roles = [_role(system) for system in spy.systems]
    check(
        roles == ["content_expert", "visual_expert", "growth_expert", "cross_critique", "judge"],
        f"role order {roles}",
    )
    for system, marker in zip(spy.systems, roles):
        check(_PHRASES[marker] in system, f"pipeline did not load {marker} markdown")
    blob = report.model_dump_json() + "".join(event.model_dump_json() for event in job.events)
    for leak in _MODEL_LEAKS:
        check(leak not in blob, f"model name {leak} leaked into the public payload")


def test_missing_prompts_still_finish() -> None:
    saved = prompts_mod.PROMPTS_DIR
    spy = _Spy()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            prompts_mod.PROMPTS_DIR = Path(tmp)
            job = asyncio.run(_run_cover_caption(spy))
    finally:
        prompts_mod.PROMPTS_DIR = saved
    check(job.status == JobStatus.DONE, f"fallback pipeline status {job.status} error={job.error}")
    check(job.report is not None and len(job.report.top3_fixes) == 3, "fallback dropped top3")
    check(spy.systems, "fallback made no LLM calls")
    check(
        all("You are the " in system or "You facilitate" in system for system in spy.systems),
        "fallback did not use the inline system strings",
    )
    check(all("内容专家" not in system for system in spy.systems), "missing dir still loaded markdown")


def test_deepseek_alias_and_openai_precedence() -> None:
    deepseek = settings_from(DEEPSEEK_API_KEY="sk-deepseek-test")
    primary, judge = get_llm_clients(deepseek)
    check(isinstance(primary, OpenAICompatibleClient), "DeepSeek key did not build a real client")
    check(judge is primary, "unset judge settings should reuse the primary client")
    assert isinstance(primary, OpenAICompatibleClient)
    check(primary.base_url == DEEPSEEK_BASE_URL, f"base_url {primary.base_url}")
    check(primary.model == DEEPSEEK_DEFAULT_MODEL, f"model {primary.model}")
    check(_key_of(primary) == "sk-deepseek-test", "DeepSeek key was not passed to the client")
    check("api.deepseek.com" in str(primary._client.base_url), "SDK base_url is not DeepSeek")

    both = settings_from(
        OPENAI_API_KEY="sk-openai-test",
        DEEPSEEK_API_KEY="sk-deepseek-test",
    )
    client, _same = get_llm_clients(both)
    assert isinstance(client, OpenAICompatibleClient)
    check(client.base_url == OPENAI_DEFAULT_BASE_URL, f"OpenAI base lost: {client.base_url}")
    check(client.model == OPENAI_DEFAULT_MODEL, f"OpenAI model lost: {client.model}")
    check(_key_of(client) == "sk-openai-test", "OPENAI_API_KEY did not win")

    overridden = settings_from(
        DEEPSEEK_API_KEY="sk-deepseek-test",
        OPENAI_BASE_URL="https://api.deepseek.com/v1",
        OPENAI_MODEL="deepseek-reasoner",
    )
    client, _same = get_llm_clients(overridden)
    assert isinstance(client, OpenAICompatibleClient)
    check(client.base_url == "https://api.deepseek.com/v1", f"override base {client.base_url}")
    check(client.model == "deepseek-reasoner", f"override model {client.model}")
    check(_key_of(client) == "sk-deepseek-test", "override dropped the DeepSeek key")

    judged = settings_from(
        DEEPSEEK_API_KEY="sk-deepseek-test",
        JUDGE_MODEL="deepseek-reasoner",
    )
    primary, judge = get_llm_clients(judged)
    assert isinstance(primary, OpenAICompatibleClient)
    assert isinstance(judge, OpenAICompatibleClient)
    check(primary.model == DEEPSEEK_DEFAULT_MODEL, "primary model changed")
    check(judge.model == "deepseek-reasoner", f"judge model {judge.model}")
    check(judge.base_url == primary.base_url, "judge base should follow the primary host")
    check(primary is not judge, "judge model override reused the same client")
    check(_key_of(judge) == "sk-deepseek-test", "judge did not reuse the primary key")

    alias = settings_from(
        DEEPSEEK_API_KEY="sk-deepseek-test",
        JUDGE_OPENAI_MODEL="gpt-4o",
        JUDGE_OPENAI_API_KEY="sk-judge-test",
        JUDGE_OPENAI_BASE_URL="https://api.openai.com/v1",
    )
    _primary, judge = get_llm_clients(alias)
    assert isinstance(judge, OpenAICompatibleClient)
    check(judge.model == "gpt-4o", f"JUDGE_OPENAI_MODEL {judge.model}")
    check(judge.base_url == OPENAI_DEFAULT_BASE_URL, f"judge base {judge.base_url}")
    check(_key_of(judge) == "sk-judge-test", "judge key was not used")

    prefer_judge_model = settings_from(
        DEEPSEEK_API_KEY="sk-deepseek-test",
        JUDGE_MODEL="deepseek-reasoner",
        JUDGE_OPENAI_MODEL="gpt-4o",
    )
    _primary, judge = get_llm_clients(prefer_judge_model)
    assert isinstance(judge, OpenAICompatibleClient)
    check(judge.model == "deepseek-reasoner", "JUDGE_MODEL should win over JUDGE_OPENAI_MODEL")

    bare = settings_from()
    mock, mock_judge = get_llm_clients(bare)
    check(isinstance(mock, MockLLMClient), "empty env was not the mock")
    check(mock_judge is mock, "mock judge diverged")
    check(bare.has_llm is False, "has_llm true with no keys")
    check(deepseek.has_llm is True, "has_llm false with DeepSeek key")

    model_only = settings_from(JUDGE_MODEL="gpt-4o")
    mock, mock_judge = get_llm_clients(model_only)
    check(isinstance(mock, MockLLMClient) and mock_judge is mock, "JUDGE_MODEL alone left mock mode")


def test_ffmpeg_resolver() -> None:
    resolved = resolve_ffmpeg()
    check(bool(resolved), "resolve_ffmpeg returned nothing")
    assert resolved is not None
    check(Path(resolved).exists(), f"ffmpeg binary missing: {resolved}")

    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "ffmpeg"
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = tmp + os.pathsep + old_path
        try:
            preferred = resolve_ffmpeg()
        finally:
            os.environ["PATH"] = old_path
    check(preferred is not None and Path(preferred).resolve() == fake.resolve(), "system ffmpeg was not preferred")

    # Same helper the extractor uses, after PATH is restored.
    again = resolve_ffmpeg()
    system = shutil.which("ffmpeg")
    if system:
        check(os.path.realpath(again or "") == os.path.realpath(system), "resolver disagreed with shutil.which")
    else:
        check(bool(again) and Path(again).exists(), "imageio-ffmpeg fallback missing")


def test_http_snapshot_contract() -> None:
    """Public poll helper returns the same phases the SSE stream emits."""
    from fastapi.testclient import TestClient

    from app.main import app

    get_settings.cache_clear()
    with TestClient(app) as client:
        created = client.post(
            "/diagnose",
            data={
                "caption": "前三秒还在讲背景，没有给出结果。",
                "cover_url": "https://example.com/cover.jpg",
            },
        )
        check(created.status_code == 200, f"POST /diagnose {created.status_code} {created.text}")
        job_id = created.json().get("job_id")
        check(bool(job_id), "missing job_id")

        # The router schedules the pipeline with asyncio.create_task. Drive the
        # loop by polling the snapshot until the job reaches a terminal state.
        snapshot = None
        for _ in range(100):
            resp = client.get(f"/diagnose/{job_id}/snapshot")
            check(resp.status_code == 200, f"snapshot {resp.status_code}")
            snapshot = resp.json()
            if snapshot["status"] in ("done", "error"):
                break
        check(snapshot is not None, "no snapshot")
        assert snapshot is not None
        check(snapshot["status"] == "done", f"http status {snapshot['status']} error={snapshot.get('error')}")
        phases = [event["phase"] for event in snapshot["events"]]
        for phase in ("parsing", "experts", "debate", "judge", "done"):
            check(phase in phases, f"http missing phase {phase}")
        report = snapshot["report"]
        check(report and report["top3_fixes"], "http report missing top3_fixes")
        check("findings" in report["dimensions"][0], "http dimension missing findings")
        blob = resp.text
        for leak in _MODEL_LEAKS:
            check(leak not in blob, f"model name {leak} in snapshot")


def main() -> None:
    test_prompts_load_five_files()
    test_missing_prompt_falls_back()
    test_mock_pipeline_loads_prompts()
    test_missing_prompts_still_finish()
    test_deepseek_alias_and_openai_precedence()
    test_ffmpeg_resolver()
    test_http_snapshot_contract()
    print("smoke_diagnose: ok")


if __name__ == "__main__":
    main()
