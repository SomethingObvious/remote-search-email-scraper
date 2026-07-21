# Remote Search over SMS

Search the web from a phone that only has texting. You send a question by SMS,
your carrier's SMS-to-email gateway drops it into a Gmail label, this script
reads it, looks the answer up, and texts the answer back through Twilio.

I built it for trips where there's cell signal but no data. Text `weather
Tofino` from a trailhead and you get the forecast back as a normal SMS.

## Commands

The first word of your text picks a source. Everything else is the query.

| Text | Source |
| --- | --- |
| `weather <place>` | current conditions from wttr.in |
| `define <word>` | Dictionary API |
| `wiki <topic>` | Wikipedia summary |
| `so <question>` | top Stack Overflow answer |
| `reddit <query>` | top Reddit result (best effort, see below) |
| `help` | the command list |
| anything else | DuckDuckGo, falling back to Wikipedia |

Replies are trimmed to about 300 characters so they fit in a couple of SMS
segments.

## Try it without any accounts

The lookups don't need Gmail or Twilio. Run one straight from a terminal:

```
python RemoteSearch.py --query "weather Toronto"
python RemoteSearch.py --query "define albedo"
python RemoteSearch.py --query "why is the sky blue"
```

## Setup

```
pip install google-auth google-auth-oauthlib google-api-python-client requests beautifulsoup4 twilio
```

Copy `config.example.txt` to `config.txt` and fill it in. Any setting can also
come from an environment variable of the same name, which takes priority — handy
for a systemd unit or a container.

- **Twilio**: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_FROM`
  (the number that sends), `PHONE_TO` (the number that gets the answer). All
  from the Twilio console.
- **Gmail**: enable the Gmail API in the Google Cloud console, create an OAuth
  client ID of type "Desktop app", and download its JSON. Point
  `GMAIL_CREDENTIALS_FILE` at it. `GMAIL_TOKEN_FILE` is where the login gets
  cached after the first run (the script writes it).
- **Scope**: use `https://www.googleapis.com/auth/gmail.modify`. The poller
  marks each message read after answering it, so it needs write access. A
  read-only scope will fail on that step.
- **Optional**: `LABEL_NAME` (default `Remote Server`), `POLL_INTERVAL`
  (seconds, default 5), `MAX_SMS_CHARS` (default 300).

`config.txt`, the credentials JSON, and the cached token are all gitignored.

## Running it

```
python RemoteSearch.py            # poll forever
python RemoteSearch.py --once     # answer what's unread now, then exit (good for cron)
python RemoteSearch.py --dry-run  # log the replies instead of paying for SMS
```

The first run opens a browser to authorize Gmail. After that it polls the label
and answers new mail. On startup it clears the existing backlog without replying
and texts "Remote search online" once, so you don't get spammed by old messages
after a restart — pass `--catch-up` to answer the backlog instead.

## How it holds up

- One pooled HTTP session with retries on 429/5xx, and the default search hits
  DuckDuckGo and Wikipedia at the same time, so a reply is usually a second or
  two. Repeat lookups are cached in memory.
- It reads every unread message each poll, oldest first, and marks them read, so
  a burst of texts all get answered and nothing is answered twice across
  restarts.

## Limitations

- It polls, so there's up to `POLL_INTERVAL` seconds of lag, and it calls the
  Gmail API on every tick whether or not new mail arrived.
- No rate limiting. A burst of incoming texts means a burst of outgoing SMS, and
  a Twilio charge for each.
- Reddit throttles clients that aren't using its OAuth API, so `reddit` queries
  often fall through to a plain web search. The other sources are keyless public
  APIs; Stack Exchange caps anonymous use at 300 requests/day per IP.
- The carrier-boilerplate stripping in `clean_query` was tuned for one MMS-to-
  email gateway. If your provider wraps texts differently, adjust that regex.

## License

MIT, see [LICENSE](LICENSE).
