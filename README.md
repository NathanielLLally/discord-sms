# discordSMS

A Discord ↔ Twilio SMS/MMS bridge.

- **Inbound** — Twilio posts incoming SMS/MMS to the bot's webhook; the bot
  relays each message (and any MMS media) into a Discord channel.
- **Outbound** — any message posted in that channel by a user holding the
  `DISCORD_ADMIN_ROLE` is sent out as an SMS via the Twilio REST API.

## Installation

### 1. Clone and create the venv

```bash
git clone https://github.com/NathanielLLally/discord-sms.git
cd discord-sms
python3 -m venv .venv
.venv/bin/pip install "discord.py>=2.4" twilio aiohttp python-dotenv vdirsyncer
```

### 2. Configure `.env`

Copy the template and fill in your values:

```bash
cp .env.example .env   # or edit .env directly
```

### 3. Discord bot setup

In the [Discord Developer Portal](https://discord.com/developers/applications):

- Enable the **Message Content** and **Server Members** privileged intents.
- Invite the bot to your server with `bot` scope and **Send Messages** /
  **Read Message History** / **Add Reactions** permissions.

### 4. SMS bridge — `happytailspawcare-sms` (system service)

The bot runs as a system service. On Rocky Linux / RHEL the `.venv` lives under
`$HOME` which is labeled `user_home_t` by SELinux — relabel the venv binaries
so the system service can execute them:

```bash
sudo semanage fcontext -a -t bin_t \
  '/home/nathaniel/src/discordSMS/.venv/bin(/.*)?'
sudo restorecon -Rv /home/nathaniel/src/discordSMS/.venv/bin/
```

Then install and start the service:

```bash
sudo cp happytailspawcare-sms.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now happytailspawcare-sms
```

### 5. vdirsyncer (user service, runs every minute)

Because the venv is in `$HOME`, run vdirsyncer as a **user** service to avoid
SELinux `user_home_t` execution denials:

```bash
mkdir -p ~/.config/systemd/user
cp vdirsyncer.service vdirsyncer.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vdirsyncer.timer
```

Enable lingering so the timer keeps running after logout:

```bash
sudo loginctl enable-linger nathaniel
```

### 6. nginx reverse proxy

Copy and adapt `nginx.conf.example`, then point the Twilio number's
"A message comes in" webhook at `https://<host>/sms`.

```bash
sudo cp nginx.conf.example /etc/nginx/conf.d/discord-sms.conf
# edit the file to set your domain / cert paths
sudo nginx -t && sudo systemctl reload nginx
```

### Running manually

```bash
./run.sh    # or: .venv/bin/python bot.py
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
