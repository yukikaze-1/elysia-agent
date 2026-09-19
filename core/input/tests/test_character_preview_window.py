"""F8 / Batch 7：角色预览必须服从 resolved 模型的上下文窗口（PW1..PW6）。

## 被测缺陷（canonical backlog F8）

`core/character/preview.py` 组装预览 prompt 时**不传** `prompt_budget_tokens`，而
chat 路径（`core/cognitive/loop.py::_build_turn_prompt`）传。于是预览这条**独立组装
路径**自 2026-09-16 引入 prompt 预算（S2-H9 `076ebee8`）后从未迁移：小上下文
provider/model（如 `ollama/qwen2.5:3b`，`providers.yaml` 声明 8192）上，
`system + userMessage` 超过声明窗口就照发，provider 侧失败后路由折成 503
`CHARACTER_PREVIEW_FAILED`（retryable —— 谎报可恢复）。

## 复现/守卫的边界（本文件的判据）

真实用户入口是 `POST /api/characters/preview`。本文件**一律走这条 HTTP 路由**，
经真实 `CharacterService` → 真实 `PromptCharacterPreviewAdapter` → 真实
`PromptAssembler`，只把最底层 provider 换成**窗口约束 stub**（见
`_WindowEnforcingBackend`）——因此能同时检查"路由返回什么"与"provider 实际收到
什么参数"，不会因为路由提前返回 mock 成功而假绿。

## stub 模拟的是哪一类 provider（诚实声明）

`_WindowEnforcingBackend` 模拟**强制窗口**的 provider：输入超过声明窗口就失败
（严格 OpenAI 风格 API 的真实行为）。**本地 ollama 0.24.0 不这样**：实测超窗输入
返回 HTTP 200 + **静默截断**（`prompt_tokens` 被压到 `num_ctx`）；两类 provider 都
会让"超窗照发"变成用户可见的坏结果（一个报错、一个悄悄丢掉人设），故判据仍成立。
stub 的输入计数用**仓内既有**的 estimator（`estimate_messages_tokens`，且传
**resolved model** —— Batch 18 起估算按模型选 profile，不传就落到保守 profile，
那样 stub 会比真实 provider 更严、把用例变成"证明估算器"而不是"证明本层"），
`max_tokens` 缺省即 0（本仓 chat/预览都不发该参数）——它不会比"强制窗口"的真实
provider 更宽松。

**这条判据的已知误差带**（Batch 7 Challenger）：estimator 对 CJK 偏高、对 JSON/代码
偏低（实测 7302 est vs qwen 真值 12212），故本地拒绝有双向误差带，见
`docs/architecture/invariants.md` §1.1「已知边界」与 `todo.md` `KB-12`。
"""
from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio

from Module.system_prompt.budget import (
    budget_from_context_window,
    effective_context_window,
    estimate_messages_tokens,
)
from core.character.builtin_source import BuiltinCharacterSource
from core.character.catalog import CharacterCatalog
from core.character.draft import CharacterDraft
from core.character.preview import PromptCharacterPreviewAdapter
from core.character.service import CharacterService
from core.profile.profile_store import UserIdentityStore
from core.storage.character_repo import CharacterRepository
from core.storage.character_revision_repo import CharacterRevisionRepository
from core.storage.tests._fake_documentdb import FakeDocumentDBBackend
from core.tests.fake_llm import FakeLLMBackend

pytestmark = pytest.mark.service

# 与 `Module/config/providers.yaml::llm.ollama.context_window` 同值——小窗口 provider
# 的 canonical 声明。改动该数应同时改动这里（断言会随 providers.yaml 现算，见
# `_resolved_window`）。
_OLLAMA_MODEL = "ollama/qwen2.5:3b"
_LARGE_MODEL = "deepseek/deepseek-chat"
_STUB_REPLY = "预览回复♪"


class _ProviderContextLengthError(Exception):
    """模拟 provider 的 400（OpenAI 兼容：上下文超限）。"""

    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)


