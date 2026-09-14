"""Register the robot tools with the inherited tool system.

This file exists because ``auto_import_modules("plugins_func.functions")`` is what the
unified tool handler scans (``core/providers/tools/unified_tool_handler.py``), and a
module has to live in that directory to be scanned. Everything it does lives in
``robot/agent/bridge.py``; nothing here is robot logic.

The tools register as ``IOT_CTL``, which is the only plugin type the server exposes to the
model without a ``config.yaml`` edit, so a deployment that connects a robot gets the robot
vocabulary and nothing else changes (docs/robot-agent.md).
"""

from robot.agent.bridge import register_robot_tools

register_robot_tools()
