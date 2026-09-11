"""ASGI entry for `uvicorn chat_search.asgi:app`; settings come from the environment."""
from .app import Settings, create_app

app = create_app(Settings())
