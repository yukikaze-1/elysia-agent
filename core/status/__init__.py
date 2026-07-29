"""core/status/ — Agent 运行时状态接口层。

protocol.py — AgentStatus（对外只读）+ CognitivePhaseController（对内写），纯接口，零依赖。
fsm.py     — FSMBackedStatus + FSMBackedPhaseController，基于 AgentFSM 的实现。
"""
from core.status.protocol import AgentStatus, CognitivePhaseController
from core.status.fsm import FSMBackedStatus, FSMBackedPhaseController

__all__ = [
    "AgentStatus",
    "CognitivePhaseController",
    "FSMBackedStatus",
    "FSMBackedPhaseController",
]