class _WindowEnforcingBackend(FakeLLMBackend):
    """高保真 provider stub：把「输入 + 输出预算 ≤ 声明窗口」当硬约束。

    记录每次 `chat()` 收到的 messages 与 kwargs，供断言"实际发出去的请求装得下"。
    """

    def __init__(self, *, window: int, model_id: str | None = None,
                 response: str = _STUB_REPLY) -> None:
        super().__init__(response=response)
        self.window = window
        self.model_id = model_id
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages: list[dict], **kwargs: Any) -> str:
        # 与真实 provider 同口径：按 resolved model 的估算 profile 计数
        input_tokens = estimate_messages_tokens(messages, model=self.model_id)
        requested = kwargs.get("max_tokens")
        reserved = int(requested) if isinstance(requested, int) else 0
        self.calls.append({
            "messages": messages,
            "input_tokens": input_tokens,
            "reserved_output": reserved,
        })
        if input_tokens + reserved > self.window:
            raise _ProviderContextLengthError(
                f"This model's maximum context length is {self.window} tokens. "
                f"However, your messages resulted in {input_tokens} tokens."
            )
        return self.response


def _resolved_window(model: str) -> int:
    """现算：resolved model 的**有效**窗口（不写死数字）。

    未声明窗口的第三方 provider 按既有兼容策略取保守默认（与预算同一个回退点）。
    """
    from Module.llm.registry import LLMFactory

    return effective_context_window(LLMFactory.context_window(model))


def _cjk(count: int, char: str) -> str:
    return char * count


def _prompt_profile(
    core: int, relationship: int, speaking: int, emotion: int, initiative: int
) -> dict[str, Any]:
    """合成 promptProfile：每段都是纯 CJK，故 token 数与字数 1:1（estimator 语义）。"""
    return {
        "corePersonality": _cjk(core, "人"),
        "relationshipStyle": _cjk(relationship, "关"),
        "emotionStyle": _cjk(emotion, "情"),
        "speakingStyle": _cjk(speaking, "语"),
        "conversationInitiative": _cjk(initiative, "主"),
        "examples": [],
    }


def _draft(
    *, core: int = 3000, relationship: int = 800, speaking: int = 800,
    emotion: int = 400, initiative: int = 300,
) -> CharacterDraft:
    return CharacterDraft(
        displayName="窗口探针",
        description="预览窗口探针。",
        aliases=[],
        promptProfile=_prompt_profile(core, relationship, speaking, emotion, initiative),
    )


def _wire_draft_for(draft: CharacterDraft) -> dict[str, Any]:
    """域 draft → 与前端同形的 wire payload。

    wire 上的 `runtimeDefaults` / `presentation` 是**必填结构**（契约模型），而域
    draft 的默认值是空 dict；真实前端总是发完整结构，故这里补齐同样的形状。
    """
    raw = draft.runtimeDefaults.model_dump(mode="json")
    voice = dict(raw.get("voice") or {})
    voice.setdefault("providerId", None)
    voice.setdefault("voiceId", None)
    emotion = dict(raw.get("emotion") or {})
    emotion.setdefault("idleExpressions", ["neutral"])
    proactive = dict(raw.get("proactive") or {})
    proactive.setdefault("enabled", False)
    proactive.setdefault("intervalSeconds", 0)
    # 内置角色 YAML 的 triggers 是结构化对象，而 service 在 wire 边界已归一化为
    # 字符串 id（`helpers._normalise_revision`）——前端回填的 draft 正是这个形状。
    proactive["triggers"] = [
        value if isinstance(value, str) else str(value.get("type", ""))
        for value in proactive.get("triggers") or []
    ]
    runtime = {
        "voice": voice,
        "emotion": emotion,
        "wakeWords": list(raw.get("wakeWords") or []),
        "proactive": proactive,
    }
    presentation = draft.presentation.model_dump(mode="json")
    for key in ("avatarAssetId", "live2dModelId", "themeColor"):
        presentation.setdefault(key, None)
    return {
        "displayName": draft.displayName,
        "description": draft.description,
        "aliases": list(draft.aliases),
        "promptProfile": draft.promptProfile.model_dump(mode="json"),
        "runtimeDefaults": runtime,
        "presentation": presentation,
        "openingMessage": draft.openingMessage,
    }


