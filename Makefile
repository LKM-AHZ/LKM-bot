.PHONY: worktree worktree-add worktree-rm pr-test-neo pr-test-full pr-test-full-fast clean-temp-deployment

WORKTREE_DIR ?= ../lkmbot_worktree
BRANCH ?= $(word 2,$(MAKECMDGOALS))
# ?= 的位置参数版本一赋值就算「已定义」，写在它后面的 BASE ?= master 是死代码；
# 位置参数缺省时用 $(or ...) 回落到 master
BASE ?= $(or $(word 3,$(MAKECMDGOALS)),master)

worktree:
	@echo "Usage:"
	@echo "  make worktree-add <branch> [base-branch]"
	@echo "  make worktree-rm  <branch>"

# BRANCH/BASE/WORKTREE_DIR 都直接来自命令行：必须加引号，否则会被当成 shell 语法执行
worktree-add:
ifeq ($(strip $(BRANCH)),)
	$(error Branch name required. Usage: make worktree-add <branch> [base-branch])
endif
	@if [ -e "$(WORKTREE_DIR)/$(BRANCH)" ]; then \
		echo "Target already exists: $(WORKTREE_DIR)/$(BRANCH). Run 'make worktree-rm $(BRANCH)' first." >&2; \
		exit 1; \
	fi
	@if git show-ref --verify --quiet "refs/heads/$(BRANCH)"; then \
		echo "Branch $(BRANCH) already exists. Delete it or use another name." >&2; \
		exit 1; \
	fi
	@mkdir -p "$(WORKTREE_DIR)"
	git worktree add "$(WORKTREE_DIR)/$(BRANCH)" -b "$(BRANCH)" "$(BASE)"

worktree-rm:
ifeq ($(strip $(BRANCH)),)
	$(error Branch name required. Usage: make worktree-rm <branch>)
endif
	@if [ -d "$(WORKTREE_DIR)/$(BRANCH)" ]; then \
		git worktree remove "$(WORKTREE_DIR)/$(BRANCH)"; \
	else \
		echo "Worktree $(WORKTREE_DIR)/$(BRANCH) not found; pruning stale worktree metadata."; \
		git worktree prune; \
	fi

pr-test-neo:
	./scripts/pr_test_env.sh --profile neo

pr-test-full:
	./scripts/pr_test_env.sh --profile full

pr-test-full-fast:
	./scripts/pr_test_env.sh --profile full --skip-sync --no-dashboard

clean-temp-deployment:
	@set -eu; \
	update_sandbox="$$(mktemp -d "$${TMPDIR:-/tmp}/lkmbot-update-test.XXXXXX")"; \
	echo "Copying the current workspace to $$update_sandbox"; \
	rsync -a \
		--exclude='.git/' \
		--exclude='.venv/' \
		--exclude='data/' \
		--exclude='node_modules/' \
		--exclude='.pnpm-store/' \
		--exclude='.pytest_cache/' \
		--exclude='.ruff_cache/' \
		--exclude='__pycache__/' \
		./ "$$update_sandbox/"; \
	cd "$$update_sandbox"; \
	uv sync; \
	echo; \
	echo "Update test sandbox is ready: $$update_sandbox"; \
	echo "Start it with:"; \
	printf '  cd "%s" && uv run main.py\n' "$$update_sandbox"; \
	echo "Remove it with:"; \
	printf '  rm -rf "%s"\n' "$$update_sandbox"

# Swallow extra args (branch/base) so make doesn't treat them as targets；
# 其余目标一律报错——原来无条件 @true 会把 `make pr-test-ful` 这类拼写错误变成静默成功
%:
	@if [ "$@" != "$(BRANCH)" ] && [ "$@" != "$(BASE)" ]; then \
		echo "make: *** No rule to make target '$@'. Stop." >&2; \
		exit 1; \
	fi
