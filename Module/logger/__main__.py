"""集成测试：输出各级别日志验证 LoggerFactory 和 get_caller_logger。运行: python -m Module.logger"""

from . import get_caller_logger, LoggerFactory


def main():
    print("Logger 模块 — 日志输出测试")
    print()

    # 1. get_caller_logger — 自动检测调用方模块名
    print("=== get_caller_logger ===")
    logger = get_caller_logger()
    print(f"  自动检测模块名: {logger.name}")
    logger.debug("DEBUG 级别日志")
    logger.info("INFO 级别日志")
    logger.warning("WARNING 级别日志")
    print("  日志输出完成")
    print()

    # 2. LoggerFactory — 显式创建
    print("=== LoggerFactory ===")
    factory = LoggerFactory()
    custom = factory.get(__name__)
    print(f"  显式创建 logger: {custom.name}")
    custom.info("自定义 logger INFO")
    print("  自定义 logger 输出完成")
    print()

    print("全部测试完成!")


if __name__ == "__main__":
    main()
