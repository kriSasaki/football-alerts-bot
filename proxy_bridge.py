"""
Lightweight HTTP proxy bridge for SofaScore API.
Run this on a machine where SofaScore is NOT blocked (e.g., home PC).

Usage:
    python proxy_bridge.py [port]

Then on VPS set in .env:
    SOFASCORE_PROXY_BRIDGE=http://YOUR_HOME_IP:9090
"""
import asyncio
import logging
import sys
from aiohttp import web, ClientSession, TCPConnector
import ssl

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9090
SOFASCORE_BASE = "https://www.sofascore.com/api/v1"

# Reuse session
_session = None

async def get_session():
    global _session
    if _session is None or _session.closed:
        try:
            import certifi
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except:
            ssl_ctx = None
        _session = ClientSession(connector=TCPConnector(ssl=ssl_ctx))
    return _session


async def proxy_handler(request: web.Request):
    """Forward /api/v1/* requests to SofaScore."""
    path = request.match_info.get("path", "")
    url = f"{SOFASCORE_BASE}/{path}"
    
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.sofascore.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    
    try:
        session = await get_session()
        async with session.get(url, headers=headers, params=dict(request.query)) as resp:
            body = await resp.read()
            logger.info("%s %s → %d (%d bytes)", request.method, path, resp.status, len(body))
            return web.Response(
                body=body,
                status=resp.status,
                content_type=resp.content_type,
            )
    except Exception as e:
        logger.error("Proxy error: %s", e)
        return web.json_response({"error": str(e)}, status=502)


async def health(request):
    return web.json_response({"status": "ok"})


async def on_shutdown(app):
    global _session
    if _session:
        await _session.close()


app = web.Application()
app.on_shutdown.append(on_shutdown)
app.router.add_get("/health", health)
app.router.add_get("/api/v1/{path:.*}", proxy_handler)

if __name__ == "__main__":
    print(f"🌐 SofaScore Proxy Bridge starting on port {PORT}")
    print(f"   Test: http://localhost:{PORT}/health")
    print(f"   API:  http://localhost:{PORT}/api/v1/sport/football/events/live")
    print(f"")
    print(f"   On VPS add to .env:")
    print(f"   SOFASCORE_BRIDGE_URL=http://YOUR_IP:{PORT}")
    web.run_app(app, port=PORT, print=None)
