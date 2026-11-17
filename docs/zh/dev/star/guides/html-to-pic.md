# 文转图

LKMBot 支持将文字渲染成图片。渲染由本地 Pillow 完成，不依赖远程服务或 HTML 模板。

```python
@filter.command("image")  # 注册一个 /image 指令，接收 text 参数。
async def on_aiocqhttp(self, event: BotMessageEvent, text: str):
    url = await self.text_to_image(text)  # text_to_image() 是 Star 类的一个方法。
    # path = await self.text_to_image(text, return_url = False) # 如果你想保存图片到本地
    yield event.image_result(url)
```
