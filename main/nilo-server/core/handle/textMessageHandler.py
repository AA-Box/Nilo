from abc import abstractmethod, ABC
from typing import Dict, Any

from core.handle.textMessageType import TextMessageType

TAG = __name__


class TextMessageHandler(ABC):
    """Abstract base class for text message handlers"""

    @abstractmethod
    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        """Handle a message"""
        pass

    @property
    @abstractmethod
    def message_type(self) -> TextMessageType:
        """The message type this handler processes"""
        pass
