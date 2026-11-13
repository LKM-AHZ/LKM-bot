FROM node:24-alpine AS dashboard
WORKDIR /panel
ARG BOT_BASE_PATH=/
COPY dashboard/ ./
RUN corepack enable && pnpm install --frozen-lockfile \
    && VITE_BASE_PATH=${BOT_BASE_PATH} pnpm build:deploy

FROM python:3.12-slim
WORKDIR /LKMBot

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    build-essential \
    python3-dev \
    libffi-dev \
    libssl-dev \
    ca-certificates \
    bash \
    ffmpeg \
    libavcodec-extra \
    fonts-noto-cjk \
    curl \
    gnupg \
    git \
    ripgrep \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
# 注：gcc/build-essential/python3-dev/libffi-dev/libssl-dev 与 git 是刻意留给**运行期**的——
# 插件依赖由进程内 pip 安装（bot/core/utils/pip_installer.py），sdist-only 的包要就地编译、
# git+https 依赖要 git 可执行文件。若要 purge 瘦身或多阶段构建，先确认放弃「运行期装插件」这条路径。

# Node 运行时（MCP 服务经 npx 拉起，见 bot/core/agent/mcp_client.py）。
# 不再 `curl https://deb.nodesource.com/setup_lts.x | bash -`：那是把「当前最新 LTS」的
# 未固定远端脚本以 root 执行，构建不可复现。改为固定 node_22.x 仓库，
# 并用其 GPG key 校验包签名。
RUN set -eux; \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
      | gpg --dearmor -o /usr/share/keyrings/nodesource.gpg; \
    echo "deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
      > /etc/apt/sources.list.d/nodesource.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends nodejs; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# 注：`COPY .` 刻意排在 pip 安装**之前**——`uv pip install -r requirements.txt` 会安装本项目
# 自身（`lkmbot @ file:///LKMBot`），那需要完整的源码树（README.md + scripts/hatch_build.py
# 等），拆出"只 COPY pyproject 装依赖"的缓存优化会让构建失败。代价是改任何源码都会重跑依赖层。
COPY . /LKMBot/

# uv 固定版本：解析器/lock 语义随版本变化，浮动版本会让同一份 uv.lock 装出不同环境。
# 不再 `uv lock`：那会在构建期重新联网解析并可能改写 lock，镜像未必等于仓库里已提交的
# uv.lock（lock 过期或 pyproject 漂移都会静默装出别的版本）。直接 --frozen 导出已提交的 lock。
# 运行期额外要装的东西：socksio 已随 lock 导出到 requirements.txt（里面有 socksio==1.0.0），
# 不必再显式装；pilk（QQ 语音 silka 编解码）不在 lock 里，必须固定版本，否则同一份提交
# 在不同时间构建会装到不同版本。要彻底纳入 lock，应把它写进 pyproject 依赖再导出。
RUN python -m pip install uv==0.12.12 \
    && echo "3.12" > .python-version \
    && uv export --format requirements.txt --output-file requirements.txt --frozen \
    && uv pip install -r requirements.txt --no-cache-dir --system \
    && uv pip install "pilk==0.2.4" --no-cache-dir --system \
    && uv pip install -e . --no-deps --system \
    && touch /LKMBot/.bot

EXPOSE 6185

COPY --from=dashboard /bot/dashboard/dist /LKMBot/bot/dashboard/dist

CMD ["python", "main.py"]
