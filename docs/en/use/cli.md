# CLI Commands

The LKMBot CLI initializes instances, starts LKMBot, updates common config values, and manages plugins.

If you install LKMBot with `uv`:

```bash
uv tool install git+https://github.com/Alma1314/LKM-bot.git --python 3.12
```

`uv` creates the `lkmbot` executable and puts it on `PATH`. You can inspect the path with:

::: code-group

```bash [Linux / macOS]
which lkmbot
```

```powershell [Windows]
where.exe lkmbot
```

:::

> [!TIP]
> Run the commands below from the LKMBot working directory.

## Quick Start

Initialize the directory once, then start LKMBot:

```bash
lkmbot init
lkmbot run
```

`lkmbot init` creates the data directories and configuration files required by LKMBot. After initialization, use `lkmbot run` for later starts.

## Top-Level Commands

| Command | Purpose |
| --- | --- |
| `lkmbot init` | Initialize the current directory as an LKMBot working directory. |
| `lkmbot run` | Start LKMBot in the foreground. |
| `lkmbot conf` | Read or update common config values. |
| `lkmbot password` | Change the WebUI login password interactively. |
| `lkmbot plug` | Create, install, update, remove, or search plugins. |
| `lkmbot help` | Show CLI help. |
| `lkmbot --version` | Show the LKMBot CLI version. |

## Start LKMBot

```bash
lkmbot run
```

Common options:

| Option | Purpose |
| --- | --- |
| `-p, --port <PORT>` | Set the WebUI port. |
| `-r, --reload` | Enable plugin auto-reload for plugin development. |
| `--reset-password` | Reset the WebUI initial password on startup and print the new password in startup logs. |

Examples:

```bash
lkmbot run --port 6185
lkmbot run --reload
lkmbot run --reset-password
```

If you forget the WebUI login password, run this from the LKMBot working directory:

```bash
lkmbot run --reset-password
```

LKMBot regenerates the initial password during startup and prints it in startup logs. After logging in, change the password in the WebUI immediately.

When starting directly from source, you can also run:

```bash
python main.py --reset-password
```

## Config

`lkmbot conf` reads and updates common config values.

```bash
lkmbot conf get
lkmbot conf get dashboard.port
lkmbot conf set dashboard.port 6185
```

Supported keys:

| Key | Description |
| --- | --- |
| `timezone` | Time zone, for example `Asia/Shanghai`. |
| `log_level` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `dashboard.port` | WebUI port. |
| `dashboard.username` | WebUI username. |
| `dashboard.password` | WebUI password. |
| `callback_api_base` | Callback API base URL. Must start with `http://` or `https://`. |

Changing the dashboard password writes the current password hashes automatically:

```bash
lkmbot conf set dashboard.password "new-password"
```

You can also use the dedicated interactive password command:

```bash
lkmbot password
lkmbot password --username admin
```

## Plugins

`lkmbot plug` manages plugins under `data/plugins`.

| Command | Purpose |
| --- | --- |
| `lkmbot plug list` | List installed plugins. |
| `lkmbot plug list --all` | Also show uninstalled plugins. |
| `lkmbot plug search <QUERY>` | Search plugins. |
| `lkmbot plug install <NAME>` | Install a plugin. |
| `lkmbot plug update [NAME]` | Update one plugin, or all updatable plugins if no name is given. |
| `lkmbot plug remove <NAME>` | Remove an installed plugin. |
| `lkmbot plug new <NAME>` | Create a new plugin from the template. |

Use a GitHub proxy when installing or updating plugins:

```bash
lkmbot plug install example-plugin --proxy https://gh-proxy.example.com/
lkmbot plug update --proxy https://gh-proxy.example.com/
```

Creating a new plugin asks for the author, description, version, and repository URL:

```bash
lkmbot plug new my-plugin
```

## Help

Show general CLI help:

```bash
lkmbot help
```

Show help for a specific command:

```bash
lkmbot help run
lkmbot run --help
lkmbot help conf
lkmbot plug --help
```

Show the version:

```bash
lkmbot --version
```
