"""Robot pipeline boundary; unsupported bodies cannot fall through to Zerith rules."""


class RobotAdapter:
    id = ""
    name = ""
    enabled = False

    def check(self, *args, **kwargs):
        raise NotImplementedError(f"{self.name} 尚未适配")

    def inspect(self, *args, **kwargs):
        raise NotImplementedError(f"{self.name} 尚未适配")

    def derive(self, *args, **kwargs):
        raise NotImplementedError(f"{self.name} 尚未适配")

    def export(self, *args, **kwargs):
        raise NotImplementedError(f"{self.name} 尚未适配")

    def split(self, *args, **kwargs):
        raise NotImplementedError(f"{self.name} 尚未适配")


class ZerithAdapter(RobotAdapter):
    id, name, enabled = "zerith", "零次方", True

    def check(self, *args, **kwargs):
        from .checks import raw_checks

        return raw_checks(*args, **kwargs)

    def inspect(self, *args, **kwargs):
        from .yolo_gate import inspect

        return inspect(*args, **kwargs)

    def derive(self, *args, **kwargs):
        from .repair import derive

        return derive(*args, **kwargs)

    def export(self, *args, **kwargs):
        from .export import create_dataset

        return create_dataset(*args, **kwargs)

    def split(self, *args, **kwargs):
        from .export import split_dataset

        return split_dataset(*args, **kwargs)


class AgilexAdapter(RobotAdapter):
    id, name = "agilex", "松灵（待适配）"


ADAPTERS = {a.id: a for a in [ZerithAdapter(), AgilexAdapter()]}


def register_adapter(adapter: RobotAdapter):
    if not adapter.id:
        raise ValueError("机器人适配器必须有独立 ID")
    ADAPTERS[adapter.id] = adapter


def get_adapter(robot="zerith"):
    adapter = ADAPTERS.get(robot)
    if adapter is None or not adapter.enabled:
        raise ValueError("该机器人尚未适配，不能使用零次方规则处理")
    return adapter


def profiles():
    return [dict(id=a.id, name=a.name, enabled=a.enabled) for a in ADAPTERS.values()]
