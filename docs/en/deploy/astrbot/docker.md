# Deploy LKMBot with Docker

> [!WARNING]
> Docker provides a convenient way to deploy LKMBot on Windows, Mac, and Linux.
>
> This tutorial assumes you have Docker installed in your environment. If not, please refer to the [Docker official documentation](https://docs.docker.com/get-docker/) for installation.

## Deploy with Docker Compose

::: details Deploy LKMBot Only (General Method)

First, clone the LKMBot repository to your local machine:

```bash
git clone https://github.com/Alma1314/LKM-bot.git
cd LKM-bot
```

Then, run Compose:

```bash
sudo docker compose up -d
```
:::

::: details Deploy with Agent Sandbox Environment

Supports native Python code execution, Shell code execution, and other features.

Deployment method:

```bash
git clone https://github.com/Alma1314/LKM-bot.git
cd LKM-bot
# Modify the environment variable configuration in the compose-with-shipyard.yml file, such as Shipyard's access token, etc.
docker compose -f compose-with-shipyard.yml up -d
docker pull soulter/shipyard-ship:latest
```

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
