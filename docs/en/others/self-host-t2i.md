# Self-host the Text-to-Image Service

LKMBot uses [AstrBotDevs/astrbot-t2i-service](https://github.com/AstrBotDevs/astrbot-t2i-service) as the default text-to-image service. The default service endpoints are:

```plain
https://t2i.soulter.top/text2img
https://t2i.rcfortress.site/text2img
```

This interface can ensure normal response for most of the time. However, due to the deployment of servers in New York, the response speed may be slower in some areas.

You can choose to self-host the text-to-image service to improve response speed.

```bash
docker run -itd -p 8999:8999 soulter/bot-t2i-service:latest
```

After deployment, go to LKMBot Dashboard -> Config -> Ext. Under "Text-to-Image," set `Text-to-Image Strategy` to `remote`. The `Text-to-Image Service API Endpoint` field will then appear; set it to the URL of your deployed service.

> If you deployed LKMBot using the Docker tutorial in this documentation, the URL should be `http://<t2i-service-container-name>:8999`.

> If you deployed on the same machine as LKMBot, the URL should be `http://localhost:8999`.
