"""HTTP clients for the outbound calls reflection needs.

Identity only makes outbound calls during the self-model reflection
flow — reads recent turns from memory, asks a configured hemisphere-
driver to write the reflection. These clients are constructed lazily
in the lifespan so identity can boot even when both URLs are unset
(reflection just returns 503 in that case).
"""

from .hemisphere_client import HemisphereClient
from .memory_client import MemoryClient

__all__ = ["HemisphereClient", "MemoryClient"]
