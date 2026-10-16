"""后端协议面检查 —— 把「协议要求哪些成员」变成可机器验证的事实。

## 为什么需要它（而不是 `isinstance` / 「碰巧访问时报错」）

TTS/STT 后端协议是 **ABC**（`Module/tts/protocol.py` / `Module/stt/protocol.py`），
但真正被注入生产路径的对象**大多不继承它**——测试替身是 duck-typed 的普通类，
而 `make pyright` 整片排除 `**/tests/**`。历史缺陷正是从这条缝里出来的：

    测试假 TTS 后端没实现协议成员 ``model``
    → ``build_server_info`` 裸读 ``backend.model``
    → AttributeError → 整个 ``server_info`` 帧消失（2026-09-27 修复）

三类"看起来像检查"的手段在这里都不成立：

| 手段 | 为什么不成立 |
|------|-------------|
| ``isinstance(x, TTSBackend)`` | duck-typed 替身**故意不继承**，恒 False；ABC 的 ``__subclasshook__`` 也不看数据成员。要它成立就得把替身改成继承——那是**换了一种实现**，不是检查 |
| 「反正 server_info 会读 ``.model``」 | 那是**碰巧**访问到的一个成员。某条测试从不调用 ``synthesize`` ⇒ 该实现违反协议也永远绿 |
| ``make pyright`` | 只覆盖生产代码（``exclude: **/tests/**``），且 `typeCheckingMode: basic` 下**不检查覆写形态**——实测把 ``async def`` 覆写成同步 ``def``、把 ``model`` 覆写成返回 ``None``，pyright 均报 **0 error**。它唯一抓得住的是 ``reportAbstractUsage``（继承者漏实现抽象成员 → 实例化即红） |

所以本模块负责**运行期可执行**的那一半：给定一个**对象**与它声称满足的**协议类**，
逐项回答"它真的满足吗"，并把答案做成可断言的列表。

## 判据为什么是"``dir(协议类)`` 的全部公开成员"

`required = {n for n in dir(protocol) if not n.startswith("_")}` —— 一个手写清单都
不需要。协议类**声明**了什么，实现就必须有什么：抽象成员（``synthesize`` …）与
"带默认实现的具体成员"（``model`` / ``list_voices`` …）在契约上一视同仁——
继承者漏写后者只会拿到默认值，duck-typed 替身漏写后者就是 ``AttributeError``。

这条判据**自动跟着协议走**：往协议里加一个成员，所有合规性检查立刻要求它存在。
"""

from __future__ import annotations

import inspect

__all__ = ["member_kind", "public_members", "violations"]


def public_members(protocol: type) -> frozenset[str]:
    """协议类声明的**全部**公开成员（抽象 + 带默认实现的具体成员）。

    Args:
        protocol: 协议类（如 ``Module.tts.protocol.TTSBackend``）。

    Returns:
        成员名集合，已滤掉 ``_`` 前缀与 dunder。
    """
    return frozenset(name for name in dir(protocol) if not name.startswith("_"))


def _declared_kind(protocol: type, name: str) -> str:
    """协议类上 ``name`` 的**声明形态**。

    刻意用 ``inspect.getattr_static``——它不触发 property getter，因此拿得到
    ``property`` 对象本身（``getattr`` 只会拿到它求出的值）。

    Returns:
        ``"property"`` / ``"coroutine"`` / ``"asyncgen"`` / ``"callable"`` / ``"data"``。
    """
    member = inspect.getattr_static(protocol, name)
    if isinstance(member, property):
        return "property"
    if isinstance(member, (staticmethod, classmethod)):
        member = member.__func__
    if inspect.isasyncgenfunction(member):
        return "asyncgen"
    if inspect.iscoroutinefunction(member):
        return "coroutine"
    if callable(member):
        return "callable"
    return "data"


def member_kind(target: object, name: str) -> str:
    """``target`` 上 ``name`` 的**取值形态**。

    与 ``_declared_kind`` 对称，但作用于实例并**真实取值**（property 会被调用）。

    Args:
        target: 任意对象。
        name: 成员名。

    Returns:
        同 ``_declared_kind`` 的取值集合，外加取值失败时的 ``"raises:<类型名>"``。
    """
    try:
        member = getattr(target, name)
    except Exception as exc:  # noqa: BLE001 —— 诊断工具：取值失败本身就是要报告的事实
        return f"raises:{type(exc).__name__}"
    if inspect.isasyncgenfunction(member):
        return "asyncgen"
    if inspect.iscoroutinefunction(member):
        return "coroutine"
    if callable(member):
        return "callable"
    return "data"


#: 声明形态 → 允许的实际形态。``coroutine`` 兼容 ``asyncgen``：TTS 协议里
#: ``synthesize_stream`` 是 ``async def`` + ``...``（协程函数），而各后端按
#: 语义实现成异步生成器——两者都是"可 ``async for``"之外的正确形态，不必强求一致。
_ACCEPTED: dict[str, frozenset[str]] = {
    "property": frozenset({"data"}),          # 值成员：能读出来，且不该是可调用对象
    "data": frozenset({"data"}),
    "coroutine": frozenset({"coroutine", "asyncgen"}),
    "asyncgen": frozenset({"asyncgen"}),
    "callable": frozenset({"callable", "coroutine", "asyncgen"}),
}


def violations(target: object, protocol: type) -> list[str]:
    """``target`` 相对 ``protocol`` 的契约违规清单（空列表 = 合规）。

    只回答**形状**问题（成员是否存在、是不是 async、能不能读），不回答语义
    （``model`` 的值对不对）——语义由各模块自己的 contract test 断言。

    Args:
        target: 被检查的对象（生产后端、测试替身、任何声称满足协议的东西）。
        protocol: 它声称满足的协议类。

    Returns:
        人类可读的违规说明列表，按成员名排序；合规时为空。
    """
    problems: list[str] = []
    for name in sorted(public_members(protocol)):
        if not hasattr(target, name):
            problems.append(f"{name}: 缺失（协议要求此成员存在）")
            continue
        declared = _declared_kind(protocol, name)
        actual = member_kind(target, name)
        if actual not in _ACCEPTED[declared]:
            problems.append(
                f"{name}: 形态不符——协议声明 {declared}，实现是 {actual}"
                f"（允许 {sorted(_ACCEPTED[declared])}）"
            )
    return problems
