from dataclasses import dataclass
from collections import OrderedDict


@dataclass(frozen=True)
class Preset:
    name: str
    model: str = ""
    mmproj: str = ""
    ctx_size: int = 4096
    keys: OrderedDict[str, str] = None  # type: ignore

    def __post_init__(self):
        if self.keys is None:
            object.__setattr__(self, "keys", OrderedDict())
        # Derive ctx_size from keys if not explicitly set
        if self.model and "ctx-size" not in self.keys:
            pass  # keep default

    @classmethod
    def from_section(cls, name: str, items: OrderedDict[str, str]) -> "Preset":
        model = items.get("model", "")
        mmproj = items.get("mmproj", "")
        try:
            ctx_size = int(items.get("ctx-size", "4096"))
        except (ValueError, TypeError):
            ctx_size = 4096
        return cls(name=name, model=model, mmproj=mmproj, ctx_size=ctx_size, keys=items)

    def to_dict(self) -> dict[str, str]:
        return dict(self.keys)

    def model_basename(self) -> str:
        import os
        return os.path.basename(self.model) if self.model else "\u2014"

    def mmproj_basename(self) -> str:
        import os
        return os.path.basename(self.mmproj) if self.mmproj else "\u2014"

    def clone_name(self, suffix: str = "-copy") -> str:
        return self.name + suffix
