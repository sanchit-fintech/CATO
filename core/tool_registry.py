from typing import Callable


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Callable] = {}

    def register(self, name: str, function: Callable):
        self._tools[name] = function

    def get(self, name: str) -> Callable | None:
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    def run(self, name: str, **kwargs):
        tool = self.get(name)

        if tool is None:
            raise ValueError(f"Tool '{name}' is not registered.")

        return tool(**kwargs)
