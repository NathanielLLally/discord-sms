// Twilio Function: /forward-and-notify  (visibility: Protected)
// Env vars (enter URLs without scheme; https:// and sip: are prepended if missing):
//   FORWARD_TO          number to ring, E.164                          e.g. +19175550123
//   CALLER_ID           caller ID on forwarded legs (default: caller)   e.g. +16465550123
//   SIP_URI             SIP endpoint to ring alongside FORWARD_TO       e.g. user@domain.sip.us1.twilio.com
//   WHISPER_URL         whisper TwiML for the FORWARD_TO leg            e.g. handler.twilio.com/twiml/EHxxxx
//   NOTIFY_WEBHOOK_URL  receives a Twilio outbound-SMS status callback  e.g. relay.example.com/twilio/sms
//   NOTIFY_TO           To on the callback (the number the SMS would go to)
//   NOTIFY_FROM         From on the callback (default: the dialed Twilio number)
// All optional except at least one of FORWARD_TO / SIP_URI.
// Keep "Add my Twilio Credentials" checked: AUTH_TOKEN signs the webhook (X-Twilio-Signature).

const twilio = require('twilio');
const crypto = require('crypto');

const KEYS = ['FORWARD_TO', 'CALLER_ID', 'SIP_URI', 'WHISPER_URL', 'NOTIFY_WEBHOOK_URL', 'NOTIFY_TO', 'NOTIFY_FROM'];

const withHttps = (v) => (v && !/^[a-z][a-z\d+.-]*:\/\//i.test(v) ? `https://${v}` : v);
const withSip = (v) => (v && !/^sips?:/i.test(v) ? `sip:${v}` : v);

async function sendWebhook(context, cfg, event, url, caller, loc) {
  // Fake MessageSid derived from the CallSid so it's traceable (not a real message; API lookups will 404)
  const sid = 'SM' + (event.CallSid ? event.CallSid.slice(2) : crypto.randomBytes(16).toString('hex'));

  // Same shape as Twilio's status callback for an outbound SMS (MessageStatus=sent)
  const params = Object.fromEntries(
    Object.entries({
      AccountSid: event.AccountSid || context.ACCOUNT_SID,
      ApiVersion: '2010-04-01',
      Body: `Incoming call from ${caller}${loc ? ` (${loc})` : ''}`, // extra: real status callbacks omit Body
      From: cfg.NOTIFY_FROM || event.To,
      MessageSid: sid,
      MessageStatus: 'sent',
      SmsSid: sid,
      SmsStatus: 'sent',
      To: cfg.NOTIFY_TO,
    }).filter(([, v]) => v != null)
  );

  const token = context.AUTH_TOKEN;
  const headers = { 'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'TwilioProxy/1.1' };
  if (token) headers['X-Twilio-Signature'] = twilio.getExpectedTwilioSignature(token, url, params);
  else console.warn('AUTH_TOKEN missing; sending unsigned (enable "Add my Twilio Credentials")');

  try {
    const res = await fetch(url, {
      method: 'POST',
      headers,
      body: new URLSearchParams(params),
      signal: AbortSignal.timeout(3000), // don't hold up the call on a slow receiver
    });
    if (!res.ok) {
      console.error('Webhook error:', res.status, await res.text());
      console.error('Signed URL:', url, '| token ends:', token ? token.slice(-4) : 'none');
    }
  } catch (err) {
    console.error('Webhook failed:', err.message); // never block the call
  }
}

exports.handler = async function (context, event, callback) {
  const twiml = new Twilio.twiml.VoiceResponse();

  // Dial action callback: log why the <Dial> ended, then hang up
  if (event.DialCallStatus) {
    const info = Object.fromEntries(Object.entries(event).filter(([k]) => /^Dial|Error/i.test(k)));
    console.log('Dial ended:', JSON.stringify(info));
    return callback(null, twiml);
  }

  // Trim env values (stray spaces/newlines from the console break numbers and URLs)
  const cfg = Object.fromEntries(KEYS.map((k) => [k, (context[k] || '').trim() || undefined]));

  const caller = event.From || 'Unknown';
  const loc = [event.FromCity, event.FromState].filter(Boolean).join(', ');

  const webhookUrl = withHttps(cfg.NOTIFY_WEBHOOK_URL);
  if (webhookUrl) await sendWebhook(context, cfg, event, webhookUrl, caller, loc);

  // Rings SIP + number simultaneously; first to answer wins
  const dialAttrs = {};
  if (cfg.CALLER_ID) dialAttrs.callerId = cfg.CALLER_ID;
  if (context.DOMAIN_NAME && context.PATH) dialAttrs.action = `https://${context.DOMAIN_NAME}${context.PATH}`;
  const dial = twiml.dial(dialAttrs);

  if (cfg.SIP_URI) dial.sip(withSip(cfg.SIP_URI));
  if (cfg.FORWARD_TO) {
    const whisper = withHttps(cfg.WHISPER_URL);
    let attrs = {};
    if (whisper) {
      const u = new URL(whisper);
      u.searchParams.set('caller', caller);
      attrs = { url: u.toString() };
    }
    dial.number(attrs, cfg.FORWARD_TO);
  }
  if (!cfg.SIP_URI && !cfg.FORWARD_TO) console.error('Set FORWARD_TO and/or SIP_URI');

  console.log('TwiML:', twiml.toString());
  return callback(null, twiml);
};