def _builtin_draft(role: str) -> CharacterDraft:
    _definition, revision, _instance = BuiltinCharacterSource().load(role)
    return CharacterDraft(
        displayName=revision.displayName,
        description=revision.description,
        aliases=list(revision.aliases),
        promptProfile=revision.promptProfile,
        runtimeDefaults=revision.runtimeDefaults,
        presentation=revision.presentation,
    )


def _service(port: Any, store: Any) -> CharacterService:
    return CharacterService(
        repo=CharacterRepository(db=FakeDocumentDBBackend()),
        rev_repo=CharacterRevisionRepository(db=FakeDocumentDBBackend()),
        catalog=CharacterCatalog(source=BuiltinCharacterSource()),
        user_identity_store=store,
        preview_port=port,
    )


async def _assemble_preview_tokens(
    draft: CharacterDraft, user_message: str, *, budget: int | None,
    model_id: str | None = _OLLAMA_MODEL,
) -> int:
    """用**生产组装路径**（PromptAssembler）现算预览 prompt 的估算 token 数。

    仅供测试计算"未加预算 / 加预算"两个前提，不发任何 provider 请求。
    """
    from core.prompt.prompt_assembler import PromptAssembler

    profile = _preview_profile(draft)
    assembler = PromptAssembler(
        character=profile.config, session=None, long_term=None)
    assembler.enable_preview_mode()
    assembler.set_character(profile.config.name, profile.config.name, profile)
    messages = await assembler.build_messages(
        user_message=user_message,
        user_name="妖梦",
        user_pronoun_override="她",
        user_timezone_override="Asia/Shanghai",
        prompt_budget_tokens=budget,
        model_id=model_id,
    )
    return estimate_messages_tokens(messages, model=model_id)


def _preview_profile(draft: CharacterDraft):  # noqa: ANN202
    from core.character.definition import CharacterDefinition
    from core.character.revision import CharacterRevision
    from core.character.runtime_profile import compile_runtime_profile

    definition = CharacterDefinition(
        characterId="chr_preview_probe", slug="probe", source="custom",
        mutable=True, status="active", currentRevision=1, ownerUserId="usr",
    )
    revision = CharacterRevision(
        characterId="chr_preview_probe", revision=1,
        displayName=draft.displayName, description=draft.description,
        aliases=[], promptProfile=draft.promptProfile.model_dump(mode="python"),
        runtimeDefaults=draft.runtimeDefaults.model_dump(mode="python"),
        presentation=draft.presentation.model_dump(mode="python"),
        checksum="0" * 64,
    )
    return compile_runtime_profile(definition=definition, revision=revision)


class _PreviewHarness:
    """真实 Agent/app 装配 + 窗口约束 stub provider 的路由级夹具。"""

    def __init__(self, client: Any, backend: _WindowEnforcingBackend) -> None:
        self.client = client
        self.backend = backend

    async def preview(
        self, draft: dict[str, Any], user_message: str
    ) -> tuple[int, dict]:
        resp = await self.client.post(
            "/api/characters/preview",
            json={"draft": draft, "userMessage": user_message},
        )
        return resp.status, await resp.json()


