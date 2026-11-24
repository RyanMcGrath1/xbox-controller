"""Local Xbox remote. Open it on this computer or a phone on the same Wi-Fi."""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from remote import Hub, RemoteError

ROOT = Path(__file__).resolve().parent
hub = Hub()


@asynccontextmanager
async def lifespan(app: FastAPI):
    boot = asyncio.create_task(hub.boot())
    try:
        yield
    finally:
        boot.cancel()


app = FastAPI(title="Xbox Remote", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.exception_handler(RemoteError)
async def remote_error(_request, exc: RemoteError):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


class LiveBody(BaseModel):
    liveid: Optional[str] = None


class RememberBody(BaseModel):
    liveid: str
    name: str = "Xbox"
    address: str = ""


class InputBody(BaseModel):
    button: str
    action: str = "down"


class StickBody(BaseModel):
    stick: str
    x: float = 0
    y: float = 0


class MediaBody(BaseModel):
    command: str
    position: Optional[int] = None


class RecordBody(BaseModel):
    seconds: int = Field(default=30, ge=5, le=120)


class TextBody(BaseModel):
    text: str


class LaunchBody(BaseModel):
    uri: str


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/status")
async def status():
    return hub.snapshot()


@app.post("/api/discover")
async def discover():
    return await hub.discover()


@app.post("/api/connect")
async def connect(body: LiveBody):
    return await hub.connect(body.liveid)


@app.post("/api/remember")
async def remember(body: RememberBody):
    hub.remember_manual(body.liveid, body.name, body.address)
    hub.message = "Saved. Power can wake this Xbox when it is asleep."
    hub.error = None
    return hub.snapshot()


@app.post("/api/power/on")
async def power_on(body: LiveBody):
    return await hub.power_on(body.liveid)


@app.post("/api/power/off")
async def power_off():
    return await hub.power_off()


@app.post("/api/input")
async def gamepad(body: InputBody):
    action = body.action.lower()
    if action not in ("down", "up"):
        raise RemoteError("Action must be down or up.")
    await hub.set_button(body.button.lower(), action == "down")
    return {"ok": True}


@app.post("/api/stick")
async def stick(body: StickBody):
    await hub.set_stick(body.stick.lower(), body.x, body.y)
    return {"ok": True}


@app.post("/api/media")
async def media(body: MediaBody):
    return await hub.media(body.command, body.position)


@app.post("/api/record")
async def record(body: RecordBody):
    return await hub.record(body.seconds)


@app.post("/api/text")
async def text(body: TextBody):
    return await hub.send_text(body.text)


@app.post("/api/launch")
async def launch(body: LaunchBody):
    return await hub.launch(body.uri)


def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def main():
    parser = argparse.ArgumentParser(description="Xbox remote for a laptop or phone browser")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print()
    print("  Xbox remote")
    if args.host in ("0.0.0.0", "::"):
        print(f"  On this laptop   http://127.0.0.1:{args.port}")
        phone = lan_ip()
        if phone != "127.0.0.1":
            print(f"  On your phone    http://{phone}:{args.port}")
    else:
        print(f"  Open             http://{args.host}:{args.port}")
    print()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
