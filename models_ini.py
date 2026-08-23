from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

__all__ = ["Block", "Document", "ModelsIniError", "parse"]


class ModelsIniError(Exception):
    """Invalid document operation (unknown section, duplicate name, ...)."""


# ─────────────────────── Regexes ─────────────────────────────────
# Header lines are matched on the stripped core; raw lines keep their
# exact bytes (incl. EOL) so render() is a byte-preserving join.
_HEADER_RE   = re.compile(r"^\[(?P<name>[^\]]+)\]$")
_C_HEADER_RE = re.compile(r"^#\s*\[(?P<name>[^\]]+)\]$")
_MARKER_RE   = re.compile(r"^#\s*==\s*([^=]+?)\s*==$")
_KEY_RE      = re.compile(r"^(?P<key>[^=\s#;]+)\s*=(.*)$")
_REGIONS     = {"PROFILES": "profiles", "INDIVIDUAL MODELS": "individual", "ARCHIVED": "archived"}


def _eol(line: str) -> str:
    """Trailing EOL of a raw line (may be '' for the final line)."""
    n = len(line) - len(line.rstrip("\r\n"))
    return line[-n:] if n else ""


def _uncomment(line: str) -> str:
    """Strip one '#' comment prefix from an archived line (EOL kept)."""
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return stripped[1:].lstrip()
    return line


@dataclass
class Block:
    """One section: active ('[name]') or archived ('# [name]').

    ``leading`` owns the comment/blank lines above the header; ``body``
    owns lines up to the next header. ``settled`` is parse-internal: once
    the body sees a blank line, further lines belong to the next block.
    """

    name: str
    region: str
    archived: bool
    leading: List[str]
    header: str
    body: List[str]
    settled: bool = field(default=False, repr=False)

    def render_lines(self) -> List[str]:
        return self.leading + [self.header] + self.body

    def keys(self) -> List[Tuple[str, str]]:
        """Parsed ``(key, value)`` pairs (archived lines uncommented)."""
        out: List[Tuple[str, str]] = []
        for line in self.body:
            core = line.strip()
            if self.archived and core.startswith("#"):
                core = _uncomment(line).strip()
            if not core or core[0] in "#;":
                continue
            m = _KEY_RE.match(core)
            if m:
                out.append((m.group("key"), m.group(2).strip()))
        return out


@dataclass
class Document:
    header: List[str]   # raw lines before the first section
    blocks: List[Block]
    trailer: List[str]  # raw lines after the last section

    # ── rendering ─────────────────────────────────────────────

    def render(self) -> str:
        lines: List[str] = list(self.header)
        for b in self.blocks:
            lines += b.render_lines()
        lines += list(self.trailer)
        return "".join(lines)

    # ── lookup ────────────────────────────────────────────────

    def block(self, name: str, archived: bool = False) -> Block:
        candidates = [b for b in self.blocks if b.name == name and b.archived == archived]
        if not candidates:
            kind = "archived" if archived else "active"
            raise ModelsIniError(f"section [{name}] not found ({kind})")
        if len(candidates) > 1:
            raise ModelsIniError(f"section [{name}] is ambiguous ({kind})")
        return candidates[0]

    def group_aliases(self) -> Dict[str, List[str]]:
        """active model path -> list of section names sharing it"""
        groups: Dict[str, List[str]] = {}
        for b in self.blocks:
            if b.archived:
                continue
            for k, v in b.keys():
                if k == "model":
                    groups.setdefault(v, []).append(b.name)
        return groups

    # ── mutations ─────────────────────────────────────────────

    def _first_archived_idx(self) -> int:
        for i, b in enumerate(self.blocks):
            if b.archived:
                return i
        return len(self.blocks)

    def add_section(self, name: str, keys: List[Tuple[str, str]],
                    region: str = "individual") -> None:
        """Append a new active section at the end of ``region``'s area."""
        if any(b.name == name and not b.archived for b in self.blocks):
            raise ModelsIniError(f"section [{name}] already exists")
        idx = -1
        for i, b in enumerate(self.blocks):
            if b.region == region:
                idx = i
        idx = idx + 1 if idx >= 0 else self._first_archived_idx()
        block = Block(
            name=name,
            region=region,
            archived=False,
            leading=["\n"] if idx else [],
            header=f"[{name}]\n",
            body=[f"{k} = {v}\n" for k, v in keys],
        )
        self.blocks.insert(idx, block)

    def archive_section(self, name: str) -> None:
        """Comment an active section out IN PLACE (keeps position/region)."""
        b = self.block(name)
        body = []
        for line in b.body:
            core = line.strip()
            if not core or _MARKER_RE.match(core) or core.startswith("#"):
                body.append(line)          # blanks, markers, already commented
            else:
                body.append("# " + line)
        b.header = f"# [{name}]{_eol(b.header)}"
        b.body = body
        b.archived = True

    def restore_section(self, name: str) -> None:
        """Uncomment an archived section (in place; true inverse of archive)."""
        b = self.block(name, archived=True)
        body = []
        for line in b.body:
            if line.lstrip().startswith("#"):
                body.append(_uncomment(line))
            else:
                body.append(line)
        b.header = f"[{b.name}]{_eol(b.header)}"
        b.body = body
        b.archived = False

    def delete_section(self, name: str, archived: bool = False) -> None:
        """Remove a section entirely (no git — this is the ini level)."""
        b = self.block(name, archived)
        idx = self.blocks.index(b)
        surviving = [l for l in b.leading if _MARKER_RE.match(l.strip())]
        self.blocks.pop(idx)
        if surviving and idx < len(self.blocks):
            nxt = self.blocks[idx]
            nxt.leading = surviving + nxt.leading
        # Marker owned by a block at end of file goes away with it: it
        # labels an area that no longer has any sections.

    def rename_section(self, name: str, new: str) -> None:
        b = self.block(name)
        if new != name and any(x.name == new and not x.archived for x in self.blocks):
            raise ModelsIniError(f"section [{new}] already exists")
        b.header = f"[{new}]{_eol(b.header)}"
        b.name = new

    def upsert_key(self, name: str, key: str, value: str) -> None:
        b = self.block(name)
        line_eol = "\n"
        for i, line in enumerate(b.body):
            core = line.strip()
            if not core or core[0] in "#;":
                continue
            m = _KEY_RE.match(core)
            if m and m.group("key") == key:
                line_eol = _eol(line)
                b.body[i] = f"{key} = {value}{line_eol}"
                return
        b.body.append(f"{key} = {value}{line_eol}")

    def remove_key(self, name: str, key: str) -> None:
        b = self.block(name)
        for i, line in enumerate(b.body):
            core = line.strip()
            if not core or core[0] in "#;":
                continue
            m = _KEY_RE.match(core)
            if m and m.group("key") == key:
                del b.body[i]
                return
        raise ModelsIniError(f"key '{key}' not found in [{name}]")


# ─────────────────────── Parser ──────────────────────────────────

def parse(text: str) -> Document:
    """Parse ``text`` into a Document that renders back byte-identical.

    Attribution rules (what makes round-tripping exact):
    - lines before the first header      -> Document.header
    - comment / blank lines between headers -> next block's "leading",
      once the current block's body has "settled" (seen a blank line);
      a marker line ("# == X ==") also settles and switches region.
    - everything else                    -> current block's body
    - leftover after the last block      -> Document.trailer
    """
    header: List[str] = []
    trailer: List[str] = []
    blocks: List[Block] = []
    pending: List[str] = []     # leading lines for the next block
    cur: Optional[Block] = None
    region = "global"

    for raw in text.splitlines(True):
        core = raw.strip()

        m_marker = _MARKER_RE.match(core)
        if m_marker:
            region = _REGIONS.get(m_marker.group(1).strip(), region)
            if cur is not None:
                cur.settled = True

        m_hdr = _HEADER_RE.match(core)
        m_chdr = _C_HEADER_RE.match(core)
        if m_hdr:
            blocks.append(Block(
                name=m_hdr.group("name").strip(),
                region=region,
                archived=False,
                leading=pending,
                header=raw,
                body=[],
            ))
            pending = []
            cur = blocks[-1]
            continue
        if m_chdr:
            blocks.append(Block(
                name=m_chdr.group("name").strip(),
                region=region,
                archived=True,
                leading=pending,
                header=raw,
                body=[],
            ))
            pending = []
            cur = blocks[-1]
            continue
        if not core:
            if cur is None:
                header.append(raw)
            else:
                cur.settled = True
                pending.append(raw)
            continue
        # Comment or key line: body until the block settles (first blank
        # line / marker), then leading of the next block, then header.
        if core[0] in "#;":
            if cur is None:
                header.append(raw)
            elif cur.settled:
                pending.append(raw)
            else:
                cur.body.append(raw)
        elif cur is None:
            header.append(raw)
        elif cur.settled:
            pending.append(raw)
        else:
            cur.body.append(raw)

    return Document(header=header, blocks=blocks, trailer=pending)