@pytest_asyncio.fixture
async def preview_env(aiohttp_client, monkeypatch):
    """构造：真实 create_app + 真实 CharacterService + 真实 preview port + 窗口 stub。

    返回一个工厂 `make(model_id) -> _PreviewHarness`，便于同一测试内切换模型。
    """
    from core.adapter.llm_adapter import LLMAdapter
    from core.config import AppConfig
    from core.input.server import create_app

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    created: list[Any] = []

    async def make(model_id: str) -> _PreviewHarness:
        from core.agent import Agent

        agent = Agent(config=AppConfig())
        app = create_app(agent, serve_static=False)
        window = _resolved_window(model_id)
        backend = _WindowEnforcingBackend(window=window, model_id=model_id)
        llm = LLMAdapter(model=model_id, _test_backend=backend)
        # 与生产同构的本地身份快照（非权威模式）——权威模式在无 Mongo 的
        # 单测里恒 fail-closed 503，那是另一条已验证的语义（P0-3），不是本文件的对象。
        store = UserIdentityStore()
        store.bootstrap("妖梦")
        app["character_service"] = _service(
            PromptCharacterPreviewAdapter(llm), store)
        client = await aiohttp_client(app)
        created.append(agent)
        return _PreviewHarness(client, backend)

    yield make

    for agent in created:
        await agent.stop()


# ── PPW-T1：小窗口模型上的预览（F8 原始场景）─────────────────────────────────


@pytest.mark.asyncio
async def test_preview_fits_small_window_when_trimming_suffices(preview_env) -> None:
    """PPW-1/PPW-3：可裁剪到窗口内的预览必须成功，且 provider 只收到装得下的请求。

    PRE_FIX_RED：修前预览不传预算 → 请求超窗 → provider 400 → 路由 503。
    """
    harness = await preview_env(_OLLAMA_MODEL)
    window = _resolved_window(_OLLAMA_MODEL)
    # Batch 18：尺寸重新校准。旧尺寸（core=3000/emotion=400 + 1300 字）建立在
    # "CJK 1 字 1 token"的旧估算上；估算修正后它已装得下窗口，本用例失去鉴别力。
    # 现在把**可丢节**（C3/Emotion）调大，使"裁剪前后"有 ~1900 token 的真实落差。
    draft = _draft(emotion=3000)
    message = "在吗？" + _cjk(4000, "呀")

    # 前提现算（不写死）：未加预算超窗；加预算装得下 —— 这就把本用例钉在边界上。
    untrimmed = await _assemble_preview_tokens(draft, message, budget=None)
    trimmed = await _assemble_preview_tokens(
        draft, message, budget=budget_from_context_window(window))
    assert untrimmed > window, (
        f"前提不成立：未加预算的预览 prompt（{untrimmed}）没有超过窗口（{window}）——"
        "角色/prompt 模板已变化，请重新挑选尺寸以保持本用例的鉴别力")
    assert trimmed <= window, (
        f"前提不成立：加预算后（{trimmed}）仍装不下窗口（{window}）")

    status, body = await harness.preview(_wire_draft_for(draft), message)

    assert status == 200, f"预览应成功，实际 {status}: {body}"
    assert body["reply"] == _STUB_REPLY
    # 防假绿：provider 必须真的被调了一次，且收到的请求确实装得下。
    assert len(harness.backend.calls) == 1
    assert harness.backend.calls[0]["input_tokens"] <= window


# ── PPW-T2：大窗口 provider 不回归（ALREADY_GREEN_GUARD）────────────────────


@pytest.mark.asyncio
async def test_preview_large_window_provider_unchanged(preview_env) -> None:
    """PPW-5：大窗口 provider 上同样的（超小窗口）请求必须继续成功。"""
    large_window = _resolved_window(_LARGE_MODEL)
    small_window = _resolved_window(_OLLAMA_MODEL)
    draft = _draft(emotion=3000)
    message = "在吗？" + _cjk(4000, "呀")

    harness = await preview_env(_LARGE_MODEL)
    status, body = await harness.preview(_wire_draft_for(draft), message)

    assert status == 200, f"{body}"
    assert body["reply"] == _STUB_REPLY
    assert len(harness.backend.calls) == 1
    # 这份请求本来就在大窗口内 —— 修复不得把它截成"另一种东西"。
    assert small_window < harness.backend.calls[0]["input_tokens"] <= large_window


