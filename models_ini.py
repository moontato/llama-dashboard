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


def _marker_region(line: str):
    """Document region named by a marker line, or None."""
    m = _MARKER_RE.match(line.strip())
    return _REGIONS.get(m.group(1).strip()) if m else None


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

    def _region_span(self, region: str):
        """(start, last) block indices of ``region``, or None."""
        start = next((i for i, b in enumerate(self.blocks)
                      if b.region == region), None)
        if start is None:
            return None
        last = start
        while last + 1 < len(self.blocks) and \
                self.blocks[last + 1].region == region:
            last += 1
        return start, last

    def _active_group_end(self, region: str) -> int:
        """Insertion index preserving the canonical layout: active blocks
        on top, archived blocks sunk to the bottom of their region."""
        span = self._region_span(region)
        if span is None:
            return self._first_archived_idx()
        start, last = span
        for i in range(start, last + 1):
            if self.blocks[i].archived:
                return i
        return last + 1

    def _relocate(self, b: Block, insert_at: int) -> None:
        """Move ``b`` to ``insert_at`` (index into the block list AFTER
        removal). A region marker owned by ``b``'s leading is handed to
        whichever block now starts the region, so the marker stays at the
        region head. The moved block's leading is normalised to a blank
        separator plus any comment lines it carried."""
        markers = [l for l in b.leading if _MARKER_RE.match(l.strip())]
        rest = [l for l in b.leading if not _MARKER_RE.match(l.strip())]
        content = list(rest)
        while content and not content[0].strip():
            content.pop(0)
        while content and not content[-1].strip():
            content.pop()
        idx = next(k for k, x in enumerate(self.blocks) if x is b)
        del self.blocks[idx]
        self.blocks.insert(insert_at, b)
        if content:
            b.leading = ["\n"] + content + ["\n"]
        else:
            b.leading = [] if insert_at == 0 else ["\n"]
        if markers:
            start = next(i for i, x in enumerate(self.blocks)
                         if x.region == b.region)
            h = self.blocks[start]
            pos = 1 if h.leading and not h.leading[0].strip() else 0
            h.leading[pos:pos] = markers

    def add_section(self, name: str, keys: List[Tuple[str, str]],
                    region: str = "individual") -> None:
        """Append a new active section at the bottom of its region's
        active group (above any archived blocks)."""
        if any(b.name == name and not b.archived for b in self.blocks):
            raise ModelsIniError(f"section [{name}] already exists")
        idx = self._active_group_end(region)
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
        """Archive an active section: comment it and sink it to the bottom
        of its region (below all other blocks of that region)."""
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
        span = self._region_span(b.region)
        i = next(k for k, x in enumerate(self.blocks) if x is b)
        if span is not None and span[1] > i:
            self._relocate(b, span[1])

    def restore_section(self, name: str) -> None:
        """Restore an archived section: uncomment it and raise it to the
        bottom of its region's active group (above the first archived)."""
        b = self.block(name, archived=True)
        span = self._region_span(b.region)
        if span is not None:
            target = next(k for k in range(span[0], span[1] + 1)
                          if self.blocks[k].archived)
            i = next(k for k, x in enumerate(self.blocks) if x is b)
            if target != i:
                self._relocate(b, target)
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
        """Remove a section entirely (no git — this is the ini level).

        If the deleted block owns a region marker and the region still has
        blocks, the marker is handed to the block that now starts the
        region. If the region itself becomes empty, the marker is dropped
        with the block: handing it to the first block of the next region
        would leave a stale marker labelling an empty area, and it would
        travel with that block on later moves."""
        b = self.block(name, archived)
        idx = self.blocks.index(b)
        surviving = [l for l in b.leading if _MARKER_RE.match(l.strip())]
        self.blocks.pop(idx)
        if surviving:
            nxt = self.blocks[idx] if idx < len(self.blocks) else None
            if nxt is not None and nxt.region == b.region:
                pos = 1 if nxt.leading and not nxt.leading[0].strip() \
                    else 0
                nxt.leading[pos:pos] = surviving
            # else: the region is now empty — the marker is dropped with
            # the block (at end of file it simply goes away: it labelled
            # an area that no longer has any sections).
        elif idx == 0:
            # The first block's region marker may live in the document
            # header; drop it when the region no longer has any blocks.
            self.header = [l for l in self.header
                            if _marker_region(l) != b.region]

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
