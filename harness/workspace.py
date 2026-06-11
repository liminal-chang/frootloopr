"""Per-run shared scratchpad. All agents in a run read/write the same workspace;
context is pulled from files on demand instead of pushed through transcripts."""

from __future__ import annotations

from pathlib import Path


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.tool_results_dir = self.root / "tool_results"
        self.notes_dir = self.root / "notes"
        self.tool_results_dir.mkdir(exist_ok=True)
        self.notes_dir.mkdir(exist_ok=True)

    def resolve(self, rel_path: str) -> Path:
        """Resolve a workspace-relative path, refusing escapes."""
        p = (self.root / rel_path).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(f"Path escapes the workspace: {rel_path}")
        return p

    def relative(self, p: Path) -> str:
        return str(p.relative_to(self.root))