# ── PPW-T3：边界（刚好装得下 / 刚好装不下）──────────────────────────────────


@pytest.mark.asyncio
async def test_preview_boundary_sends_when_fits_and_validates_when_not(
    preview_env,
) -> None:
    """PPW-1/PPW-4：留有余量 → 发一次；**恰好占满窗口 → 本地拒绝且不发请求**。

    边界语义刻意钉成 `估算输入 >= 窗口 ⇒ 拒绝`（Challenger H）：`== 窗口` 时
    已无输出余量（预览要的是"一句回复"），属"装得下但生成不出东西"，同样不发。
    """
    harness = await preview_env(_OLLAMA_MODEL)
    window = _resolved_window(_OLLAMA_MODEL)
    budget = budget_from_context_window(window)
    draft = _draft()

    # 现算边界：系统 prompt（裁剪后）为 base；用户消息是纯 CJK。Batch 18 起每字符
    # 成本不再是 1（旧公式被证伪），且估算含向上取整 ⇒ 闭式公式算不准，**二分找**
    # 最小的 m 使 `est(m) >= window`（这正是被钉住的那条边界）。
    base = await _assemble_preview_tokens(draft, "", budget=budget)
    assert base < window, f"前提不成立：system prompt 自身 {base} >= 窗口 {window}"
    lo, hi = 1, 40000
    while lo < hi:
        mid = (lo + hi) // 2
        if await _assemble_preview_tokens(draft, _cjk(mid, "呀"), budget=budget) >= window:
            hi = mid
        else:
            lo = mid + 1
    exact = lo
    assert exact > 2, f"边界字面量不合法：{exact}"
    low = _cjk(exact - 1, "呀")
    high = _cjk(exact, "呀")

    fits = await _assemble_preview_tokens(draft, low, budget=budget)
    over = await _assemble_preview_tokens(draft, high, budget=budget)
    assert fits < window <= over, (
        f"边界前提不成立：fits={fits} over={over} window={window}")
    assert over - fits <= 3, f"边界不够紧：fits={fits} over={over}"

    status_fit, body_fit = await harness.preview(_wire_draft_for(draft), low)
    assert status_fit == 200, f"{body_fit}"
    assert len(harness.backend.calls) == 1
    assert window - 3 <= harness.backend.calls[0]["input_tokens"] < window

    status_over, body_over = await harness.preview(_wire_draft_for(draft), high)
    assert status_over == 400, f"占满窗口应本地拒绝，实际 {status_over}: {body_over}"
    assert body_over["code"] == "CHARACTER_VALIDATION"
    # 已知必失败的请求不得发出去。
    assert len(harness.backend.calls) == 1


# ── PPW-T4：输入本身超过整个窗口（不可能请求）───────────────────────────────


@pytest.mark.asyncio
async def test_preview_impossible_input_fails_locally_without_provider_call(
    preview_env,
) -> None:
    """PPW-4：连窗口都装不下的角色档案必须在本地失败，不发已知必 400 的请求。"""
    harness = await preview_env(_OLLAMA_MODEL)
    window = _resolved_window(_OLLAMA_MODEL)
    # Batch 18：估算修正后旧尺寸（core=6000…）裁剪后已装得下；按角色档案上限
    # （corePersonality 12000 / relationshipStyle 6000）重新取尺寸。
    draft = _draft(core=9000, relationship=3000, speaking=3000,
                   emotion=2000, initiative=2000)

    trimmed = await _assemble_preview_tokens(
        draft, "在吗？", budget=budget_from_context_window(window))
    assert trimmed > window, (
        f"前提不成立：裁剪后 {trimmed} 未超过窗口 {window}，本用例失去意义")

    status, body = await harness.preview(_wire_draft_for(draft), "在吗？")

    assert status == 400, f"应本地拒绝，实际 {status}: {body}"
    assert body["code"] == "CHARACTER_VALIDATION"
    assert harness.backend.calls == [], "不得发送已知必失败的 provider 请求"


