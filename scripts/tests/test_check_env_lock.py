# 测试文件：scripts/tests/test_check_env_lock.py
"""S2-9：依赖锁校验必须做**版本比对**，不能只比包名。

原实现只检查"requirements.txt 的顶层包在锁的 pip 段有 pin"——把 `pydantic>=2.0`
改成 `pydantic>=99.0` 门禁**照样通过**（实测退出码 0），于是"声明的下限"与"实际装的
版本"可以任意脱节：环境重建时才发现装不上，而 CI 从没跑过那个组合。典型真实场景是
在 requirements.txt 抬了下限却忘了 `make lock-env`。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

from check_env_lock import compare, parse_lock_pins, parse_requirements  # noqa: E402

pytestmark = pytest.mark.unit

_LOCK_SAMPLE = """\
name: Elysia
dependencies:
  - pip:
      - aiohttp==3.14.3
      - pydantic==2.13.4
      - tzdata==2026.3
"""


def test_parse_lock_pins():
    pins = parse_lock_pins(_LOCK_SAMPLE)
    assert pins["aiohttp"] == "3.14.3"
    assert pins["pydantic"] == "2.13.4"


def test_missing_pin_is_reported():
    reqs = parse_requirements("aiohttp>=3.9\nbrandnew>=1.0\n")
    problems = compare(reqs, parse_lock_pins(_LOCK_SAMPLE))
    assert any("缺 pin" in p and "brandnew" in p for p in problems), problems


def test_locked_version_satisfies_floor():
    reqs = parse_requirements("pydantic>=2.0,<3\n")
    assert compare(reqs, parse_lock_pins(_LOCK_SAMPLE)) == []


def test_impossible_floor_is_reported():
    """**核心**：声明一个锁不满足的下限必须报出来（这正是原来漏掉的那条）。"""
    reqs = parse_requirements("pydantic>=99.0\n")
    problems = compare(reqs, parse_lock_pins(_LOCK_SAMPLE))
    assert len(problems) == 1, problems
    assert "版本不满足" in problems[0]
    assert "2.13.4" in problems[0]


def test_upper_bound_violated_is_reported():
    """上限方向同样要查（锁里是 3.x 而声明 `aiohttp<3`）。"""
    reqs = parse_requirements("aiohttp<3\n")
    problems = compare(reqs, parse_lock_pins(_LOCK_SAMPLE))
    assert any("版本不满足" in p for p in problems), problems


def test_pin_without_specifier_only_requires_presence():
    """无版本声明（如 `tzdata`）→ 只要求存在，不产生版本问题。"""
    reqs = parse_requirements("tzdata\n")
    assert compare(reqs, parse_lock_pins(_LOCK_SAMPLE)) == []


def test_comments_and_options_are_skipped():
    reqs = parse_requirements(
        "# 注释\n\n-r other.txt\npydantic>=2.0  # 行内注释\n")
    names = {r.name for r in reqs}
    assert names == {"pydantic"}, names


#: 允许不写上界的依赖 → 理由。**必须是显式豁免**：静默放过会让"新增依赖忘了写
#: 上界"和"有意不写"看起来一样。
_NO_UPPER_BOUND_OK = {
    "tzdata": "纯数据包（时区库），无 API 破坏面；版本按日历递增，上界无意义",
}


def test_every_declared_dep_has_an_upper_bound():
    """S2-9：仓库里的依赖声明**必须都带上限**（或在豁免表里给出理由）。

    没有上限时，依赖发大版本会在 CI 里静默换掉实现——本地（按锁）与 CI（按本文件）
    跑的是两套代码。这条按真实文件断言，新增依赖忘了写上界就会红。
    """
    text = (_ROOT / "requirements.txt").read_text(encoding="utf-8")
    offenders: list[str] = []
    for req in parse_requirements(text):
        spec = str(req.specifier)
        if "<" in spec:
            continue
        if req.name.lower() in _NO_UPPER_BOUND_OK:
            continue
        offenders.append(f"{req.name}{spec or '（无版本声明）'}")
    assert not offenders, (
        f"以下依赖没有上限：{', '.join(offenders)}"
        f"（若确无 API 破坏面，请加进 _NO_UPPER_BOUND_OK 并写明理由）")


def test_exemptions_are_still_declared():
    """豁免表不得腐烂：写进去的包必须真的还在 requirements.txt 里。"""
    text = (_ROOT / "requirements.txt").read_text(encoding="utf-8")
    names = {r.name.lower() for r in parse_requirements(text)}
    for name in _NO_UPPER_BOUND_OK:
        assert name in names, f"{name} 已不在 requirements.txt——请删掉该豁免"


def test_repo_lock_is_consistent():
    """真实文件之间的比对必须通过（与 CI 的同一条判据）。"""
    from check_env_lock import _LOCK, _REQS

    reqs = parse_requirements(_REQS.read_text(encoding="utf-8"))
    pins = parse_lock_pins(_LOCK.read_text(encoding="utf-8"))
    assert compare(reqs, pins) == []


# ══════════════════════════════════════════════════════════════════
# S2-9：镜像 pin 级别 —— 它保证的是「不会静默漂移」，与「是否升级」无关
# ══════════════════════════════════════════════════════════════════

#: 数据服务的镜像 pin 级别 → 理由。**必须是显式的**：静默允许浮动 tag 就等于允许
#: `docker compose pull` 在无人察觉时换掉存储引擎版本（数据卷就地升级，不可回退）。
_IMAGE_PIN_POLICY = {
    "mongo:8": "MongoDB 单节点：主版本内小版本可就地升级，允许在 8.x 内浮动",
    "quay.io/coreos/etcd:v3.5.14": "精确 pin（Milvus 元数据存储）",
    "minio/minio:RELEASE.2023-03-20T20-16-18Z": (
        "精确 pin（Milvus 的对象存储）。tag 来自 Milvus 官方三件套示例，"
        "**不为版本新旧本身升级**——升级依据应来自容器镜像 CVE 扫描结果"
        "（pip-audit 不覆盖容器镜像），见 CODE_REVIEW_REPORT §0.4.6"),
    "milvusdb/milvus:v2.6.22": "精确 pin（向量库）",
}


def test_data_service_images_are_explicitly_pinned():
    """每个镜像都必须显式声明 tag，且 pin 级别在策略表里有理由。

    没有 tag（隐式 `latest`）或 pin 级别变化而没更新策略表 → 红。这样"某次
    `docker compose pull` 换掉了存储引擎版本"就不会是无声事件。
    """
    import yaml

    doc = yaml.safe_load((_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    for name, svc in doc["services"].items():
        image = svc.get("image")
        assert image, f"{name} 未声明 image"
        assert ":" in image.rsplit("/", 1)[-1], (
            f"{name} 的镜像 {image} 没有显式 tag —— 隐式 latest 会在 pull 时漂移")
        assert image in _IMAGE_PIN_POLICY, (
            f"{name} 的镜像 {image} 不在 _IMAGE_PIN_POLICY 中——"
            f"请在策略表里登记它的 pin 级别与理由")


def test_pin_policy_table_has_no_stale_entries():
    """策略表不得腐烂：登记过的镜像必须仍在使用。"""
    import yaml

    doc = yaml.safe_load((_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    used = {svc.get("image") for svc in doc["services"].values()}
    for image in _IMAGE_PIN_POLICY:
        assert image in used, f"{image} 已不在 compose 中——请删掉该条目"
