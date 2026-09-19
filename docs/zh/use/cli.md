# CLI 指令

LKMBot CLI 用于初始化实例、启动 LKMBot、修改常用配置和管理插件。

如果你使用 `uv` 安装：

```bash
uv tool install git+https://github.com/Alma1314/LKM-bot.git --python 3.12
```

`uv` 会生成 `lkmbot` 可执行文件，并把它放到 `PATH` 中。可以用下面的命令确认路径：

::: code-group

```bash [Linux / macOS]
which lkmbot
```

```powershell [Windows]
where.exe lkmbot
```

:::

> [!TIP]
> 下面的命令都需要在 LKMBot 工作目录中执行。

## 快速开始

第一次部署时先初始化目录，再启动 LKMBot：

```bash
lkmbot init
lkmbot run
```

`lkmbot init` 会在当前目录创建 LKMBot 所需的数据目录和配置文件。初始化完成后，后续启动只需要执行 `lkmbot run`。

## 顶层指令

| 指令 | 用途 |
| --- | --- |
| `lkmbot init` | 初始化当前目录为 LKMBot 工作目录。 |
| `lkmbot run` | 在前台启动 LKMBot。 |
| `lkmbot conf` | 查看或修改常用配置项。 |
| `lkmbot password` | 交互式修改 WebUI 登录密码。 |
| `lkmbot plug` | 创建、安装、更新、删除或搜索插件。 |
| `lkmbot help` | 查看 CLI 帮助。 |
| `lkmbot --version` | 查看 LKMBot CLI 版本。 |

## 启动 LKMBot

```bash
lkmbot run
```

常用选项：

| 选项 | 用途 |
| --- | --- |
| `-p, --port <PORT>` | 指定 WebUI 端口。 |
| `-r, --reload` | 启用插件自动重载，适合插件开发调试。 |
| `--reset-password` | 启动时重置 WebUI 初始密码，并在启动日志中打印新密码。 |

示例：

```bash
lkmbot run --port 6185
lkmbot run --reload
lkmbot run --reset-password
```

如果你忘记了 WebUI 登录密码，可以在 LKMBot 工作目录中执行：

```bash
lkmbot run --reset-password
```

LKMBot 会在启动时重新生成初始密码，并在启动日志中打印。登录后请立即在 WebUI 中修改密码。

使用源码方式直接启动时，也可以执行：

```bash
python main.py --reset-password
```

## 配置

`lkmbot conf` 用于查看和修改常用配置项。

```bash
lkmbot conf get
lkmbot conf get dashboard.port
lkmbot conf set dashboard.port 6185
```

支持的配置项：

| 配置项 | 说明 |
| --- | --- |
| `timezone` | 时区，例如 `Asia/Shanghai`。 |
| `log_level` | 日志等级：`DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL`。 |
| `dashboard.port` | WebUI 端口。 |
| `dashboard.username` | WebUI 用户名。 |
| `dashboard.password` | WebUI 密码。 |
| `callback_api_base` | 回调 API 基础地址，需要以 `http://` 或 `https://` 开头。 |

修改密码时会自动写入新版密码哈希：

```bash
lkmbot conf set dashboard.password "new-password"
```

也可以使用专门的交互式密码指令：

```bash
lkmbot password
lkmbot password --username admin
```

## 插件

`lkmbot plug` 用于管理 `data/plugins` 下的插件。

| 指令 | 用途 |
| --- | --- |
| `lkmbot plug list` | 查看已安装插件。 |
| `lkmbot plug list --all` | 同时显示未安装插件。 |
| `lkmbot plug search <QUERY>` | 搜索插件。 |
| `lkmbot plug install <NAME>` | 安装插件。 |
| `lkmbot plug update [NAME]` | 更新指定插件；不传名称时更新所有可更新插件。 |
| `lkmbot plug remove <NAME>` | 删除已安装插件。 |
| `lkmbot plug new <NAME>` | 基于模板创建新插件。 |

安装或更新插件时可以使用 GitHub 代理：

```bash
lkmbot plug install example-plugin --proxy https://gh-proxy.example.com/
lkmbot plug update --proxy https://gh-proxy.example.com/
```

创建新插件会交互式询问作者、描述、版本和仓库地址：

```bash
lkmbot plug new my-plugin
```

## 帮助

查看全部 CLI 帮助：

```bash
lkmbot help
```

查看指定指令帮助：

```bash
lkmbot help run
lkmbot run --help
lkmbot help conf
lkmbot plug --help
```

查看版本：

```bash
lkmbot --version
```
