#!/bin/sj
curl -X POST "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_ACCOUNT_SID/Messages.json" \
  --data-urlencode "To=+19179911982" \
  --data-urlencode "From=+16468468087" \
  --data-urlencode "Body=Hello from Twilio" \
  -u "$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN"
