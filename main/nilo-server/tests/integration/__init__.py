"""End-to-end tests: a real server on a real socket, with the real simulator as its device.

Unlike ``tests/robot/``, which never opens a socket, everything in this package starts the
inherited WebSocket server in-process and drives it over ``ws://127.0.0.1``.
"""
