"""破坏性批量清空操作的**类型化确认**守卫。

## 背景

`DELETE /debug/api/{ltm,archive,sessions}` 会不可逆地清空整个集合——`ltm` 更是
`drop + recreate collection`。此前服务端**没有任何守卫**：前端 `ConfirmModal` 只是
客户端礼貌，`curl`、脚本、UI 回归都能直接抹库，且无法回滚。

（同类事故已发生过一次：`test_ltm_clear_all` 跑一遍就清空了真实
`long_term_memory_v2`——当时靠测试库隔离堵住，但守卫本身始终缺失。）

## 约定

调用方必须**报出目标资源名**作为确认（`?confirm=<token>`）；不匹配即 fail-closed
抛 `DestructiveConfirmRequired`。与 `scripts/*` 的「默认 dry-run、`--apply` 才实跑」
同源：破坏性操作必须显式表达意图，不能靠默认值兜底。

## 为什么令牌是稳定资源名，而不是物理集合名

每个仓储以类属性 `CONFIRM_TOKEN` 声明自己的令牌，**与 `COLLECTION` 解耦**：

1. 同一个逻辑资源可能落在不同物理集合——`LongTermMemory` 注入生产
   `MemoryRepositoryV2`（`long_term_memory_v2`）时走 v2，而独立调用（如
   `scripts/clean_db.py`）走临时 `MemoryRepository`（`long_term_memory`）。
   若令牌取物理集合名，**客户端就得猜服务端装配**，猜错即 400——把内部接线
   泄漏成了外部契约。
2. 测试防线（`core/tests/_real_db_guard.py`）会把 `COLLECTION` 重写为 `*__test`，
   物理集合名在测试中不稳定；稳定令牌不受影响。

令牌的**权威校验仍在存储层**（`require_confirm(target=self.CONFIRM_TOKEN, ...)`），
HTTP 层只负责「必须提供」的存在性检查，并把存储层错误映射为 400。
"""

from __future__ import annotations


class DestructiveConfirmRequired(ValueError):
    """破坏性操作缺少/错误的类型化确认（fail-closed）。

    `ValueError` 子类：属于调用方 API 误用，而非存储故障——不应被映射为 503。
    """


def require_confirm(*, target: str, confirm: str | None, operation: str) -> None:
    """校验破坏性操作的类型化确认。

    Args:
        target: 期望的确认令牌（本仓储的 `CONFIRM_TOKEN`）。
        confirm: 调用方提供的确认值。
        operation: 操作的中文名，用于错误信息。

    Raises:
        DestructiveConfirmRequired: confirm 与 target 不一致（含缺失/空串）。
    """
    if confirm != target:
        raise DestructiveConfirmRequired(
            f"{operation}是破坏性且不可逆的操作，需显式确认目标资源："
            f"confirm={target!r}（实际收到 {confirm!r}）"
        )
