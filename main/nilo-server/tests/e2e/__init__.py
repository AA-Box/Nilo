"""The end-to-end suite: a real server, a real socket, a real simulated robot.

What is real here is everything between the device socket and the device socket. What is
faked is exactly the four things that would otherwise need a model or a cloud account,
and each fake sits at a seam the production code already has:

``ASR``     the simulator sends the recognizer's *output* (``listen``/``detect``), which
            is a frame a real device sends. No audio, no model; the server path is real.
``TTS``     :class:`~tests.e2e.fakes.FakeTTS` replaces synthesis and the Opus encoder at
            the ``conn.tts`` seam. The speech arbiter, the sink, the audio state machine
            and the ``tts`` frames the device receives are all production code.
``LLM``     :class:`~tests.e2e.fakes.RuleBasedLLM` satisfies the same
            :class:`~robot.agent.agent.LLMProvider` protocol every inherited provider
            does, and answers from a table instead of a network.
``vision``  :class:`~robot.vision.providers.ColourBlobDetector` reads the pixels the
            simulator's camera actually rendered. Nothing is scripted: a person in a
            detection is a person the scenario put in the room.

Everything else — the WebSocket server, the session handler, device MCP, capability
discovery, telemetry ingestion, the world model, the safety policy, the action executor
and its watchdog thread, the behaviour scheduler, the speech arbiter — is the code that
ships.
"""
