# ── WebUI 构建阶段 ──
# 面板前端在本仓构建（而非运行期从上游 registry 下载）：本部署把它挂在社区站同域 /bot/ 子路径
# 下，静态资源引用与 API 前缀都由**构建期** base 决定；上游下载的是根 base 包，在子路径下会全部
# 404。且后端 `AstrBotUpdater.ensure_dashboard()` 只在 bundled dist 版本匹配时才跳过下载，故
# 构建产物必须带 `assets/version`（见 dashboard/scripts/write-dist-version.mjs）。
FROM node:22-slim AS dashboard
WORKDIR /dashboard
# pnpm 版本必须与 lockfile 的生成版本一致：dashboard/pnpm-lock.yaml 是 lockfileVersion 9.0
# （pnpm 10 生成，CI 亦用 10.28.2），pnpm 11 会因 overrides 段格式差异让 --frozen-lockfile 直接失败。
RUN npm install -g pnpm@10.28.2
# 依赖层单独缓存：只改前端源码时不必重装依赖。
COPY dashboard/package.json dashboard/pnpm-lock.yaml dashboard/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY dashboard/ ./
# vite.config.ts（t2i shiki runtime 资产）与 write-dist-version.mjs（版本号）按相对路径读这两处
# Core 侧文件，故按同样的相对结构放进构建阶段。
COPY astrbot/__init__.py /astrbot/__init__.py
COPY astrbot/core/utils/t2i/template/shiki_runtime.iife.js /astrbot/core/utils/t2i/template/shiki_runtime.iife.js
# 子路径前缀（默认 /bot/，与根编排的网关路由一致）。构建脚本跳过 vue-tsc（类型检查在开发/CI 侧做，
# 不让它卡住部署构建）。
ARG VITE_BASE_PATH=/bot/
ENV VITE_BASE_PATH=${VITE_BASE_PATH}
RUN pnpm build:subpath

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
    && curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# 注：`COPY .` 刻意排在 pip 安装**之前**——`uv pip install -r requirements.txt` 会安装本项目
# 自身（`lkmbot @ file:///LKMBot`），那需要完整的源码树（README.md + scripts/hatch_build.py
# 等），拆出"只 COPY pyproject 装依赖"的缓存优化会让构建失败。代价是改任何源码都会重跑依赖层。
COPY . /LKMBot/

# 自建 WebUI 覆盖 bundled 位置（.dockerignore 已放开 dashboard/ 源码以便构建，此处清掉源码只留 dist）。
COPY --from=dashboard /dashboard/dist /LKMBot/astrbot/dashboard/dist
RUN rm -rf /LKMBot/dashboard

RUN python -m pip install uv \
    && echo "3.12" > .python-version \
    && uv lock \
    && uv export --format requirements.txt --output-file requirements.txt --frozen \
    && uv pip install -r requirements.txt --no-cache-dir --system \
    && uv pip install socksio uv pilk --no-cache-dir --system

EXPOSE 6185

CMD ["python", "main.py"]
