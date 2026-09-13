import os
import base64
import functools
import threading
from typing import Optional, Dict

import httpx

TAG = __name__


class DeviceNotFoundException(Exception):
    pass


class DeviceBindException(Exception):
    def __init__(self, bind_code):
        self.bind_code = bind_code
        super().__init__(f"Device bind exception, bind code: {bind_code}")


class ManageApiClient:
    _instance = None
    _instance_lock = threading.Lock()  # Guards reads/writes of _instance and _closed
    _async_clients = {}  # One client per event loop
    _secret = None
    _closed = False  # Set to True by safe_close(); no new connections are created afterwards

    def __new__(cls, config):
        """Singleton: one global instance, accepting a config argument"""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._init_client(config)
            return cls._instance

    @classmethod
    def _init_client(cls, config):
        """Initialize the config (client creation is deferred)"""
        cls.config = config.get("manager-api")

        if not cls.config:
            raise Exception("manager-api configuration error")

        if not cls.config.get("url") or not cls.config.get("secret"):
            raise Exception("manager-api url or secret is misconfigured")

        from config.placeholders import is_placeholder

        if is_placeholder(cls.config.get("secret")):
            raise Exception("manager-api.secret is still a placeholder; set it before enabling remote config")

        cls._secret = cls.config.get("secret")
        cls.max_retries = cls.config.get("max_retries", 6)  # Max retry count
        cls.retry_delay = cls.config.get("retry_delay", 10)  # Initial retry delay (seconds)
        # Do not create the AsyncClient here; defer until first use
        cls._async_clients = {}
        cls._closed = False

    @classmethod
    async def _ensure_async_client(cls):
        """Ensure the async client exists (one per event loop)"""
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            loop_id = id(loop)

            # Checking the closed flag and creating the pool must be one critical section,
            # so no request can write a new client back after safe_close() clears the pool.
            with cls._instance_lock:
                if cls._closed:
                    raise Exception("ManageApiClient is closed; no new HTTP clients will be created")

                if loop_id not in cls._async_clients:
                    # The server may close connections itself, which the httpx pool cannot reliably detect and clean up
                    limits = httpx.Limits(
                        max_keepalive_connections=0,  # Disable keep-alive; open a new connection every time
                    )
                    cls._async_clients[loop_id] = httpx.AsyncClient(
                        base_url=cls.config.get("url"),
                        headers={
                            "User-Agent": f"PythonClient/2.0 (PID:{os.getpid()})",
                            "Accept": "application/json",
                            "Authorization": "Bearer " + cls._secret,
                        },
                        timeout=cls.config.get("timeout", 30),
                        limits=limits,  # Apply the limits
                        trust_env=False,
                    )
                return cls._async_clients[loop_id]
        except RuntimeError:
            # No running event loop
            raise Exception("Must be called from an async context")

    @classmethod
    async def _async_request(cls, method: str, endpoint: str, **kwargs) -> Dict:
        """Send a single async HTTP request and handle the response"""
        # Make sure the client exists
        client = await cls._ensure_async_client()
        endpoint = endpoint.lstrip("/")
        response = None
        try:
            response = await client.request(method, endpoint, **kwargs)
            response.raise_for_status()

            result = response.json()

            # Handle business errors returned by the API
            if result.get("code") == 10041:
                raise DeviceNotFoundException(result.get("msg"))
            elif result.get("code") == 10042:
                raise DeviceBindException(result.get("msg"))
            elif result.get("code") != 0:
                raise Exception(f"API returned an error: {result.get('msg', 'unknown error')}")

            # Return the payload on success
            return result.get("data") if result.get("code") == 0 else None
        finally:
            # Make sure the response is closed (runs even on exceptions)
            if response is not None:
                await response.aclose()

    @classmethod
    def _should_retry(cls, exception: Exception) -> bool:
        """Decide whether an exception is retryable"""
        # Network connection errors
        if isinstance(
            exception, (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError)
        ):
            return True

        # HTTP status code errors
        if isinstance(exception, httpx.HTTPStatusError):
            status_code = exception.response.status_code
            return status_code in [408, 429, 500, 502, 503, 504]

        return False

    @classmethod
    async def _execute_async_request(cls, method: str, endpoint: str, **kwargs) -> Dict:
        """Async request executor with retries"""
        import asyncio

        retry_count = 0

        while retry_count <= cls.max_retries:
            try:
                # Execute the async request
                return await cls._async_request(method, endpoint, **kwargs)
            except Exception as e:
                # Decide whether to retry
                if retry_count < cls.max_retries and cls._should_retry(e):
                    retry_count += 1
                    print(
                        f"{method} {endpoint} async request failed; retrying in {cls.retry_delay:.1f} s (attempt {retry_count})"
                    )
                    await asyncio.sleep(cls.retry_delay)
                    continue
                else:
                    # Not retryable; re-raise
                    raise

    @classmethod
    def _get_instance(cls):
        """Thread-safely get a reference to the singleton instance

        Callers should use the returned local reference instead of re-reading
        ManageApiClient._instance after the None check: even if safe_close()
        later sets _instance to None, the reference already obtained still
        points at the original object, closing the TOCTOU window between
        "checked for None" and "used".
        """
        with cls._instance_lock:
            return cls._instance

    @classmethod
    def safe_close(cls):
        """Safely close all async connection pools"""
        import asyncio

        with cls._instance_lock:
            cls._closed = True
            clients = list(cls._async_clients.values())
            cls._async_clients.clear()
            cls._instance = None

        for client in clients:
            try:
                asyncio.run(client.aclose())
            except Exception:
                pass


