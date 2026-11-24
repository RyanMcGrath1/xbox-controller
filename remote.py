"""Xbox session built on xbox-smartglass-core."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from enum import Enum, Flag
from pathlib import Path
from typing import Any, Dict, Optional

from xbox.sg.console import Console
from xbox.sg.enum import (
    ConnectionState,
    GamePadButton,
    MediaControlCommand,
    MediaPlaybackStatus,
)
from xbox.sg.manager import InputManager, MediaManager, TextManager
from xbox.sg.protocol import ProtocolError

log = logging.getLogger("xbox.remote")

DATA_PATH = Path(__file__).resolve().parent / "data" / "consoles.json"

BUTTONS = {
    "a": GamePadButton.PadA,
    "b": GamePadButton.PadB,
    "x": GamePadButton.PadX,
    "y": GamePadButton.PadY,
    "up": GamePadButton.DPadUp,
    "down": GamePadButton.DPadDown,
    "left": GamePadButton.DPadLeft,
    "right": GamePadButton.DPadRight,
    "lb": GamePadButton.LeftShoulder,
    "rb": GamePadButton.RightShoulder,
    "view": GamePadButton.View,
    "menu": GamePadButton.Menu,
    "nexus": GamePadButton.Nexu,
    "l3": GamePadButton.LeftThumbStick,
    "r3": GamePadButton.RightThumbStick,
}

MEDIA = {
    "play": MediaControlCommand.Play,
    "pause": MediaControlCommand.Pause,
    "toggle": MediaControlCommand.PlayPauseToggle,
    "stop": MediaControlCommand.Stop,
    "next": MediaControlCommand.NextTrack,
    "previous": MediaControlCommand.PreviousTrack,
    "fastforward": MediaControlCommand.FastForward,
    "rewind": MediaControlCommand.Rewind,
    "seek": MediaControlCommand.Seek,
}

# Dashboard navigation is a level, not a click. Repeat the held state so the
# console does not treat a single packet as a tap that already ended.
# The console treats a gamepad packet as a level. A click's release often
# arrives before a delayed sender runs, so the Xbox only ever sees "nothing
# held". Keep every press asserted at least this long, matching the library.
MIN_PRESS_SECONDS = 0.1


class RemoteError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _quiet(manager_cls):
    original = manager_cls._on_message

    def safe(self, msg, channel):
        try:
            original(self, msg, channel)
        except Exception:
            log.debug("%s ignored a message", manager_cls.__name__, exc_info=True)

    manager_cls._on_message = safe


_quiet(InputManager)
_quiet(MediaManager)
_quiet(TextManager)


def _name(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.name
    return value


def _title_label(aum: str) -> str:
    if not aum:
        return "Xbox"
    name = aum.split("!")[0]
    name = name.replace("_8wekyb3d8bbwe", "")
    if name.startswith("Xbox."):
        name = name[5:]
    return name.replace(".", " ") or "Xbox"


def _focused_title(status) -> Optional[dict]:
    if not status:
        return None
    titles = getattr(status, "active_titles", None) or []
    chosen = None
    for title in titles:
        disposition = getattr(title, "disposition", None)
        focused = bool(getattr(disposition, "has_focus", False))
        if focused or chosen is None:
            location = getattr(disposition, "title_location", None)
            aum = getattr(title, "aum", "") or ""
            chosen = {
                "name": _title_label(aum),
                "aum": aum,
                "title_id": int(getattr(title, "title_id", 0) or 0),
                "focused": focused,
                "location": _name(location) if location is not None else None,
            }
            if focused:
                break
    return chosen


def _media_view(console: Optional[Console]) -> Optional[dict]:
    if console is None or "media" not in console.managers:
        return None
    state = console.media.media_state
    if not state:
        return None
    meta = {}
    for item in getattr(state, "metadata", None) or []:
        meta[str(item.name)] = str(item.value)
    title = (
        meta.get("Title")
        or meta.get("title")
        or meta.get("PrimaryText")
        or _title_label(getattr(state, "aum_id", "") or "")
    )
    subtitle = meta.get("Artist") or meta.get("Album") or meta.get("Subtitle") or ""
    position = int(getattr(state, "position", 0) or 0)
    end = int(getattr(state, "media_end", 0) or 0)
    playback = getattr(state, "playback_status", None)
    return {
        "title": title,
        "subtitle": subtitle,
        "title_id": int(getattr(state, "title_id", 0) or 0),
        "playback": _name(playback) if playback is not None else None,
        "playing": playback == MediaPlaybackStatus.Playing,
        "position": position,
        "duration": end,
        "metadata": meta,
    }


def _status_view(console: Optional[Console]) -> Optional[dict]:
    if console is None or not console.console_status:
        return None
    status = console.console_status
    major = int(getattr(status, "major_version", 0) or 0)
    minor = int(getattr(status, "minor_version", 0) or 0)
    build = int(getattr(status, "build_number", 0) or 0)
    return {
        "version": f"{major}.{minor}.{build}",
        "locale": getattr(status, "locale", "") or "",
        "title": _focused_title(status),
    }


def _pack_buttons(held: set) -> Any:
    mask = 0
    for button in held:
        mask |= int(button.value)
    for member in GamePadButton:
        if int(member.value) == mask:
            return member
    return mask


class Hub:
    def __init__(self):
        self.known: Dict[str, dict] = {}
        self.discovered: Dict[str, Console] = {}
        self.active: Optional[Console] = None
        self.selected: Optional[str] = None
        self.error: Optional[str] = None
        self.message: Optional[str] = None
        # Python 3.9 binds a lock to whatever loop exists at creation time.
        # The server loop does not exist yet while this object is imported.
        self._io = None
        self._scan = None
        self._held: set = set()
        self._pressed_at: Dict[GamePadButton, float] = {}
        self._triggers = {"lt": 0.0, "rt": 0.0}
        self._sticks = {"left": (0.0, 0.0), "right": (0.0, 0.0)}
        self._media_request = 0
        self.load()

    def _guard(self, name: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = getattr(self, name)
        if lock is None or lock._loop is not loop:
            lock = asyncio.Lock()
            setattr(self, name, lock)
        return lock

    def load(self) -> None:
        if not DATA_PATH.exists():
            return
        try:
            saved = json.loads(DATA_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            log.warning("Could not read %s", DATA_PATH)
            return
        self.known = {item["liveid"]: item for item in saved.get("consoles", []) if item.get("liveid")}
        self.selected = saved.get("selected") or self.selected

    def save(self) -> None:
        DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "selected": self.selected,
            "consoles": list(self.known.values()),
        }
        DATA_PATH.write_text(json.dumps(payload, indent=2))

    def remember(self, console: Console) -> None:
        previous = self.known.get(console.liveid, {})
        self.known[console.liveid] = {
            "liveid": console.liveid,
            "name": console.name or previous.get("name") or "Xbox",
            "address": console.address or previous.get("address"),
            "anonymous": bool(console.anonymous_connection_allowed),
        }
        if not self.selected:
            self.selected = console.liveid
        self.save()

    def remember_manual(self, liveid: str, name: str, address: str) -> None:
        liveid = liveid.strip().upper()
        if not liveid:
            raise RemoteError("Live ID is required.")
        previous = self.known.get(liveid, {})
        self.known[liveid] = {
            "liveid": liveid,
            "name": name.strip() or previous.get("name") or "Xbox",
            "address": address.strip() or previous.get("address"),
            "anonymous": previous.get("anonymous", True),
        }
        self.selected = liveid
        self.save()

    def snapshot(self) -> dict:
        discovered_ids = set(self.discovered)
        consoles = []
        for liveid, item in self.known.items():
            online = liveid in discovered_ids
            consoles.append({
                **item,
                "available": online,
                "selected": liveid == self.selected,
                "connected": bool(self.active and self.active.connected and self.active.liveid == liveid),
            })
        consoles.sort(key=lambda item: (not item["selected"], not item["available"], item["name"].lower()))
        active = self.active if self.active and self.active.connected else None
        text_prompt = None
        if active and "text" in active.managers and active.text.got_active_session:
            text_prompt = active.text.text_prompt or "Text field"
        return {
            "error": self.error,
            "message": self.message,
            "selected": self.selected,
            "connected": active is not None,
            "connection": _name(self.active.connection_state) if self.active else "Disconnected",
            "consoles": consoles,
            "console": None if not self.active else {
                "liveid": self.active.liveid,
                "name": self.active.name,
                "address": self.active.address,
                "anonymous": bool(self.active.anonymous_connection_allowed),
            },
            "status": _status_view(active),
            "media": _media_view(active),
            "text_prompt": text_prompt,
        }

    async def boot(self) -> None:
        try:
            await self.discover()
            target = self.selected if self.selected in self.discovered else None
            if target is None and len(self.discovered) == 1:
                target = next(iter(self.discovered))
            if target:
                await self.connect(target)
        except RemoteError as exc:
            self.error = str(exc)
            log.info("Startup did not connect: %s", exc)
        except Exception as exc:
            self.error = str(exc)
            log.exception("Startup discovery failed")

    async def _scan_once(self) -> None:
        try:
            found = await Console.discover(timeout=2, tries=2)
        except (OSError, ProtocolError) as exc:
            raise RemoteError(f"Discovery failed: {exc}", 503) from exc
        self.discovered = {}
        for console in found:
            self.discovered[console.liveid] = console
            self.remember(console)
            log.info("Found %s at %s (%s)", console.name, console.address, console.liveid)
        if not found:
            self.message = "No Xbox answered. If it is asleep, use Power."
        else:
            self.message = None
            self.error = None

    async def discover(self) -> dict:
        async with self._guard("_scan"):
            await self._scan_once()
        return self.snapshot()

    def _prepare(self, console: Console) -> None:
        if "input" not in console.managers:
            console.add_manager(InputManager)
        if "media" not in console.managers:
            console.add_manager(MediaManager)
        if "text" not in console.managers:
            console.add_manager(TextManager)

    async def connect(self, liveid: Optional[str] = None) -> dict:
        liveid = (liveid or self.selected or "").strip().upper()
        if not liveid:
            raise RemoteError("Choose an Xbox first.", 404)
        async with self._guard("_scan"):
            if liveid not in self.discovered:
                await self._scan_once()
            console = self.discovered.get(liveid)
        if console is None:
            known = self.known.get(liveid)
            if known:
                raise RemoteError(
                    f"{known.get('name', 'Xbox')} is not responding. Use Power to wake it, then connect.",
                    409,
                )
            raise RemoteError("No Xbox with that Live ID is on the network.", 404)
        async with self._guard("_io"):
            if self.active and self.active.connected and self.active.liveid == liveid:
                self.selected = liveid
                return self.snapshot()
            if self.active and self.active.connected:
                try:
                    await self.active.disconnect()
                except Exception:
                    log.debug("Disconnect before switch failed", exc_info=True)
            self.active = None
            if not console.anonymous_connection_allowed:
                raise RemoteError(
                    "This Xbox is refusing anonymous remotes. On the console open "
                    "Settings → Devices & connections → Remote features, turn remote "
                    "features on, and allow connections from any device.",
                    409,
                )
            self._prepare(console)
            try:
                state = await asyncio.wait_for(console.connect(), timeout=8)
            except asyncio.TimeoutError as exc:
                self.error = "Timed out connecting to the Xbox."
                raise RemoteError(self.error, 504) from exc
            except (ConnectionError, ProtocolError, OSError) as exc:
                self.error = str(exc)
                raise RemoteError(
                    "Could not connect. On the Xbox, open Settings → Devices & connections "
                    "→ Remote features and allow connections from any device. "
                    f"({exc})",
                    409,
                ) from exc
            if state != ConnectionState.Connected:
                raise RemoteError(f"Console stayed in { _name(state) }.", 409)
            self.active = console
            self.selected = liveid
            self.error = None
            self.message = f"Connected to {console.name}."
            self.save()
            await asyncio.sleep(0.35)
            return self.snapshot()

    def _target_record(self, liveid: Optional[str]) -> dict:
        liveid = (liveid or self.selected or "").strip().upper()
        if not liveid or liveid not in self.known:
            raise RemoteError(
                "Power needs a Live ID from a previous discovery, or one you enter under Consoles.",
                404,
            )
        return self.known[liveid]

    async def power_on(self, liveid: Optional[str] = None) -> dict:
        record = self._target_record(liveid)
        self.selected = record["liveid"]
        self.message = f"Waking {record['name']}…"
        self.save()
        try:
            await Console.power_on(record["liveid"], record.get("address") or None, tries=4)
        except (OSError, ProtocolError) as exc:
            raise RemoteError(f"Power on failed: {exc}", 503) from exc
        for _ in range(8):
            await asyncio.sleep(2)
            try:
                await self.discover()
            except RemoteError:
                continue
            if record["liveid"] in self.discovered:
                try:
                    return await self.connect(record["liveid"])
                except RemoteError as exc:
                    self.error = str(exc)
                    return self.snapshot()
        self.message = (
            "Wake packet sent. If the console stays off, set Power mode to Instant-on "
            "(Settings → General → Power options)."
        )
        return self.snapshot()

    async def power_off(self) -> dict:
        console = await self._require()
        async with self._guard("_io"):
            try:
                await console.power_off()
            except (OSError, ProtocolError) as exc:
                raise RemoteError(f"Power off failed: {exc}", 503) from exc
            self._held.clear()
            self._pressed_at.clear()
            self._triggers = {"lt": 0.0, "rt": 0.0}
            self._sticks = {"left": (0.0, 0.0), "right": (0.0, 0.0)}
            self.active = None
            self.discovered.pop(console.liveid, None)
            self.message = f"{console.name} is turning off."
            self.error = None
            return self.snapshot()

    async def _require(self) -> Console:
        if self.active and self.active.connected:
            return self.active
        if self.selected:
            await self.connect(self.selected)
            if self.active and self.active.connected:
                return self.active
        raise RemoteError("Not connected to an Xbox.", 409)

    def _require_live(self) -> Console:
        if self.active and self.active.connected:
            return self.active
        raise RemoteError("Not connected to an Xbox.", 409)

    async def set_button(self, name: str, down: bool) -> None:
        self._require_live()
        button = BUTTONS.get(name)
        if button is None:
            if name in ("lt", "rt"):
                self._triggers[name] = 1.0 if down else 0.0
                await self._flush_input()
                return
            raise RemoteError(f"Unknown button '{name}'.")
        if down:
            self._held.add(button)
            self._pressed_at[button] = time.monotonic()
            await self._flush_input()
            log.info("Sent %s down", name)
            return
        started = self._pressed_at.pop(button, None)
        if started is not None:
            remain = MIN_PRESS_SECONDS - (time.monotonic() - started)
            if remain > 0:
                await asyncio.sleep(remain)
        self._held.discard(button)
        await self._flush_input()

    async def set_stick(self, stick: str, x: float, y: float) -> None:
        self._require_live()
        if stick not in self._sticks:
            raise RemoteError("Stick must be left or right.")
        x = max(-1.0, min(1.0, float(x)))
        y = max(-1.0, min(1.0, float(y)))
        if abs(x) < 0.08:
            x = 0.0
        if abs(y) < 0.08:
            y = 0.0
        self._sticks[stick] = (x, y)
        await self._flush_input()

    async def _flush_input(self) -> None:
        console = self._require_live()
        left = self._sticks["left"]
        right = self._sticks["right"]
        try:
            async with self._guard("_io"):
                if not console.connected:
                    raise RemoteError("Not connected to an Xbox.", 409)
                await console.gamepad_input(
                    _pack_buttons(self._held),
                    l_trigger=float(self._triggers["lt"]),
                    r_trigger=float(self._triggers["rt"]),
                    l_thumb_x=float(left[0]),
                    l_thumb_y=float(left[1]),
                    r_thumb_x=float(right[0]),
                    r_thumb_y=float(right[1]),
                )
        except RemoteError:
            raise
        except Exception as exc:
            log.exception("Gamepad send failed")
            raise RemoteError(f"The Xbox did not take that input. ({exc})", 502) from exc

    async def media(self, command: str, position: Optional[int] = None) -> dict:
        name = command.strip().lower()
        if name not in MEDIA:
            raise RemoteError(f"Unknown media command '{command}'.")
        console = await self._require()
        async with self._guard("_io"):
            state = console.media.media_state
            title_id = int(getattr(state, "title_id", 0) or 0) if state else 0
            if not title_id:
                raise RemoteError("Nothing is playing that accepts media controls.", 409)
            self._media_request += 1
            await console.media_command(
                title_id,
                MEDIA[name],
                request_id=self._media_request,
                seek_position=position if name == "seek" else None,
            )
        self.message = None
        self.error = None
        await asyncio.sleep(0.2)
        return self.snapshot()

    async def record(self, seconds: int = 30) -> dict:
        seconds = max(5, min(int(seconds), 120))
        console = await self._require()
        async with self._guard("_io"):
            try:
                ack = await console.game_dvr_record(-seconds, 0)
            except (OSError, ProtocolError) as exc:
                raise RemoteError(
                    "Game DVR did not accept the clip. Captures sometimes require a "
                    f"signed-in Microsoft account session. ({exc})",
                    409,
                ) from exc
        self.message = f"Asked the Xbox to save the last {seconds}s ({_name(ack)})."
        self.error = None
        return self.snapshot()

    async def send_text(self, text: str) -> dict:
        if not text:
            raise RemoteError("Enter some text first.")
        console = await self._require()
        async with self._guard("_io"):
            if not console.text.got_active_session:
                raise RemoteError(
                    "Focus a text field on the Xbox so the on-screen keyboard appears, then send.",
                    409,
                )
            limit = console.text.max_text_length
            if limit and len(text) > int(limit):
                raise RemoteError(f"That field only accepts {limit} characters.")
            await console.send_systemtext_input(text)
            await console.finish_text_input()
        self.message = "Text sent."
        self.error = None
        return self.snapshot()

    async def launch(self, uri: str) -> dict:
        uri = uri.strip()
        if not uri:
            raise RemoteError("Enter a launch URI, such as a game protocol link.")
        console = await self._require()
        async with self._guard("_io"):
            try:
                ack = await console.launch_title(uri)
            except (OSError, ProtocolError) as exc:
                raise RemoteError(f"Launch failed: {exc}", 409) from exc
        self.message = f"Launch sent ({_name(ack)})."
        self.error = None
        return self.snapshot()
