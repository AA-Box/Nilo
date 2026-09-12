from ..base import MemoryProviderBase, logger
import time
import json
import os
import yaml
from config.config_loader import get_project_dir
from config.manage_api_client import generate_and_save_chat_summary
import asyncio
from core.utils.util import check_model_key


short_term_memory_prompt = """
# Spatio-temporal Memory Weaver

## Core Mission
Build a growing, dynamic memory network that keeps key information within a limited space while intelligently tracking how that information evolves.
Summarize the important information about the user from the conversation log so future conversations can be more personalized.

## Memory Rules
### 1. Three-dimensional memory scoring (run on every update)
| Dimension        | Criterion                                        | Weight |
|------------------|--------------------------------------------------|--------|
| Recency          | Freshness of the information (by dialogue turn)  | 40%    |
| Emotional weight | Carries a 💖 marker / number of repeat mentions  | 35%    |
| Link density     | Number of connections to other information       | 25%    |

### 2. Dynamic update mechanism
**Example: handling a name change**
Original memory: "former_names": ["Alex"], "current_name": "Alexander"
Trigger: a naming signal such as "my name is X" or "call me Y" is detected
Steps:
1. Move the old name into the "former_names" list
2. Record the naming timeline: "2024-02-15 14:32: switched to Alexander"
3. Append to memory_cubes: "identity shift from Alex to Alexander"

### 3. Space optimization
- **Compression**: use a symbol system to raise density
  - ✅"Alexander[BJ/SWE/🐱]"
  - ❌"Software engineer in Beijing, has a cat"
- **Eviction warning**: triggered when the total length is >=900 characters
  1. Delete information with a weight <60 that has not been mentioned for 3 turns
  2. Merge similar entries (keep the one with the most recent timestamp)

## Memory Structure
The output must be a parseable JSON string with no explanation, comments or notes. When saving memory, extract information only from the conversation; never mix in the example content.
```json
{
  "profile": {
    "identity": {
      "current_name": "",
      "former_names": [],
      "traits": []
    },
    "memory_cubes": [
      {
        "event": "Started a new job",
        "timestamp": "2024-03-20",
        "emotional_weight": 0.9,
        "links": ["afternoon tea"],
        "shelf_life_days": 30
      }
    ]
  },
  "relationships": {
    "frequent_topics": {"work": 12},
    "implicit_links": [""]
  },
  "pending": {
    "urgent": ["tasks that need immediate attention"],
    "proactive_care": ["help that could be offered proactively"]
  },
  "highlights": [
    "the most moving moments, strong emotional expressions, the user's own words"
  ]
}
```
"""


def extract_json_data(json_code):
    start = json_code.find("```json")
    # find the next closing ``` after start
    end = json_code.find("```", start + 1)
    # print("start:", start, "end:", end)
    if start == -1 or end == -1:
        try:
            jsonData = json.loads(json_code)
            return json_code
        except Exception as e:
            print("Error:", e)
        return ""
    jsonData = json_code[start + 7 : end]
    return jsonData


TAG = __name__


class MemoryProvider(MemoryProviderBase):
    def __init__(self, config, summary_memory):
        super().__init__(config)
        self.short_memory = ""
        self.save_to_file = True
        self.memory_path = get_project_dir() + "data/.memory.yaml"
        self.load_memory(summary_memory)

    def init_memory(
        self, role_id, llm, summary_memory=None, save_to_file=True, **kwargs
    ):
        super().init_memory(role_id, llm, **kwargs)
        self.save_to_file = save_to_file
        self.load_memory(summary_memory)

    def load_memory(self, summary_memory):
        # return directly when the summary memory came from the API
        if summary_memory or not self.save_to_file:
            self.short_memory = summary_memory
            return

        all_memory = {}
        if os.path.exists(self.memory_path):
            with open(self.memory_path, "r", encoding="utf-8") as f:
                all_memory = yaml.safe_load(f) or {}
        if self.role_id in all_memory:
            self.short_memory = all_memory[self.role_id]

    def save_memory_to_file(self):
        all_memory = {}
        if os.path.exists(self.memory_path):
            with open(self.memory_path, "r", encoding="utf-8") as f:
                all_memory = yaml.safe_load(f) or {}
        all_memory[self.role_id] = self.short_memory
        with open(self.memory_path, "w", encoding="utf-8") as f:
            yaml.dump(all_memory, f, allow_unicode=True)

    async def save_memory(self, msgs, session_id=None):
        # log the model being used
        model_info = getattr(self.llm, "model_name", str(self.llm.__class__.__name__))
        logger.bind(tag=TAG).debug(f"Memory save model: {model_info}")
        api_key = getattr(self.llm, "api_key", None)
        memory_key_msg = check_model_key("memory summary LLM", api_key)
        if memory_key_msg:
            logger.bind(tag=TAG).error(memory_key_msg)
        if self.llm is None:
            logger.bind(tag=TAG).error("LLM is not set for memory provider")
            return None

        if len(msgs) < 2:
            return None

        msgStr = ""
        for msg in msgs:
            content = msg.content

            # Extract content from JSON format if present (for ASR with emotion/language tags)
            try:
                if content and content.strip().startswith("{") and content.strip().endswith("}"):
                    data = json.loads(content)
                    if "content" in data:
                        content = data["content"]
            except (json.JSONDecodeError, KeyError, TypeError):
                # If parsing fails, use original content
                pass

            if msg.role == "user":
                msgStr += f"User: {content}\n"
            elif msg.role == "assistant":
                msgStr += f"Assistant: {content}\n"
        if self.short_memory and len(self.short_memory) > 0:
            msgStr += "Previous memory:\n"
            msgStr += self.short_memory

        # current time
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        msgStr += f"Current time: {time_str}"

        if self.save_to_file:
            try:
                result = self.llm.response_no_stream(
                    short_term_memory_prompt,
                    msgStr,
                    max_tokens=2000,
                    temperature=0.2,
                )
                json_str = extract_json_data(result)
                json.loads(json_str)  # validate the JSON
                self.short_memory = json_str
                self.save_memory_to_file()
            except Exception as e:
                logger.bind(tag=TAG).error(f"Error in saving memory: {e}")
        else:
            # when save_to_file is False, call the Java-side chat summary endpoint
            summary_id = session_id if session_id else self.role_id
            await generate_and_save_chat_summary(summary_id)
        logger.bind(tag=TAG).info(
            f"Save memory successful - Role: {self.role_id}, Session: {session_id}"
        )

        return self.short_memory

    async def query_memory(self, query: str) -> str:
        return self.short_memory
