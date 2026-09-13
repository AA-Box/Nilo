import time
import asyncio
from collections import deque
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class AudioRateController:
    """
    Audio rate controller - paces audio sending precisely by the 60ms frame duration
    Avoids accumulated timing drift under high concurrency
    """

    def __init__(self, frame_duration=60, send_delay=0):
        """
        Args:
            frame_duration: duration of one audio frame (ms), default 60ms
            send_delay: custom send interval (ms).
                        0 = pace by frame_duration;
                        >0 = pace by send_delay (no enqueue delay on the main thread, no pre-buffering)
        """
        # pacing interval (ms): use the configured value when send_delay > 0, else frame_duration
        self.interval_ms = send_delay if send_delay > 0 else frame_duration
        self.queue = deque()
        self.play_position = 0  # virtual playback position (ms)
        self.start_timestamp = None  # start timestamp (read-only, never modified)
        self.pending_send_task = None
        self.logger = logger
        self.queue_empty_event = asyncio.Event()  # queue-empty event
        self.queue_empty_event.set()  # starts out empty
        self.queue_has_data_event = asyncio.Event()  # queue-has-data event
        self._last_queue_empty_time = 0  # time the queue last became empty (seconds)

    def reset(self):
        """Reset the controller state"""
        if self.pending_send_task and not self.pending_send_task.done():
            self.pending_send_task.cancel()
            # the cancelled task is cleaned up on the next loop iteration; no need to block

        self.queue.clear()
        self.play_position = 0
        self.start_timestamp = None  # set by the first audio packet
        self._last_queue_empty_time = 0  # reset time
        # update events
        self.queue_empty_event.set()
        self.queue_has_data_event.clear()

    def add_audio(self, opus_packet):
        """Add an audio packet to the queue"""
        # If the queue was empty, shift the timestamp to keep playback time continuous
        # so audio added after waiting on a tool call is not sent early.
        # A very short gap (<1 frame) is normal streaming and needs no reset.
        if len(self.queue) == 0 and self.play_position > 0:
            elapsed_since_empty = (time.monotonic() - self._last_queue_empty_time) * 1000
            # only a gap longer than one frame counts as a real "resume after pause"
            if elapsed_since_empty >= self.interval_ms:
                self.start_timestamp = time.monotonic() - (self.play_position / 1000)
                self.logger.bind(tag=TAG).debug(
                    f"Queue resumed from empty, timestamp reset, playback position: {self.play_position}ms, gap: {elapsed_since_empty:.0f}ms"
                )

        self.queue.append(("audio", opus_packet))
        # update events
        self.queue_empty_event.clear()
        self.queue_has_data_event.set()

    def add_message(self, message_callback):
        """
        Add a message to the queue (sent immediately, takes no playback time)

        Args:
            message_callback: message send callback, async def()
        """
        if len(self.queue) == 0 and self.play_position > 0:
            elapsed_since_empty = (time.monotonic() - self._last_queue_empty_time) * 1000
            if elapsed_since_empty >= self.interval_ms:
                self.start_timestamp = time.monotonic() - (self.play_position / 1000)
                self.logger.bind(tag=TAG).debug(
                    f"Queue resumed from empty, timestamp reset, playback position: {self.play_position}ms, gap: {elapsed_since_empty:.0f}ms"
                )

        self.queue.append(("message", message_callback))
        # update events
        self.queue_empty_event.clear()
        self.queue_has_data_event.set()

    def _get_elapsed_ms(self):
        """Get the elapsed time (ms)"""
        if self.start_timestamp is None:
            return 0
        return (time.monotonic() - self.start_timestamp) * 1000

    async def check_queue(self, send_audio_callback):
        """
        Check the queue and send audio/messages on schedule

        Args:
            send_audio_callback: audio send callback, async def(opus_packet)
        """
        while self.queue:
            item = self.queue[0]
            item_type = item[0]

            if item_type == "message":
                # message: send immediately, takes no playback time
                _, message_callback = item
                self.queue.popleft()
                try:
                    await message_callback()
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"Failed to send message: {e}")
                    raise

            elif item_type == "audio":
                if self.start_timestamp is None:
                    self.start_timestamp = time.monotonic()

                _, opus_packet = item

                # wait in a loop until the send time arrives
                while True:
                    # compute the time difference
                    elapsed_ms = self._get_elapsed_ms()
                    output_ms = self.play_position

                    if elapsed_ms < output_ms:
                        # not time to send yet; compute how long to wait
                        wait_ms = output_ms - elapsed_ms

                        # wait, then re-check (may be interrupted)
                        try:
                            await asyncio.sleep(wait_ms / 1000)
                        except asyncio.CancelledError:
                            self.logger.bind(tag=TAG).debug("Audio send task cancelled")
                            raise
                        # after waiting, re-check the time (loop back to while True)
                    else:
                        # time reached; leave the wait loop
                        break

                # time reached; pop from the queue and send
                self.queue.popleft()
                self.play_position += self.interval_ms
                try:
                    await send_audio_callback(opus_packet)
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"Failed to send audio: {e}")
                    raise

        # queue drained; update events
        self.queue_empty_event.set()
        self.queue_has_data_event.clear()
        self._last_queue_empty_time = time.monotonic()  # record when the queue became empty

    def start_sending(self, send_audio_callback):
        """
        Start the async send task

        Args:
            send_audio_callback: audio send callback

        Returns:
            asyncio.Task: the send task
        """

        async def _send_loop():
            try:
                while True:
                    # wait for the queue-has-data event instead of polling
                    await self.queue_has_data_event.wait()

                    await self.check_queue(send_audio_callback)
            except asyncio.CancelledError:
                self.logger.bind(tag=TAG).debug("Audio send loop stopped")
            except Exception as e:
                self.logger.bind(tag=TAG).error(f"Audio send loop error: {e}")

        self.pending_send_task = asyncio.create_task(_send_loop())
        return self.pending_send_task

    def stop_sending(self):
        """Stop the send task"""
        if self.pending_send_task and not self.pending_send_task.done():
            self.pending_send_task.cancel()
            self.logger.bind(tag=TAG).debug("Audio send task cancelled")
