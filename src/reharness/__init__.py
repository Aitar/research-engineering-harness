"""RE Harness: an AI-native research and engineering ledger."""

from . import services as _services
from .hardening import HardenedHarness

_services.Harness = HardenedHarness
Harness = HardenedHarness

__version__ = "0.2.0"

__all__ = ["Harness", "__version__"]
