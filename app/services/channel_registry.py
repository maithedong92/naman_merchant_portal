import logging
from typing import Dict, List, Optional
from app.interfaces.channel_adapter import BaseChannelAdapter

logger = logging.getLogger("naman_portal.channel_registry")


class ChannelRegistry:
    """
    Central Registry for external sales channel adapters.
    Maintains a dictionary of registered adapters keyed by channel_code (e.g. 'SHOPEEFOOD', 'GRABMART').
    Enables loose coupling and zero overlap between modules.
    """
    _adapters: Dict[str, BaseChannelAdapter] = {}

    @classmethod
    def register(cls, adapter: BaseChannelAdapter) -> None:
        """Register a channel adapter into the system."""
        code = adapter.channel_code.upper()
        if code in cls._adapters:
            logger.warning(f"Overwriting existing adapter for channel '{code}'")
        cls._adapters[code] = adapter
        logger.info(f"Registered channel adapter: {code} ({adapter.display_name})")

    @classmethod
    def get(cls, channel_code: str) -> Optional[BaseChannelAdapter]:
        """Retrieve adapter for a given channel code."""
        return cls._adapters.get(channel_code.upper())

    @classmethod
    def list_channels(cls) -> List[str]:
        """List all currently registered channel codes."""
        return list(cls._adapters.keys())

    @classmethod
    def is_supported(cls, channel_code: str) -> bool:
        """Check whether a channel adapter is registered."""
        return channel_code.upper() in cls._adapters


channel_registry = ChannelRegistry()
