"""pytest conftest — 集成测试模块级 .env 加载。

在测试收集阶段自动加载项目根目录的 .env 文件。
各测试文件中分散的 .env 加载代码可逐步删除。
"""

from pathlib import Path

# ── 加载 .env ────────────────────────────────────────────────────────────────────

_env_file = Path(__file__).resolve().parents[4] / ".env"
# 路径：conftest.py → integration/ → tests/ → task_system/ → Module/ → Agent/ → .env
# parents[0] = conftest.py 所在目录 (integration/)
# parents[4] = Agent/ (repo root)

if _env_file.exists():
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _value = _line.partition("=")
                _key = _key.strip()
                _value = _value.strip().strip('"').strip("'")
                import os
                if _key not in os.environ:
                    os.environ[_key] = _value
