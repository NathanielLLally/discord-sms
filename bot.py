#!/usr/bin/env python3
"""Discord <-> Twilio SMS/MMS bridge.

Inbound:  Twilio webhook -> posts the SMS/MMS into a Discord channel.
Outbound: messages in that channel from a user holding DISCORD_ADMIN_ROLE
          are sent as SMS via the Twilio REST API.

The outbound recipient ("To") is resolved from either:
  * a case-insensitive  to:<number>  token in the message text, or
  * the message the admin is replying to (the referenced Twilio post is
    treated as envelope metadata; only the new reply text is sent).
"""

import asyncio
import io
import logging
import os
import re

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient

import discord

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("discordSMS")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
DISCORD_ADMIN_ROLE = os.environ["DISCORD_ADMIN_ROLE"]
DISCORD_WEBHOOK_PORT = int(os.environ.get("DISCORD_WEBHOOK_PORT", "2324"))

TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]

# Optional: the public https URL nginx forwards to this service. When set,
# inbound Twilio requests are verified against the X-Twilio-Signature header.
TWILIO_PUBLIC_URL = os.environ.get("TWILIO_PUBLIC_URL", "").strip()

DISCORD_MAX_LEN = 2000
SMS_MAX_LEN = 1600  # Twilio hard limit per message

# to:<number>  (case-insensitive). Captures a loose phone string.
TO_RE = re.compile(r"(?i)\bto:\s*(\+?[\d().\-\s]{6,}\d)")
# E.164 number as emitted by this bot in inbound posts, for reply parsing.
E164_RE = re.compile(r"\+\d{7,15}")

twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def normalize_phone(raw: str | None) -> str | None:
    """Best-effort normalize a user-supplied number to E.164."""
    if not raw:
        return None
    raw = raw.strip()
    has_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if has_plus:
        return "+" + digits
    if len(digits) == 10:  # bare US number
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


def extract_to_and_body(text: str) -> tuple[str | None, str]:
    """Pull a `to:<number>` token out of text; return (number, remaining)."""
    m = TO_RE.search(text)
    if not m:
        return None, text
    number = normalize_phone(m.group(1))
    before, after = text[: m.start()].rstrip(), text[m.end():].lstrip()
    body = (before + (" " if before and after else "") + after).strip()
    return number, body


def phone_from_reference(content: str) -> str | None:
    """Extract the originating number from a bot-posted inbound SMS message."""
    m = E164_RE.search(content or "")
    return m.group(0) if m else None


