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
import hashlib
import hmac
import io
import json
import logging
import os
import re
import sys
import syslog
import uuid

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from khard.address_book import AnyQuery, VdirAddressBook
from khard.query import NameQuery
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
import vobject

import discord
from discord import app_commands

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

# Second listener for Twilio call status callbacks (StatusCallback URL).
TWILIO_STATUS_PORT = int(os.environ.get("TWILIO_STATUS_PORT", "2325"))
TWILIO_STATUS_PUBLIC_URL = os.environ.get(
    "TWILIO_STATUS_PUBLIC_URL", "https://status.twilio.happytailspawcare.com"
).strip()

# Third listener for Vapi server/status webhooks, authenticated with a Vapi
# "HMAC" credential (Dashboard -> Credentials). Defaults match Vapi's defaults.
VAPI_HMAC_PSK = os.environ.get("VAPI_HMAC_PSK", "").strip()
VAPI_STATUS_PORT = int(os.environ.get("VAPI_STATUS_PORT", "2326"))
VAPI_HMAC_SIGNATURE_HEADER = os.environ.get("VAPI_HMAC_SIGNATURE_HEADER", "x-signature")
VAPI_HMAC_TIMESTAMP_HEADER = os.environ.get("VAPI_HMAC_TIMESTAMP_HEADER", "x-timestamp")

DISCORD_MAX_LEN = 2000
SMS_MAX_LEN = 1600  # Twilio hard limit per message

KHARD_PATH = os.path.expanduser(
    os.environ.get("KHARD_CONTACTS_PATH", "~/.contacts/card/")
)

# to:<phone>  — strict phone match (stops at non-phone char).
TO_PHONE_RE = re.compile(r"(?i)\bto:\s*(\+?[\d().\-\s]{6,}\d)")
# to:<name>  multi-word: name delimited by two-or-more spaces ("to:John Smith  hi").
TO_NAME_MULTI_RE = re.compile(r"(?i)\bto:\s*(.+?)\s{2,}")
# to:<name>  single word: stops at first whitespace or end-of-string.
TO_NAME_WORD_RE = re.compile(r"(?i)\bto:\s*(\S+)")
# E.164 number as emitted by this bot in inbound posts, for reply parsing.
E164_RE = re.compile(r"\+\d{7,15}")
# Looks like a phone number (has mostly digits once stripped).
PHONE_RE = re.compile(r"^[+\d().\-\s]{6,}$")
# Call notification relayed by the twilio-func-forward-notify.js Function:
#   "Incoming call from +11234567890 (CITY, STATE)"  — location optional.
INCOMING_CALL_RE = re.compile(
    r"^Incoming call from\s+(\S+)(?:\s*\(([^)]*)\))?\s*$", re.IGNORECASE
)

# Patterns to guess a sender's name from SMS body text.
_NAME_HINTS: list[re.Pattern] = [
    re.compile(r"(?i)(?:my name is|this is|it'?s|i'?m|i am)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)"),
    re.compile(r"(?i)^([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)\s+here\b"),
    re.compile(r"(?i)^Hi[,!]?\s+(?:I'?m\s+)?([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)"),
]

CONTACT_CATEGORIES = ["Happy Tails Paw Care", "Added with discord-sms"]

twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN)


