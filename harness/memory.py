"""Persistent cross-run memory.

Plain files + an index, exposed to models as ordinary custom tools — deliberately
NOT a provider-specific memory tool type, so it works with every adapter.
One markdown file per fact with frontmatter; INDEX.md is regenerated on every
write/delete and injected into the orchestrator's first message each run
(progressive disclosure: the index is cheap, full entries are read on demand).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

VALID_TYPES = ("preference", "learning", "project-fact", "reference")


class MemoryStore:
    def __init__(self, root: Path, run_id: str):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self._index_path = self.root / "INDEX.md"

    # -- tool-facing operations -------------------------------------------------

    def read(self, name: str) -> str:
        path = self._path(name)
        if not path.exists():
            raise FileNotFoundError(f"No memory named '{name}'. Check the index for valid names.")
        return path.read_text()

    def write(self, name: str, description: str, mem_type: str, content: str) -> str:
        name = self._slugify(name)
        if mem_type not in VALID_TYPES:
            raise ValueError(f"type must be one of {VALID_TYPES}, got '{mem_type}'")
        path = self._path(name)
        existed = path.exists()
        created_by = f"{self.run_id} ({date.today().isoformat()})"
        if existed:
            prior = _parse_frontmatter(path.read_text())
            created_by = prior.get("created_by", created_by)
        lines = [
            "---",
            f"name: {name}",
            f"description: {description}",
            f"type: {mem_type}",
            f"created_by: {created_by}",
        ]
        if existed:
            lines.append(f"updated_by: {self.run_id} ({date.today().isoformat()})")
        lines += ["---", "", content.strip(), ""]
        path.write_text("\n".join(lines))
        self._regen_index()
        return f"Memory '{name}' {'updated' if existed else 'created'}."

    def delete(self, name: str) -> str:
        path = self._path(name)
        if not path.exists():
            raise FileNotFoundError(f"No memory named '{name}'.")
        path.unlink()
        self._regen_index()
        return f"Memory '{name}' deleted."

    # -- run-start injection ----------------------------------------------------

    def load_index(self) -> str:
        """Return the index text (regenerating it first), or '' if no memories."""
        self._regen_index()
        if not self._index_path.exists():
            return ""
        text = self._index_path.read_text().strip()
        return "" if text == _EMPTY_INDEX.strip() else text

    # -- internals ----------------------------------------------------------------

    def _path(self, name: str) -> Path:
        return self.root / f"{self._slugify(name)}.md"

    @staticmethod
    def _slugify(name: str) -> str:
        slug = re.sub(r"[^a-z0-9-]+", "-", name.strip().lower()).strip("-")
        if not slug:
            raise ValueError(f"Invalid memory name: '{name}'")
        return slug

    def _regen_index(self) -> None:
        entries = []
        for f in sorted(self.root.glob("*.md")):
            if f.name == "INDEX.md":
                continue
            meta = _parse_frontmatter(f.read_text())
            name = meta.get("name", f.stem)
            desc = meta.get("description", "")
            mtype = meta.get("type", "")
            entries.append(f"- [{name}] ({mtype}) — {desc}")
        if entries:
            self._index_path.write_text(
                "# Memory index\n\nUse memory_read(name) for the full entry.\n\n"
                + "\n".join(entries) + "\n"
            )
        else:
            self._index_path.write_text(_EMPTY_INDEX)


_EMPTY_INDEX = "# Memory index\n\n(no memories yet)\n"


def _parse_frontmatter(text: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return meta
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta
