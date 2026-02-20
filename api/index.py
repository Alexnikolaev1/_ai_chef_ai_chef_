"""
Vercel Serverless Function — точка входа.
Vercel ищет переменную `app` (ASGI).
"""
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

try:
    from api.main import app
except ImportError:
    try:
        from main import app  # когда запуск из корня
    except ImportError as e:
        from fastapi import FastAPI
        app = FastAPI()
        @app.get("/")
        async def _err():
            return {"error": str(e)}
