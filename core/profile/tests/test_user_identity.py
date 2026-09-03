import pytest
pytestmark = pytest.mark.unit   # 纯模型，无 I/O
from pydantic import ValidationError
from core.profile.user_identity import UserIdentity


def test_user_identity_defaults():
    ui = UserIdentity()
    assert ui.userId == "usr_local_default"
    assert ui.displayName == ""
    assert ui.pronouns == "她"
    assert ui.locale == "zh-CN"
    assert ui.timezone == "Asia/Shanghai"
    assert ui.initialized is False
    assert ui.revision == 1


def test_user_identity_custom():
    ui = UserIdentity(displayName="妖梦", pronouns="她", timezone="Asia/Shanghai")
    assert ui.displayName == "妖梦"
    assert ui.pronouns == "她"


def test_user_identity_revision_validation():
    with pytest.raises(ValidationError):
        UserIdentity(revision=0)
