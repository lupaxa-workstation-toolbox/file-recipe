"""Exit-coded errors for File Recipe."""

from __future__ import annotations


class FileRecipeError(Exception):
    """Base error. ``code`` is the process exit status."""

    code = 1

    def __init__(self, message: str, code: int | None = None) -> None:
        """Store a message and an optional exit code."""
        super().__init__(message)
        if code is not None:
            self.code = code


class RecipeError(FileRecipeError):
    """Invalid arguments or an invalid recipe. Exit 2."""

    code = 2


class ConflictError(FileRecipeError):
    """The plan has unresolved conflicts. Exit 3."""

    code = 3


class FilesystemError(FileRecipeError):
    """A filesystem operation failed. Exit 4."""

    code = 4


class VerificationError(FileRecipeError):
    """A result did not match the plan. Exit 5."""

    code = 5


class PartialError(FileRecipeError):
    """Some operations finished and some did not. Exit 6."""

    code = 6


class RecoveryError(FileRecipeError):
    """The journal needs a person to decide. Exit 7."""

    code = 7


class UndoError(FileRecipeError):
    """Undo could not restore the recorded state. Exit 8."""

    code = 8


class LimitError(FileRecipeError):
    """A configured resource limit was hit. Exit 9."""

    code = 9


class StalePlanError(FileRecipeError):
    """A source changed after the plan was built. Exit 10."""

    code = 10
