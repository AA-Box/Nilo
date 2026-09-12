import asyncio
from typing import List, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from ..base import IntentProviderBase
from plugins_func.functions.play_music import initialize_music_handler
from config.logger import setup_logging
from core.utils.util import get_system_error_response
import re
import json
import hashlib
import time



TAG = __name__
logger = setup_logging()


class IntentProvider(IntentProviderBase):
    def __init__(self, config):
        super().__init__(config)
        self.llm = None
        self.promot = ""
        # Import the global cache manager
        from core.utils.cache.manager import cache_manager, CacheType

        self.cache_manager = cache_manager
        self.CacheType = CacheType
        self.history_count = 4  # Use the 4 most recent dialogue turns by default

    def get_intent_system_prompt(self, functions_list: str) -> str:
        """
        Dynamically build the system prompt from the configured intent options and available functions
        Args:
            functions: list of available functions, as a JSON-format string
        Returns:
            the formatted system prompt
        """

        # Build the function description section
        functions_desc = "Available functions:\n"
        for func in functions_list:
            func_info = func.get("function", {})
            name = func_info.get("name", "")
            desc = func_info.get("description", "")
            params = func_info.get("parameters", {})

            functions_desc += f"\nFunction name: {name}\n"
            functions_desc += f"Description: {desc}\n"

            if params:
                functions_desc += "Parameters:\n"
                for param_name, param_info in params.get("properties", {}).items():
                    param_desc = param_info.get("description", "")
                    param_type = param_info.get("type", "")
                    functions_desc += f"- {param_name} ({param_type}): {param_desc}\n"

            functions_desc += "---\n"

        prompt = (
            "[STRICT FORMAT REQUIREMENT] You must return JSON only. Never return any natural language!\n\n"
            "You are an intent recognition assistant. Analyze the user's last utterance, determine the user's intent and call the appropriate function.\n\n"
            "[IMPORTANT RULES] For the following kinds of queries, return result_for_context directly without calling a function:\n"
            "- Asking for the current time (e.g. what time is it, current time, check the time)\n"
            "- Asking for today's date (e.g. what's the date today, what day of the week is it, what is today's date)\n"
            "- Asking for today's lunar calendar date (e.g. what's today's lunar date, which solar term is it today)\n"
            "- Asking which city they are in (e.g. where am I now, do you know which city I'm in)"
            "The system will build the answer directly from context information.\n\n"
            "- If the user asks a question about exiting using an interrogative (e.g. 'how', 'why'), such as 'how did it exit?', note that this is NOT asking you to exit; return {'function_call': {'name': 'continue_chat'}\n"
            "- Only trigger handle_exit_intent when the user explicitly issues a command such as 'exit the system', 'end the conversation', or 'I don't want to talk to you anymore'\n\n"
            f"{functions_desc}\n"
            "Processing steps:\n"
            "1. Analyze the user input and determine the user's intent\n"
            "2. Check whether it is one of the basic information queries above (time, date, etc.); if so, return result_for_context\n"
            "3. Select the best-matching function from the available function list\n"
            "4. If a matching function is found, produce the corresponding function_call format\n"
            '5. If no matching function is found, return {"function_call": {"name": "continue_chat"}}\n\n'
            "Return format requirements:\n"
            "1. Must return pure JSON with no other text\n"
            "2. Must contain a function_call field\n"
            "3. function_call must contain a name field\n"
            "4. If the function requires parameters, an arguments field must be included\n\n"
            "Examples:\n"
            "```\n"
            "User: What time is it now?\n"
            'Return: {"function_call": {"name": "result_for_context"}}\n'
            "```\n"
            "```\n"
            "User: What is the current battery level?\n"
            'Return: {"function_call": {"name": "get_battery_level", "arguments": {"response_success": "The current battery level is {value}%", "response_failure": "Unable to get the current battery percentage"}}}\n'
            "```\n"
            "```\n"
            "User: What is the current screen brightness?\n"
            'Return: {"function_call": {"name": "self_screen_get_brightness"}}\n'
            "```\n"
            "```\n"
            "User: Set the screen brightness to 50%\n"
            'Return: {"function_call": {"name": "self_screen_set_brightness", "arguments": {"brightness": 50}}}\n'
            "```\n"
            "```\n"
            "User: I want to end the conversation\n"
            'Return: {"function_call": {"name": "handle_exit_intent", "arguments": {"say_goodbye": "goodbye"}}}\n'
            "```\n"
            "```\n"
            "User: Hello there\n"
            'Return: {"function_call": {"name": "continue_chat"}}\n'
            "```\n\n"
            "Notes:\n"
            "1. Return JSON only, with no other text\n"
            '2. First check whether the user query is a basic information query (time, date, etc.); if so, return {"function_call": {"name": "result_for_context"}} without an arguments parameter\n'
            '3. If no matching function is found, return {"function_call": {"name": "continue_chat"}}\n'
            "4. Make sure the returned JSON is well-formed and contains all required fields\n"
            "5. result_for_context takes no parameters; the system obtains the information from context automatically\n"
            "Special note:\n"
            "- When a single user input contains multiple commands (e.g. 'turn on the light and turn up the volume')\n"
            "- Return a JSON array of multiple function_call entries\n"
            "- Example: {'function_calls': [{name:'light_on'}, {name:'volume_up'}]}\n\n"
            "[FINAL WARNING] Never output any natural language, emoji or explanatory text! Output valid JSON only! Violating this rule will cause a system error!"
        )
        return prompt

    async def replyResult(self, text: str, original_text: str):
        """Use asyncio.to_thread to avoid blocking the event loop"""
        try:
            user_prompt = (
                "Based on the content above, reply to the user in a natural, human-sounding tone. Keep it concise and return the result directly. The user now says: "
                + original_text
            )
            # Run the blocking synchronous call in the thread pool via to_thread so the event loop is not blocked
            llm_result = await asyncio.to_thread(
                self.llm.response_no_stream,
                system_prompt=text,
                user_prompt=user_prompt,
            )
            return llm_result
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in generating reply result: {e}")
            return get_system_error_response(self.config)

    async def detect_intent(
        self, conn: "ConnectionHandler", dialogue_history: List[Dict], text: str
    ) -> str:
        if not self.llm:
            raise ValueError("LLM provider not set")
        if conn.func_handler is None:
            return '{"function_call": {"name": "continue_chat"}}'

        # Record the overall start time
        total_start_time = time.time()

        # Log which model is in use
        model_info = getattr(self.llm, "model_name", str(self.llm.__class__.__name__))
        logger.bind(tag=TAG).debug(f"Using intent recognition model: {model_info}")

        # Compute the cache key
        cache_key = hashlib.md5((conn.device_id + text).encode()).hexdigest()

        # Check the cache
        cached_intent = self.cache_manager.get(self.CacheType.INTENT, cache_key)
        if cached_intent is not None:
            cache_time = time.time() - total_start_time
            logger.bind(tag=TAG).debug(
                f"Using cached intent: {cache_key} -> {cached_intent}, took: {cache_time:.4f}s"
            )
            return cached_intent

        if self.promot == "":
            functions = conn.func_handler.get_functions()
            if hasattr(conn, "mcp_client"):
                mcp_tools = conn.mcp_client.get_available_tools()
                if mcp_tools is not None and len(mcp_tools) > 0:
                    if functions is None:
                        functions = []
                    functions.extend(mcp_tools)

            self.promot = self.get_intent_system_prompt(functions)

        music_config = initialize_music_handler(conn)
        music_file_names = music_config["music_file_names"]
        prompt_music = f"{self.promot}\n<musicNames>{music_file_names}\n</musicNames>"

        home_assistant_cfg = conn.config["plugins"].get("home_assistant")
        if home_assistant_cfg:
            devices = home_assistant_cfg.get("devices", [])
        else:
            devices = []
        if len(devices) > 0:
            hass_prompt = "\nBelow is the list of smart devices in my home (location, device name, entity_id), controllable via homeassistant\n"
            for device in devices:
                hass_prompt += device + "\n"
            prompt_music += hass_prompt

        logger.bind(tag=TAG).debug(f"User prompt: {prompt_music}")

        # Build the prompt from the user's dialogue history
        msgStr = ""

        # Get the most recent dialogue history
        start_idx = max(0, len(dialogue_history) - self.history_count)
        for i in range(start_idx, len(dialogue_history)):
            msgStr += f"{dialogue_history[i].role}: {dialogue_history[i].content}\n"

        msgStr += f"User: {text}\n"
        user_prompt = f"current dialogue:\n{msgStr}"

        # Record preprocessing completion time
        preprocess_time = time.time() - total_start_time
        logger.bind(tag=TAG).debug(f"Intent recognition preprocessing took: {preprocess_time:.4f}s")

        # Use the LLM for intent recognition
        llm_start_time = time.time()
        logger.bind(tag=TAG).debug(f"Starting LLM intent recognition call, model: {model_info}")

        try:
            # Run the blocking synchronous call in the thread pool via to_thread to avoid blocking the event loop
            intent = await asyncio.to_thread(
                self.llm.response_no_stream,
                system_prompt=prompt_music,
                user_prompt=user_prompt,
            )
        except Exception as e:
            logger.bind(tag=TAG).error(f"Error in intent detection LLM call: {e}")
            return '{"function_call": {"name": "continue_chat"}}'

        # Record LLM call completion time
        llm_time = time.time() - llm_start_time
        logger.bind(tag=TAG).debug(
            f"External LLM intent recognition complete, model: {model_info}, call took: {llm_time:.4f}s"
        )

        # Record post-processing start time
        postprocess_start_time = time.time()

        # Clean and parse the response
        intent = intent.strip()
        # Try to extract the JSON part
        match = re.search(r"\{.*\}", intent, re.DOTALL)
        if match:
            intent = match.group(0)

        # Record total processing time
        total_time = time.time() - total_start_time
        logger.bind(tag=TAG).debug(
            f"[Intent recognition perf] model: {model_info}, total: {total_time:.4f}s, LLM call: {llm_time:.4f}s, query: '{text[:20]}...'"
        )

        # Try to parse as JSON
        try:
            intent_data = json.loads(intent)
            # If it contains function_call, normalise it into a form suitable for handling
            if "function_call" in intent_data:
                function_data = intent_data["function_call"]
                function_name = function_data.get("name")
                function_args = function_data.get("arguments", {})

                # Log the recognised function call
                logger.bind(tag=TAG).info(
                    f"LLM recognised intent: {function_name}, arguments: {function_args}"
                )

                # Handle the different intent types
                if function_name == "result_for_context":
                    # Basic information query: build the result directly from context
                    logger.bind(tag=TAG).info(
                        "Detected result_for_context intent; will answer directly from context information"
                    )

                elif function_name == "continue_chat":
                    # Plain conversation
                    # Keep only non-tool-related messages
                    clean_history = [
                        msg
                        for msg in conn.dialogue.dialogue
                        if msg.role not in ["tool", "function"]
                    ]
                    conn.dialogue.dialogue = clean_history

                else:
                    # Function call
                    logger.bind(tag=TAG).info(f"Detected function call intent: {function_name}")

            # Unified cache handling and return
            self.cache_manager.set(self.CacheType.INTENT, cache_key, intent)
            postprocess_time = time.time() - postprocess_start_time
            logger.bind(tag=TAG).debug(f"Intent post-processing took: {postprocess_time:.4f}s")
            return intent
        except json.JSONDecodeError:
            # Post-processing time
            postprocess_time = time.time() - postprocess_start_time
            logger.bind(tag=TAG).error(
                f"Unable to parse intent JSON: {intent}, post-processing took: {postprocess_time:.4f}s"
            )
            # If parsing fails, fall back to the continue-chat intent
            return '{"function_call": {"name": "continue_chat"}}'