# ── PPW-T5：模型权威（config/bootstrap ≠ resolved runtime model）────────────


@pytest.mark.asyncio
async def test_preview_budget_follows_resolved_model_not_config(preview_env) -> None:
    """PPW-2：预算必须来自 **resolved runtime model**，不是 config/bootstrap 模型。

    `AppConfig()` 的 `llm.model` 默认是大窗口的 `deepseek/deepseek-chat`，而运行期
    adapter 解析到小窗口 `ollama/qwen2.5:3b`（等价于一次 `switch_model`）。若预算读
    config（或任何 provider-wide 假设），这份请求会以 8000+ token 发出去并被 400。
    """
    from core.config import AppConfig

    assert AppConfig().llm.model == _LARGE_MODEL, "本用例依赖 config 默认模型是大窗口"
    small_window = _resolved_window(_OLLAMA_MODEL)

    harness = await preview_env(_OLLAMA_MODEL)
    draft = _draft(emotion=3000)
    message = "在吗？" + _cjk(4000, "呀")

    status, body = await harness.preview(_wire_draft_for(draft), message)

    assert status == 200, f"{body}"
    assert len(harness.backend.calls) == 1
    assert harness.backend.calls[0]["input_tokens"] <= small_window, (
        "预览用了 config/大窗口的权威，而不是 resolved runtime model 的窗口")


# ── 正控制：小窗口模型并非"任何预览都不可能"（§54）────────────────────────


@pytest.mark.asyncio
async def test_small_window_model_can_preview_a_small_request(preview_env) -> None:
    """正控制：小窗口模型 + 小请求必须成功（否则"修后成功"可能只是绕过 provider）。"""
    harness = await preview_env(_OLLAMA_MODEL)
    window = _resolved_window(_OLLAMA_MODEL)
    draft = _builtin_draft("cyrene")

    status, body = await harness.preview(_wire_draft_for(draft), "在吗？")

    assert status == 200, f"{body}"
    assert body["reply"] == _STUB_REPLY
    assert len(harness.backend.calls) == 1
    assert harness.backend.calls[0]["input_tokens"] <= window


# ── PPW-T6：未声明窗口的第三方 provider（兼容策略）──────────────────────────


@pytest.mark.asyncio
async def test_preview_undeclared_window_keeps_compat_policy(preview_env) -> None:
    """§20：没有 contextWindow metadata 的 provider 维持既有兼容策略。

    `LLMFactory.context_window` 对未声明 provider 返回 None；预览必须回退到与预算
    同一个保守默认（不是"无限大"、也不是"0 ⇒ 永远失败"），且不崩溃。
    """
    from Module.llm.registry import LLMFactory

    unknown = "thirdparty-unknown/some-model"
    assert LLMFactory.context_window(unknown) is None

    harness = await preview_env(unknown)
    status, body = await harness.preview(_wire_draft_for(_draft()), "在吗？")

    assert status == 200, f"{body}"
    assert body["reply"] == _STUB_REPLY
    assert len(harness.backend.calls) == 1
    assert harness.backend.calls[0]["input_tokens"] <= effective_context_window(None)


# ── 内置角色（F8 原始证据场景）：留档其真实规模 ─────────────────────────────


@pytest.mark.asyncio
async def test_builtin_character_preview_records_window_margin(preview_env) -> None:
    """内置 elysia 预览在小窗口 provider 上的真实余量（F8 原始证据的现算版本）。

    这条刻意**不**断言具体数字，只断言不变量：预览成功，且 provider 收到的请求装得下。
    """
    harness = await preview_env(_OLLAMA_MODEL)
    window = _resolved_window(_OLLAMA_MODEL)
    draft = _builtin_draft("elysia")

    status, body = await harness.preview(_wire_draft_for(draft), "在吗？" + _cjk(700, "呀"))

    assert status == 200, f"{body}"
    assert len(harness.backend.calls) == 1
    assert harness.backend.calls[0]["input_tokens"] <= window
