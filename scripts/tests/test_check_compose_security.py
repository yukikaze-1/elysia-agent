"""Compose 网络与凭据策略门禁测试（P1-19，marker=unit）。

验证 scripts/check_compose_security.py：
- 拒绝 0.0.0.0 / 裸端口发布的数据服务、minioadmin/root/空 token 等默认凭据、
  生产 profile 发布数据库管理端口。
- 通过当前仓库 docker-compose.yml（数据服务仅 loopback 端口 + ${VAR:?} 强制凭据）。
- 非数据服务发布 0.0.0.0 不在本门禁范围内（策略只约束数据库基础设施）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.check_compose_security import check_compose_file, check_compose_text

pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _dump(services: dict) -> str:
    """把服务字典序列化为 compose YAML 文本（供 check_compose_text 解析）。"""
    return yaml.safe_dump({"services": services}, sort_keys=False)


class TestRejectInsecureCompose:
    """默认暴露 / 默认凭据的 compose 必须被拒绝（P1-19）。"""

    def test_0_0_0_0_data_port_rejected(self):
        text = _dump({
            "mongodb": {
                "image": "mongo:8",
                "ports": ["0.0.0.0:27017:27017"],
                "environment": {"MONGO_INITDB_ROOT_PASSWORD": "${MONGO_PW:?err}"},
            }
        })
        violations = check_compose_text(text)
        assert any("mongodb" in v and "0.0.0.0" in v for v in violations), violations

    def test_bare_port_rejected(self):
        """裸 `27017:27017`（未绑定 host_ip）等价于 0.0.0.0，必须拒绝。"""
        text = _dump({
            "mongodb": {"image": "mongo:8", "ports": ["27017:27017"]},
        })
        violations = check_compose_text(text)
        assert any("mongodb" in v and "127.0.0.1" in v for v in violations), violations

    def test_minioadmin_rejected(self):
        text = _dump({
            "minio": {
                "image": "minio/minio:latest",
                "environment": {"MINIO_ROOT_PASSWORD": "minioadmin"},
            }
        })
        violations = check_compose_text(text)
        assert any("minioadmin" in v for v in violations), violations

    def test_empty_token_rejected(self):
        """空 token（MILVUS_TOKEN: \"\"）必须被拒绝。"""
        text = _dump({
            "milvus": {
                "image": "milvusdb/milvus:latest",
                "environment": {"MILVUS_TOKEN": ""},
            }
        })
        violations = check_compose_text(text)
        assert any("MILVUS_TOKEN" in v and "空值" in v for v in violations), violations

    def test_root_username_rejected(self):
        text = _dump({
            "mongodb": {
                "image": "mongo:8",
                "environment": {
                    "MONGO_INITDB_ROOT_USERNAME": "root",
                    "MONGO_INITDB_ROOT_PASSWORD": "${MONGO_PW:?err}",
                },
            }
        })
        violations = check_compose_text(text)
        assert any("MONGO_INITDB_ROOT_USERNAME" in v for v in violations), violations

    def test_production_management_port_rejected(self):
        """生产 profile 发布 MinIO console(9001) 必须被拒绝。"""
        text = _dump({
            "minio-prod": {
                "image": "minio/minio:latest",
                "profiles": ["production"],
                "ports": ["127.0.0.1:9001:9001"],
                "environment": {
                    "MINIO_ROOT_PASSWORD": "${MINIO_PW:?err}",
                },
            }
        })
        violations = check_compose_text(text)
        assert any("minio-prod" in v and "9001" in v for v in violations), violations

    def test_unparseable_yaml_rejected(self):
        violations = check_compose_text("services:\n  mongodb: [unclosed")
        assert violations and "解析失败" in violations[0]


class TestAcceptSecureCompose:
    """已加固的 compose 必须通过（P1-19 验收）。"""

    def test_current_docker_compose_passes(self):
        """当前仓库 docker-compose.yml 即 good 用例。"""
        violations = check_compose_file(_PROJECT_ROOT / "docker-compose.yml")
        assert violations == [], "\n".join(violations)

    def test_required_ref_with_chinese_message_passes(self):
        """${VAR:?中文消息} 属于强制引用，不构成默认凭据。"""
        text = _dump({
            "mongodb": {
                "image": "mongo:8",
                "ports": ["127.0.0.1:27017:27017"],
                "environment": {
                    "MONGO_INITDB_ROOT_USERNAME": "${MONGO_USER:?在 .env 设置}",
                    "MONGO_INITDB_ROOT_PASSWORD": "${MONGO_PW:?在 .env 设置}",
                },
            }
        })
        assert check_compose_text(text) == []

    def test_non_data_service_0_0_0_0_out_of_scope(self):
        """非数据服务（如应用容器）发布 0.0.0.0 不在本门禁范围（只约束数据库基础设施）。"""
        text = _dump({
            "app": {"image": "elysia:latest", "ports": ["8080:8080"]},
        })
        assert check_compose_text(text) == []

    def test_production_data_port_allowed(self):
        """生产 profile 发布后端必需的数据端口（127.0.0.1）允许，但不得是管理端口。"""
        text = _dump({
            "milvus-prod": {
                "image": "milvusdb/milvus:v2.6.22",
                "profiles": ["production"],
                "ports": ["127.0.0.1:19530:19530"],
                "environment": {
                    "common.security.defaultRootPassword": "${MILVUS_ROOT_PW:?err}",
                },
            }
        })
        assert check_compose_text(text) == []
