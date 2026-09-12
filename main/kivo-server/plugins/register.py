from enum import Enum

# Minimal logger so this module does not depend on loguru
class SimpleLogger:
    def __init__(self, tag):
        self.tag = tag

    def info(self, msg):
        print(f"[INFO] [{self.tag}] {msg}")

    def debug(self, msg):
        print(f"[DEBUG] [{self.tag}] {msg}")

    def error(self, msg):
        print(f"[ERROR] [{self.tag}] {msg}")

    def bind(self, tag):
        return SimpleLogger(tag)

TAG = __name__
logger = SimpleLogger(TAG)


class ToolType(Enum):
    NONE = (1, "Call the tool and do nothing else")
    WAIT = (2, "Call the tool and wait for it to return")
    CHANGE_SYS_PROMPT = (3, "Change the system prompt to switch persona or role")
    SYSTEM_CTL = (
        4,
        "System control that affects the conversation flow (exit, play music, ...); requires the conn parameter",
    )
    IOT_CTL = (5, "IoT device control; requires the conn parameter")
    MCP_CLIENT = (6, "MCP client")

    def __init__(self, code, message):
        self.code = code
        self.message = message


class Action(Enum):
    ERROR = (-1, "Error")
    NOTFOUND = (0, "Function not found")
    NONE = (1, "Do nothing")
    RESPONSE = (2, "Reply directly")
    REQLLM = (3, "Call the function, then ask the LLM to generate the reply")
    RECORD = (4, "Record the tool call in the dialogue history without calling the LLM")

    def __init__(self, code, message):
        self.code = code
        self.message = message


class ActionResponse:
    def __init__(self, action: Action, result=None, response=None):
        self.action = action  # action type
        self.result = result  # result produced by the action
        self.response = response  # content to reply with directly


class FunctionItem:
    def __init__(self, name, description, func, type):
        self.name = name
        self.description = description
        self.func = func
        self.type = type


class DeviceTypeRegistry:
    """Registry of IoT device types and their functions"""

    def __init__(self):
        self.type_functions = {}  # type_signature -> {func_name: FunctionItem}

    def generate_device_type_id(self, descriptor):
        """Generate a type ID from the device capability descriptor"""
        properties = sorted(descriptor["properties"].keys())
        methods = sorted(descriptor["methods"].keys())
        # The combination of properties and methods uniquely identifies the device type
        type_signature = (
            f"{descriptor['name']}:{','.join(properties)}:{','.join(methods)}"
        )
        return type_signature

    def get_device_functions(self, type_id):
        """Get all functions for a device type"""
        return self.type_functions.get(type_id, {})

    def register_device_type(self, type_id, functions):
        """Register a device type and its functions"""
        if type_id not in self.type_functions:
            self.type_functions[type_id] = functions


# Global function registry
all_function_registry = {}
# module name -> list of function names; expands module-level plugin names into concrete function names
module_func_map = {}


def register_function(name, desc, type=None):
    """Decorator that registers a function in the global registry"""

    def decorator(func):
        all_function_registry[name] = FunctionItem(name, desc, func, type)
        # Record module -> function mapping so module-level plugin config can be expanded
        module_name = func.__module__.split(".")[-1]
        module_func_map.setdefault(module_name, []).append(name)
        logger.bind(tag=TAG).debug(f"Function '{name}' loaded and available for registration")
        return func

    return decorator


def register_device_function(name, desc, type=None):
    """Decorator for registering device-level functions"""

    def decorator(func):
        logger.bind(tag=TAG).debug(f"Device function '{name}' loaded")
        return func

    return decorator


class FunctionRegistry:
    def __init__(self):
        self.function_registry = {}
        self.logger = logger

    def register_function(self, name, func_item=None):
        # Register directly when a func_item is given
        if func_item:
            self.function_registry[name] = func_item
            self.logger.bind(tag=TAG).debug(f"Function '{name}' registered directly")
            return func_item

        # Otherwise look it up in all_function_registry
        func = all_function_registry.get(name)
        if not func:
            self.logger.bind(tag=TAG).error(f"Function '{name}' not found")
            return None
        self.function_registry[name] = func
        self.logger.bind(tag=TAG).debug(f"Function '{name}' registered")
        return func

    def unregister_function(self, name):
        # Unregister a function; check that it exists first
        if name not in self.function_registry:
            self.logger.bind(tag=TAG).error(f"Function '{name}' not found")
            return False
        self.function_registry.pop(name, None)
        self.logger.bind(tag=TAG).info(f"Function '{name}' unregistered")
        return True

    def get_function(self, name):
        return self.function_registry.get(name)

    def get_all_functions(self):
        return self.function_registry

    def get_all_function_desc(self):
        return [func.description for _, func in self.function_registry.items()]
