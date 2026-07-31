# check.sh — 独立环境预检脚本

## 动机

项目已有 `scripts/setup.sh`（首次安装）和 `scripts/start.sh`（启动服务），但缺少独立的预检脚本。用户想单独跑环境检查时，需要手动输入 `conda activate Elysia && python app.py --check --full`，与 `bash scripts/` 的模式不一致。README 故障排查章节也缺少脚本化的入口。

## 用法

```bash
bash scripts/check.sh           # 快速预检（= python app.py --check）
bash scripts/check.sh --full    # 含连通性探测（= python app.py --check --full）
```

## 行为

1. `set -euo pipefail` — 与 setup.sh/start.sh 一致，出错尽早退出
2. 检测 Python 环境（复用 start.sh 的 conda `--no-capture-output` > python3 优先级）
3. 将参数透传给 `app.py --check [--full]`
4. 退出码透传 `app.py --check` 的返回值
5. 无彩色输出 — 与 start.sh 保持一致（start.sh 全程纯文本，只有 setup.sh 定义了颜色）

### conda 输出捕获注意

必须使用 `conda run -n Elysia --no-capture-output python`（start.sh:13），**不能**用裸 `conda run -n Elysia python`（setup.sh:46）。后者会缓冲 stdout，用户看不到 `app.py --check` 的实时输出。

## 输出示意

```
=== Elysia Agent 环境预检 ===

→ Python 环境...
  ✓ conda Elysia

→ 预检...
  [app.py --check 的原始输出]

✓ 预检通过
```

如果预检失败：

```
⚠ 预检发现问题，详见上方输出
```

## 文件变更

| 文件 | 操作 | 内容 |
|------|------|------|
| `scripts/check.sh` | 新增 | ~30 行，conda 检测 + 透传 app.py --check |
| `README.md` | 修改 | 两处补充 `bash scripts/check.sh` 入口 |

## README 改动点

**1. "高级用法" 章节** — 在 `python app.py --check` / `python app.py --check --full` 旁边补脚本形式：

```bash
bash scripts/check.sh           # 快速预检
bash scripts/check.sh --full    # 环境预检（含连通性）
```

**2. "故障排查" 章节** — "预检报错" 部分改为脚本优先：

```bash
bash scripts/check.sh --full    # 完整预检，准确定位问题
```

## 不做

- 不做故障分类汇总 — `preflight.py` 自己输出已经够细，脚本叠一层无意义
- 不改变 setup.sh/start.sh 现有的 check 逻辑 — 它们继续内联调用 `app.py --check`
- 不加颜色 — start.sh 也不用颜色，短脚本没必要引入颜色常量
