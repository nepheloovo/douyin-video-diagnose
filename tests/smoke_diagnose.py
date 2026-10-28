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
from app.schemas.modes import DegradationMode, ParseErrorKind  # noqa: E402
from app.schemas.report import FrameMeta  # noqa: E402
from app.schemas.request import DiagnoseRequest  # noqa: E402
from app.services.frames import resolve_ffmpeg, set_frame_extract_override  # noqa: E402
from app.services.mode import decide_mode  # noqa: E402
from app.services.parse import ParseResult  # noqa: E402
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


_RETIRED_MODES = ("caption_only", "cover_only")
_LOCKED_MODES = ("video_3s", "cover_caption", "link_fail")


def runtime_blob(job) -> str:
    parts = [event.model_dump_json() for event in job.events]
    if job.report is not None:
        parts.append(job.report.model_dump_json())
    return "".join(parts)


def assert_no_retired_mode(blob: str, label: str) -> None:
    for retired in _RETIRED_MODES:
        check(retired not in blob, f"{label} emitted {retired}")


def _phases(job) -> list[str]:
    return [event.phase.value for event in job.events]


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
        assert_no_retired_mode(loaded, f"prompt {filename}")
        for mode in _LOCKED_MODES:
            check(mode in loaded, f"{filename} missing locked mode {mode}")
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


async def _run_job(
    req: DiagnoseRequest,
    spy: _Spy | None = None,
    *,
    video_bytes: bytes | None = None,
    cover_bytes: bytes | None = None,
):
    import app.services.pipeline as pipeline

    original = pipeline.get_llm_clients
    if spy is not None:
        pipeline.get_llm_clients = lambda settings=None: (spy, spy)
    try:
        # No keys: even if the patch is absent, the client must be the mock.
        get_settings.cache_clear()
        job = job_store.create()
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            video_path = None
            cover_path = None
            if video_bytes is not None:
                video_path = work / "input.mp4"
                video_path.write_bytes(video_bytes)
            if cover_bytes is not None:
                cover_path = work / "cover.jpg"
                cover_path.write_bytes(cover_bytes)
            await pipeline.run_diagnose_job(
                job,
                JobInputs(
                    request=req,
                    work_dir=work,
                    video_path=video_path,
                    cover_path=cover_path,
                ),
            )
        return job
    finally:
        pipeline.get_llm_clients = original


async def _run_cover_caption(spy: _Spy | None = None):
    return await _run_job(
        DiagnoseRequest(
            caption="前三秒还在讲背景，没有给出结果。今天分享一个方法。",
            cover_url="https://example.com/cover.jpg",
        ),
        spy,
    )


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
    blob = runtime_blob(job)
    for leak in _MODEL_LEAKS:
        check(leak not in blob, f"model name {leak} leaked into the public payload")
    assert_no_retired_mode(blob, "cover_caption report")
    check(report.mode.value in _LOCKED_MODES, f"unlocked mode {report.mode}")


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
        assert_no_retired_mode(blob, "http snapshot")
        check(report["mode"] == "cover_caption", f"http mode {report['mode']}")
        check("frames_extract_unavailable" in report["degradation_flags"], "yellow-bar flags missing")


def _dim(report, name: str):
    return next(dim for dim in report.dimensions if dim.name == name)


def test_mode_enum_is_locked() -> None:
    values = tuple(mode.value for mode in DegradationMode)
    check(values == _LOCKED_MODES, f"DegradationMode values {values}")


