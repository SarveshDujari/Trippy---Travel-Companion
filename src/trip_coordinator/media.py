"""Bounded image ingestion and deterministic plan graphics; no model API."""

import base64
import io
import json
import re
import warnings
from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from . import storage as db

MAX_BYTES = 8 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 20_000_000


def validate_references(attachments):
    if not 1 <= len(attachments) <= 2:
        raise ValueError("Please send one or two travel images at a time")
    for item in attachments:
        if item.get("mimetype") not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError(
                "I can inspect PNG, JPEG and WebP images. Please send a screenshot of a PDF for now"
            )
        if not re.fullmatch(
            r"(?:mxc://[A-Za-z0-9._:-]+/[A-Za-z0-9._~-]+|switch-media://[A-Za-z0-9_-]{1,128})",
            item.get("mxc", ""),
        ):
            raise ValueError("The image must come through Switch's attachment store")
        size = item.get("size")
        if size is not None and (type(size) is not int or not 0 < size <= MAX_BYTES):
            raise ValueError("Please keep each image under 8 MB")


def checked_image(data):
    if not 0 < len(data) <= MAX_BYTES:
        raise ValueError("Image exceeds the 8 MB limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if (
                    image.format not in {"PNG", "JPEG", "WEBP"}
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise ValueError("Unsupported or animated image")
                image.load()
                output = io.BytesIO()
                image.convert("RGB").save(output, format="JPEG", quality=94)
                content = output.getvalue()
                if len(content) > MAX_BYTES:
                    raise ValueError("Decoded image is too large to forward")
                return content
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError(
            "This image could not be decoded safely; please send a smaller screenshot"
        ) from exc


def visual_requested(text):
    return bool(
        re.search(
            r"\b(visual|diagram|timeline|chart|map|picture)\b|show.{0,25}(plan|itinerary)",
            text,
            re.I,
        )
    )


def render_plan(c, comparison_id=None):
    """A schematic itinerary card, not an invented geographic map."""
    image = Image.new("RGB", (1200, 1000), "#101a2c")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.load_default(size=38)
    font = ImageFont.load_default(size=22)
    small = ImageFont.load_default(size=18)
    draw.text((45, 30), "TRIPPY / YOUR TRAVEL PLAN", font=title_font, fill="#eef5ff")
    comparison_query = select(db.comparisons)
    if comparison_id:
        comparison_query = comparison_query.where(db.comparisons.c.id == comparison_id)
    latest = (
        c.execute(comparison_query.order_by(db.comparisons.c.created_at.desc()).limit(1))
        .mappings()
        .first()
    )
    imported = [
        dict(x) for x in c.execute(select(db.imported).order_by(db.imported.c.start)).mappings()
    ]
    if latest:
        body = json.loads(latest["body"])
        draw.text(
            (45, 90),
            "CACHED DEMO / historical estimates / not live"
            if any(p.get("demo_mode") for p in body.get("proof", []))
            else "Mapped route estimates / schematic comparison",
            font=font,
            fill="#7dd3fc",
        )
        for index, plan in enumerate(body["plans"][:2]):
            x = 45 + index * 575
            draw.rounded_rectangle((x, 145, x + 535, 865), radius=18, fill="#1e2c44")
            draw.text((x + 20, 165), plan["name"][:35], font=font, fill="white")
            y = 220
            for stop in plan["stops"][:6]:
                draw.ellipse((x + 20, y + 4, x + 34, y + 18), fill="#38bdf8")
                if y < 220 + (min(6, len(plan["stops"])) - 1) * 55:
                    draw.line((x + 27, y + 20, x + 27, y + 53), fill="#475f80", width=3)
                draw.text((x + 48, y), stop["name"][:34], font=small, fill="#eef5ff")
                y += 55
            walking = plan["walking_m"]
            draw.text(
                (x + 20, 610),
                f"Mapped walking: {walking if walking is not None else 'unknown'} m",
                font=font,
                fill="#a7f3d0",
            )
            draw.text(
                (x + 20, 650),
                f"Route time: {plan['travel_minutes']} min",
                font=font,
                fill="#a7f3d0",
            )
            rank = plan["evaluation"]
            draw.text(
                (x + 20, 695),
                (
                    "Personal breakdown hidden"
                    if rank.get("privacy_redacted")
                    else "No personal preferences scored"
                    if not rank["scores"]
                    else "Needs checks"
                    if rank["blockers"]
                    else "Within known constraints"
                ),
                font=small,
                fill="#fde68a",
            )
            for offset, (person, score) in enumerate(list(rank["scores"].items())[:4]):
                draw.text(
                    (x + 20, 735 + offset * 27),
                    f"{person[:22]}: {score}/100",
                    font=small,
                    fill="#eef5ff",
                )
        footer = (
            "Venue walking, missing accessibility and opening status need confirmation. "
            "OSM / routing / Open-Meteo."
        )
    else:
        rows = imported or [
            dict(x, title=x["id"])
            for x in c.execute(select(db.items).order_by(db.items.c.start)).mappings()
        ]
        draw.text(
            (45, 95),
            "User-confirmed imported information"
            if imported
            else "SIMULATED itinerary / no real bookings",
            font=font,
            fill="#7dd3fc",
        )
        for index, item in enumerate(rows[:7]):
            y = 165 + index * 100
            draw.rounded_rectangle((45, y, 1155, y + 80), radius=14, fill="#1e2c44")
            titles = {
                "flight": "Flight arrival",
                "transfer": "Airport pickup",
                "hotel": "Hotel check-in",
                "dinner": "Dinner",
            }
            draw.text(
                (65, y + 12), titles.get(item["title"], item["title"])[:65], font=font, fill="white"
            )
            stamp = (
                datetime.fromisoformat(item["start"])
                .astimezone(ZoneInfo("Asia/Kolkata"))
                .strftime("%a, %d %b - %I:%M %p IST")
                if item.get("start")
                else "Time to be confirmed"
            )
            draw.text(
                (65, y + 45),
                stamp,
                font=small,
                fill="#a7f3d0",
            )
        footer = (
            "Imported details are user-confirmed information, not supplier verification."
            if imported
            else "Fictional demonstration itinerary. No actual tickets or reservations."
        )
    draw.text((45, 915), footer[:108], font=small, fill="#cbd5e1")
    draw.text(
        (45, 950),
        "Times and distances are estimates. Graphic generated locally by Trippy.",
        font=small,
        fill="#94a3b8",
    )
    output = io.BytesIO()
    image.save(output, format="PNG")
    return {
        "files": [
            {
                "filename": "trippy-plan.png",
                "mimetype": "image/png",
                "base64": base64.b64encode(output.getvalue()).decode(),
            }
        ]
    }
