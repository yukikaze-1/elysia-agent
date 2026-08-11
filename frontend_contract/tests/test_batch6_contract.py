"""批次 6 契约测试——三种语音 + TTS 播放控制消息。"""
from typing import Annotated, get_args

import pytest
from frontend_contract import (
    AudioCancelMessage, AudioCancelledMessage, AudioMessage, AudioStartMessage,
    DictationCancelledMessage, DictationFinalMessage, DictationPartialMessage,
    DictationStartMessage, DictationStopMessage, DictationCancelMessage,
    ServerInfoFeatures, ServerMessage, VoiceCommitMessage, VoiceDiscardMessage,
    VoiceRecordStartMessage, VoiceRecordStopMessage, VoiceSessionAckMessage,
    VoiceSessionEndedMessage, VoiceSessionPartialMessage, VoiceSessionStartedMessage,
    VoiceSessionStartMessage, VoiceSessionStopMessage, VoiceSessionTurnMessage,
    VoiceTranscriptMessage,
)

pytestmark = pytest.mark.unit


def _union_members():
    """ServerMessage 判别联合成员名——递归展开嵌套 Annotated（Task 6 UserAckMessage 别名）。

    ServerMessage = Annotated[Union[...], ...]；get_args(ServerMessage) → (Union, FieldInfo)；
    嵌套成员（UserAckMessage = Annotated[Union[Accepted, Rejected], ...]）递归剥皮后
    返回具体 BaseModel 名（否则含 "Annotated" 字面量）。
    """
    from typing import Union, get_origin

    def _expand(inner):
        if get_origin(inner) is Annotated:
            return _expand(get_args(inner)[0])
        if get_origin(inner) is Union:
            result = []
            for m in get_args(inner):
                result += _expand(m)
            return result
        return [inner]

    return {c.__name__ for c in _expand(get_args(ServerMessage)[0])}


def test_server_message_union_members():
    members = _union_members()
    for cls in (
        VoiceTranscriptMessage, VoiceSessionStartedMessage, VoiceSessionPartialMessage,
        VoiceSessionTurnMessage, VoiceSessionAckMessage, VoiceSessionEndedMessage,
        DictationPartialMessage, DictationFinalMessage, DictationCancelledMessage,
        AudioStartMessage, AudioCancelledMessage, AudioMessage,
    ):
        assert cls.__name__ in members, f"{cls.__name__} 未入 ServerMessage 联合"


def test_voice_commit_wire():
    m = VoiceCommitMessage(recordingId="rec_1", text="你好", clientRequestId="req_1")
    d = m.wire_dict()
    assert d["type"] == "voice.commit"
    assert d["text"] == "你好"


def test_audio_cancel_stream_id_key():
    m = AudioCancelMessage(stream_id=42)
    d = m.wire_dict()
    assert d == {"type": "audio.cancel", "stream_id": 42}


def test_audio_message_is_final_modeled():
    m = AudioMessage(stream_id=1, seq=0, data="eA==")
    assert m.is_final is False


def test_server_info_features_new_flags():
    f = ServerInfoFeatures()
    assert f.voice_recording is False and f.voice_realtime is False and f.dictation is False
