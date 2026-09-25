# Deploy LKMBot with Docker

> [!WARNING]
> Docker provides a convenient way to deploy LKMBot on Windows, Mac, and Linux.
>
> This tutorial assumes you have Docker installed in your environment. If not, please refer to the [Docker official documentation](https://docs.docker.com/get-docker/) for installation.

> [!NOTE]
> **This repository is the LKM fork and no longer ships `compose.yml` / `compose-with-shipyard.yml`** —
> they are managed by the root orchestration repository **LKM-Website** (the `--profile bot` services in
> its `docker-compose.yml`, the APISIX gateway route on a dedicated subdomain, and its `deploy/k8s/`
> manifests). Deployment is defined by the "LKM Bot" section of that repo's `DEPLOYMENT.md`.
> The generic instructions below are kept for upstream compatibility; "Deploy with Docker" (`docker run`)
> still applies when you are not using the LKM stack.

## Deploy with Docker Compose

::: details Deploy LKMBot Only (LKM stack)

The LKM environment does **not** use this repository's compose file. Use the root orchestration repo
(LKMBot is an optional component and is not started with the main stack):

```bash
git clone https://github.com/LKM-AHZ/LKM-Website.git
cd LKM-Website
git clone https://github.com/Alma1314/LKM-bot.git
docker compose --profile bot up -d --build
```

The dashboard publishes no host port; it is exposed by the gateway as `bot.<community-domain>`
(add the DNS record for that subdomain and issue its certificate first).
If you only want a standalone LKMBot without the LKM stack, use the "Deploy with Docker" section below.
:::

::: details Deploy with Agent Sandbox Environment

Supports native Python code execution, Shell code execution, and other features.

In the LKM stack the sandbox (Shipyard) shares the same `--profile bot`, so it starts together:

```bash
cd LKM-Website
docker compose --profile bot up -d --build
```

⚠️ The sandbox mounts `/var/run/docker.sock` (equivalent to host root). Enable it only when needed and
tear it down with `docker compose --profile bot down`. Note that LKMBot defaults to
`sandbox.booter=shipyard_neo` while the root stack runs the **legacy Bay**: set the booter to `shipyard`
in the dashboard (Configuration → Sandbox), point the endpoint at `http://shipyard:8156` and fill in
the access token for it to take effect.

For configuration and usage details, see the [Agent Sandbox Environment](/en/use/astrbot-agent-sandbox.md) documentation.
:::


## Deploy with Docker

```bash
git clone https://github.com/Alma1314/LKM-bot.git
cd LKM-bot
docker build -t lkmbot:latest .
sudo docker run -itd -p 6185:6185 -p 6199:6199 -v $PWD/data:/LKMBot/data -v /etc/localtime:/etc/localtime:ro -v /etc/timezone:/etc/timezone:ro --name lkmbot lkmbot:latest
```

> No need to add sudo on Windows, same below
> Sync Host Time on Windows (requires WSL2)

```
-v \\wsl.localhost\(your-wsl-os)\etc\timezone:/etc/timezone:ro
-v \\wsl.localhost\(your-wsl-os)\etc\localtime:/etc/localtime:ro
```

View LKMBot logs with the following command:

```bash
sudo docker logs -f lkmbot
```


## Deploy via Docker Desktop on Windows

### For Windows CMD

Set `TZ` to the standard IANA time zone format (Region/City). Use `Asia/Shanghai` for China.

```bash
docker run -itd -p 6185:6185 -p 6199:6199 -e TZ=Asia/Shanghai -v "%cd%\data:/LKMBot/data" --name lkmbot lkmbot:latest
```

### For PowerShell

Set `TZ` to the standard IANA time zone format (Region/City). Use `Asia/Shanghai` for China.

```powershell
docker run -itd -p 6185:6185 -p 6199:6199 -e TZ=Asia/Shanghai -v "${PWD}\data:/LKMBot/data" --name lkmbot lkmbot:latest
```

## 🎉 All Done

If everything goes well, you will see logs printed by LKMBot.

If there are no errors, you will see a log message similar to `🌈 Dashboard started, accessible at` with several links. Open one of the links to access the LKMBot dashboard.

> [!TIP]
> Since Docker isolates the network environment, you cannot use `localhost` to access the dashboard.
>
> New users must use the random password printed in the startup logs to log in for the first time. Use the username shown in the logs (usually `lkmbot`) and change the password after first login.
>
> If deployed on a cloud server, you need to open ports `6180-6200` and `11451` in the cloud provider's console.

Next, you need to deploy any messaging platform to use LKMBot on that platform.

## Configure an HTTP proxy in Docker

Set the HTTP proxy in the WebUI under `Settings → Network → Proxy & Dependency Sources → HTTP Proxy`. LKMBot reaches that address **from inside its own container**, so `http://127.0.0.1:7890` points at the LKMBot container itself rather than the host or another container.

If the proxy runs on the host, or in another container with the port published to the host:

- Mac / Windows (Docker Desktop): `http://host.docker.internal:7890`
- Linux: `http://172.17.0.1:7890` (replace `172.17.0.1` with your docker0 gateway if it differs)

If LKMBot and the proxy share a Docker network, use the proxy container name, for example `http://clash:7890`.

Clash-style clients commonly use HTTP on `7890` and SOCKS on `7891`. Use `http://` or `socks5://` to match the protocol. For a host-installed proxy, it must listen on a host interface reachable from Docker, such as `0.0.0.0` or the Docker gateway interface, rather than only `127.0.0.1`; publish the proxy port to the host.
