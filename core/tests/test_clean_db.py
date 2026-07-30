"""scripts/clean_db.py 测试。"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from pathlib import Path
import sys

# 将 scripts/ 加入 path，以便 import clean_db
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

pytestmark = pytest.mark.unit


class TestMongoCleaner:
    @pytest.mark.asyncio
    async def test_mongo_cleaner_deletes_all_collections(self):
        from clean_db import MongoCleaner  # type: ignore[reportMissingImports]

        mock_db = AsyncMock()
        mock_db.delete_many = AsyncMock(return_value=10)
        mock_db.close = AsyncMock()

        with patch(
            "clean_db.DocumentDBFactory.create", return_value=mock_db
        ) as mock_create:
            cleaner = MongoCleaner()
            result = await cleaner.clean()

        mock_create.assert_called_once_with("mongodb/default")

        # 验证五个 collection 都被 delete_many({})
        assert mock_db.delete_many.call_count == 5
        mock_db.delete_many.assert_has_calls([
            call("sessions", {}),
            call("message_archive", {}),
            call("token_usage", {}),
            call("character_versions", {}),
            call("character_l2_versions", {}),
        ], any_order=True)

        mock_db.close.assert_awaited_once()

        # result 是一个包含 5 个结果的列表
        assert result == [
            ("sessions", True, 10), ("message_archive", True, 10),
            ("token_usage", True, 10), ("character_versions", True, 10),
            ("character_l2_versions", True, 10),
        ]

    @pytest.mark.asyncio
    async def test_mongo_cleaner_handles_failure(self):
        from clean_db import MongoCleaner  # type: ignore[reportMissingImports]

        mock_db = AsyncMock()
        mock_db.close = AsyncMock()

        delete_results = [10, RuntimeError("timeout"), 5, RuntimeError("c6 fail"), 0]

        async def delete_many_side_effect(collection, filter_expr):
            r = delete_results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        mock_db.delete_many = AsyncMock(side_effect=delete_many_side_effect)

        with patch("clean_db.DocumentDBFactory.create", return_value=mock_db):
            cleaner = MongoCleaner()
            result = await cleaner.clean()

        assert result == [
            ("sessions", True, 10),
            ("message_archive", False, "timeout"),
            ("token_usage", True, 5),
            ("character_versions", False, "c6 fail"),
            ("character_l2_versions", True, 0),
        ]
        mock_db.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mongo_cleaner_factory_failure_is_caught(self):
        """DocumentDBFactory.create failure propagates so gather can catch it"""
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

        # 创建目标文件
        (data_dir / "session_default.json").write_text("{}")
        (data_dir / "token_usage_pending.jsonl").write_text("line1\n")
        (data_dir / "scheduler_checkpoint.json").write_text("{}")
        (cache_dir / "img1.json").write_text("{}")
        (cache_dir / "img2.json").write_text("{}")

        # 创建不应被删除的文件
        (data_dir / "keep_me.txt").write_text("important")

        cleaner = FileCleaner(data_dir=str(data_dir))
        results = cleaner.clean()

        # 验证目标文件被删除
        assert not (data_dir / "session_default.json").exists()
        assert not (data_dir / "token_usage_pending.jsonl").exists()
        assert not (data_dir / "scheduler_checkpoint.json").exists()
        assert not (cache_dir / "img1.json").exists()
        assert not (cache_dir / "img2.json").exists()

        # 验证无关文件未被删除
        assert (data_dir / "keep_me.txt").exists()

        # 结果计数 — 5 个目标文件全部应被删除
        succeeded = [r for r in results if r[1] is True]
        assert len(succeeded) == 5

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