# ---------------------------------------------------------------------------
# Contacts (khard)
# ---------------------------------------------------------------------------
class Contacts:
    """Thin synchronous wrapper around a khard VdirAddressBook."""

    def __init__(self, path: str) -> None:
        self._ab: VdirAddressBook | None = None
        self._path = path
        self._sig: frozenset | None = None

    def _signature(self) -> frozenset:
        """(name, mtime, size) of every .vcf, so external edits (khard, vdirsyncer) are noticed."""
        try:
            with os.scandir(os.path.expanduser(self._path)) as it:
                return frozenset(
                    (e.name, st.st_mtime_ns, st.st_size)
                    for e in it
                    if e.name.endswith(".vcf") and (st := e.stat())
                )
        except OSError:
            return frozenset()

    def _book(self) -> VdirAddressBook:
        sig = self._signature()
        if self._ab is None or sig != self._sig:
            ab = VdirAddressBook("contacts", self._path, skip=True)
            ab.load(AnyQuery())
            self._ab, self._sig = ab, sig
        return self._ab

    def reload(self) -> None:
        self._ab = None

    def lookup(self, number: str) -> tuple[str, list[str]] | None:
        """Return (formatted_name, [emails]) for a phone number, or None."""
        normed = normalize_phone(number)
        if not normed:
            return None
        for contact in self._book().contacts.values():
            for nums in contact.phone_numbers.values():
                for n in nums:
                    if normalize_phone(n) == normed:
                        emails = [e for es in contact.emails.values() for e in es]
                        return contact.formatted_name, list(dict.fromkeys(emails))
        return None

    def lookup_name(self, number: str) -> str | None:
        """Return the contact's formatted name for a phone number, or None."""
        found = self.lookup(number)
        return found[0] if found else None

    def lookup_number(self, name: str) -> list[tuple[str, str]]:
        """Return [(name, e164), ...] for all contacts matching *name*."""
        results = []
        for contact in self._book().search(NameQuery(name)):
            for nums in contact.phone_numbers.values():
                for n in nums:
                    normed = normalize_phone(n)
                    if normed:
                        results.append((contact.formatted_name, normed))
        return results


contacts = Contacts(KHARD_PATH)


# ---------------------------------------------------------------------------
# Session-seen unknown numbers
# ---------------------------------------------------------------------------
class SeenNumbers:
    """Track phone numbers seen this session that are not in khard, most-recent first."""

    def __init__(self) -> None:
        self._order: list[str] = []       # insertion order, most-recent first
        self._guesses: dict[str, str | None] = {}  # number -> guessed name

    def add(self, number: str, guessed_name: str | None = None) -> None:
        if number in self._guesses:
            if guessed_name and not self._guesses[number]:
                self._guesses[number] = guessed_name
            return
        self._guesses[number] = guessed_name
        self._order.insert(0, number)

    def remove(self, number: str) -> None:
        self._order = [n for n in self._order if n != number]
        self._guesses.pop(number, None)

    def all(self) -> list[tuple[str, str | None]]:
        """Return [(number, guessed_name), ...] most-recent first."""
        return [(n, self._guesses[n]) for n in self._order]

    def guess(self, number: str) -> str | None:
        return self._guesses.get(number)


seen_numbers = SeenNumbers()


# ---------------------------------------------------------------------------
# Contact creation
# ---------------------------------------------------------------------------
def extract_name_from_text(text: str) -> str | None:
    for pat in _NAME_HINTS:
        m = pat.search(text)
        if m:
            return m.group(1).strip()
    return None


def create_contact_vcf(name: str, number: str) -> str:
    """Write a new vCard to KHARD_PATH. Returns the created filename."""
    uid = str(uuid.uuid4())
    card = vobject.vCard()
    card.add("uid").value = uid
    card.add("fn").value = name
    n = card.add("n")
    parts = name.rsplit(None, 1)
    n.value = vobject.vcard.Name(
        family=parts[1] if len(parts) == 2 else "",
        given=parts[0],
    )
    tel = card.add("tel")
    tel.value = number
    tel.type_param = ["CELL", "VOICE", "pref"]
    cats = card.add("categories")
    cats.value = CONTACT_CATEGORIES
    card.add("note").value = "Happy Tails Paw Care Discord Bot"

    filename = f"{uid}.vcf"
    with open(os.path.join(KHARD_PATH, filename), "w") as fh:
        fh.write(card.serialize())
    return filename


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
    """Pull a `to:<target>` token out of text; return (raw_target, remaining body).

    Resolution order:
      1. Phone number  — `to:917-555-1234 body`
      2. Multi-word name delimited by two spaces — `to:John Smith  body`
      3. Single-word name — `to:Chrissa body`
    """
    for pattern in (TO_PHONE_RE, TO_NAME_MULTI_RE, TO_NAME_WORD_RE):
        m = pattern.search(text)
        if m:
            break
    else:
        return None, text
    target = m.group(1).strip()
    before, after = text[: m.start()].rstrip(), text[m.end():].lstrip()
    body = (before + (" " if before and after else "") + after).strip()
    return target, body


