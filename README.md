# Remote Search over SMS

Search the web from a phone that can text but has no data. A computer at home watches for questions, looks each one up in free web sources and texts the answer back. I built it for trips with a bar of signal or a satellite and no data, so `tide Tofino` from a trailhead comes back as a normal SMS.

## Ways to Reach It

Texting your Twilio number is the simplest. The computer polls Twilio's API for new texts, so there's no webhook or public address to set up, and listing your number in `ALLOWED_SENDERS` turns it on. That works from anything that sends ordinary SMS:

- An iPhone 14 or later on iOS 18, through Messages via satellite in Canada and the US. It sends SMS to non-Apple numbers like a Twilio one when your carrier supports SMS by satellite, and replies come back the same way, though you need to be outside with a clear view of the sky.
- Rogers Satellite in Canada or T-Mobile's T-Satellite in the US, which carry plain SMS to any number.
- A Garmin inReach, texting the Twilio number as it would any phone, and the answer comes back to the device as a normal reply. The inReach has no number of its own though, and Garmin's forums say its texts go out from whichever Garmin number the gateway picks, so it can change between messages. Each of those has to be in `ALLOWED_SENDERS`, and a text from one that isn't gets ignored, with its number in the log.

It also reads a Gmail label. A text to the Gmail address through your carrier's SMS-to-email gateway (from something like `6045551234@txt.bell.ca`) gets its answer texted to `PHONE_TO`, and plain email from an allowed address gets an email back in the same thread. Several carriers have closed their gateways (Rogers in 2023, AT&T in 2025), so that part depends on yours. Don't send inReach messages to the Gmail address, as they come from `no.reply.inreach@garmin.com` and the only way back is a form on Garmin's site, so it skips them. Text the Twilio number from the inReach instead.

## Commands

The first word picks the source and the rest is the question. Anything else is a web search.

| Text | Answer |
| --- | --- |
| `weather <place>`, `forecast <place>` | Open-Meteo, now or over 3 days, plus Environment Canada alerts |
| `sun <place>`, `time <place>` | sunrise and sunset, or the local time |
| `tide <place>` | next highs and lows at the nearest Canadian Hydrographic Service station |
| `avy <place>` | Avalanche Canada danger ratings and problems |
| `road <highway or BC town>` | DriveBC closures and events, like `road coquihalla` |
| `drive <place> to <place>` | distance and time from OSRM, with no traffic |
| `calc`, `convert`, `translate` | arithmetic, units and currency (ECB rates), MyMemory translation |
| `news [topic]`, `score <team>` | Google News headlines, ESPN scores |
| `hours <business> <town>` | opening hours, address and phone from OpenStreetMap |
| `define`, `wiki`, `so` | Wiktionary, Wikipedia, Stack Overflow |
| `reddit`, `quora`, `youtube`, `site <domain>` | a web search of that one site |
| `more`, `help`, `help <word>` | the rest of a long answer, the command list, one command's details |

The news comes from the Google News edition in `NEWS_REGION`, which is a country code like `GB` for the English edition there, or `CA:fr` for the French one in Canada. It's `CA` unless you set it. Reddit's API is closed to personal apps and Quora has none, so those answers are the top search result's title and snippet rather than the thread itself. Replies are folded into GSM-7 and cut into 300-character pages, since one character outside GSM-7 bills a reply at more than twice as many segments.

It says so when it doesn't know. A result only counts when it overlaps what you asked, and the reply names its source, so a near miss is easy to spot.

With `AI_API_KEY` set, a model writes one short answer from the search results and says "I don't know" when they don't cover it. The key's prefix picks Anthropic, OpenAI, OpenRouter, Groq, Google or xAI, and `AI_BASE_URL` with `AI_MODEL` points it at any OpenAI-compatible server, like a local Ollama. The results are handed to it as untrusted text, and whatever it writes only goes back to whoever asked.

Web searches go through the ddgs package, which scrapes DuckDuckGo and moves on to the other engines it supports while DuckDuckGo is blocking. With `BRAVE_API_KEY` set, Brave's search API gets asked before any of them, and it's the dependable option as it's a real API rather than a scraped page.

## Setup

It needs Python 3.11 or newer.

```
python -m venv .venv
.venv/bin/pip install -e ".[dev]"           # .venv\Scripts\pip on Windows
python RemoteSearch.py --query "tide Tofino" # no accounts needed for this
```

Copy `config.example.txt` to `config.txt` and fill it in, or set the same names as environment variables. For mail, make a Gmail filter that files your gateway's mail under `LABEL_NAME` and set the OAuth consent screen to In production, since a Testing app's login expires after 7 days. The first run opens a browser to log in.

```
python RemoteSearch.py            # poll until Ctrl-C
python RemoteSearch.py --once     # answer what's waiting and exit, for cron
python RemoteSearch.py --dry-run  # log replies instead of sending them
python -m pytest                  # offline tests
python tests/live_smoke.py        # one real question to each keyless source
```

At startup it marks mail that's already waiting as read, unless you pass `--catch-up`. Texts to the Twilio number that arrive while it's stopped get answered once it's back, and the ones it has handled are kept in `STATE_FILE`. A dry run leaves all of that alone, so mail stays unread and `STATE_FILE` isn't written, and it only remembers what it has answered until it stops.

## What It Won't Do

It won't start without `ALLOWED_SENDERS`, and it only ever answers the sender or `PHONE_TO`. Every reply is billed, so `MAX_REPLIES_PER_POLL` and `MAX_REPLIES_PER_HOUR` (30 by default, and kept across restarts) cap them, and questions past a cap wait for a later poll. A US Twilio number needs A2P 10DLC registration before it can text US phones, and Canadian numbers bought since March 2025 need registering too. Twilio handles STOP and HELP itself on North American numbers, so a text of just STOP blocks every reply to that phone until it texts START. The scraped engines all block automated searches after a burst now and then, and once every one of them has, the reply says the web search isn't answering. A Brave key is the fix if that keeps happening. The mail check trusts the From header, which can be forged.

## License

MIT, see [LICENSE](LICENSE).
