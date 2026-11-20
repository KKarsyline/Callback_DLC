"""Read-only, body-only adapter for Ombre-Brain 3.6.14 Markdown vaults."""

from .vault import OmbreVault, host_recall, local_recall

__all__ = ["OmbreVault", "host_recall", "local_recall"]