def test_decide_mode_locked_table() -> None:
    frames = decide_mode(
        has_cover=False,
        has_caption=True,
        aweme_url=None,
        parse=None,
        has_frames=True,
    )
    check(frames.mode == DegradationMode.VIDEO_3S, f"frames mode {frames.mode}")
    check(frames.run_content and frames.run_visual and frames.run_growth, "video_3s skipped an expert")
    check(frames.link_fail is False, "video_3s marked link_fail")

    url_fail = ParseResult(
        ok=False,
        error_kind=ParseErrorKind.FETCH_BLOCKED,
        error_message="blocked",
        degradation_flags=["url_fetch:fetch_blocked"],
    )
    yellow = decide_mode(
        has_cover=True,
        has_caption=True,
        aweme_url="https://v.douyin.com/abc/",
        parse=url_fail,
        has_frames=False,
    )
    check(yellow.mode == DegradationMode.COVER_CAPTION, f"yellow mode {yellow.mode}")
    check(yellow.link_fail is False, "cover+caption after URL fail took link_fail")
    check(yellow.run_content and yellow.run_visual and yellow.run_growth, "yellow path skipped an expert")
    check("frames_extract_unavailable" in yellow.degradation_flags, "yellow flags missing frames")
    check("url_fetch:fetch_blocked" in yellow.degradation_flags, "yellow flags dropped parse failure")
    check("link_fail" not in yellow.degradation_flags, "yellow path set link_fail flag")

    caption = decide_mode(
        has_cover=False,
        has_caption=True,
        aweme_url="https://example.com/nope",
        parse=url_fail,
        has_frames=False,
        video_attempted=True,
    )
    check(caption.mode == DegradationMode.COVER_CAPTION, f"caption degrade {caption.mode}")
    check(caption.run_growth and caption.run_content and not caption.run_visual, "caption degrade expert set")
    check("skip:visual" in caption.degradation_flags, "caption degrade missing skip:visual")

    cover = decide_mode(
        has_cover=True,
        has_caption=False,
        aweme_url=None,
        parse=None,
        has_frames=False,
    )
    check(cover.mode == DegradationMode.COVER_CAPTION, f"cover degrade {cover.mode}")
    check(cover.run_visual and not cover.run_growth, "cover degrade expert set")

    bare = decide_mode(
        has_cover=False,
        has_caption=False,
        aweme_url="https://www.douyin.com/video/7123456789012345678",
        parse=ParseResult(ok=False, error_kind=ParseErrorKind.PARSE_ERROR, error_message="bad link"),
        has_frames=False,
    )
    check(bare.mode == DegradationMode.LINK_FAIL and bare.link_fail, f"bare link {bare.mode}")
    check(not (bare.run_content or bare.run_visual or bare.run_growth), "link_fail still runs experts")
    check(bool(bare.error_message and bare.actionable), "link_fail missing guidance")

    upload = decide_mode(
        has_cover=False,
        has_caption=False,
        aweme_url=None,
        parse=None,
        has_frames=False,
        video_attempted=True,
    )
    check(upload.mode == DegradationMode.LINK_FAIL and upload.link_fail, f"upload fail {upload.mode}")
    check("frames_extract_unavailable" in upload.degradation_flags, "upload fail missing frame flag")


def test_pipeline_mode_paths() -> None:
    caption_without_cover = DiagnoseRequest(caption="只有文案，没有封面，抽帧也没有发生。")
    job = asyncio.run(_run_job(caption_without_cover))
    check(job.status == JobStatus.DONE, f"caption degrade status {job.status} {job.error}")
    report = job.report
    assert report is not None
    check(report.mode == DegradationMode.COVER_CAPTION, f"caption degrade mode {report.mode}")
    check(_dim(report, "visual").skipped, "caption degrade ran visual")
    check(not _dim(report, "content").skipped and not _dim(report, "growth").skipped, "caption degrade skipped text experts")
    for phase in ("experts", "debate", "judge", "done"):
        check(phase in _phases(job), f"caption degrade missing {phase}")
    check(len(report.top3_fixes) == 3, "caption degrade dropped top3")
    assert_no_retired_mode(runtime_blob(job), "caption degrade payload")

    cover_job = asyncio.run(
        _run_job(DiagnoseRequest(cover_url="https://example.com/cover.jpg"))
    )
    assert cover_job.report is not None
    check(cover_job.report.mode == DegradationMode.COVER_CAPTION, f"cover degrade {cover_job.report.mode}")
    check(_dim(cover_job.report, "growth").skipped, "cover degrade ran growth")
    check(not _dim(cover_job.report, "visual").skipped, "cover degrade skipped visual")
    assert_no_retired_mode(runtime_blob(cover_job), "cover degrade payload")

    yellow = asyncio.run(
        _run_job(
            DiagnoseRequest(
                aweme_url="https://example.com/not-a-douyin-link",
                caption="链接失败，但封面和文案还在。",
                cover_url="https://example.com/cover.jpg",
            )
        )
    )
    assert yellow.report is not None
    check(yellow.report.mode == DegradationMode.COVER_CAPTION, f"url+cover mode {yellow.report.mode}")
    check(yellow.status == JobStatus.DONE, f"url+cover status {yellow.status}")
    names = {dim.name for dim in yellow.report.dimensions if not dim.skipped}
    check(names == {"content", "visual", "growth"}, f"url+cover experts {names}")
    check("frames_extract_unavailable" in yellow.report.degradation_flags, "url+cover flags")
    check("url_parse:parse_error" in yellow.report.degradation_flags, "url+cover dropped parse flag")
    parsing = next(event for event in yellow.events if event.data and "link_fail" in event.data)
    check(parsing.data["link_fail"] is False, "yellow bar event marked link_fail")
    for phase in ("experts", "debate", "judge", "done"):
        check(phase in _phases(yellow), f"yellow bar missing {phase}")
    assert_no_retired_mode(runtime_blob(yellow), "yellow bar payload")

    spy = _Spy()
    link = asyncio.run(
        _run_job(
            DiagnoseRequest(aweme_url="https://www.douyin.com/video/7123456789012345678"),
            spy,
        )
    )
    assert link.report is not None
    check(link.report.mode == DegradationMode.LINK_FAIL, f"id-only mode {link.report.mode}")
    check(link.report.dimensions == [], "link_fail invented dimensions")
    check(link.report.top3_fixes == [], "link_fail invented top3")
    check(bool(link.report.one_liner) and bool(link.report.verdict), "link_fail missing guidance")
    check(spy.systems == [], f"link_fail called the LLM {len(spy.systems)} times")
    got = _phases(link)
    for banned in ("experts", "debate", "judge"):
        check(banned not in got, f"link_fail emitted {banned}: {got}")
    check(got[0] == "parsing" and got[-1] == "done", f"link_fail phases {got}")
    done = link.events[-1]
    check(bool(done.error_code), "link_fail done missing error_code")
    check(bool((done.data or {}).get("actionable")), "link_fail done missing actionable")
    assert_no_retired_mode(runtime_blob(link), "link_fail payload")

    set_frame_extract_override(
        inject=[FrameMeta(path="frame_000.jpg", t_sec=0.0, source="video_0_3s")]
    )
    try:
        video = asyncio.run(
            _run_job(
                DiagnoseRequest(caption="上传视频并且抽出了前三秒。"),
                video_bytes=b"not-a-real-video",
            )
        )
    finally:
        set_frame_extract_override()
    assert video.report is not None
    check(video.report.mode == DegradationMode.VIDEO_3S, f"frames mode {video.report.mode}")
    check(not any(dim.skipped for dim in video.report.dimensions), "video_3s skipped an expert")
    assert_no_retired_mode(runtime_blob(video), "video_3s payload")

    set_frame_extract_override(force_fail=True)
    try:
        failed_with_caption = asyncio.run(
            _run_job(
                DiagnoseRequest(caption="抽帧失败，但文案还在。"),
                video_bytes=b"not-a-real-video",
            )
        )
        failed_bare = asyncio.run(
            _run_job(
                DiagnoseRequest(video_present=True),
                video_bytes=b"not-a-real-video",
            )
        )
    finally:
        set_frame_extract_override()
    assert failed_with_caption.report is not None
    check(
        failed_with_caption.report.mode == DegradationMode.COVER_CAPTION,
        f"frame-fail caption mode {failed_with_caption.report.mode}",
    )
    check(
        any(flag.startswith("frames_fail:") for flag in failed_with_caption.report.degradation_flags),
        "frame-fail caption missing frames_fail",
    )
    check(
        "frames_extract_unavailable" in failed_with_caption.report.degradation_flags,
        "frame-fail caption missing yellow flag",
    )
    check("experts" in _phases(failed_with_caption), "frame-fail caption skipped experts")
    assert_no_retired_mode(runtime_blob(failed_with_caption), "frame-fail caption payload")

    assert failed_bare.report is not None
    check(failed_bare.report.mode == DegradationMode.LINK_FAIL, f"frame-fail bare {failed_bare.report.mode}")
    check(failed_bare.report.top3_fixes == [], "frame-fail bare invented top3")
    check(failed_bare.report.dimensions == [], "frame-fail bare invented findings")
    check("experts" not in _phases(failed_bare), "frame-fail bare ran experts")
    assert_no_retired_mode(runtime_blob(failed_bare), "frame-fail bare payload")


