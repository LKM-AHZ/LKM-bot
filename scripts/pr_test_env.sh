#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PROFILE="neo"
RUN_SYNC=true
RUN_LINT=true
RUN_SMOKE=true
RUN_DASHBOARD=false
# 记录命令行是否显式给过 dashboard 开关：full profile 的默认值只在没给过时兜底，
# 否则 --no-dashboard 会被下面「full 强制打开」的默认逻辑吃掉
DASHBOARD_FLAG_SET=false

usage() {
  cat <<'EOF'
Usage:
  scripts/pr_test_env.sh [options]

Options:
  --profile <neo|full>  Test profile. Default: neo
  --with-dashboard      Build dashboard before finishing checks
  --no-dashboard        Disable dashboard build (even for full profile)
  --skip-sync           Skip `uv sync`
  --skip-lint           Skip `ruff format --check` and `ruff check`
  --skip-smoke          Skip startup smoke test
  -h, --help            Show this help message

Environment:
  PYTEST_ARGS           Extra args appended to pytest command
EOF
}

while (($# > 0)); do
  case "$1" in
    --profile)
      PROFILE="${2:-}"
      if [[ "$PROFILE" != "neo" && "$PROFILE" != "full" ]]; then
        echo "Unsupported profile: $PROFILE" >&2
        exit 1
      fi
      shift 2
      ;;
    --with-dashboard)
      RUN_DASHBOARD=true
      DASHBOARD_FLAG_SET=true
      shift
      ;;
    --skip-sync)
      RUN_SYNC=false
      shift
      ;;
    --skip-lint)
      RUN_LINT=false
      shift
      ;;
    --skip-smoke)
      RUN_SMOKE=false
      shift
      ;;
    --no-dashboard)
      RUN_DASHBOARD=false
      DASHBOARD_FLAG_SET=true
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ "$PROFILE" == "full" && "$DASHBOARD_FLAG_SET" == false ]]; then
  RUN_DASHBOARD=true
fi

echo "==> Profile: $PROFILE"
echo "==> Sync dependencies: $RUN_SYNC"
echo "==> Run lint: $RUN_LINT"
echo "==> Run smoke test: $RUN_SMOKE"
echo "==> Build dashboard: $RUN_DASHBOARD"

if [[ "$RUN_SYNC" == true ]]; then
  echo "==> Syncing dependencies with uv"
  uv sync --group dev
fi

echo "==> Preparing test directories"
mkdir -p data/plugins data/config data/temp data/skills
export TESTING="${TESTING:-true}"
export ZHIPU_API_KEY="${ZHIPU_API_KEY:-test-api-key}"

if [[ "$RUN_LINT" == true ]]; then
  echo "==> Running Ruff format check"
  uv run ruff format --check .
  echo "==> Running Ruff lint check"
  uv run ruff check .
fi

# PYTEST_ARGS 拆成数组再引用：原来直接 ${PYTEST_ARGS:-} 靠 shell 分词，值里带空格/引号
# 会切错，带 glob（如 tests/test_*.py）还会被路径展开成当前目录下恰好匹配的文件
pytest_args=()
if [[ -n "${PYTEST_ARGS:-}" ]]; then
  read -r -a pytest_args <<<"${PYTEST_ARGS}"
fi

echo "==> Running pytest"
if [[ "$PROFILE" == "neo" ]]; then
  NEO_TESTS=(
    "tests/test_neo_skill_sync.py"
    "tests/test_neo_skill_tools.py"
    "tests/test_computer_skill_sync.py"
    "tests/test_skill_manager_sandbox_cache.py"
    "tests/test_dashboard.py::test_neo_skills_routes"
  )
  uv run pytest -q "${NEO_TESTS[@]}" "${pytest_args[@]}"
else
  uv run pytest --cov=. -v -o log_cli=true -o log_level=DEBUG "${pytest_args[@]}"
fi

run_smoke_test() {
  # 不复刻一套 bash 版冒烟：CI（.github/workflows/smoke_test.yml）用的就是
  # scripts/smoke_startup_check.py，这里保持同一个实现，避免两套各写各的探活逻辑。
  # 该脚本自带临时 ASTRBOT_ROOT 与 stub webui，不写仓库 data/，也不依赖 dashboard dist
  # 是否已构建（neo profile 从不构建它），并自己在 finally 里收进程、清临时目录。
  echo "==> Running startup smoke check"
  uv run python scripts/smoke_startup_check.py
}

if [[ "$RUN_SMOKE" == true ]]; then
  run_smoke_test
fi

if [[ "$RUN_DASHBOARD" == true ]]; then
  if ! command -v pnpm >/dev/null 2>&1; then
    echo "pnpm is required for dashboard build. Install it with: npm install -g pnpm" >&2
    exit 1
  fi
  echo "==> Building dashboard"
  pnpm --dir dashboard install --frozen-lockfile
  pnpm --dir dashboard run build
fi

echo "==> PR checks completed successfully"
