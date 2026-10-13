"""Job handlers. Each module here registers its handlers with ``unshelve.queue.registry``.

The worker imports every module of this package when it starts (``discover``), so a new
handler needs no other wiring.
"""
