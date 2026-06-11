from .base import BackendError, BackendResult, CLIBackend
from .claude import ClaudeBackend
from .codex import CodexBackend
from .gemini import GeminiBackend

BACKENDS = {
    "claude": ClaudeBackend,
    "codex": CodexBackend,
    "gemini": GeminiBackend,
}

__all__ = [
    "BACKENDS",
    "BackendError",
    "BackendResult",
    "CLIBackend",
    "ClaudeBackend",
    "CodexBackend",
    "GeminiBackend",
]
