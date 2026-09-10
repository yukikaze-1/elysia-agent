"""scripts/clean_db.py 测试。"""
import pytest
from unittest.mock import AsyncMock, patch, call
from pathlib import Path
import sys

# 将 scripts/ 加入 path，以便 import clean_db
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

pytestmark = pytest.mark.unit


class TestCollectionScope:
    """清理范围单源——删库脚本的成员清单必须被钉死。

    这里刻意把集合名字面量写死（而非引用常量）：本测试的职责就是「删哪些集合」
    这份契约本身。改名/增删应当让这个测试红，而不是静默跟着走。
    """

    def test_conversation_scope_is_exactly_the_six_conversation_collections(self):
        from clean_db import CONVERSATION_COLLECTIONS  # type: ignore[reportMissingImports]

        assert CONVERSATION_COLLECTIONS == (
            "sessions", "message_archive", "token_usage",
            "generations", "idempotency", "desktop_calls",
        )

    def test_full_scope_adds_character_and_attachment_collections(self):
        from clean_db import (  # type: ignore[reportMissingImports]
            FULL_ONLY_COLLECTIONS, collections_for)

        assert collections_for("full") == (
            collections_for("conversation") + FULL_ONLY_COLLECTIONS)
        assert set(FULL_ONLY_COLLECTIONS) == {
            "characters", "character_revisions", "character_instances",
            "character_versions", "character_l2_versions", "attachments",
        }

    def test_neither_scope_touches_user_identity(self):
        """用户身份是 Stage H Mongo 权威（BE-STAGEH-02），删了会退回 bootstrap。

        两种范围都不得含它——这是回归守卫：谁把它加进清单，这里必须红。
        """
        from clean_db import (  # type: ignore[reportMissingImports]
            USER_IDENTITY_COLLECTION, collections_for)

        assert USER_IDENTITY_COLLECTION == "user_identity_profiles"
        for scope in ("conversation", "full"):
            assert USER_IDENTITY_COLLECTION not in collections_for(scope)

    def test_unknown_scope_is_rejected(self):
        from clean_db import collections_for  # type: ignore[reportMissingImports]

        with pytest.raises(ValueError):
            collections_for("everything")


class TestMongoCleaner:
    @pytest.mark.asyncio
    async def test_full_scope_deletes_every_collection(self):
        from clean_db import MongoCleaner, collections_for  # type: ignore[reportMissingImports]

        mock_db = AsyncMock()
        mock_db.delete_many = AsyncMock(return_value=10)
        mock_db.close = AsyncMock()

        with patch(
            "clean_db.DocumentDBFactory.create", return_value=mock_db
        ) as mock_create:
            cleaner = MongoCleaner()
            result = await cleaner.clean()

        mock_create.assert_called_once_with("mongodb/default")

        scope = collections_for("full")
        assert mock_db.delete_many.call_args_list == [call(c, {}) for c in scope]
        assert result == [(c, True, 10) for c in scope]
        mock_db.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_conversation_scope_deletes_only_conversation_collections(self):
        """--conversation-only：只动对话数据，角色/身份/附件一概不碰。"""
        from clean_db import (  # type: ignore[reportMissingImports]
            CONVERSATION_COLLECTIONS, MongoCleaner)

        mock_db = AsyncMock()
        mock_db.delete_many = AsyncMock(return_value=3)
        mock_db.close = AsyncMock()

        with patch("clean_db.DocumentDBFactory.create", return_value=mock_db):
            result = await MongoCleaner(scope="conversation").clean()

        assert mock_db.delete_many.call_args_list == [
            call(c, {}) for c in CONVERSATION_COLLECTIONS]
        assert [r[0] for r in result] == list(CONVERSATION_COLLECTIONS)

        deleted = {c.args[0] for c in mock_db.delete_many.call_args_list}
        assert deleted & {
            "characters", "character_revisions", "character_instances",
            "character_versions", "character_l2_versions", "attachments",
            "user_identity_profiles",
        } == set()

    @pytest.mark.asyncio
    async def test_one_collection_failure_does_not_stop_the_rest(self):
        """任一集合失败不得中断其余集合，且失败要如实进报告（否则退出码会假绿）。"""
        from clean_db import MongoCleaner, collections_for  # type: ignore[reportMissingImports]

        mock_db = AsyncMock()
        mock_db.close = AsyncMock()
        attempted: list[str] = []

        async def delete_many_side_effect(collection, filter_expr):
            attempted.append(collection)
            if collection == "message_archive":
                raise RuntimeError("timeout")
            return 5

        mock_db.delete_many = AsyncMock(side_effect=delete_many_side_effect)

        with patch("clean_db.DocumentDBFactory.create", return_value=mock_db):
            result = await MongoCleaner().clean()

        assert attempted == list(collections_for("full"))  # 后续集合照跑
        by_name = {name: (ok, detail) for name, ok, detail in result}
        assert by_name["message_archive"] == (False, "timeout")
        assert by_name["generations"] == (True, 5)
        mock_db.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mongo_cleaner_factory_failure_is_caught(self):
        """DocumentDBFactory.create 的失败向上传播，以便 gather 能捕获到它"""
        from clean_db import MongoCleaner  # type: ignore[reportMissingImports]

        with patch(
            "clean_db.DocumentDBFactory.create",
            side_effect=RuntimeError("connection refused"),
        ):
            cleaner = MongoCleaner()
            with pytest.raises(RuntimeError, match="connection refused"):
                await cleaner.clean()


class TestMilvusCleaner:
    @pytest.mark.asyncio
    async def test_milvus_cleaner_calls_clear_all(self):
        from clean_db import MilvusCleaner, MEM_COLLECTION  # type: ignore[reportMissingImports]
        from core.memory.long_term import LongTermMemory

        with patch(
            "clean_db.LongTermMemory", spec=LongTermMemory
        ) as MockLTM:
            mock_ltm = MockLTM.return_value
            mock_ltm.clear_all = AsyncMock(return_value=42)
            mock_ltm.close = AsyncMock()

            cleaner = MilvusCleaner()
            result = await cleaner.clean()

        MockLTM.assert_called_once()
        mock_ltm.clear_all.assert_awaited_once()
        mock_ltm.close.assert_awaited_once()
        # 验证返回的 collection 名来自 MEM_COLLECTION 常量
        assert result == (MEM_COLLECTION, True, 42)
        assert result[0] == MEM_COLLECTION

    @pytest.mark.asyncio
    async def test_milvus_cleaner_handles_failure(self):
        from clean_db import MilvusCleaner, MEM_COLLECTION  # type: ignore[reportMissingImports]
        from core.memory.long_term import LongTermMemory

        with patch(
            "clean_db.LongTermMemory", spec=LongTermMemory
        ) as MockLTM:
            mock_ltm = MockLTM.return_value
            mock_ltm.clear_all = AsyncMock(side_effect=RuntimeError("connection refused"))
            mock_ltm.close = AsyncMock()

            cleaner = MilvusCleaner()
            result = await cleaner.clean()

        mock_ltm.close.assert_awaited_once()
        assert result == (MEM_COLLECTION, False, "connection refused")


class TestFileCleaner:
    def test_file_cleaner_deletes_known_files(self, tmp_path):
        from clean_db import FileCleaner  # type: ignore[reportMissingImports]

        data_dir = tmp_path / "data"
        cache_dir = data_dir / "cache" / "vision"
        cache_dir.mkdir(parents=True)

        targets = [
            "token_usage_pending.jsonl",
            "scheduler_checkpoint.json",
            "memory_pending_deletes.json",
            "memory_pending_syncs.json",
            # 多会话：每会话一个 data/session_{session_id}.json
            "session_default.json",
            "session_conv_abc.json",
            "session_pending_cleanup.json",
        ]
        for name in targets:
            (data_dir / name).write_text("{}")
        (cache_dir / "img1.json").write_text("{}")
        (cache_dir / "img2.json").write_text("{}")

        # 不应被删除的无关文件（含 glob 不该越界的近名文件）
        (data_dir / "keep_me.txt").write_text("important")
        (data_dir / "session_backup.txt").write_text("important")

        cleaner = FileCleaner(data_dir=str(data_dir))
        results = cleaner.clean()

        for name in targets:
            assert not (data_dir / name).exists(), name
        assert not (cache_dir / "img1.json").exists()
        assert not (cache_dir / "img2.json").exists()

        assert (data_dir / "keep_me.txt").exists()
        assert (data_dir / "session_backup.txt").exists()

        succeeded = [r for r in results if r[1] is True]
        assert len(succeeded) == len(targets) + 2

    def test_file_cleaner_removes_every_session_file(self, tmp_path):
        """多会话残留回归。

        写死只删 session_default.json 时，其余会话的 data/session_{id}.json 会留下——
        SessionMemory.load() 在 Mongo 读失败时会 fallback 读这些文件，清库后被复活。
        """
        from clean_db import FileCleaner  # type: ignore[reportMissingImports]

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        sessions = [f"session_conv_{i}.json" for i in range(3)]
        for name in sessions:
            (data_dir / name).write_text("{}")

        FileCleaner(data_dir=str(data_dir)).clean()

        for name in sessions:
            assert not (data_dir / name).exists(), name

    def test_file_cleaner_handles_missing_files(self, tmp_path):
        from clean_db import FileCleaner  # type: ignore[reportMissingImports]

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        # 不创建任何目标文件

        cleaner = FileCleaner(data_dir=str(data_dir))
        results = cleaner.clean()

        # 文件不存在 = 已是干净状态 = ok=True
        for name, ok, detail in results:
            assert ok is True
            assert "already clean" in str(detail)


