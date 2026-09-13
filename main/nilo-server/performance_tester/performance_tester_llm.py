import asyncio
import logging
import os
import statistics
import time
import concurrent.futures
from typing import Dict, Optional
import yaml
import aiohttp
from tabulate import tabulate
from core.utils.llm import create_instance as create_llm_instance
from config.settings import load_config

# Set global log level to WARNING to suppress INFO logs
logging.basicConfig(level=logging.WARNING)

description = "LLM performance test"


class LLMPerformanceTester:
    def __init__(self, config):
        self.config = config
        # Use agent-style test content that includes a system prompt
        self.system_prompt = self._load_system_prompt()
        self.test_sentences = self.config.get("module_test", {}).get(
            "test_sentences",
            [
                "Hi, I'm feeling a bit down today, can you cheer me up?",
                "Can you check tomorrow's weather for me?",
                "I'd like to hear a fun story, can you tell me one?",
                "What time is it? What day of the week is it today?",
                "Set an alarm for 8 AM tomorrow to remind me about my meeting",
            ],
        )
        self.results = {}

    def _load_system_prompt(self) -> str:
        """Load the system prompt"""
        try:
            prompt_file = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), self.config.get("prompt_template", "agent-base-prompt.txt")
            )
            with open(prompt_file, "r", encoding="utf-8") as f:
                content = f.read()
                # Replace template variables with test values
                content = content.replace(
                    "{{base_prompt}}", "You are a smart and friendly AI assistant"
                )
                content = content.replace(
                    "{{emojiList}}", "😀,😃,😄,😁,😊,😍,🤔,😮,😱,😢,😭,😴,😵,🤗,🙄"
                )
                content = content.replace("{{current_time}}", "2024-08-17 12:30:45")
                content = content.replace("{{today_date}}", "2024-08-17")
                content = content.replace("{{today_weekday}}", "Saturday")
                content = content.replace("{{lunar_date}}", "14th day of the 7th lunar month, Jiachen year")
                content = content.replace("{{local_address}}", "Beijing")
                content = content.replace("{{weather_info}}", "Sunny today, 25-32℃")
                return content
        except Exception as e:
            print(f"Failed to load system prompt file: {e}")
            return "You are a smart and friendly AI assistant. Reply to the user in a warm, friendly tone."

    def _collect_response_sync(self, llm, messages, llm_name, sentence_start):
        """Helper that collects response data synchronously"""
        chunks = []
        first_token_received = False
        first_token_time = None

        try:
            response_generator = llm.response("perf_test", messages)
            chunk_count = 0
            for chunk in response_generator:
                chunk_count += 1
                # Every few chunks, check whether we should stop
                if chunk_count % 10 == 0:
                    # Exit early if the current thread has been flagged to stop
                    import threading

                    if (
                        threading.current_thread().ident
                        != threading.main_thread().ident
                    ):
                        # Not the main thread: check whether we should stop
                        pass

                # Check whether the chunk contains an error message
                chunk_str = str(chunk)
                if (
                    "error" in chunk_str.lower()
                    or "exception" in chunk_str.lower()
                    or "502" in chunk_str.lower()
                ):
                    error_msg = chunk_str.lower()
                    print(f"{llm_name} response contains an error: {error_msg}")
                    # Raise an exception carrying the error message
                    raise Exception(chunk_str)

                if not first_token_received and chunk.strip() != "":
                    first_token_time = time.time() - sentence_start
                    first_token_received = True
                    print(f"{llm_name} first token: {first_token_time:.3f}s")
                chunks.append(chunk)
        except Exception as e:
            # More detailed error info
            error_msg = str(e).lower()
            print(f"{llm_name} response collection error: {error_msg}")
            # For 502 or network errors, re-raise so the caller handles it
            if (
                "502" in error_msg
                or "bad gateway" in error_msg
                or "error code: 502" in error_msg
                or "error" in str(e).lower()
                or "exception" in str(e).lower()
            ):
                raise e
            # For other errors, return partial results
            return chunks, first_token_time

        return chunks, first_token_time

    async def _check_ollama_service(self, base_url: str, model_name: str) -> bool:
        """Check Ollama service status asynchronously"""
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(f"{base_url}/api/version") as response:
                    if response.status != 200:
                        print(f"Ollama service not running or unreachable: {base_url}")
                        return False
                async with session.get(f"{base_url}/api/tags") as response:
                    if response.status == 200:
                        data = await response.json()
                        models = data.get("models", [])
                        if not any(model["name"] == model_name for model in models):
                            print(
                                f"Ollama model {model_name} not found, download it first with `ollama pull {model_name}`"
                            )
                            return False
                    else:
                        print("Failed to fetch Ollama model list")
                        return False
                return True
            except Exception as e:
                print(f"Cannot connect to Ollama service: {str(e)}")
                return False

    async def _test_single_sentence(
        self, llm_name: str, llm, sentence: str
    ) -> Optional[Dict]:
        """Test performance for a single sentence"""
        try:
            print(f"{llm_name} testing: {sentence[:20]}...")
            sentence_start = time.time()
            first_token_received = False
            first_token_time = None

            # Build messages including the system prompt
            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": sentence},
            ]

            # Use asyncio.wait_for for timeout control
            try:
                loop = asyncio.get_event_loop()
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    # Submit the response-collection task
                    future = executor.submit(
                        self._collect_response_sync,
                        llm,
                        messages,
                        llm_name,
                        sentence_start,
                    )

                    # Enforce the timeout with asyncio.wait_for
                    try:
                        response_chunks, first_token_time = await asyncio.wait_for(
                            asyncio.wrap_future(future), timeout=10.0
                        )
                    except asyncio.TimeoutError:
                        print(f"{llm_name} test timed out (10s), skipping")
                        # Force-cancel the future
                        future.cancel()
                        # Wait briefly so the thread pool task can respond to cancellation
                        try:
                            await asyncio.wait_for(
                                asyncio.wrap_future(future), timeout=1.0
                            )
                        except (
                            asyncio.TimeoutError,
                            concurrent.futures.CancelledError,
                            Exception,
                        ):
                            # Ignore all exceptions so the program keeps running
                            pass
                        return None

            except Exception as timeout_error:
                print(f"{llm_name} processing error: {timeout_error}")
                return None

            response_time = time.time() - sentence_start
            print(f"{llm_name} response complete: {response_time:.3f}s")

            return {
                "name": llm_name,
                "type": "llm",
                "first_token_time": first_token_time,
                "response_time": response_time,
            }
        except Exception as e:
            error_msg = str(e).lower()
            # Check for 502 or network errors
            if (
                "502" in error_msg
                or "bad gateway" in error_msg
                or "error code: 502" in error_msg
            ):
                print(f"{llm_name} hit a 502 error, skipping test")
                return {
                    "name": llm_name,
                    "type": "llm",
                    "errors": 1,
                    "error_type": "502 network error",
                }
            print(f"{llm_name} sentence test failed: {str(e)}")
            return None

    async def _test_llm(self, llm_name: str, config: Dict) -> Dict:
        """Test a single LLM's performance asynchronously"""
        try:
            # For Ollama, skip the api_key check and handle specially
            if llm_name == "Ollama":
                base_url = config.get("base_url", "http://localhost:11434")
                model_name = config.get("model_name")
                if not model_name:
                    print("Ollama model_name not configured")
                    return {
                        "name": llm_name,
                        "type": "llm",
                        "errors": 1,
                        "error_type": "network error",
                    }

                if not await self._check_ollama_service(base_url, model_name):
                    return {
                        "name": llm_name,
                        "type": "llm",
                        "errors": 1,
                        "error_type": "network error",
                    }
            else:
                if "api_key" in config and any(
                    x in config["api_key"] for x in ["你的", "placeholder", "sk-xxx"]
                ):
                    print(f"Skipping unconfigured LLM: {llm_name}")
                    return {
                        "name": llm_name,
                        "type": "llm",
                        "errors": 1,
                        "error_type": "config error",
                    }

            # Get the actual type (compatible with legacy config)
            module_type = config.get("type", llm_name)
            llm = create_llm_instance(module_type, config)

            # Normalize to UTF-8 encoding
            test_sentences = [
                s.encode("utf-8").decode("utf-8") for s in self.test_sentences
            ]

            # Create test tasks for all sentences
            sentence_tasks = []
            for sentence in test_sentences:
                sentence_tasks.append(
                    self._test_single_sentence(llm_name, llm, sentence)
                )

            # Run all sentence tests concurrently, handling any exceptions
            sentence_results = await asyncio.gather(
                *sentence_tasks, return_exceptions=True
            )

            # Process results, filtering out exceptions and None values
            valid_results = []
            for result in sentence_results:
                if isinstance(result, dict) and result is not None:
                    valid_results.append(result)
                elif isinstance(result, Exception):
                    error_msg = str(result).lower()
                    if "502" in error_msg or "bad gateway" in error_msg:
                        print(f"{llm_name} hit a 502 error, skipping this sentence")
                        return {
                            "name": llm_name,
                            "type": "llm",
                            "errors": 1,
                            "error_type": "502 network error",
                        }
                    else:
                        print(f"{llm_name} sentence test error: {result}")

            if not valid_results:
                print(f"{llm_name} no valid data, possibly a network issue or config error")
                return {
                    "name": llm_name,
                    "type": "llm",
                    "errors": 1,
                    "error_type": "network error",
                }

            # Treat the test as failed if too few results are valid
            if len(valid_results) < len(test_sentences) * 0.3:  # require at least a 30% success rate
                print(
                    f"{llm_name} too few sentences succeeded ({len(valid_results)}/{len(test_sentences)}), network may be unstable or the API may be faulty"
                )
                return {
                    "name": llm_name,
                    "type": "llm",
                    "errors": 1,
                    "error_type": "network error",
                }

            first_token_times = [
                r["first_token_time"]
                for r in valid_results
                if r.get("first_token_time")
            ]
            response_times = [r["response_time"] for r in valid_results]

            # Filter outliers (beyond 3 standard deviations)
            if len(response_times) > 1:
                mean = statistics.mean(response_times)
                stdev = statistics.stdev(response_times)
                filtered_times = [t for t in response_times if t <= mean + 3 * stdev]
            else:
                filtered_times = response_times

            return {
                "name": llm_name,
                "type": "llm",
                "avg_response": sum(response_times) / len(response_times),
                "avg_first_token": (
                    sum(first_token_times) / len(first_token_times)
                    if first_token_times
                    else 0
                ),
                "success_rate": f"{len(valid_results)}/{len(test_sentences)}",
                "errors": 0,
            }
        except Exception as e:
            error_msg = str(e).lower()
            if "502" in error_msg or "bad gateway" in error_msg:
                print(f"LLM {llm_name} hit a 502 error, skipping test")
            else:
                print(f"LLM {llm_name} test failed: {str(e)}")
            error_type = "network error"
            if "timeout" in str(e).lower():
                error_type = "connection timeout"
            return {
                "name": llm_name,
                "type": "llm",
                "errors": 1,
                "error_type": error_type,
            }

    def _print_results(self):
        """Print test results"""
        print("\n" + "=" * 50)
        print("LLM performance test results")
        print("=" * 50)

        if not self.results:
            print("No test results available")
            return

        headers = ["Model", "Avg response time (s)", "First token time (s)", "Success rate", "Status"]
        table_data = []

        # Collect and categorize all data
        valid_results = []
        error_results = []

        for name, data in self.results.items():
            if data["errors"] == 0:
                # Successful result
                avg_response = f"{data['avg_response']:.3f}"
                avg_first_token = (
                    f"{data['avg_first_token']:.3f}"
                    if data["avg_first_token"] > 0
                    else "-"
                )
                success_rate = data.get("success_rate", "N/A")
                status = "✅ OK"

                # Keep the value used for sorting
                first_token_value = (
                    data["avg_first_token"]
                    if data["avg_first_token"] > 0
                    else float("inf")
                )

                valid_results.append(
                    {
                        "name": name,
                        "avg_response": avg_response,
                        "avg_first_token": avg_first_token,
                        "success_rate": success_rate,
                        "status": status,
                        "sort_key": first_token_value,
                    }
                )
            else:
                # Error result
                avg_response = "-"
                avg_first_token = "-"
                success_rate = "0/5"

                # Get the specific error type
                error_type = data.get("error_type", "network error")
                status = f"❌ {error_type}"

                error_results.append(
                    [name, avg_response, avg_first_token, success_rate, status]
                )

        # Sort by first-token time ascending
        valid_results.sort(key=lambda x: x["sort_key"])

        # Convert sorted valid results into table rows
        for result in valid_results:
            table_data.append(
                [
                    result["name"],
                    result["avg_response"],
                    result["avg_first_token"],
                    result["success_rate"],
                    result["status"],
                ]
            )

        # Append error results at the end of the table
        table_data.extend(error_results)

        print(tabulate(table_data, headers=headers, tablefmt="grid"))
        print("\nTest notes:")
        print("- Content: agent conversation scenario with the full system prompt")
        print("- Timeout: each request waits at most 10 seconds")
        print("- Error handling: models with 502 or network errors are skipped automatically")
        print("- Success rate: sentences answered successfully / total sentences tested")
        print("\nTest complete!")

    async def run(self):
        """Run the full async test suite"""
        print("Scanning for available LLM modules...")

        # Create all test tasks
        all_tasks = []

        # LLM test tasks
        if self.config.get("LLM") is not None:
            for llm_name, config in self.config.get("LLM", {}).items():
                # Validate config
                if llm_name == "CozeLLM":
                    if any(x in config.get("bot_id", "") for x in ["你的"]) or any(
                        x in config.get("user_id", "") for x in ["你的"]
                    ):
                        print(f"LLM {llm_name} has no bot_id/user_id configured, skipped")
                        continue
                elif "api_key" in config and any(
                    x in config["api_key"] for x in ["你的", "placeholder", "sk-xxx"]
                ):
                    print(f"LLM {llm_name} has no api_key configured, skipped")
                    continue

                # For Ollama, check the service status first
                if llm_name == "Ollama":
                    base_url = config.get("base_url", "http://localhost:11434")
                    model_name = config.get("model_name")
                    if not model_name:
                        print("Ollama model_name not configured")
                        continue

                    if not await self._check_ollama_service(base_url, model_name):
                        continue

                print(f"Adding LLM test task: {llm_name}")
                all_tasks.append(self._test_llm(llm_name, config))

        print(f"\nFound {len(all_tasks)} available LLM modules")
        print("\nTesting all modules concurrently...\n")

        # Run all test tasks concurrently, each with its own timeout
        async def test_with_timeout(task, timeout=30):
            """Wrap a test task with timeout protection"""
            try:
                return await asyncio.wait_for(task, timeout=timeout)
            except asyncio.TimeoutError:
                print(f"Test task timed out ({timeout}s), skipping")
                return {
                    "name": "Unknown",
                    "type": "llm",
                    "errors": 1,
                    "error_type": "connection timeout",
                }
            except Exception as e:
                print(f"Test task error: {str(e)}")
                return {
                    "name": "Unknown",
                    "type": "llm",
                    "errors": 1,
                    "error_type": "network error",
                }

        # Wrap each task with timeout protection
        protected_tasks = [test_with_timeout(task) for task in all_tasks]

        # Run all test tasks concurrently
        all_results = await asyncio.gather(*protected_tasks, return_exceptions=True)

        # Process results
        for result in all_results:
            if isinstance(result, dict):
                if result.get("errors") == 0:
                    self.results[result["name"]] = result
                else:
                    # Record errors too, so failures show in the report
                    if result.get("name") != "Unknown":
                        self.results[result["name"]] = result
            elif isinstance(result, Exception):
                print(f"Error processing test result: {str(result)}")

        # Print results
        print("\nGenerating test report...")
        self._print_results()


async def main():
    config = await load_config()
    tester = LLMPerformanceTester(config)
    await tester.run()


if __name__ == "__main__":
    asyncio.run(main())