def chunk(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


# ---------------------------------------------------------------------------
# Discord client
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class Bridge(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.channel: discord.TextChannel | None = None
        self.web_runner: web.AppRunner | None = None

    async def setup_hook(self) -> None:
        # Start the Twilio webhook receiver inside the bot's event loop.
        app = web.Application()
        app.router.add_post("/sms", self.handle_twilio)
        app.router.add_post("/", self.handle_twilio)
        app.router.add_get("/health", lambda r: web.Response(text="ok"))
        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()
        site = web.TCPSite(self.web_runner, "127.0.0.1", DISCORD_WEBHOOK_PORT)
        await site.start()
        log.info("Twilio webhook listening on 127.0.0.1:%d", DISCORD_WEBHOOK_PORT)

    async def on_ready(self) -> None:
        self.channel = self.get_channel(DISCORD_CHANNEL_ID) or await self.fetch_channel(
            DISCORD_CHANNEL_ID
        )
        log.info("Logged in as %s; bridging channel #%s", self.user, self.channel)
        await self.channel.send(f"\U0001f4f2 **{self.user}** online — SMS bridge ready.")

    async def close(self) -> None:
        if self.web_runner:
            await self.web_runner.cleanup()
        await super().close()

    # ---- Inbound: Twilio -> Discord ------------------------------------
    async def handle_twilio(self, request: web.Request) -> web.Response:
        form = await request.post()

        if TWILIO_PUBLIC_URL:
            url = TWILIO_PUBLIC_URL.rstrip("/") + request.path
            signature = request.headers.get("X-Twilio-Signature", "")
            if not twilio_validator.validate(url, dict(form), signature):
                log.warning("Rejected Twilio request with bad signature")
                return web.Response(status=403, text="invalid signature")

        sender = form.get("From", "unknown")
        body = (form.get("Body") or "").strip()
        num_media = int(form.get("NumMedia", "0") or "0")

        header = f"\U0001f4e9 **SMS from {sender}**"
        text = header + (f"\n{body}" if body else "")

        files: list[discord.File] = []
        if num_media:
            files = await self._download_media(form, num_media)

        if self.channel is None:
            log.error("Channel not ready; dropping inbound SMS from %s", sender)
            return self._twiml()

        try:
            parts = chunk(text, DISCORD_MAX_LEN)
            for i, part in enumerate(parts):
                # Attach files only on the final chunk.
                await self.channel.send(
                    content=part,
                    files=files if i == len(parts) - 1 else None,
                )
        except Exception:
            log.exception("Failed to post inbound SMS to Discord")

        return self._twiml()

    async def _download_media(self, form, num_media: int) -> list[discord.File]:
        """Fetch MMS media (auth-protected) and wrap as Discord attachments."""
        files: list[discord.File] = []
        auth = aiohttp.BasicAuth(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        async with aiohttp.ClientSession(auth=auth) as session:
            for i in range(num_media):
                url = form.get(f"MediaUrl{i}")
                ctype = form.get(f"MediaContentType{i}", "application/octet-stream")
                if not url:
                    continue
                try:
                    async with session.get(url) as resp:
                        resp.raise_for_status()
                        data = await resp.read()
                    ext = ctype.split("/")[-1].split(";")[0] or "bin"
                    files.append(
                        discord.File(fp=io.BytesIO(data), filename=f"media{i}.{ext}")
                    )
                except Exception:
                    log.exception("Failed to download media %s", url)
        return files

    @staticmethod
    def _twiml() -> web.Response:
        return web.Response(
            text='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            content_type="application/xml",
        )

    # ---- Outbound: Discord -> Twilio -----------------------------------
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.author.id == self.user.id:
            return
        if message.channel.id != DISCORD_CHANNEL_ID:
            return
        if not self._is_admin(message):
            return

        content = (message.content or "").strip()
        to_number, body = extract_to_and_body(content)

        # No explicit to: -> resolve recipient from the replied-to message.
        if to_number is None and message.reference is not None:
            try:
                ref = message.reference.resolved or await message.channel.fetch_message(
                    message.reference.message_id
                )
                to_number = phone_from_reference(ref.content)
                body = content  # reply text is the whole outgoing body
            except Exception:
                log.exception("Could not resolve replied-to message")

        if not to_number:
            await message.add_reaction("❓")  # ❓ cannot determine recipient
            return
        if not body:
            await message.add_reaction("⚠️")  # ⚠️ empty body
            return

        try:
            await self._send_sms(to_number, body)
            await message.add_reaction("✅")  # ✅
        except Exception:
            log.exception("Twilio send failed")
            await message.add_reaction("❌")  # ❌

    def _is_admin(self, message: discord.Message) -> bool:
        member = message.author
        if not isinstance(member, discord.Member):
            return False
        return any(
            role.name == DISCORD_ADMIN_ROLE or str(role.id) == DISCORD_ADMIN_ROLE
            for role in member.roles
        )

    async def _send_sms(self, to_number: str, body: str) -> None:
        for part in chunk(body, SMS_MAX_LEN):
            await asyncio.to_thread(
                twilio_client.messages.create,
                to=to_number,
                from_=TWILIO_PHONE_NUMBER,
                body=part,
            )
        log.info("Sent SMS to %s (%d chars)", to_number, len(body))


def main() -> None:
    Bridge().run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
