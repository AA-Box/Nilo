import requests
from requests.exceptions import RequestException
from config.logger import setup_logging
from core.providers.llm.base import LLMProviderBase

TAG = __name__
logger = setup_logging()


class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.agent_id = config.get("agent_id")  # Home Assistant agent_id
        self.api_key = config.get("api_key")
        self.base_url = config.get("base_url", config.get("url"))  # base_url preferred, url as fallback
        self.api_url = f"{self.base_url}/api/conversation/process"  # full API URL

    def response(self, session_id, dialogue, **kwargs):
        # Home Assistant does its own intent handling, so skip the assistant's and just forward the user's utterance

        # Extract the content of the last 'user' message
        input_text = None
        if isinstance(dialogue, list):  # make sure dialogue is a list
            # walk backwards to find the last 'user' message
            for message in reversed(dialogue):
                if message.get("role") == "user":  # found a 'user' message
                    input_text = message.get("content", "")
                    break  # stop at the first match

        # Build the request payload
        payload = {
            "text": input_text,
            "agent_id": self.agent_id,
            "conversation_id": session_id,  # use session_id as conversation_id
        }
        # Request headers
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # Send the POST request
        with requests.post(self.api_url, json=payload, headers=headers) as response:
            # Raise on HTTP errors
            response.raise_for_status()

            # Parse the response
            data = response.json()
        speech = (
            data.get("response", {})
            .get("speech", {})
            .get("plain", {})
            .get("speech", "")
        )

        # Yield the generated speech
        if speech:
            yield speech
        else:
            logger.bind(tag=TAG).warning("API response contains no speech content")

    def response_with_functions(self, session_id, dialogue, functions=None):
        logger.bind(tag=TAG).error(
            f"homeassistant does not support function calling; use a different intent recognition method"
        )