def api_guard(error_msg: str = None, raise_when_closed: bool = False):
    """Decorator: fetch the singleton, null-check it, and catch exceptions in one place

    - Grabs a local reference via _get_instance() once and injects it as the
      decorated function's first argument, closing the TOCTOU window against safe_close();
    - If the instance is uninitialized or closed: raise_when_closed=True raises an
      explicit exception (for startup-path functions), otherwise returns None silently
      (for daemon-thread paths);
    - If error_msg is not None, request exceptions are caught, logged and None is returned;
      if None, the exception propagates for the caller to handle.
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            instance = ManageApiClient._get_instance()
            if instance is None:
                if raise_when_closed:
                    raise Exception("ManageApiClient is not initialized or has been closed")
                return None
            if error_msg is None:
                return await func(instance, *args, **kwargs)
            try:
                return await func(instance, *args, **kwargs)
            except Exception as e:
                print(f"{error_msg}: {e}")
                return None

        return wrapper

    return decorator


@api_guard(raise_when_closed=True)
async def get_server_config(instance) -> Optional[Dict]:
    """Fetch the server base config"""
    return await instance._execute_async_request("POST", "/config/server-base")


@api_guard(raise_when_closed=True)
async def get_agent_models(
    instance, mac_address: str, client_id: str, selected_module: Dict
) -> Optional[Dict]:
    """Fetch the agent model config"""
    return await instance._execute_async_request(
        "POST",
        "/config/agent-models",
        json={
            "macAddress": mac_address,
            "clientId": client_id,
            "selectedModule": selected_module,
        },
    )


@api_guard("Failed to fetch correction words")
async def get_correct_words(instance, mac_address: str) -> Optional[Dict]:
    """Fetch the agent's correction words"""
    return await instance._execute_async_request(
        "POST", "/config/correct-words",
        json={"macAddress": mac_address}
    )


@api_guard("Failed to generate and save chat summary")
async def generate_and_save_chat_summary(instance, session_id: str) -> Optional[Dict]:
    """Generate and save a chat summary (called from a daemon thread; returns None silently if the service is closed)"""
    return await instance._execute_async_request(
        "POST",
        f"/agent/chat-summary/{session_id}/save",
    )


@api_guard("Failed to generate and save chat title")
async def generate_and_save_chat_title(instance, session_id: str) -> Optional[Dict]:
    """Generate and save a chat title (called from a daemon thread; returns None silently if the service is closed)"""
    return await instance._execute_async_request(
        "POST",
        f"/agent/chat-title/{session_id}/generate",
    )


@api_guard("TTS report failed")
async def report(
    instance, mac_address: str, session_id: str, chat_type: int, content: str, audio, report_time
) -> Optional[Dict]:
    """Report chat history asynchronously"""
    if not content:
        return None
    return await instance._execute_async_request(
        "POST",
        f"/agent/chat-history/report",
        json={
            "macAddress": mac_address,
            "sessionId": session_id,
            "chatType": chat_type,
            "content": content,
            "reportTime": report_time,
            "audioBase64": (
                base64.b64encode(audio).decode("utf-8") if audio else None
            ),
        },
    )


@api_guard("Address book lookup failed")
async def lookup_address_book(instance, caller_mac: str, nickname: str) -> Optional[Dict]:
    """Look up the target device by nickname"""
    return await instance._execute_async_request(
        "GET",
        f"/device/address-book/lookup?callerMac={caller_mac}&nickname={nickname}",
    )


def init_service(config):
    ManageApiClient(config)


def manage_api_http_safe_close():
    ManageApiClient.safe_close()
