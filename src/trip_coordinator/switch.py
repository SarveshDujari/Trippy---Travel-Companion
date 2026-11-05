"""Switch 0.40.1 documented SSE/HTTP agent protocol; no Telegram poller."""

import asyncio
import base64
import json
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import quote

import httpx


class ProtocolStopped(RuntimeError):
    """Operator review is required; never silently skip a delivery gap."""


@dataclass
class Frame:
    event: str
    data: dict


async def parse_sse(lines):
    event, data, size = "message", [], 0
    async for line in lines:
        if len(line) > 131072:
            raise ProtocolStopped("Oversized Switch frame")
        if not line:
            if data:
                try:
                    value = json.loads("\n".join(data))
                except json.JSONDecodeError as exc:
                    raise ProtocolStopped("Malformed Switch JSON frame") from exc
                if not isinstance(value, dict):
                    raise ProtocolStopped("Switch frame must be an object")
                yield Frame(event, value)
            event, data, size = "message", [], 0
        elif line.startswith(":"):
            continue
        else:
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "event":
                event = value
            elif field == "data":
                size += len(value)
                if size > 131072:
                    raise ProtocolStopped("Oversized Switch frame")
                data.append(value)


class SwitchClient:
    async def upload_media(self, room_id, payload, caption):
        from .media import MAX_BYTES, checked_image, validate_references

        files = []
        if payload.get("forward"):
            validate_references(payload["forward"])
            for index, reference in enumerate(payload["forward"]):
                async with self.http.stream(
                    "GET",
                    f"{self.path}/rooms/{quote(payload['source_room'], safe='')}/media",
                    params={"mxc": reference["mxc"]},
                ) as response:
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise ValueError("Image exceeds the download limit")
                files.append(
                    (
                        "files",
                        (f"travel-image-{index + 1}.jpg", checked_image(bytes(data)), "image/jpeg"),
                    )
                )
        else:
            for item in payload.get("files", []):
                content = base64.b64decode(item["base64"], validate=True)
                if len(content) > MAX_BYTES:
                    raise ValueError("Generated image exceeds the media limit")
                files.append(("files", (item["filename"], content, item["mimetype"])))
        if not 1 <= len(files) <= 2:
            raise ValueError("A media message must contain one or two images")
        response = await self.http.post(
            f"{self.path}/rooms/{quote(room_id, safe='')}/media",
            files=files,
            data={"caption": caption},
        )
        response.raise_for_status()

    async def upload_calendar(self, room_id, content, caption):
        response = await self.http.post(
            f"{self.path}/rooms/{quote(room_id, safe='')}/media",
            files={"file": ("hyderabad-demo.ics", content.encode("utf-8"), "text/calendar")},
            data={"caption": caption},
        )
        response.raise_for_status()

    def __init__(self, base_url, agent_id, key, *, transport=None):
        self.path = f"/agents/{quote(agent_id, safe='')}"
        self.http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=httpx.Timeout(15, read=45),
            transport=transport,
            follow_redirects=False,
        )

    async def close(self):
        await self.http.aclose()

    async def post(self, room_id, content):
        response = await self.http.post(
            self.path + "/message",
            json={
                "room_id": room_id,
                "content": content,
            },
        )
        response.raise_for_status()

    async def listen(self, connection_id, room_id, cursor, on_frame, on_state=None, room_ids=None):
        params = {
            "connection_id": connection_id,
            "scope": "all" if room_ids else "single",
            "filter": "all",
            "rooms": ",".join(room_ids) if room_ids else room_id,
            "start_from": "head" if cursor is None else str(cursor),
        }
        headers = {"Accept": "text/event-stream"}
        if cursor is not None:
            headers["Last-Event-ID"] = str(cursor)
        async with self.http.stream(
            "GET", self.path + "/events", params=params, headers=headers
        ) as response:
            response.raise_for_status()
            beat_task = None
            owner = asyncio.current_task()
            heartbeat_error = []

            def beat_finished(task):
                if not task.cancelled() and task.exception() is not None:
                    heartbeat_error.append(task.exception())
                    owner.cancel()

            current = {"cursor": cursor}
            try:
                async for frame in parse_sse(response.aiter_lines()):
                    if frame.event == "connection_state":
                        if beat_task is not None:
                            raise ProtocolStopped("Duplicate Switch connection state")
                        if not set(room_ids or [room_id]).issubset(frame.data.get("rooms", [])):
                            raise ProtocolStopped("Switch did not grant the configured room")
                        interval = float(frame.data["heartbeat_interval_seconds"])
                        if not 0.1 <= interval <= 60:
                            raise ProtocolStopped("Unsupported Switch heartbeat interval")
                        if current["cursor"] is None:
                            current["cursor"] = frame.data.get("cursor", 0)
                        beat_task = asyncio.create_task(
                            self._beat(connection_id, current, interval)
                        )
                        beat_task.add_done_callback(beat_finished)
                        if on_state is not None:
                            await on_state(frame.data)
                    elif frame.event in {"gap", "evicted", "room_released", "subscription_changed"}:
                        raise ProtocolStopped(
                            f"Switch control frame requires review: {frame.event}"
                        )
                    else:
                        if beat_task is None:
                            raise ProtocolStopped("Domain event arrived before connection state")
                        await on_frame(frame.data)
                        # Acknowledge only after the durable application transaction commits.
                        current["cursor"] = frame.data.get("sequence", current["cursor"])
                    if beat_task is not None and beat_task.done():
                        await beat_task
            except asyncio.CancelledError:
                if heartbeat_error:
                    raise heartbeat_error[0] from None
                raise
            finally:
                if beat_task is not None:
                    beat_task.cancel()
                    with suppress(asyncio.CancelledError, httpx.HTTPError):
                        await beat_task

    async def _beat(self, connection_id, current, interval):
        while True:
            await asyncio.sleep(interval)
            payload = {"connection_id": connection_id}
            if current["cursor"] is not None:
                payload["cursor"] = current["cursor"]
            response = await self.http.post(self.path + "/connection/beat", json=payload)
            response.raise_for_status()
