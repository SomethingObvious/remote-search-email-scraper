# Remote Search over SMS

Search the web from a phone that can text but has no data. You text a question to your carrier's SMS-to-email gateway, a Gmail filter files it under a label, and this script looks the answer up and texts it back through Twilio. I built it for trips with cell signal and no data, so `sun Tofino` from a trailhead comes back as a normal SMS.

The first word of a text picks the source and the rest is the question.

| Text | Answer |
| --- | --- |
| `weather <place>` | current conditions from Open-Meteo |
| `sun <place>` | today's sunrise and sunset, in that place's timezone |
| `define <word>` | Free Dictionary API |
| `wiki <topic>` | Wikipedia summary |
| `so <question>` | top Stack Overflow answer |
| `help` | the command list |
| anything else | DuckDuckGo, then Wikipedia |

A region picks the right town when there are several (`weather Paris, France`). Replies are cut to 300 characters of plain ASCII, which fits in two SMS segments, where a single curly quote would make it five.

It says so when it doesn't know. Wikipedia's search returns a hit for almost any words, so a result only counts when its title overlaps what you asked, and the reply names the article so a near miss is easy to spot. Keywords work a lot better than whole questions.

## Setup

It needs Python 3.11 or newer.

```
python -m venv .venv
.venv/bin/pip install -e .                  # .venv\Scripts\pip on Windows
python RemoteSearch.py --query "sun Tofino" # no accounts needed for this
```

Copy `config.example.txt` to `config.txt` and fill it in, or set the same names as environment variables. Make a Gmail filter that files mail from your gateway under `LABEL_NAME`, and put the gateway's domain in `ALLOWED_SENDERS`. Set the Google OAuth consent screen to In production, since a Testing app's login expires after 7 days. The first run opens a browser to log in to Gmail. On a headless box, log in once on a machine with a browser and copy the token file over.

```
python RemoteSearch.py            # poll until Ctrl-C
python RemoteSearch.py --once     # answer what's unread and exit, for cron
python RemoteSearch.py --dry-run  # log replies instead of texting them
python test_remotesearch.py       # offline tests
```

On startup it marks mail that's already waiting as read without answering it, unless you pass `--catch-up`. Paths in the config are relative to the folder you run it from.

## What It Won't Do

It only texts `PHONE_TO`, never whoever sent the question. Each message is marked read before its reply goes out, so a failed send is dropped rather than retried and billed forever. `MAX_REPLIES_PER_POLL` caps each poll but not the day, so a Twilio usage trigger is still worth setting. The sender check trusts the From header, which can be forged, so it keeps out stray mail but probably not someone determined.

## License

MIT, see [LICENSE](LICENSE).
