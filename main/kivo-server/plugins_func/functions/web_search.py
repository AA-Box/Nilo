import httpx
from config.logger import setup_logging
from plugins_func.register import (
    register_function,
    ToolType,
    ActionResponse,
    Action,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

_DEFAULT_DESCRIPTION = (
    "Web search tool. Use it when the user explicitly asks to search the internet for something."
)

WEB_SEARCH_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": _DEFAULT_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keywords or question",
                }
            },
            "required": ["query"],
        },
    },
}


async def _search_metaso(api_key: str, query: str, max_results: int) -> str:
    """Call the Metaso search API"""
    url = "https://metaso.cn/api/v1/search"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "q": query,
        "size": max_results,
        "stream": False,
        "scope": "webpage",
        "includeSummary": True,
        "includeRawContent": False,
        "conciseSnippet": False,
    }
    logger.bind(tag=TAG).debug(f"Metaso search request | URL: {url} | payload: {payload}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.post(url, json=payload, headers=headers)
    data = response.json()
    logger.bind(tag=TAG).debug(f"Metaso search response | status: {response.status_code}")

    webpages = data.get("webpages", [])
    if not webpages:
        return "No relevant search results found."

    lines = ["[Web search results]"]
    for i, item in enumerate(webpages, 1):
        title = item.get("title", "Untitled")
        snippet = item.get("summary", "")
        date = item.get("date", "")
        lines.append(f"{i}. Title: {title}")
        if date:
            lines.append(f"   Date: {date}")
        if snippet:
            lines.append(f"   Summary: {snippet}")

    return "\n".join(lines)


async def _search_tavily(api_key: str, query: str, max_results: int) -> str:
    """Call the Tavily search API"""
    url = "https://api.tavily.com/search"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "query": query,
        "max_results": max_results,
        "search_depth": "advanced",
        "include_answer": "advanced",
    }
    logger.bind(tag=TAG).debug(f"Tavily search request | URL: {url} | payload: {payload}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.post(url, json=payload, headers=headers)
    data = response.json()
    logger.bind(tag=TAG).debug(f"Tavily search response | status: {response.status_code} | data: {data}")

    results = data.get("results", [])
    if not results:
        return "No relevant search results found."

    answer = data.get("answer", "")
    lines = [f"[Web search results]\nSummary: {answer}"]
    # for i, item in enumerate(results, 1):
    #     title = item.get("title", "Untitled")
    #     summary = item.get("content", "")
    #     lines.append(f"{i}. Title: {title}")
    #     if summary:
    #         lines.append(f"   Summary: {summary}")

    return "\n".join(lines)


@register_function("web_search", WEB_SEARCH_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def web_search(conn: "ConnectionHandler", query: str = None):
    logger.bind(tag=TAG).info(f"web_search called | query={query}")
    if not query:
        return ActionResponse(Action.REQLLM, "Please provide search keywords.", None)

    web_search_config = conn.config.get("plugins", {}).get("web_search", {})
    provider = web_search_config.get("provider", "").lower()
    max_results = int(web_search_config.get("max_results", 3))
    logger.bind(tag=TAG).info(f"web_search config | provider={provider} | max_results={max_results} | config_keys={list(web_search_config.keys())}")

    api_key = web_search_config.get("api_key", "")
    if not api_key:
        return ActionResponse(
            Action.REQLLM,
            "Web search has no API key configured; please set it in the config file.",
            None,
        )

    try:
        if provider == "metaso":
            result_text = await _search_metaso(api_key, query, max_results)
        elif provider == "tavily":
            result_text = await _search_tavily(api_key, query, max_results)
        else:
            return ActionResponse(
                Action.REQLLM,
                f"Web search is not configured or the configured provider is invalid (current: {provider}); please check the config.",
                None,
            )
        logger.bind(tag=TAG).info(f"Search results assembled:\n{result_text}")
    except httpx.TimeoutException:
        logger.bind(tag=TAG).error("Web search request timed out")
        result_text = "Web search request timed out; please try again later."
    except httpx.HTTPStatusError as e:
        logger.bind(tag=TAG).error(f"Web search request failed: {e}")
        result_text = "Web search request failed; please try again later."
    except Exception as e:
        logger.bind(tag=TAG).error(f"Web search error: {e}")
        result_text = "Web search ran into an error; please try again later."

    return ActionResponse(Action.REQLLM, result_text, None)
