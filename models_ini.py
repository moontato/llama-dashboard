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

# Paired regions: each active region has an archived twin directly below
# it. Active regions hold only active blocks, archived regions only
# archived ones, so archiving a section moves it into its twin and
# restoring moves it back.
_REGIONS = {
    "PROFILES": "profiles",
    "ARCHIVED PROFILES": "archived_profiles",
    "MODELS": "models",
    "ARCHIVED MODELS": "archived_models",
}
_REGION_MARKERS = {
    "profiles": "# == PROFILES ==\n",
    "archived_profiles": "# == ARCHIVED PROFILES ==\n",
    "models": "# == MODELS ==\n",
    "archived_models": "# == ARCHIVED MODELS ==\n",
}
_REGION_ORDER = ("profiles", "archived_profiles", "models", "archived_models")
_PAIRS = {"profiles": "archived_profiles", "models": "archived_models"}
_TWIN = dict(_PAIRS)
_TWIN.update({a: p for p, a in _PAIRS.items()})


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

    def key_index(self, key: str) -> int:
        """Index of the first body line defining ``key`` (-1 if absent).

        Archived-aware: in archived blocks the commented form
        (``# key = value``) is uncommented before matching, so edits and
        removals hit the commented line in place instead of appending an
        uncommented duplicate."""
        for i, line in enumerate(self.body):
            core = line.strip()
            if not core or core[0] in "#;":
                if self.archived and core.startswith("#"):
                    core = _uncomment(line).strip()
                else:
                    continue
            m = _KEY_RE.match(core)
            if m and m.group("key") == key:
                return i
        return -1

    def key_lines(self) -> List[Optional[str]]:
        """For each body line, the key it defines, or ``None`` when the
        line is not an effective key line (blank, comment, ...).

        Mirrors the exact classification used by :meth:`keys` and
        :meth:`key_index`, including the archived (commented) form, so a
        line counts as a key line here iff it is one for those."""
        out: List[Optional[str]] = []
        for line in self.body:
            core = line.strip()
            if self.archived and core.startswith("#"):
                core = _uncomment(line).strip()
            if not core or core[0] in "#;":
                out.append(None)
                continue
            m = _KEY_RE.match(core)
            out.append(m.group("key") if m else None)
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
        kind = "archived" if archived else "active"
        candidates = [b for b in self.blocks
                      if b.name == name and b.archived == archived]
        if not candidates:
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

    def _region_span(self, region: str, exclude: Block = None):
        """(start, last) block indices of ``region``, or None.
        ``exclude`` is skipped (a block being moved into its own region)."""
        start = next((i for i, b in enumerate(self.blocks)
                      if b.region == region and b is not exclude), None)
        if start is None:
            return None
        last = start
        while last + 1 < len(self.blocks) and \
                self.blocks[last + 1].region == region and \
                self.blocks[last + 1] is not exclude:
            last += 1
        return start, last

    def _marker_location(self, region: str):
        """Where ``region``'s marker line lives: (container, index) where
        container is the Block owning it in its leading, or 'header' or
        'trailer' for the document-level line lists. None if absent."""
        line = _REGION_MARKERS[region]
        for b in self.blocks:
            if line in b.leading:
                return b, b.leading.index(line)
        if line in self.header:
            return "header", self.header.index(line)
        if line in self.trailer:
            return "trailer", self.trailer.index(line)
        return None

    def _ensure_marker(self, region: str):
        """Return _marker_location(region), creating the marker as an
        anchor when the whole pair was emptied: right before the marker
        of the next region (in canonical order) that still has one, or in
        the document trailer if the file ends there."""
        loc = self._marker_location(region)
        if loc is not None:
            return loc
        marker = _REGION_MARKERS[region]
        for nxt in _REGION_ORDER[_REGION_ORDER.index(region) + 1:]:
            nloc = self._marker_location(nxt)
            if nloc is None:
                continue
            container, i = nloc
            lines = container.leading if isinstance(container, Block) \
                else (self.header if container == "header" else self.trailer)
            lines[i:i] = [marker, "\n"]
            return container, i
        self.trailer.extend(("\n", marker, "\n") if not self.trailer
                            else (marker, "\n"))
        return "trailer", len(self.trailer) - 2

    def _marker_of(self, region: str):
        """Remove region's marker line wherever it lives (block leading,
        document header or trailer) and return it, or None if absent.
        The separator blank following the marker goes with it."""
        line = _REGION_MARKERS[region]
        for container in list(self.blocks):
            if line not in container.leading:
                continue
            j = container.leading.index(line)
            del container.leading[j]
            if j < len(container.leading) and not container.leading[j].strip():
                del container.leading[j]
            return line
        for name in ("header", "trailer"):
            lst = getattr(self, name)
            if line not in lst:
                continue
            j = lst.index(line)
            del lst[j]
            if j < len(lst) and not lst[j].strip():
                del lst[j]
            return line
        return None

    def _move_to_region(self, b: Block, target: str) -> None:
        """Move ``b`` into region ``target`` (whose blocks all share its
        archived flag), appended at the end of the region. ``b``'s
        archived flag and comment state must already match ``target``.

        Marker bookkeeping, so the file stays parseable and the
        active/archived pairing stays adjacent:
        - a marker owned by ``b`` in its leading is handed to the block
          that now starts the region it leaves;
        - if that region becomes empty, its marker is kept as an anchor
          (see _ensure_marker) so the pair's markers stay next to each
          other and destinations stay positional;
        - if ``target`` was empty, ``b`` takes its marker and becomes the
          region head (the marker line is split out of whatever block or
          line list currently carries it).
        """
        src = b.region
        src_marker = self._marker_of(src) if src in _REGION_MARKERS else None
        content = [l for l in b.leading if _marker_region(l) is None]
        while content and not content[0].strip():
            content.pop(0)
        while content and not content[-1].strip():
            content.pop()
        idx_b = next(k for k, x in enumerate(self.blocks) if x is b)
        tspan = self._region_span(target, exclude=b)
        self.blocks.pop(idx_b)
        if tspan is not None:
            last_t = tspan[1]
            insert_at = last_t - (1 if last_t > idx_b else 0) + 1
            self.blocks.insert(insert_at, b)
            b.leading = (["\n"] + content + ["\n"] if content
                         else ["\n"]) if insert_at else (
                             content + ["\n"] if content else [])
        else:
            container, i = self._ensure_marker(target)
            marker = _REGION_MARKERS[target]
            if isinstance(container, Block):
                pre = container.leading[:i]
                container.leading = container.leading[i + 1:]
                self.blocks.insert(self.blocks.index(container), b)
                b.leading = pre + [marker] + content + ["\n"]
            elif container == "header":
                # header lines before the marker stay above the block;
                # lines after it move below it (into the trailer)
                self.trailer = self.header[i + 1:] + self.trailer
                self.header = self.header[:i]
                self.blocks.insert(0, b)
                b.leading = [marker] + content + ["\n"]
            else:  # trailer
                pre = self.trailer[:i]
                self.trailer = self.trailer[i + 1:]
                self.blocks.insert(len(self.blocks), b)
                b.leading = pre + [marker] + content + ["\n"]
        b.region = target
        if src_marker is not None:
            new_head = next((x for x in self.blocks if x.region == src),
                            None)
            if new_head is not None:
                pos = 1 if new_head.leading and \
                        not new_head.leading[0].strip() else 0
                new_head.leading[pos:pos] = [src_marker, "\n"]
            else:
                self._ensure_marker(src)

    def add_section(self, name: str, keys: List[Tuple[str, str]],
                    region: str = "models") -> None:
        """Append a new active section at the bottom of its (active)
        region."""
        if region not in _PAIRS:
            raise ModelsIniError(
                f"region must be one of {', '.join(_PAIRS)}")
        if any(b.name == name and not b.archived for b in self.blocks):
            raise ModelsIniError(f"section [{name}] already exists")
        block = Block(
            name=name,
            region=region,
            archived=False,
            leading=[],
            header=f"[{name}]\n",
            body=[f"{k} = {v}\n" for k, v in keys],
        )
        self.blocks.append(block)
        self._move_to_region(block, region)

    def archive_section(self, name: str) -> None:
        """Archive an active section: comment it and move it to the bottom
        of its region's archived twin."""
        b = self.block(name)
        if b.region not in _PAIRS:
            raise ModelsIniError(
                f"section [{name}] cannot be archived (region "
                f"'{b.region}' has no archived twin)")
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
        self._move_to_region(b, _PAIRS[b.region])

    def restore_section(self, name: str) -> None:
        """Restore an archived section: uncomment it and raise it to the
        bottom of its region's active twin."""
        b = self.block(name, archived=True)
        target = _TWIN.get(b.region)
        if target is None:
            raise ModelsIniError(
                f"section [{name}] cannot be restored (region "
                f"'{b.region}' has no active twin)")
        body = []
        for line in b.body:
            if line.lstrip().startswith("#"):
                body.append(_uncomment(line))
            else:
                body.append(line)
        b.header = f"[{b.name}]{_eol(b.header)}"
        b.body = body
        b.archived = False
        self._move_to_region(b, target)

    def move_section(self, name: str, target: str, position: str,
                     archived: bool = False) -> None:
        """Move section ``name`` directly before/after section ``target``
        (drag-and-drop reorder). Sections may only move within their own
        region. The region marker always stays with whichever block ends
        up first in the region. Dropping a section into its current slot
        is a no-op."""
        if position not in ("before", "after"):
            raise ModelsIniError("position must be 'before' or 'after'")
        b = self.block(name, archived)
        t = self.block(target, archived)
        if t is b:
            return
        if b.region != t.region:
            raise ModelsIniError(
                f"section [{name}] can only be moved within its own region")
        idx_b = self.blocks.index(b)
        idx_t = self.blocks.index(t)
        if (position == "before" and idx_b == idx_t - 1) or \
                (position == "after" and idx_b == idx_t + 1):
            return
        region = b.region
        span = self._region_span(region)
        # the marker must travel when the region's head changes owner
        head_changes = span[0] == idx_b or \
            (span[0] == idx_t and position == "before")
        marker = self._marker_of(region) if \
            (head_changes and region in _REGION_MARKERS) else None
        content = [l for l in b.leading if _marker_region(l) is None]
        while content and not content[0].strip():
            content.pop(0)
        while content and not content[-1].strip():
            content.pop()
        first_old = self.blocks[0]
        self.blocks.pop(idx_b)
        idx_t = self.blocks.index(t)
        insert_at = idx_t if position == "before" else idx_t + 1
        self.blocks.insert(insert_at, b)
        if first_old is not b and self.blocks.index(first_old) > 0 and \
                (not first_old.leading or first_old.leading[0].strip()):
            first_old.leading.insert(0, "\n")
        pre = ["\n"] if insert_at else []
        b.leading = pre + content + ["\n"] if content else pre
        if marker is not None:
            if span[0] == idx_t and position == "before":
                b.leading += [marker, "\n"]
            else:
                new_head = next(x for x in self.blocks
                                if x.region == region)
                pos = 1 if new_head.leading and \
                        not new_head.leading[0].strip() else 0
                new_head.leading[pos:pos] = [marker, "\n"]
        if self.blocks[0] is not b:
            # the first block never owns a leading seam at parse time;
            # strip one left behind if a mid-file block became first
            while self.blocks[0].leading and \
                    not self.blocks[0].leading[0].strip():
                self.blocks[0].leading.pop(0)

    def delete_section(self, name: str, archived: bool = False) -> None:
        """Remove a section entirely (no git — this is the ini level).

        Marker bookkeeping: a marker owned by the deleted block is handed
        to the block that now starts the region. If the region becomes
        empty, its marker is kept as an anchor next to the pair's other
        marker; only when the whole pair (active + archived) is empty are
        both markers dropped."""
        b = self.block(name, archived)
        idx = self.blocks.index(b)
        src = b.region
        src_marker = self._marker_of(src) if src in _REGION_MARKERS else None
        self.blocks.pop(idx)
        if src_marker is None:
            return
        new_head = next((x for x in self.blocks if x.region == src), None)
        if new_head is not None:
            pos = 1 if new_head.leading and \
                    not new_head.leading[0].strip() else 0
            new_head.leading[pos:pos] = [src_marker, "\n"]
            return
        twin = _TWIN.get(src)
        if twin is not None and self._region_span(twin) is not None:
            self._ensure_marker(src)
            return
        # whole pair empty: drop the twin's marker too
        if twin is not None:
            self._marker_of(twin)

    def rename_section(self, name: str, new: str,
                       archived: bool = False) -> None:
        b = self.block(name, archived)
        if new != name and any(x is not b and x.name == new for x in self.blocks):
            raise ModelsIniError(f"section [{new}] already exists")
        # keep the '#' prefix of archived headers, or reparse would turn
        # the section active (with all its keys still commented out)
        b.header = ("# " if b.archived else "") + f"[{new}]{_eol(b.header)}"
        b.name = new

    def upsert_key(self, name: str, key: str, value: str,
                   archived: bool = False) -> None:
        b = self.block(name, archived)
        prefix = "# " if b.archived else ""
        i = b.key_index(key)
        if i >= 0:
            b.body[i] = prefix + f"{key} = {value}{_eol(b.body[i])}"
        else:
            b.body.append(prefix + f"{key} = {value}\n")

    def remove_key(self, name: str, key: str, archived: bool = False) -> None:
        b = self.block(name, archived)
        i = b.key_index(key)
        if i < 0:
            raise ModelsIniError(f"key '{key}' not found in [{name}]")
        del b.body[i]

    def reorder_keys(self, name: str, order: List[str],
                     archived: bool = False) -> None:
        """Reorder the effective key lines of section ``name`` so that the
        keys named in ``order`` appear in that relative order.

        Only the key lines are permuted, and only among the key-line
        slots: the ``model`` line and any non-key line (comment, blank,
        marker) keep their positions, so the surrounding bytes are
        untouched. The operation is deliberately lenient so it can never
        block or corrupt an edit -- keys named in ``order`` that are not
        present are ignored, and keys it omits keep their current
        relative order. An order that matches the current arrangement is
        a no-op. A section with a duplicated key is left untouched (its
        key lines cannot be told apart by name)."""
        b = self.block(name, archived)
        kl = b.key_lines()
        slots = [i for i, k in enumerate(kl) if k is not None and k != "model"]
        cur = [kl[i] for i in slots]
        if len(set(cur)) != len(cur):
            return                      # duplicate keys: not reorderable
        cur_set = set(cur)
        seen: set = set()
        target: List[str] = []
        for k in order:
            if k in cur_set and k not in seen:
                target.append(k)
                seen.add(k)
        target += [k for k in cur if k not in seen]
        if target == cur:
            return
        by_key = {k: b.body[i] for i, k in zip(slots, cur)}
        for i, k in zip(slots, target):
            b.body[i] = by_key[k]


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
