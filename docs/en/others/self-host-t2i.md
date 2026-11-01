# Deploy a Text-to-Image Service

LKMBot ships **no built-in renderer and calls no third-party text-to-image service by default**. Remote text-to-image rendering requires a service you deploy or explicitly trust; otherwise leave the feature off or switch to local rendering.

Any service that implements the HTML-template render API (POST template + data, return an image) works. A common choice is the open-source [AstrBotDevs/astrbot-t2i-service](https://github.com/AstrBotDevs/astrbot-t2i-service) (the image below is maintained upstream; build your own for production):

```bash
docker run -itd -p 8999:8999 soulter/bot-t2i-service:latest
```

Then go to LKMBot Dashboard → Config → Ext → Text-to-Image:

1. Set `Text-to-Image Strategy` to `remote`;
2. Fill `Text-to-Image Service API Endpoint` with your service URL (see the notes below).

> If you deployed LKMBot with the Docker guide in this documentation, the URL is `http://<t2i-service-container-name>:8999`.

> If it runs on the same host as LKMBot, the URL is `http://localhost:8999`.

If you would rather not run an external service, set the strategy to `local`: PIL renders the image in-process with no network calls (simpler layout support).
