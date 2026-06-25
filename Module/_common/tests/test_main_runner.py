"""run_module_main 单元测试。"""
from Module._common.main_runner import run_module_main


class TestRunModuleMain:
    def test_runs_async_fn(self):
        called = []

        async def my_main():
            called.append(True)

        run_module_main(my_main)
        assert called == [True]

    def test_load_env_false_skips_dotenv(self):
        called = []

        async def my_main():
            called.append(True)

        run_module_main(my_main, load_env=False)
        assert called == [True]
