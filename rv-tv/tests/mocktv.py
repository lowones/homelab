import asyncio, json, os, ssl, sys
from websockets.asyncio.server import serve

PORT = int(sys.argv[1])
KEY = "mockkey123"
state = {"volume": 12, "mute": False}

async def handler(ws):
    async for raw in ws:
        m = json.loads(raw)
        if m["type"] == "register":
            ck = m["payload"].get("client-key")
            if ck not in (None, KEY):
                await ws.send(json.dumps({"type": "error", "id": m["id"], "error": "bad key"}))
                continue
            await ws.send(json.dumps({"type": "response", "id": m["id"], "payload": {"pairingType": "PROMPT", "returnValue": True}}))
            await ws.send(json.dumps({"type": "registered", "id": m["id"], "payload": {"client-key": KEY}}))
        elif m["type"] == "request":
            uri, p = m["uri"], m.get("payload", {})
            print("CALL", uri, p, flush=True)
            def reply(payload):
                return json.dumps({"type": "response", "id": m["id"], "payload": payload})
            if uri == "ssap://audio/getVolume":
                await ws.send(reply({"returnValue": True, "volumeStatus": {"volume": state["volume"], "muteStatus": state["mute"]}}))
            elif uri == "ssap://audio/setVolume":
                state["volume"] = p["volume"]
                await ws.send(reply({"returnValue": True}))
            elif uri == "ssap://audio/setMute":
                state["mute"] = p["mute"]
                await ws.send(reply({"returnValue": True}))
            elif uri == "ssap://com.webos.applicationManager/getForegroundAppInfo":
                await ws.send(reply({"returnValue": True, "appId": "com.webos.app.hdmi1"}))
            elif uri == "ssap://tv/getExternalInputList":
                await ws.send(reply({"returnValue": True, "devices": [
                    {"id": "HDMI_1", "label": "Mac", "connected": True},
                    {"id": "HDMI_2", "label": "HDMI 2", "connected": False}]}))
            elif uri == "ssap://system/turnOff":
                await ws.close()
                return
            elif uri == "ssap://notifications/bad":
                await ws.send(json.dumps({"type": "error", "id": m["id"], "error": "401 insufficient permissions", "payload": {}}))
            else:
                await ws.send(reply({"returnValue": True}))

async def main():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    d = os.environ.get("MOCK_CERT_DIR", ".")
    ctx.load_cert_chain(os.path.join(d, "mock.crt"), os.path.join(d, "mock.key"))
    async with serve(handler, "127.0.0.1", PORT, ssl=ctx):
        await asyncio.Future()

asyncio.run(main())
