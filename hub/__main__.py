"""Run the Hub with a transport-level 1 MiB WebSocket message limit."""

import os

import uvicorn

from .security import MAX_FRAME_BYTES


def main():
    uvicorn.run(
        "hub.app:create_app",
        factory=True,
        host=os.getenv("HUB_BIND_HOST", "127.0.0.1"),
        port=int(os.getenv("HUB_PORT", "8785")),
        ws_max_size=MAX_FRAME_BYTES,
        ws_max_queue=4,
        ws_per_message_deflate=False,
        proxy_headers=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()