class TestCliArgs:
    def test_default_cleans_all(self):
        from clean_db import parse_args  # type: ignore[reportMissingImports]
        args = parse_args([])
        assert args.mongo_only is False
        assert args.milvus_only is False
        assert args.files_only is False
        assert args.conversation_only is False
        assert args.force is False
        assert args.dry_run is False

    def test_force_flag(self):
        from clean_db import parse_args  # type: ignore[reportMissingImports]
        args = parse_args(["-f"])
        assert args.force is True

    def test_selective_flags(self):
        from clean_db import parse_args  # type: ignore[reportMissingImports]
        args = parse_args(["--mongo-only", "--files-only"])
        assert args.mongo_only is True
        assert args.files_only is True
        assert args.milvus_only is False

    def test_conversation_only_flag(self):
        from clean_db import parse_args  # type: ignore[reportMissingImports]
        assert parse_args(["--conversation-only"]).conversation_only is True

    def test_conversation_only_conflicts_with_mongo_only(self):
        """两者对 mongo 的范围诉求冲突——必须报错，不得静默取其一。"""
        from clean_db import parse_args  # type: ignore[reportMissingImports]
        with pytest.raises(SystemExit):
            parse_args(["--conversation-only", "--mongo-only"])

    def test_dry_run(self):
        from clean_db import parse_args  # type: ignore[reportMissingImports]
        args = parse_args(["--dry-run"])
        assert args.dry_run is True


class TestBuildTargets:
    def test_no_flags_includes_all(self):
        from clean_db import parse_args, build_targets  # type: ignore[reportMissingImports]
        args = parse_args([])
        targets = build_targets(args)
        assert "mongo" in targets
        assert "milvus" in targets
        assert "files" in targets

    def test_mongo_only(self):
        from clean_db import parse_args, build_targets  # type: ignore[reportMissingImports]
        args = parse_args(["--mongo-only"])
        targets = build_targets(args)
        assert targets == {"mongo"}

    def test_combined_flags(self):
        from clean_db import parse_args, build_targets  # type: ignore[reportMissingImports]
        args = parse_args(["--mongo-only", "--milvus-only"])
        targets = build_targets(args)
        assert targets == {"mongo", "milvus"}

    def test_conversation_only_selects_mongo(self):
        from clean_db import parse_args, build_targets  # type: ignore[reportMissingImports]
        assert build_targets(parse_args(["--conversation-only"])) == {"mongo"}

    def test_conversation_only_composes_with_other_targets(self):
        from clean_db import parse_args, build_targets  # type: ignore[reportMissingImports]
        targets = build_targets(parse_args(["--conversation-only", "--milvus-only"]))
        assert targets == {"mongo", "milvus"}


class TestMongoScope:
    def test_scope_defaults_to_full(self):
        from clean_db import parse_args, mongo_scope  # type: ignore[reportMissingImports]
        assert mongo_scope(parse_args([])) == "full"

    def test_scope_is_conversation_when_flag_given(self):
        from clean_db import parse_args, mongo_scope  # type: ignore[reportMissingImports]
        assert mongo_scope(parse_args(["--conversation-only"])) == "conversation"


class TestDescribeTargets:
    """确认提示必须列出真实清单。

    回归：原实现把集合名写死在提示里，漏掉了 generations 等新集合——
    对删库脚本而言，提示与实际不符是最危险的一类 bug。
    """

    def test_mongo_line_lists_the_exact_collections_in_scope(self):
        from clean_db import (  # type: ignore[reportMissingImports]
            CONVERSATION_COLLECTIONS, describe_targets)

        lines = describe_targets({"mongo"}, "conversation")
        assert lines == [
            "  MongoDB: " + ", ".join(CONVERSATION_COLLECTIONS) + " (delete_many)"]

    def test_full_scope_line_names_the_new_collections(self):
        from clean_db import describe_targets  # type: ignore[reportMissingImports]

        line = describe_targets({"mongo"}, "full")[0]
        for name in ("generations", "idempotency", "desktop_calls", "attachments"):
            assert name in line

    def test_all_three_targets_described(self):
        from clean_db import describe_targets  # type: ignore[reportMissingImports]

        lines = describe_targets({"mongo", "milvus", "files"}, "full")
        assert len(lines) == 3
        assert lines[0].startswith("  MongoDB:")


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_checks_connections(self):
        from clean_db import dry_run_check  # type: ignore[reportMissingImports]

        with patch(
            "clean_db.DocumentDBFactory.create"
        ) as mock_mongo_factory, patch(
            "clean_db.VectorDBFactory.create"
        ) as mock_milvus_factory:
            mock_db = AsyncMock()
            mock_db.count = AsyncMock(return_value=0)
            mock_db.close = AsyncMock()
            mock_mongo_factory.return_value = mock_db

            mock_vector_db = AsyncMock()
            mock_vector_db.collection_exists = AsyncMock(return_value=True)
            mock_vector_db.close = AsyncMock()
            mock_milvus_factory.return_value = mock_vector_db

            ok = await dry_run_check({"mongo", "milvus", "files"})

        assert ok is True
        mock_db.count.assert_awaited_once()
        mock_db.close.assert_awaited_once()
        mock_vector_db.collection_exists.assert_awaited_once()
        mock_vector_db.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dry_run_reports_failure(self):
        from clean_db import dry_run_check  # type: ignore[reportMissingImports]

        with patch(
            "clean_db.DocumentDBFactory.create",
            side_effect=RuntimeError("connection refused"),
        ):
            ok = await dry_run_check({"mongo"})

        assert ok is False
