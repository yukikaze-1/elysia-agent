#!/usr/bin/env bash
# F3 shell smoke：backend-only 默认（无 Node 可启）+ preflight 严格化
# 运行: bash scripts/tests/test_backend_only_smoke.sh
# 用受控 PATH（fake python3 + 无 node/npm/conda）+ 可控 preflight 退出码验证
# start.sh 的 backend-only / --with-frontend / preflight 失败 / --skip-preflight 四路径。
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ── fake python3：--check 退出码由 FAKE_PREFLIGHT_EXIT 控制；其余模拟启动 ──
mkdir -p "$TMP/bin"
cat > "$TMP/bin/python3" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == *"--check"* ]]; then
  exit "${FAKE_PREFLIGHT_EXIT:-0}"
fi
echo "FAKE_SERVER_STARTED"
exit 0
EOF
chmod +x "$TMP/bin/python3"

# 受控 PATH：fake bin 在前（shadow python3）+ 系统 coreutils；排除 ~/.local（node/npm）
# 与 miniconda（conda）——backend-only 必须"无 Node 也能启动"。
export PATH="$TMP/bin:/usr/bin:/bin"

PASS=0; FAIL=0
check() { # name, actual, expected  —— 断言 actual 含 expected
  if [[ "$2" == *"$3"* ]]; then
    echo "  ✓ $1"; PASS=$((PASS+1))
  else
    echo "  ✗ $1"; echo "    期望含: $3"; echo "    实际: $(echo "$2" | tr '\n' '|')"; FAIL=$((FAIL+1))
  fi
}
check_not() { # name, actual, forbidden —— 断言 actual 不含 forbidden
  if [[ "$2" != *"$3"* ]]; then
    echo "  ✓ $1"; PASS=$((PASS+1))
  else
    echo "  ✗ $1"; echo "    不应含: $3"; echo "    实际: $(echo "$2" | tr '\n' '|')"; FAIL=$((FAIL+1))
  fi
}

echo "T1: backend-only 默认——无 node/npm，preflight 通过 → 启动"
out="$(FAKE_PREFLIGHT_EXIT=0 bash "$ROOT/scripts/start.sh" 2>&1)"
check "跳过前端构建（未显式 --with-frontend）" "$out" "跳过前端构建"
check_not "无 '前端构建完成'" "$out" "前端构建完成"
check_not "无 preflight 失败提示" "$out" "预检失败"
check "进入启动" "$out" "FAKE_SERVER_STARTED"

echo "T2: --with-frontend 但无 npm → 拒绝（exit 1）"
set +e
out2="$(FAKE_PREFLIGHT_EXIT=0 bash "$ROOT/scripts/start.sh" --with-frontend 2>&1)"
rc2=$?
set -e
check "退出码非 0" "$rc2" "1"
check "提示需 npm" "$out2" "--with-frontend 需要 npm"

echo "T3: preflight 失败 → 拒绝启动（exit 1）"
set +e
out3="$(FAKE_PREFLIGHT_EXIT=1 bash "$ROOT/scripts/start.sh" 2>&1)"
rc3=$?
set -e
check "退出码非 0" "$rc3" "1"
check "提示预检失败" "$out3" "预检失败"
check_not "未进入启动" "$out3" "FAKE_SERVER_STARTED"

echo "T4: preflight 失败 + --skip-preflight → 显式降级启动"
out4="$(FAKE_PREFLIGHT_EXIT=1 bash "$ROOT/scripts/start.sh" --skip-preflight 2>&1)"
check "跳过预检" "$out4" "跳过预检"
check "进入启动" "$out4" "FAKE_SERVER_STARTED"

echo
echo "结果: $PASS 通过 / $FAIL 失败"
[[ $FAIL -eq 0 ]] || exit 1
