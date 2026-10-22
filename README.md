# discordSMS

A Discord ↔ Twilio SMS/MMS bridge.

- **Inbound** — Twilio posts incoming SMS/MMS to the bot's webhook; the bot
  relays each message (and any MMS media) into a Discord channel.
- **Outbound** — any message posted in that channel by a user holding the
  `DISCORD_ADMIN_ROLE` is sent out as an SMS via the Twilio REST API.

## Setup

```bash
# deps are installed into the project venv
.venv/bin/pip install "discord.py>=2.4" twilio aiohttp python-dotenv

# configure (see .env)
./run.sh                      # or: .venv/bin/python bot.py
```

### Required environment (`.env`)

| Variable               | Purpose                                                        |
|------------------------|----------------------------------------------------------------|
| `DISCORD_TOKEN`        | Bot token.                                                     |
| `DISCORD_CHANNEL_ID`   | Channel the bridge reads from / posts into.                   |
| `DISCORD_ADMIN_ROLE`   | Role **name** (or id) allowed to send outbound SMS.           |
| `DISCORD_WEBHOOK_PORT` | Local port for the webhook receiver (nginx proxies to it).    |
| `TWILIO_ACCOUNT_SID`   | Twilio account SID.                                           |
| `TWILIO_AUTH_TOKEN`    | Twilio auth token.                                           |
| `TWILIO_PHONE_NUMBER`  | The Twilio number outbound SMS is sent **from** (E.164).      |
| `TWILIO_PUBLIC_URL`    | *(optional)* public https URL; enables signature validation. |

The bot requires the **Message Content** and **Server Members** privileged
intents (enable them in the Discord Developer Portal).

## Choosing the outbound recipient

When an admin posts in the channel, the "To" number is resolved as follows:

1. **Explicit** — a case-insensitive `to:<number>` token anywhere in the
   message. The token is stripped; the rest of the message is the SMS body.
   `to:+19179911982 on my way` → SMS "on my way" to +19179911982.
2. **Reply** — if there is no `to:` token but the message is a *reply* to one
   of the bot's inbound SMS posts, the recipient is taken from that referenced
   message (envelope metadata only). The **new reply text** is the SMS body.

If neither resolves a number the bot reacts ❓; an empty body reacts ⚠️.
On success it reacts ✅, on a Twilio failure ❌.

## Webhook / nginx

The receiver listens on `127.0.0.1:$DISCORD_WEBHOOK_PORT` and accepts
`POST /sms` (and `POST /`). See `nginx.conf.example`. Point the Twilio number's
"A message comes in" webhook at `https://<host>/sms`.

Set `TWILIO_PUBLIC_URL` to that same public base URL to verify the
`X-Twilio-Signature` header on every inbound request.

## Running as a service

See `discord-sms.service` (systemd). Install with:

```bash
sudo cp discord-sms.service /etc/systemd/system/
sudo systemctl enable --now discord-sms
```
