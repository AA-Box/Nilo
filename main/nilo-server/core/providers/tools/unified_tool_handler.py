"""Unified tool handler"""

import json
from typing import Dict, List, Any, Optional
from config.logger import setup_logging
from plugins_func.loadplugins import auto_import_modules

from .base import ToolType
from plugins_func.register import Action, ActionResponse
from .unified_tool_manager import ToolManager
from .server_plugins import ServerPluginExecutor
from .server_mcp import ServerMCPExecutor
from .device_iot import DeviceIoTExecutor
from .device_mcp import DeviceMCPExecutor
from .mcp_endpoint import MCPEndpointExecutor
from core.handle.sendAudioHandle import send_display_message


class UnifiedToolHandler:
    """Unified tool handler"""

    def __init__(self, conn):
        self.conn = conn
        self.config = conn.config
        self.logger = setup_logging()

        # create the tool manager
        self.tool_manager = ToolManager(conn)

        # create the executors
        self.server_plugin_executor = ServerPluginExecutor(conn)
        self.server_mcp_executor = ServerMCPExecutor(conn)
        self.device_iot_executor = DeviceIoTExecutor(conn)
        self.device_mcp_executor = DeviceMCPExecutor(conn)
        self.mcp_endpoint_executor = MCPEndpointExecutor(conn)

        # register the executors
        self.tool_manager.register_executor(
            ToolType.SERVER_PLUGIN, self.server_plugin_executor
        )
        self.tool_manager.register_executor(
            ToolType.SERVER_MCP, self.server_mcp_executor
        )
        self.tool_manager.register_executor(
            ToolType.DEVICE_IOT, self.device_iot_executor
        )
        self.tool_manager.register_executor(
            ToolType.DEVICE_MCP, self.device_mcp_executor
        )
        self.tool_manager.register_executor(
            ToolType.MCP_ENDPOINT, self.mcp_endpoint_executor
        )

        # init flag
        self.finish_init = False

    async def _initialize(self):
        """Async initialization"""
        try:
            # auto-import plugin modules
            auto_import_modules("plugins_func.functions")

            # initialize server-side MCP
            await self.server_mcp_executor.initialize()

            # initialize the MCP endpoint
            await self._initialize_mcp_endpoint()

            # initialize Home Assistant (if needed)
            self._initialize_home_assistant()

            self.finish_init = True
            self.logger.debug("Unified tool handler initialized")

            # log the list of currently supported tools
            self.current_support_functions()

        except Exception as e:
            self.logger.error(f"Unified tool handler initialization failed: {e}")

    async def _initialize_mcp_endpoint(self):
        """Initialize the MCP endpoint"""
        try:
            from .mcp_endpoint import connect_mcp_endpoint

            # read the MCP endpoint URL from config
            mcp_endpoint_url = self.config.get("mcp_endpoint", "")

            from config.placeholders import is_placeholder

            if (
                mcp_endpoint_url
                and not is_placeholder(mcp_endpoint_url)
                and mcp_endpoint_url != "null"
            ):
                self.logger.info(f"Initializing MCP endpoint: {mcp_endpoint_url}")
                mcp_endpoint_client = await connect_mcp_endpoint(
                    mcp_endpoint_url, self.conn
                )

                if mcp_endpoint_client:
                    # store the MCP endpoint client on the connection
                    self.conn.mcp_endpoint_client = mcp_endpoint_client
                    self.logger.info("MCP endpoint initialized")
                else:
                    self.logger.warning("MCP endpoint initialization failed")

        except Exception as e:
            self.logger.error(f"Failed to initialize MCP endpoint: {e}")

    def _initialize_home_assistant(self):
        """Initialize the Home Assistant prompt"""
        try:
            from plugins_func.functions.hass_init import append_devices_to_prompt

            append_devices_to_prompt(self.conn)
        except ImportError:
            pass  # ignore import errors
        except Exception as e:
            self.logger.error(f"Failed to initialize Home Assistant: {e}")

    def get_functions(self) -> List[Dict[str, Any]]:
        """Get the function descriptions of all tools"""
        return self.tool_manager.get_function_descriptions()

    def current_support_functions(self) -> List[str]:
        """Get the list of currently supported function names"""
        func_names = self.tool_manager.get_supported_tool_names()
        self.logger.info(f"Currently supported functions: {func_names}")
        return func_names

    def upload_functions_desc(self):
        """Refresh the function description list"""
        self.tool_manager.refresh_tools()
        self.logger.info("Function description list refreshed")

    def has_tool(self, tool_name: str) -> bool:
        """Check whether the given tool exists"""
        return self.tool_manager.has_tool(tool_name)

    async def handle_llm_function_call(
        self, conn, function_call_data: Dict[str, Any]
    ) -> Optional[ActionResponse]:
        """Handle an LLM function call"""
        try:
            # multiple function calls
            if "function_calls" in function_call_data:
                responses = []
                for call in function_call_data["function_calls"]:
                    result = await self.tool_manager.execute_tool(
                        call["name"], call.get("arguments", {})
                    )
                    responses.append(result)
                return self._combine_responses(responses)

            # single function call
            function_name = function_call_data["name"]
            arguments = function_call_data.get("arguments", {})

            # if arguments is a string, try to parse it as JSON
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments) if arguments else {}
                except json.JSONDecodeError:
                    self.logger.error(f"Unable to parse function arguments: {arguments}")
                    return ActionResponse(
                        action=Action.ERROR,
                        response="Unable to parse function arguments",
                    )

            self.logger.debug(f"Calling function: {function_name}, arguments: {arguments}")

            # send the tool-call display message to the device
            try:
                await send_display_message(self.conn, f"% {function_name}")
            except Exception as e:
                self.logger.warning(f"Failed to send tool-call display message: {e}")

            # execute the tool call
            result = await self.tool_manager.execute_tool(function_name, arguments)
            return result

        except Exception as e:
            self.logger.error(f"Error handling function call: {e}")
            return ActionResponse(action=Action.ERROR, response=str(e))

    def _combine_responses(self, responses: List[ActionResponse]) -> ActionResponse:
        """Combine the responses of multiple function calls"""
        if not responses:
            return ActionResponse(action=Action.NONE, response="No response")

        # if any call errored, return the first error
        for response in responses:
            if response.action == Action.ERROR:
                return response

        # merge all successful responses
        contents = []
        responses_text = []

        for response in responses:
            if response.content:
                contents.append(response.content)
            if response.response:
                responses_text.append(response.response)

        # decide the final action type
        final_action = Action.RESPONSE
        for response in responses:
            if response.action == Action.REQLLM:
                final_action = Action.REQLLM
                break

        return ActionResponse(
            action=final_action,
            result="; ".join(contents) if contents else None,
            response="; ".join(responses_text) if responses_text else None,
        )

    async def register_iot_tools(self, descriptors: List[Dict[str, Any]]):
        """Register IoT device tools"""
        self.device_iot_executor.register_iot_tools(descriptors)
        self.tool_manager.refresh_tools()
        self.logger.info(f"Registered tools for {len(descriptors)} IoT devices")

    def get_tool_statistics(self) -> Dict[str, int]:
        """Get tool statistics"""
        return self.tool_manager.get_tool_statistics()

    async def cleanup(self):
        """Clean up resources"""
        try:
            await self.server_mcp_executor.cleanup()

            # close the MCP endpoint connection
            if (
                hasattr(self.conn, "mcp_endpoint_client")
                and self.conn.mcp_endpoint_client
            ):
                await self.conn.mcp_endpoint_client.close()

            self.logger.info("Tool handler cleanup complete")
        except Exception as e:
            self.logger.error(f"Tool handler cleanup failed: {e}")
