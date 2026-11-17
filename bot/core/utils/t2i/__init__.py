from abc import ABC, abstractmethod


class RenderStrategy(ABC):
    @abstractmethod
    async def render(self, text: str, return_url: bool) -> str:
        pass
