# Text to Image

LKMBot renders text into an image. Rendering happens locally with Pillow and
does not rely on a remote service or HTML templates.

```python
@filter.command("image")  # Register an /image command that takes a text argument.
async def on_aiocqhttp(self, event: BotMessageEvent, text: str):
    url = await self.text_to_image(text)  # text_to_image() is a Star method.
    # path = await self.text_to_image(text, return_url = False) # Save the image locally instead.
    yield event.image_result(url)
```
