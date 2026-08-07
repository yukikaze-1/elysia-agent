# Elysia Agent 安装问题清单（N5095 本机部署 2026-08-05）

> **更新（2026-08-08）**：以下问题已在仓库内修复——#1/2/4（requirements.txt）、
> #3（setup.sh）、#5/6（docker-compose.yml）、#11（TTS 缺 key 自动降级）。
> #7/8/9 属本机环境（代理/网络），非仓库修复；#10 已由 README 更新解决。
> 修复 commit 见 `git log --oneline 75e8b50..HEAD`。

## 🔴 必须修复（阻止安装/启动）

| # | 问题 | 根因 | 现象 | 建议修复 |
|---|------|------|------|----------|
| 1 | `webrtc-vad` 装不上 | **包已从 PyPI 下架**（versions: none），且 requirements.txt 写的是 `webrtc-vad`，代码实际 import `webrtcvad` | pip 直接失败 | requirements.txt 改为 `webrtcvad>=2.0.10`（或直接删掉这行，VAD 是可选） |
| 2 | `pkg_resources` 缺失 | webrtcvad 依赖 pkg_resources，Python 3.12 + setuptools 83 已移除 | `ModuleNotFoundError: pkg_resources` | 需 `pip install "setuptools<81"`，或换用不依赖 pkg_resources 的 VAD 库 |
| 3 | `typing.TypedDict` 报错 | 大量代码 `from typing import TypedDict`，Pydantic 2.10/2.13 在 **Python <3.12** 强制要求 typing_extensions | PydanticUserError 崩溃 | 方案 A：setup.sh 建环境用 **python=3.12**（当前写 3.13，3.13 下 openwakeword/tflite-runtime 又装不上）；方案 B：代码统一改 typing_extensions |
| 4 | `openwakeword` 装不上 | 依赖 `tflite-runtime` 在 3.12/3.13 下无可用版本 | pip 失败 | 从 requirements 移除或标记可选（它和 webrtcvad 都是 STT/VAD 可选组件） |
| 5 | `mongo:7` 起不来 | **N5095 CPU 无 AVX 指令集**，MongoDB 5.0+ 强制要求 AVX | exit 132 重启循环 | docker-compose 用 `mongo:4.4`（最后支持非 AVX 的版本）—— 建议 README 注明或脚本自动检测 |
| 6 | Milvus 连不上 etcd | compose 只定义了 milvus 容器，但 `UseEmbed=false` 需要**外部 etcd** | 容器 Up 但端口 19530 不通，日志刷 etcd 连接失败 | compose 补 etcd + minio 三件套（官方标准配置） |

## 🟡 环境相关（本机特殊，可考虑文档化）

| # | 问题 | 根因 | 现象 | 处理 |
|---|------|------|------|------|
| 7 | Docker 拉镜像 EOF | systemd 里 Docker 代理指向失效的旧机器 `192.168.1.7:7897` | pull 失败 | 重配为 `127.0.0.1:7890`（本机 mihomo） |
| 8 | hf-mirror 下载失败 | TUN 透明代理劫持直连流量，SSL `UNEXPECTED_EOF` | download_model.py 失败 | curl 走 7890 代理手动下载 1.3GB 模型 |
| 9 | `HF_ENDPOINT` 镜像仍失败 | huggingface_hub 1.26.0 有 bug（client closed / SSL EOF） | 官方源也失败 | 降级 `huggingface_hub==1.5.0` 后 curl 手动下载 |
| 10 | 启动参数错误 | README 未写明，`--server` 不存在 | usage 报错 | 正确用法 `bash scripts/start.sh` 或 `python app.py` |
| 11 | 默认 TTS 需要 MINIMAX_API_KEY | config.yaml `tts.enabled: true`，默认 minimax 模型 | 启动崩溃 | 没 key 时需手动改 `enabled: false` —— 建议代码里无 key 自动降级 |

## 🟢 建议改进（迭代方向）

1. **setup.sh 环境版本**：`python=3.13` 与依赖生态不匹配，建议改为 3.12，或 requirements 按 Python 版本分文件
2. **requirements.txt 维护**：`webrtc-vad`（下架）、`openwakeword`（tflite-runtime 依赖）建议标记 `# optional` 并拆分为 `requirements-optional.txt`
3. **docker-compose 补全**：etcd/minio 建议直接并入（本次已改，可对照）
4. **README 补环境要求**：注明 AVX 要求（mongo:4.4 兜底）、模型下载代理问题
5. **.env 缺失 key 自动降级**：TTS/STT 无 key 时应 warn 而不是 crash（预检已有 WARN，但启动仍炸，说明预检和启动逻辑不一致）

---

## 最终部署配置（N5095 本机）

- conda 环境: `Elysia` (Python 3.12.13)
- 启动: `bash scripts/start.sh`（在 ~/elysia-agent-main）
- Web: http://192.168.1.4:8765 （本机 IP 可能变动）
- Docker 服务: elysia-mongo(4.4) / elysia-milvus(v2.4.0) / elysia-etcd / elysia-minio
- 依赖绕过: webrtcvad + setuptools<81 + huggingface_hub==1.5.0
- TTS 已禁用（无 MINIMAX_API_KEY）
