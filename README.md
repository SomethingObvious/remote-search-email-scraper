# Remote Search over SMS

Search the web from a phone that only has texting. You send a question by SMS,
your carrier's SMS-to-email gateway drops it into a Gmail label, this script
reads it, looks the answer up, and texts the answer back through Twilio.

I built it for trips where there's cell signal but no data. Text `sun Tofino`
from a trailhead and you get sunrise and sunset back as a normal SMS.

## Commands

The first word of your text picks a source. Everything else is the query.

| Text | Source |
| --- | --- |
| `weather <place>` | current conditions from Open-Meteo |
| `sun <place>` | today's sunrise and sunset, in that place's timezone |
| `define <word>` | Dictionary API |
| `wiki <topic>` | Wikipedia summary |
| `so <question>` | top Stack Overflow answer |
| `help` | the command list |
| anything else | DuckDuckGo, falling back to Wikipedia |

Replies are trimmed to about 300 characters so they fit in a couple of SMS
segments.

## It says so when it doesn't know

The plain web search is good at nouns and bad at questions. Text
`photosynthesis` and you get a real answer. Text `how do I treat a blister on a
hike` and you get:

```
No confident answer for 'how do I treat a blister on a hike'. Try a keyword
instead of a question, or a command: weather, sun, define, wiki, so.
```

That's deliberate. Wikipedia's search returns a hit for any query built out of
real words, so the honest options are a miss or a wrong answer dressed up as a
right one. Out of signal range, the miss is worth more. A Wikipedia result is
only used when its article title overlaps what you asked, and the reply names
the article so you can see what actually answered you.

For anything a search engine is bad at, use a command. `sun`, `weather` and
`define` are exact.

## Try it without any accounts

The lookups don't need Gmail or Twilio. Run one straight from a terminal:

```
python RemoteSearch.py --query "sun Tofino"
python RemoteSearch.py --query "define albedo"
python RemoteSearch.py --query "photosynthesis"
```

## Setup

```
pip install -e .
```

Copy `config.example.txt` to `config.txt` and fill it in. Any setting can also
come from an environment variable of the same name, which takes priority, handy
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
- **`ALLOWED_SENDERS`**: set this. Anything reaching the label triggers a reply,
  and a reply is a billed SMS to your phone. Put your carrier's gateway domain
  in it (`@txt.bell.ca`, `@vtext.com`, whatever yours is) so a stray email can't
  ring your phone on your dime. Leaving it empty still works and logs a warning
  at startup.
- **Optional**: `LABEL_NAME` (default `Remote Server`), `POLL_INTERVAL`
  (seconds, default 5), `MAX_SMS_CHARS` (default 300), `MAX_REPLIES_PER_POLL`
  (default 10).

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
after a restart. Pass `--catch-up` to answer the backlog instead.

## How it holds up

- One pooled HTTP session with retries on 429/5xx, and the default search hits
  DuckDuckGo and Wikipedia at the same time, so a reply is usually a second or
  two. Repeat lookups are cached in memory.
- It reads every unread message each poll, oldest first, following Gmail's
  pagination, and marks them read. A burst of texts all get answered and nothing
  is answered twice across restarts.
- At most `MAX_REPLIES_PER_POLL` texts go out per tick. The rest stay unread and
  go on the next one, so an inbox flood can't become a Twilio bill.
- Repeated Gmail failures back the poll off up to five minutes instead of
  hammering the API every interval.

## Limitations

- It polls, so there's up to `POLL_INTERVAL` seconds of lag, and it calls the
  Gmail API on every tick whether or not new mail arrived. Push through Pub/Sub
  would fix that and needs a public endpoint, which a laptop in a cabin doesn't
  have.
- A message that can't be delivered is still marked read. Retrying forever would
  bill you forever for a text that never arrives, so the failure is logged and
  the question is dropped.
- The keyless sources have their own limits: Stack Exchange caps anonymous use at
  300 requests/day per IP, and the DuckDuckGo Instant Answer API only responds to
  a narrow band of queries, which is why Wikipedia does most of the work.
- The carrier-boilerplate stripping in `clean_query` was tuned for one MMS-to-
  email gateway. If your provider wraps texts differently, adjust that regex.

## Tests

```
python test_remotesearch.py
```

Offline, no network and no accounts. Covers the relevance gate against the real
Wikipedia hits it was written for, the sender allowlist, the reply cap, Gmail
pagination and the message parsing.

## What changed in 3.0

The plain web search used to answer every question with something. "How do I
treat a blister on a hike" returned a summary of *The Salt Path*, a travel
memoir that mentions blisters. "What time is sunset in Tofino" returned an
article about British Columbia. "Is highway 4 open" returned Highway 401, in
the wrong province. Wikipedia's search never comes back empty for real words, so
the no-results path was unreachable and every miss shipped as an answer. There
is now a relevance check, the article title is part of the reply, and a genuine
miss says so.

Also: `ALLOWED_SENDERS` and `MAX_REPLIES_PER_POLL`, because the old version
would answer any mail that reached the label and send a billed text for each one.
Gmail listing follows `nextPageToken`, so a backlog over 100 no longer sits
unread and then arrives all at once. Weather moved from wttr.in to Open-Meteo,
which resolves places properly (Tofino used to come back as "Clayoquot") and
brought the `sun` command with it. The `reddit` command is gone: Reddit returns
403 to non-OAuth clients now, so it never worked, it only fell through. And
`truncate` could return more text than the limit it was given when the limit was
under four characters.

## License

MIT, see [LICENSE](LICENSE).
