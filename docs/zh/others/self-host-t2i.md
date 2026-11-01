# 部署文转图服务

LKMBot **不内置、也不默认调用任何第三方文转图服务**：远程文转图必须由你自己部署或指定一个可信的渲染服务，否则请不要开启该功能（或改用本地渲染）。

远程渲染服务需要兼容 HTML 模板渲染接口（接收模板与数据的 POST 请求并返回图片）。常用做法是部署开源渲染服务，例如 [AstrBotDevs/astrbot-t2i-service](https://github.com/AstrBotDevs/astrbot-t2i-service)（镜像由上游维护，生产环境建议自行构建）：

```bash
docker run -itd -p 8999:8999 soulter/bot-t2i-service:latest
```

部署完成后，前往 LKMBot 仪表盘 → 配置文件 → 扩展功能 → 文本转图像：

1. 把「文本转图像策略」设为 `remote`；
2. 在出现的「文本转图像服务 API 地址」中填写你的服务地址（见下方路径说明）。

> 如果用本文档的 Docker 教程部署，地址应为 `http://文转图服务容器名:8999`。

> 如果与 LKMBot 部署在同一台机器上，地址应为 `http://localhost:8999`。

如果不想部署外部服务，也可以把策略设为 `local`：使用本地 PIL 渲染，不产生任何网络请求（排版与样式支持相对简单）。
