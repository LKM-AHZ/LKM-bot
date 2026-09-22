#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p ./data/plugins ./data/config ./data/temp

export TESTING="${TESTING:-true}"

# 原先这里把真实 OPENAI_API_KEY 别名成 ZHIPU_API_KEY 导出给测试子进程。两处问题一并消掉：
# ① 全仓库（含 tests/）没有任何代码读 ZHIPU_API_KEY，「兼容既有测试」的消费者并不存在；
# ② 把真实密钥灌进测试进程环境只会经 env dump / 失败输出多一条泄露面。
# 故直接删掉别名，而不是按建议加 CI 开关把它留着。

# 显式区分「没传参」与「传了空参」：原来的 ${@:-./tests} 依赖 bash 对 ${@:-word} 的实现细节，
# 且显式传空字符串也会被当成缺失、静默退回默认目标
PYTEST_TARGETS=("$@")
if (( ${#PYTEST_TARGETS[@]} == 0 )); then
  PYTEST_TARGETS=(./tests)
fi

 echo "[ci] syncing dependencies with uv"
# --locked：lock 与 pyproject 不一致就直接失败，而不是在 CI 里重新解析并就地改写 uv.lock
uv sync --dev --locked

echo "[ci] running tests: ${PYTEST_TARGETS[*]}"
# Some tests may leave non-daemon worker threads alive (e.g. aiosqlite warning path),
# which can block pytest process exit in CI. Run pytest via python and force process exit
# with pytest's return code to avoid hanging workflow jobs.
uv run python - "${PYTEST_TARGETS[@]}" <<'PY'
import logging
import os
import sys

import pytest

exit_code = int(pytest.main(sys.argv[1:]))

# os._exit 跳过 atexit 与解释器收尾：logging 的 FileHandler、loguru sink 以及收尾时才落盘的
# --cov 数据里没 flush 的部分会丢，CI 就可能「绿着」丢掉日志/artifact。先把所有 handler flush
# 干净再原样强退（强退本身是防 pytest 卡在残留非 daemon 线程上，保留）。
handlers = list(logging.getLogger().handlers)
for logger in list(logging.root.manager.loggerDict.values()):
    if isinstance(logger, logging.Logger):
        handlers.extend(logger.handlers)
for handler in handlers:
    try:
        handler.flush()
    except Exception:
        pass

sys.stdout.flush()
sys.stderr.flush()
os._exit(exit_code)
PY
