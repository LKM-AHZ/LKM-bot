# 使用 Docker 部署 LKMBot

> [!WARNING]
> 通过 Docker 可以方便地将 LKMBot 部署到 Windows, Mac, Linux 上。
>
> 以下教程默认您的环境已安装 Docker。如果没有安装，请参考 [Docker 官方文档](https://docs.docker.com/get-docker/) 进行安装。

> [!NOTE]
> **本仓库是 LKM 定制 fork，不再自带 `compose.yml` / `compose-with-shipyard.yml`**——它们已并入
> 根编排仓库 **LKM-Website**（`docker-compose.yml` 里的 `--profile bot`、APISIX 网关子域名路由、
> `deploy/k8s/` 清单），部署以该仓 `DEPLOYMENT.md` 的「LKM Bot」一节为准。
> 下文保留上游通用部署方式，其中「通过 Docker 部署」（`docker run`）在不接入 LKM 全栈时仍然适用。

## 通过 Docker Compose 部署

::: details 只部署 LKMBot（LKM 全栈方式）

LKM 环境**不**使用本仓库的 compose 文件，请在根编排仓库里起（bot 为可选组件，默认不随主栈启动）：

```bash
git clone https://github.com/LKM-AHZ/LKM-Website.git
cd LKM-Website
git clone https://github.com/Alma1314/LKM-bot.git
docker compose --profile bot up -d --build
```

面板不发布宿主端口，经网关以 `bot.<社群域名>` 暴露（需先给该子域加 DNS 并首签证书）。
若只想单机跑 LKMBot、不接入 LKM 全栈，请用下面的「通过 Docker 部署」`docker run` 方式。
:::

::: details 带 Agent 沙盒环境的部署

支持原生的 Python 代码执行、Shell 代码执行等功能。

沙箱（Shipyard）在 LKM 全栈里与 bot 同属 `--profile bot`，一起起即可：

```bash
cd LKM-Website
docker compose --profile bot up -d --build
```

⚠️ 沙箱会挂载 `/var/run/docker.sock`（等价宿主机 root），仅在需要时启用、用完
`docker compose --profile bot down` 收掉。另外 bot 默认 `sandbox.booter=shipyard_neo`，
而根编排起的是**旧版 Bay**：需在面板「配置 → 沙箱」把 booter 改成 `shipyard`、
endpoint 填 `http://shipyard:8156` 并填上 access token 才会生效。

配置和使用详见 [Agent 沙盒环境](/use/astrbot-agent-sandbox.md) 文档。
:::

::: details 和 NapCat 一起部署

如果您想对接 NapCat，使用这种方式可以同时部署 LKMBot 和 NapCat。

```bash
git clone https://github.com/Alma1314/LKM-bot.git
cd LKM-bot
docker build -t lkmbot:latest .
wget https://raw.githubusercontent.com/NapNeko/NapCat-Docker/main/compose/astrbot.yml
sudo docker compose -f astrbot.yml up -d
```

:::


## 通过 Docker 部署

```bash
git clone https://github.com/Alma1314/LKM-bot.git
cd LKM-bot
docker build -t lkmbot:latest .
sudo docker run -itd -p 6185:6185 -p 6199:6199 -v $PWD/data:/LKMBot/data -v /etc/localtime:/etc/localtime:ro -v /etc/timezone:/etc/timezone:ro --name lkmbot lkmbot:latest
```
> 
> Windows 下不需要加 sudo，下同
>
Windows 同步 Host Time（需要WSL2）

```
-v \\wsl.localhost\(your-wsl-os)\etc\timezone:/etc/timezone:ro
-v \\wsl.localhost\(your-wsl-os)\etc\localtime:/etc/localtime:ro
```

通过以下命令查看 LKMBot 的日志：

```bash
sudo docker logs -f lkmbot
```

## 通过 Windows Docker Desktop 部署

### 使用`Windows CMD`

`TZ` 的值请设置为 **IANA 时区标准格式**（地区/城市），例如中国为 `Asia/Shanghai`

```bash
docker run -itd -p 6185:6185 -p 6199:6199 -e TZ=Asia/Shanghai -v "%cd%\data:/LKMBot/data" --name lkmbot lkmbot:latest
```

### 使用`PowerShell`

`TZ` 的值请设置为 **IANA 时区标准格式**（地区/城市），例如中国为 `Asia/Shanghai`

```powershell
docker run -itd -p 6185:6185 -p 6199:6199 -e TZ=Asia/Shanghai -v "${PWD}\data:/LKMBot/data" --name lkmbot lkmbot:latest
```




## 🎉 大功告成

如果一切顺利，你会看到 LKMBot 打印出的日志。

如果没有报错，你会看到一条日志显示类似 `🌈 管理面板已启动，可访问` 并附带了几条链接。打开其中一个链接即可访问 LKMBot 管理面板。

> [!TIP]
> 由于 Docker 隔离了网络环境，所以不能使用 `localhost` 访问管理面板。
>
> 首次登录请使用启动日志中打印的随机初始密码（用户名通常为 `lkmbot`）。登录后请立即修改密码。
>
> 如果部署在云服务器上，需要在相应厂商控制台里放行对应端口。

接下来，你需要部署任何一个消息平台，才能够实现在消息平台上使用 LKMBot。