def resolve_to(target: str) -> tuple[str | None, str | None]:
    """Resolve a to: target to (e164, display_name).

    If *target* looks like a phone number it is normalised directly.
    Otherwise khard is searched by name; a unique match is returned,
    multiple matches return (None, None) so the caller can ask the user
    to be more specific.
    """
    if PHONE_RE.match(target):
        number = normalize_phone(target)
        name = contacts.lookup_name(number) if number else None
        return number, name

    # Name lookup
    matches = contacts.lookup_number(target)
    if len(matches) == 1:
        return matches[0][1], matches[0][0]
    if len(matches) > 1:
        log.info("Ambiguous to: %r — %d matches", target, len(matches))
        return None, None  # caller will react with ❓ and log
    return None, None


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
        self.status_runner: web.AppRunner | None = None
        self.vapi_runner: web.AppRunner | None = None
        self.tree = app_commands.CommandTree(self)
        # Temporarily hosted media: token -> (bytes, content_type)
        self._media_cache: dict[str, tuple[bytes, str]] = {}
        self._register_commands()

    def _register_commands(self) -> None:
        @self.tree.command(name="sms", description="Send an SMS to a contact or number")
        @app_commands.describe(
            to="Contact name or phone number",
            message="Message body",
            attachment="Optional file or image to send as MMS",
        )
        async def sms_command(
            interaction: discord.Interaction,
            to: str,
            message: str = "",
            attachment: discord.Attachment | None = None,
        ) -> None:
            if not self._is_admin_interaction(interaction):
                await interaction.response.send_message("Not authorized.", ephemeral=True)
                return
            if not message and not attachment:
                await interaction.response.send_message(
                    "Provide a message body, an attachment, or both.", ephemeral=True
                )
                return
            to_number, contact_name = await asyncio.to_thread(resolve_to, to)
            if not to_number:
                await interaction.response.send_message(
                    f"Could not resolve **{to}** to a phone number.", ephemeral=True
                )
                return
            label = f"{contact_name} ({to_number})" if contact_name else to_number
            media_urls = await self._stage_discord_attachments([attachment]) if attachment else []
            try:
                await self._send_sms(to_number, message, media_urls or None)
                envelope = f"\U0001f4e4 **SMS to {label}**"
                if message:
                    envelope += f"\n{message}"
                if attachment:
                    envelope += f"\n📎 {attachment.filename}"
                await interaction.response.send_message(envelope)
            except Exception:
                log.exception("Twilio send failed via /sms to %s", label)
                await interaction.response.send_message(f"❌ Failed to send to {label}", ephemeral=True)

        @sms_command.autocomplete("to")
        async def sms_to_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> list[app_commands.Choice[str]]:
            if not current:
                return []
            try:
                matches = await asyncio.to_thread(contacts.lookup_number, current)
            except Exception:
                return []
            return [
                app_commands.Choice(name=f"{name} ({number})", value=number)
                for name, number in matches[:25]
            ]

        # /add /new /create — identical commands registered via decorator pattern.
        for _cmd_name in ("add", "new", "create"):

            @self.tree.command(name=_cmd_name, description="Add an unknown number as a new khard contact")
            @app_commands.describe(number="Phone number (E.164)", name="Contact name")
            async def _add_cmd(
                interaction: discord.Interaction,
                number: str,
                name: str,
            ) -> None:
                await self._add_contact_command(interaction, number, name)

            @_add_cmd.autocomplete("number")
            async def _add_number_ac(
                interaction: discord.Interaction,
                current: str,
            ) -> list[app_commands.Choice[str]]:
                choices = []
                for num, guess in seen_numbers.all():
                    label = f"{guess} ({num})" if guess else num
                    if not current or current.lower() in label.lower():
                        choices.append(app_commands.Choice(name=label, value=num))
                return choices[:25]

            @_add_cmd.autocomplete("name")
            async def _add_name_ac(
                interaction: discord.Interaction,
                current: str,
            ) -> list[app_commands.Choice[str]]:
                number_val = interaction.namespace.number or ""
                guess = seen_numbers.guess(number_val)
                if guess and (not current or current.lower() in guess.lower()):
                    return [app_commands.Choice(name=guess, value=guess)]
                return []

    async def setup_hook(self) -> None:
        # Start the Twilio webhook receiver inside the bot's event loop.
        app = web.Application()
        app.router.add_post("/sms", self.handle_twilio)
        app.router.add_post("/", self.handle_twilio)
        app.router.add_get("/health", lambda r: web.Response(text="ok"))
        app.router.add_get("/media/{token}", self.serve_media)
        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()
        site = web.TCPSite(self.web_runner, "127.0.0.1", DISCORD_WEBHOOK_PORT)
        await site.start()
        log.info("Twilio webhook listening on 127.0.0.1:%d", DISCORD_WEBHOOK_PORT)

        # Separate receiver for Twilio call status callbacks.
        status_app = web.Application()
        status_app.router.add_get("/health", lambda r: web.Response(text="ok"))
        status_app.router.add_post("/{tail:.*}", self.handle_call_status)
        self.status_runner = web.AppRunner(status_app)
        await self.status_runner.setup()
        status_site = web.TCPSite(self.status_runner, "127.0.0.1", TWILIO_STATUS_PORT)
        await status_site.start()
        log.info("Twilio call-status webhook listening on 127.0.0.1:%d", TWILIO_STATUS_PORT)

        # Separate receiver for Vapi status/server messages.
        vapi_app = web.Application()
        vapi_app.router.add_get("/health", lambda r: web.Response(text="ok"))
        vapi_app.router.add_post("/{tail:.*}", self.handle_vapi_status)
        self.vapi_runner = web.AppRunner(vapi_app)
        await self.vapi_runner.setup()
        vapi_site = web.TCPSite(self.vapi_runner, "127.0.0.1", VAPI_STATUS_PORT)
        await vapi_site.start()
        log.info("Vapi status webhook listening on 127.0.0.1:%d", VAPI_STATUS_PORT)
        if not VAPI_HMAC_PSK:
            log.warning("VAPI_HMAC_PSK unset: Vapi webhooks will be rejected")
        await self.tree.sync()
        log.info("Slash commands synced")

    async def on_ready(self) -> None:
        self.channel = self.get_channel(DISCORD_CHANNEL_ID) or await self.fetch_channel(
            DISCORD_CHANNEL_ID
        )
        log.info("Logged in as %s; bridging channel #%s", self.user, self.channel)
        await self.channel.send(f"\U0001f4f2 **{self.user}** online — SMS bridge ready.")

    async def close(self) -> None:
        if self.web_runner:
            await self.web_runner.cleanup()
        if self.status_runner:
            await self.status_runner.cleanup()
        if self.vapi_runner:
            await self.vapi_runner.cleanup()
        await super().close()

    # ---- Inbound: Twilio -> Discord ------------------------------------
    @staticmethod
    def _check_signature(
        request: web.Request, form, base_url: str
    ) -> web.Response | None:
        """Verify X-Twilio-Signature. Returns an error response, or None if OK."""
        # Reconstruct the public URL Twilio signed: prefer explicit config,
        # fall back to X-Forwarded-Proto + Host headers set by nginx.
        if base_url:
            public_url = base_url.rstrip("/") + request.path_qs
        else:
            proto = request.headers.get("X-Forwarded-Proto", "https")
            host = request.headers.get("Host", "")
            public_url = f"{proto}://{host}{request.path_qs}" if host else ""

        if not public_url:
            log.warning("Cannot validate signature: no public URL determinable")
            return web.Response(status=400, text="misconfigured webhook")

        signature = request.headers.get("X-Twilio-Signature", "")
        if not twilio_validator.validate(public_url, dict(form), signature):
            log.warning("Rejected Twilio request with bad signature for %s", public_url)
            return web.Response(status=403, text="invalid signature")
        return None

    # ---- Twilio call status callbacks ------------------------------------
    async def handle_call_status(self, request: web.Request) -> web.Response:
        form = await request.post()
        if (err := self._check_signature(request, form, TWILIO_STATUS_PUBLIC_URL)):
            return err

        data = {k: form.getone(k) for k in form.keys()}
        # Console: pretty-printed like `| jq`. Syslog: one compact line.
        print(json.dumps(data, indent=2, sort_keys=True), file=sys.stdout, flush=True)
        syslog.syslog(syslog.LOG_INFO, json.dumps(data, sort_keys=True, separators=(",", ":")))
        return web.Response(status=204)

    # ---- Vapi status / server messages -----------------------------------
    @staticmethod
    def _check_vapi_hmac(request: web.Request, body: bytes) -> web.Response | None:
        """Verify Vapi HMAC credential signature (sha256, hex, '{timestamp}.{body}').

        Returns an error response, or None if OK.
        """
        if not VAPI_HMAC_PSK:
            log.warning("Rejected Vapi request: VAPI_HMAC_PSK not configured")
            return web.Response(status=503, text="webhook not configured")

        signature = request.headers.get(VAPI_HMAC_SIGNATURE_HEADER, "").strip()
        if "=" in signature[:8]:  # tolerate a "sha256=" style prefix
            signature = signature.split("=", 1)[1]
        timestamp = request.headers.get(VAPI_HMAC_TIMESTAMP_HEADER, "")
        payload = f"{timestamp}.".encode() + body if timestamp else body
        expected = hmac.new(VAPI_HMAC_PSK.encode(), payload, hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(expected, signature.lower()):
            log.warning("Rejected Vapi request with bad signature for %s", request.path_qs)
            return web.Response(status=403, text="invalid signature")
        return None

    async def handle_vapi_status(self, request: web.Request) -> web.Response:
        body = await request.read()
        if (err := self._check_vapi_hmac(request, body)):
            return err

        try:
            data = json.loads(body)
        except ValueError:
            data = {"raw": body.decode("utf-8", "replace")}
        # Console: pretty-printed like `| jq`. Syslog: one compact line.
        print(json.dumps(data, indent=2, sort_keys=True), file=sys.stdout, flush=True)
        syslog.openlog("discord-sms-vapistatus", syslog.LOG_PID, syslog.LOG_USER)
        syslog.syslog(syslog.LOG_INFO, json.dumps(data, sort_keys=True, separators=(",", ":")))
        syslog.openlog("discord-sms-callstatus", syslog.LOG_PID, syslog.LOG_USER)
        return web.json_response({})

    async def handle_twilio(self, request: web.Request) -> web.Response:
        form = await request.post()
        if (err := self._check_signature(request, form, TWILIO_PUBLIC_URL)):
            return err

        sender = form.get("From", "")
        body = (form.get("Body") or "").strip()
        num_media = int(form.get("NumMedia", "0") or "0")

        call = INCOMING_CALL_RE.match(body)
        if call:
            await self._post_incoming_call(call.group(1), call.group(2))
            return self._twiml()

        display = await asyncio.to_thread(contacts.lookup_name, sender) if sender else None
        if sender and not display:
            guessed = extract_name_from_text(body)
            seen_numbers.add(sender, guessed)
            unknown_hint = "  *(unknown — /add to save)*"
        else:
            unknown_hint = ""
        sender_label = f"{display} ({sender})" if display else sender
        header = (
            f"\U0001f4e9 **SMS from {sender_label}**{unknown_hint}"
            if sender_label
            else "\U0001f4e9 **SMS**"
        )
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

    async def _post_incoming_call(self, raw_caller: str, location: str | None) -> None:
        """Post a call notification, enriched with khard name/email when known."""
        caller = normalize_phone(raw_caller) or raw_caller
        found = await asyncio.to_thread(contacts.lookup, caller) if caller else None
        location = (location or "").strip()

        if found:
            name, emails = found
            header = f"\U0001f4de **Incoming call from {name} ({caller})**"
        else:
            name, emails = None, []
            if PHONE_RE.match(caller):
                seen_numbers.add(caller)
                header = f"\U0001f4de **Incoming call from {caller}**  *(unknown — /add to save)*"
            else:  # e.g. "Anonymous" / "Unknown"
                header = f"\U0001f4de **Incoming call from {caller}**"

        lines = [header]
        if location:
            lines.append(f"\U0001f4cd {location}")
        if emails:
            lines.append("✉️ " + ", ".join(emails))
        log.info("Incoming call from %s%s", f"{name} " if name else "", caller)

        if self.channel is None:
            log.error("Channel not ready; dropping call notification from %s", caller)
            return
        try:
            await self.channel.send("\n".join(lines))
        except Exception:
            log.exception("Failed to post call notification to Discord")

    async def serve_media(self, request: web.Request) -> web.Response:
        """One-shot endpoint: serve a staged Discord attachment to Twilio then evict it."""
        token = request.match_info["token"]
        entry = self._media_cache.pop(token, None)
        if entry is None:
            return web.Response(status=404)
        data, ctype = entry
        return web.Response(body=data, content_type=ctype)

    async def _stage_discord_attachments(
        self, attachments: list[discord.Attachment]
    ) -> list[str]:
        """Download Discord attachments and stage them on our own server.

        Returns a list of public URLs Twilio can fetch (via nginx).
        Discord CDN URLs use signed expiring params; serving through our own
        endpoint avoids any access issues.
        """
        if not TWILIO_PUBLIC_URL:
            # Fall back to raw Discord URLs if we have no public base URL.
            return [a.url for a in attachments]

        base = TWILIO_PUBLIC_URL.rstrip("/")
        urls: list[str] = []
        async with aiohttp.ClientSession() as session:
            for attachment in attachments:
                try:
                    async with session.get(attachment.url) as resp:
                        resp.raise_for_status()
                        data = await resp.read()
                        ctype = resp.content_type or "application/octet-stream"
                    token = uuid.uuid4().hex
                    self._media_cache[token] = (data, ctype)
                    urls.append(f"{base}/media/{token}")
                except Exception:
                    log.exception("Failed to stage attachment %s", attachment.url)
        return urls

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
        if message.channel.id != DISCORD_CHANNEL_ID:
            return

        attachments = f" [{len(message.attachments)} attachment(s)]" if message.attachments else ""
        log.info("[#channel] %s: %s%s", message.author, message.content or "<no text>", attachments)

        if message.author.bot or message.author.id == self.user.id:
            return
        if not self._is_admin(message):
            return

        content = (message.content or "").strip()
        to_target, body = extract_to_and_body(content)

        to_number: str | None = None
        contact_name: str | None = None

        if to_target is not None:
            if not PHONE_RE.match(to_target):
                # Name lookup — check for ambiguity before resolve_to discards the list.
                matches = await asyncio.to_thread(contacts.lookup_number, to_target)
                if len(matches) > 1:
                    await self._post_contact_choices(message, to_target, matches)
                    return

            to_number, contact_name = await asyncio.to_thread(resolve_to, to_target)
        elif message.reference is not None:
            # No explicit to: — resolve recipient from the replied-to message.
            try:
                ref = message.reference.resolved or await message.channel.fetch_message(
                    message.reference.message_id
                )
                to_number = phone_from_reference(ref.content)
                if to_number:
                    contact_name = await asyncio.to_thread(contacts.lookup_name, to_number)
                body = content
            except Exception:
                log.exception("Could not resolve replied-to message")

        media_urls = (
            await self._stage_discord_attachments(message.attachments)
            if message.attachments
            else []
        )

        if not to_number:
            await message.add_reaction("❓")
            return
        if not body and not media_urls:
            await message.add_reaction("⚠️")
            return

        label = f"{contact_name} ({to_number})" if contact_name else to_number
        log.info("Sending SMS to %s%s", label, f" + {len(media_urls)} media" if media_urls else "")

        try:
            await self._send_sms(to_number, body, media_urls)
            await message.add_reaction("✅")
        except Exception:
            log.exception("Twilio send failed to %s", label)
            await message.add_reaction("❌")

    async def _add_contact_command(
        self,
        interaction: discord.Interaction,
        number: str,
        name: str,
    ) -> None:
        if not self._is_admin_interaction(interaction):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return
        normed = normalize_phone(number)
        if not normed:
            await interaction.response.send_message(
                f"Could not parse **{number}** as a phone number.", ephemeral=True
            )
            return
        existing = await asyncio.to_thread(contacts.lookup_name, normed)
        if existing:
            await interaction.response.send_message(
                f"**{normed}** is already saved as **{existing}**.", ephemeral=True
            )
            return
        try:
            filename = await asyncio.to_thread(create_contact_vcf, name, normed)
            contacts.reload()
            seen_numbers.remove(normed)
            log.info("Created contact %s (%s) → %s", name, normed, filename)
            await interaction.response.send_message(
                f"✅ Saved **{name}** ({normed}) to contacts."
            )
        except Exception:
            log.exception("Failed to create contact %s %s", name, normed)
            await interaction.response.send_message(
                f"❌ Failed to save contact.", ephemeral=True
            )

    async def _post_contact_choices(
        self,
        trigger: discord.Message,
        query: str,
        matches: list[tuple[str, str]],
    ) -> None:
        """Post one line per ambiguous match; user replies to one to send SMS."""
        await trigger.reply(
            f"Multiple contacts match **{query}** — reply to one to send:",
            mention_author=False,
        )
        for name, number in matches:
            await self.channel.send(f"\U0001f4cb **{name}** {number}")

    def _is_admin_interaction(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        return any(
            role.name == DISCORD_ADMIN_ROLE or str(role.id) == DISCORD_ADMIN_ROLE
            for role in member.roles
        )

    def _is_admin(self, message: discord.Message) -> bool:
        member = message.author
        if not isinstance(member, discord.Member):
            return False
        return any(
            role.name == DISCORD_ADMIN_ROLE or str(role.id) == DISCORD_ADMIN_ROLE
            for role in member.roles
        )

    async def _send_sms(
        self,
        to_number: str,
        body: str,
        media_urls: list[str] | None = None,
    ) -> None:
        parts = chunk(body, SMS_MAX_LEN) if body else [None]
        for i, part in enumerate(parts):
            urls = media_urls if i == 0 else None
            kwargs: dict = {"to": to_number, "from_": TWILIO_PHONE_NUMBER}
            if part:
                kwargs["body"] = part
            if urls:
                kwargs["media_url"] = urls
            await asyncio.to_thread(twilio_client.messages.create, **kwargs)
        log.info(
            "Sent to %s: %d chars%s",
            to_number,
            len(body),
            f" + {len(media_urls)} media" if media_urls else "",
        )


def main() -> None:
    syslog.openlog("discord-sms-callstatus", syslog.LOG_PID, syslog.LOG_USER)
    Bridge().run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
