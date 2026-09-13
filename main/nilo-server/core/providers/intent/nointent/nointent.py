from ..base import IntentProviderBase
from typing import List, Dict
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class IntentProvider(IntentProviderBase):
    async def detect_intent(self, conn, dialogue_history: List[Dict], text: str) -> str:
        """
        Default intent detection: always returns continue_chat
        Args:
            dialogue_history: list of dialogue history entries
            text: the current utterance
        Returns:
            always the "continue_chat" function call
        """
        logger.bind(tag=TAG).debug(
            "Using NoIntentProvider, always returning continue chat"
        )
        return '{"function_call": {"name": "continue_chat"}}'
