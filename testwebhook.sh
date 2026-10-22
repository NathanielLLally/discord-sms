#!/bin/sj
curl -X POST "https://discord.crm.happytailspawcare.com/" \
  --data-urlencode "To=+19179911982" \
  --data-urlencode "From=+16468468087" \
  --data-urlencode "Body=Hello from Twilio" \
  -u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN"
