from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..models import ToolDefinition


@dataclass
class AndroidPlugin:
    name: str
    description: str
    definitions: list[ToolDefinition] = field(default_factory=list)
    available: Callable[[], bool] | None = None

    def is_available(self) -> bool:
        return self.available() if self.available else True


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, AndroidPlugin] = {}
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, plugin: AndroidPlugin) -> None:
        if plugin.name in self._plugins:
            raise ValueError(f"duplicate plugin: {plugin.name}")
        self._plugins[plugin.name] = plugin
        if not plugin.is_available():
            return
        for definition in plugin.definitions:
            if definition.name in self._tools:
                raise ValueError(f"duplicate tool: {definition.name}")
            self._tools[definition.name] = definition

    def get(self, name: str) -> ToolDefinition:
        return self._tools[name]

    def definitions(self) -> list[ToolDefinition]:
        return sorted(self._tools.values(), key=lambda item: item.name)

    def plugins(self) -> list[AndroidPlugin]:
        return sorted(self._plugins.values(), key=lambda item: item.name)

    def dispatch(self, name: str, *, action: str | None = None, **kwargs: Any) -> Any:
        definition = self._tools.get(name)
        if not definition:
            raise KeyError(name)
        if action and definition.actions and action not in definition.actions:
            raise ValueError(f"unsupported action: {name}/{action}")
        if definition.actions:
            return definition.handler(action=action, **kwargs)
        if action is not None:
            kwargs.setdefault("action", action)
        return definition.handler(**kwargs)
