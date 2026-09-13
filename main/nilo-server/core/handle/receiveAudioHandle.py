import time
import json
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler
from core.utils.util import audio_to_data
from core.handle.abortHandle import handleAbortMessage
from core.handle.intentHandler import handle_user_intent
from core.utils.output_counter import check_device_output_limit
from core.handle.sendAudioHandle import send_stt_message, SentenceType
from core.providers.tts.dto.dto import ContentType, TTSMessageDTO
from plugins.base import PluginAction

TAG = __name__


async def handleAudioMessage(conn: "ConnectionHandler", pcm_frame):
    # Whether anyone is speaking in the current chunk
    have_voice = conn.vad.is_vad(conn, pcm_frame)
    # If the device was just woken up, briefly ignore VAD
    if hasattr(conn, "just_woken_up") and conn.just_woken_up:
        have_voice = False
        # Resume VAD after a short delay
        if not hasattr(conn, "vad_resume_task") or conn.vad_resume_task.done():
            conn.vad_resume_task = asyncio.create_task(resume_vad_detection(conn))
        return
    # Server-side AEC needs to trigger interruption in real time
    if conn.client_aec and have_voice:
        if conn.client_is_speaking and conn.client_listen_mode != "manual":
            await handleAbortMessage(conn)
    # Long-idle detection, used to say goodbye
    await no_voice_close_connect(conn, have_voice)
    # Receive audio
    await conn.asr.receive_audio(conn, pcm_frame, have_voice)


async def resume_vad_detection(conn: "ConnectionHandler"):
    # Wait 2 seconds, then resume VAD
    await asyncio.sleep(2)
    conn.just_woken_up = False


async def startToChat(conn: "ConnectionHandler", text):
    # Check whether the input is JSON (carrying speaker info)
    speaker_name = None
    actual_text = text

    try:
        # Try to parse JSON-formatted input
        if text.strip().startswith("{") and text.strip().endswith("}"):
            data = json.loads(text)
            if "speaker" in data and "content" in data:
                speaker_name = data["speaker"]
                actual_content = data["content"]
                conn.logger.bind(tag=TAG).info(f"Parsed speaker info: {speaker_name}")

                # Keep the {"speaker":...} JSON only the first time this speaker appears so the
                # model addresses them naturally once; later turns fall back to plain text so the
                # repeated name does not tempt the model to keep addressing them
                if speaker_name not in conn.introduced_speakers:
                    conn.introduced_speakers.add(speaker_name)
                    actual_text = text
                else:
                    actual_text = actual_content
    except (json.JSONDecodeError, KeyError):
        # If parsing fails, keep using the original text
        pass

    # Save the speaker info on the connection object
    if speaker_name:
        conn.current_speaker = speaker_name
    else:
        conn.current_speaker = None

    if conn.need_bind:
        await check_bind_device(conn)
        return

    # If today's output character count exceeds the limit
    if conn.max_output_size > 0:
        if check_device_output_limit(
            conn.headers.get("device-id"), conn.max_output_size
        ):
            await max_out_size(conn)
            return

    # In manual mode, do not interrupt playback
    if conn.client_is_speaking and conn.client_listen_mode != "manual":
        await handleAbortMessage(conn)

    # Run intent analysis first, using the actual text content
    intent_handled = await handle_user_intent(conn, actual_text)

    if intent_handled:
        # Intent already handled, skip the chat
        return

    await send_stt_message(conn, actual_text)

    # Prepare to start a new session
    conn.client_abort = False

    # Plugin handling: text pre-processing and interception
    processed_text = actual_text
    if hasattr(conn, "plugin_manager") and conn.plugin_manager:
        result, action = await conn.plugin_manager.process_text(conn, actual_text)

        if action == PluginAction.CLOSE:
            # Intercept and finish - play the result and close the connection
            if result:
                conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=result)
                conn.tts.tts_end(conn)
            return

        elif action == PluginAction.INTERCEPT:
            # Intercept without finishing - play the result
            if result:
                conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=result)
                conn.tts.tts_end(conn)
            return

        # RELEASE - continue processing
        processed_text = result

    # Intent not handled and plugins did not intercept, continue the normal chat flow
    conn.executor.submit(conn.chat, processed_text)


async def no_voice_close_connect(conn: "ConnectionHandler", have_voice):
    if have_voice:
        conn.last_activity_time = time.time() * 1000
        return
    # Only check for timeout once the timestamp has been initialized
    if conn.last_activity_time > 0.0:
        no_voice_time = time.time() * 1000 - conn.last_activity_time
        close_connection_no_voice_time = int(
            conn.config.get("close_connection_no_voice_time", 120)
        )
        if (
            not conn.close_after_chat
            and no_voice_time > 1000 * close_connection_no_voice_time
        ):
            conn.close_after_chat = True
            conn.client_abort = False
            end_prompt = conn.config.get("end_prompt", {})
            if end_prompt and end_prompt.get("enable", True) is False:
                conn.logger.bind(tag=TAG).info("Ending conversation, no closing prompt needed")
                await conn.close()
                return
            prompt = end_prompt.get("prompt")
            if not prompt:
                prompt = "Please begin with ```Time really flies``` and end this conversation with warm, heartfelt words that show you are reluctant to say goodbye."
            await startToChat(conn, prompt)


async def max_out_size(conn: "ConnectionHandler"):
    # Play the prompt for exceeding the maximum output size
    conn.client_abort = False
    text = "Sorry, something has come up. Let us talk again tomorrow around this time. See you then!"
    await send_stt_message(conn, text)
    file_path = "config/assets/max_output_size.wav"
    opus_packets = await audio_to_data(file_path)
    conn.tts.tts_audio_queue.put((SentenceType.LAST, opus_packets, text))
    conn.close_after_chat = True


async def check_bind_device(conn: "ConnectionHandler"):
    if conn.bind_code:
        # Make sure bind_code is 6 digits
        if len(conn.bind_code) != 6:
            conn.logger.bind(tag=TAG).error(f"Invalid bind code format: {conn.bind_code}")
            text = "Bind code format is invalid, please check the configuration."
            await send_stt_message(conn, text)
            return

        text = f"Open the control panel and enter {conn.bind_code} to bind this device."
        await send_stt_message(conn, text)

        # Play the prompt sound
        music_path = "config/assets/bind_code.wav"
        opus_packets = await audio_to_data(music_path)
        conn.tts.tts_audio_queue.put((SentenceType.FIRST, opus_packets, text))

        # Play the digits one by one
        for i in range(6):  # only play 6 digits
            try:
                digit = conn.bind_code[i]
                num_path = f"config/assets/bind_code/{digit}.wav"
                num_packets = await audio_to_data(num_path)
                conn.tts.tts_audio_queue.put((SentenceType.MIDDLE, num_packets, None))
            except Exception as e:
                conn.logger.bind(tag=TAG).error(f"Failed to play digit audio: {e}")
                continue
        conn.tts.tts_audio_queue.put((SentenceType.LAST, [], None))
    else:
        # Play the not-bound prompt
        conn.client_abort = False
        text = "No version information was found for this device. Set the OTA address correctly and reflash the firmware."
        await send_stt_message(conn, text)
        music_path = "config/assets/bind_not_found.wav"
        opus_packets = await audio_to_data(music_path)
        conn.tts.tts_audio_queue.put((SentenceType.LAST, opus_packets, text))
