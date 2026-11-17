from .local_strategy import LocalRenderStrategy


class HtmlRenderer:
    """Render Markdown to an image with the local Pillow strategy."""

    def __init__(self) -> None:
        self.local_strategy = LocalRenderStrategy()

    async def initialize(self) -> None:
        return

    async def render_t2i(self, text: str, return_url: bool = False) -> str:
        """Render text to an image path.

        Args:
            text: Markdown source.
            return_url: Retained for callers that expect a URL return type; the
                local strategy always returns a file path.

        Returns:
            Path to the rendered image.
        """
        return await self.local_strategy.render(text, return_url=return_url)
