import configparser
import os
import shutil
from datetime import datetime, timezone
from collections import OrderedDict
from pathlib import Path


class IniStore:
    """Parse/edit/save models.ini with top-matter preservation."""

    def __init__(self, ini_path: str):
        self.ini_path = ini_path
        self.top_matter = ""
        self._sections: OrderedDict[str, OrderedDict[str, str]] = OrderedDict()
        self._section_order: list[str] = []
        self._key_order: dict[str, list[str]] = {}
        self._load()

    def _load(self):
        path = Path(self.ini_path)
        if not path.exists():
            self.top_matter = ""
            self._sections = OrderedDict()
            self._section_order = []
            self._key_order = {}
            return

        raw = path.read_text(encoding="utf-8")

        # Extract top-matter: everything before the first [section]
        first_section_pos = self._find_first_section_pos(raw)
        if first_section_pos is not None:
            self.top_matter = raw[:first_section_pos]
            section_text = raw[first_section_pos:]
        else:
            self.top_matter = raw
            section_text = ""

        parser = configparser.RawConfigParser(interpolation=None)
        parser.optionxform = str  # preserve key case
        if section_text:
            parser.read_string(section_text, source=str(path))

        # Extract sections preserving order
        self._sections = OrderedDict()
        self._section_order = []
        self._key_order = {}

        for section in parser.sections():
            self._section_order.append(section)
            items = OrderedDict(parser.items(section))
            self._sections[section] = items
            self._key_order[section] = list(items.keys())

    def get_section(self, name: str) -> OrderedDict[str, str] | None:
        return self._sections.get(name)

    def get_all_presets(self) -> OrderedDict[str, OrderedDict[str, str]]:
        """Return all sections except [*]."""
        result = OrderedDict()
        for name in self._section_order:
            if name != "*":
                result[name] = self._sections[name]
        return result

    def get_global_defaults(self) -> OrderedDict[str, str] | None:
        return self._sections.get("*")

    def section_names(self) -> list[str]:
        return [n for n in self._section_order if n != "*"]

    def section_names_all(self) -> list[str]:
        return list(self._section_order)

    def set_section(self, name: str, items: OrderedDict[str, str]) -> None:
        if name in self._sections:
            self._sections[name] = items
            # Preserve key order
            self._key_order[name] = list(items.keys())
        else:
            self._section_order.append(name)
            self._sections[name] = items
            self._key_order[name] = list(items.keys())

    def delete_section(self, name: str) -> bool:
        if name in self._sections:
            self._section_order.remove(name)
            del self._sections[name]
            self._key_order.pop(name, None)
            return True
        return False

    def rename_section(self, old: str, new: str) -> bool:
        if old not in self._sections or old == new:
            return False
        idx = self._section_order.index(old)
        self._section_order[idx] = new
        self._sections[new] = self._sections.pop(old)
        if old in self._key_order:
            self._key_order[new] = self._key_order.pop(old)
        return True

    def save(self) -> None:
        path = Path(self.ini_path)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)

        # Backup current file
        backup_dir = path.parent
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        backup_path = backup_dir / f"{path.name}.bak.{timestamp}"
        if path.exists():
            shutil.copy2(str(path), str(backup_path))

        # Prune old backups (keep 10 most recent)
        backups = sorted(
            backup_dir.glob(f"{path.name}.bak.*"),
            key=lambda p: p.stat().st_mtime,
        )
        for old_backup in backups[:-10]:
            old_backup.unlink(missing_ok=True)

        # Build output
        lines = []
        lines.append(self.top_matter)
        if self.top_matter and not self.top_matter.endswith("\n"):
            lines.append("\n")

        first = True
        for section in self._section_order:
            if not first:
                lines.append("\n")
            first = False
            lines.append(f"[{section}]\n")
            keys = self._key_order.get(section, list(self._sections[section].keys()))
            for key in keys:
                value = self._sections[section].get(key, "")
                lines.append(f"{key} = {value}\n")

        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(lines), encoding="utf-8")
        os.replace(str(tmp), str(path))

    @staticmethod
    def _find_first_section_pos(raw: str) -> int | None:
        for line in raw.split("\n"):
            stripped = line.strip()
            if stripped.startswith("[") and "]" in stripped[1:]:
                return raw.find(stripped)
        return None
