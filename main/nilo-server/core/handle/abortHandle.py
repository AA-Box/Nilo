import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
TAG = __name__


async def handleAbortMessage(conn: "ConnectionHandler"):
    conn.logger.bind(tag=TAG).info("Abort message received")
    # Set the abort state; this automatically interrupts the LLM and TTS tasks
    conn.close_after_chat = False
    conn.client_abort = True
    conn.clear_queues()
    # Interrupt the client's speaking state
    await conn.websocket.send(
        json.dumps({"type": "tts", "state": "stop", "session_id": conn.session_id})
    )
    conn.clearSpeakStatus()
    # Nilo robot seam: the voice loop keeps the conversation and starts listening again.
    # Returns False for a session it does not own; never raises.
    from robot.voice import handle_barge_in as robot_barge_in

    await robot_barge_in(conn)
    conn.logger.bind(tag=TAG).info("Abort message received-end")
