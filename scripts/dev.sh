#!/usr/bin/env bash
# VeriForge 本地开发引导脚本（Git Bash / WSL / macOS 通用）
#
# 用法：
#   ./scripts/dev.sh setup     # 安装前后端依赖（幂等，可重复执行）
#   ./scripts/dev.sh verify    # 类型检查 + 前端构建 + 前端测试 + 后端测试
#   ./scripts/dev.sh frontend  # 只装前端依赖
#   ./scripts/dev.sh backend   # 只装后端依赖
#
# 设计要点：不假设 node / python 已在 PATH 中。
# 若系统里没有，会自动回落到 WorkBuddy 托管运行时目录。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND="$ROOT/frontend"
BACKEND="$ROOT/backend"
VENV="$BACKEND/.venv"

# ---------- 运行时探测 ----------

detect_node() {
  if command -v node >/dev/null 2>&1; then
    command -v node
    return
  fi
  local latest
  latest="$(ls -d "$HOME"/.workbuddy/binaries/node/versions/*/node.exe 2>/dev/null | sort -V | tail -n 1 || true)"
  if [ -n "$latest" ] && [ -x "$latest" ]; then
    echo "$latest"
    return
  fi
  echo ""
}

detect_python() {
  if command -v python >/dev/null 2>&1; then
    command -v python
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi
  local latest
  latest="$(ls -d "$HOME"/.workbuddy/binaries/python/versions/*/python.exe 2>/dev/null | sort -V | tail -n 1 || true)"
  if [ -n "$latest" ] && [ -x "$latest" ]; then
    echo "$latest"
    return
  fi
  echo ""
}

NODE_BIN="$(detect_node)"
PYTHON_BIN="$(detect_python)"

# npm 可能没有 .cmd 包装（托管 node 目录里 npm 是 shell 脚本），
# 统一走 node + npm-cli.js，避免 "npm is not recognized"。
# 注意：Git Bash 的 /c/... 路径作为「参数」传给 Windows 版 node 时不会被转换，
# 必须显式转成 C:\... 形式，否则会拼成 D:\c\Users\... 而找不到模块。
to_win_path() {
  local p="$1"
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$p"
  else
    echo "$p"
  fi
}

NPM_CLI=""
if [ -n "$NODE_BIN" ]; then
  NODE_DIR="$(dirname "$NODE_BIN")"
  if [ -f "$NODE_DIR/node_modules/npm/bin/npm-cli.js" ]; then
    NPM_CLI="$(to_win_path "$NODE_DIR/node_modules/npm/bin/npm-cli.js")"
  fi
fi

npm_run() {
  if [ -n "$NPM_CLI" ]; then
    "$NODE_BIN" "$NPM_CLI" "$@"
  else
    npm "$@"
  fi
}

require_node() {
  if [ -z "${NODE_BIN:-}" ]; then
    echo "[dev.sh] 未找到 Node.js。请安装 Node 20+，或确认 ~/.workbuddy/binaries/node 下存在托管版本。" >&2
    exit 1
  fi
  echo "[dev.sh] Node: $NODE_BIN"
}

require_python() {
  if [ -z "${PYTHON_BIN:-}" ]; then
    echo "[dev.sh] 未找到 Python。请安装 Python 3.11+，或确认 ~/.workbuddy/binaries/python 下存在托管版本。" >&2
    exit 1
  fi
  echo "[dev.sh] Python: $PYTHON_BIN"
}

# ---------- 子命令 ----------

setup_frontend() {
  require_node
  echo "[dev.sh] 安装前端依赖 -> $FRONTEND"
  (cd "$FRONTEND" && npm_run ci --no-audit --no-fund)
  echo "[dev.sh] 前端依赖就绪"
}

setup_backend() {
  require_python
  echo "[dev.sh] 准备后端虚拟环境 -> $VENV"
  if [ ! -x "$VENV/Scripts/python.exe" ] && [ ! -x "$VENV/bin/python" ]; then
    "$PYTHON_BIN" -m venv "$VENV"
  fi
  local vpy
  if [ -x "$VENV/Scripts/python.exe" ]; then
    vpy="$VENV/Scripts/python.exe"
  else
    vpy="$VENV/bin/python"
  fi
  # 注意：不要在这里升级 pip —— 沙箱环境下 pip 自升级会因无法删除旧 exe 而失败。
  echo "[dev.sh] 安装后端依赖"
  (cd "$BACKEND" && "$vpy" -m pip install --disable-pip-version-check \
      -r requirements.txt -r requirements-dev.txt)
  echo "[dev.sh] 后端依赖就绪"
}

venv_python() {
  if [ -x "$VENV/Scripts/python.exe" ]; then
    echo "$VENV/Scripts/python.exe"
  elif [ -x "$VENV/bin/python" ]; then
    echo "$VENV/bin/python"
  else
    echo ""
  fi
}

verify() {
  local failed=0
  require_node

  echo "== 1/4 前端类型检查 =="
  (cd "$FRONTEND" && npm_run run build) || failed=1

  echo "== 2/4 前端单元测试 =="
  (cd "$FRONTEND" && npm_run run test -- --run) || failed=1

  local vpy
  vpy="$(venv_python)"
  if [ -z "$vpy" ]; then
    echo "== 3-4/4 跳过后端测试：未找到 $VENV，请先执行 ./scripts/dev.sh backend =="
    failed=1
  else
    echo "== 3/4 后端测试 =="
    (cd "$BACKEND" && "$vpy" -m pytest -q) || failed=1
    echo "== 4/4 后端依赖自检 =="
    (cd "$BACKEND" && "$vpy" -c "import fastapi, uvicorn, aiosqlite; print('fastapi/uvicorn/aiosqlite 导入正常')") || failed=1
  fi

  if [ "$failed" -ne 0 ]; then
    echo "[dev.sh] 验证失败" >&2
    exit 1
  fi
  echo "[dev.sh] 全部验证通过"
}

case "${1:-}" in
  setup)
    setup_frontend
    setup_backend
    ;;
  frontend)
    setup_frontend
    ;;
  backend)
    setup_backend
    ;;
  verify)
    verify
    ;;
  *)
    echo "用法: $0 {setup|frontend|backend|verify}" >&2
    exit 1
    ;;
esac