def test_http_link_fail_skips_experts() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    get_settings.cache_clear()
    with TestClient(app) as client:
        created = client.post(
            "/diagnose",
            data={"aweme_url": "https://www.douyin.com/video/7123456789012345678"},
        )
        check(created.status_code == 200, f"link_fail POST {created.status_code} {created.text}")
        job_id = created.json().get("job_id")
        snapshot = None
        for _ in range(100):
            resp = client.get(f"/diagnose/{job_id}/snapshot")
            check(resp.status_code == 200, f"link_fail snapshot {resp.status_code}")
            snapshot = resp.json()
            if snapshot["status"] in ("done", "error"):
                break
        assert snapshot is not None
        check(snapshot["status"] == "done", f"link_fail http {snapshot['status']} {snapshot.get('error')}")
        phases = [event["phase"] for event in snapshot["events"]]
        for banned in ("experts", "debate", "judge"):
            check(banned not in phases, f"link_fail http emitted {banned}")
        check("parsing" in phases and phases[-1] == "done", f"link_fail http phases {phases}")
        report = snapshot["report"]
        check(report["mode"] == "link_fail", f"link_fail http mode {report['mode']}")
        check(report["top3_fixes"] == [], "link_fail http invented top3")
        check(report["dimensions"] == [], "link_fail http invented dimensions")
        check(bool(report["one_liner"]), "link_fail http missing guidance")
        assert_no_retired_mode(resp.text, "link_fail http snapshot")


def main() -> None:
    test_prompts_load_five_files()
    test_missing_prompt_falls_back()
    test_mode_enum_is_locked()
    test_decide_mode_locked_table()
    test_mock_pipeline_loads_prompts()
    test_missing_prompts_still_finish()
    test_pipeline_mode_paths()
    test_deepseek_alias_and_openai_precedence()
    test_ffmpeg_resolver()
    test_http_snapshot_contract()
    test_http_link_fail_skips_experts()
    print("smoke_diagnose: ok")


if __name__ == "__main__":
    main()
