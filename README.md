# WaterTracker

Texts you water reminders over iMessage and logs the replies you text back. It
runs on your Mac, talks to Messages, and keeps no account and no server — your
intake log is a JSON file on your own disk.

```
You  22:14   💧 Water break. 32/100 oz ███░░░░░░░ 32% 28 oz behind pace.
              How much?
Me   22:16   just had a couple glasses
You  22:16   💧 Logged 16 oz. 48/100 oz ████░░░░░░ 48% — 52 oz to go.
```

Replies are read as sentences, so `drank 500ml and a bottle at the gym` and
`ok you can stop for today` both work.

---

## Requirements

- macOS with Messages signed in
- Python 3.10+ (uses `X | None` type syntax)
- Standard library only. `pip install anthropic` is optional and turns on
  Claude reading your replies; without it, pattern matching handles them.

Texting your own number works and is the intended setup: it creates a self-chat
you can read on your phone.

## Setup

```bash
export WATER_PHONE="+15551234567"     # your number, digits included
python3 waterTracker.py test          # confirm a text arrives
python3 waterTracker.py install       # keep it running in the background
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.watertracker.reminders.plist
python3 waterTracker.py doctor        # verify everything
```

Optionally, to have Claude read your replies rather than pattern matching:

```bash
pip install anthropic
export ANTHROPIC_API_KEY="sk-ant-..."
python3 waterTracker.py install       # copies the key into the agent, chmod 600
launchctl bootout gui/$(id -u)/com.watertracker.reminders
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.watertracker.reminders.plist
```

`install` writes a LaunchAgent that starts the loop at login and restarts it if
it dies. Reminders only go out while the loop is running, so without this they
stop the moment you close the terminal.

### The two permissions

macOS gates both halves of this, and they fail differently.

| | Needed for | Symptom when missing |
|---|---|---|
| **Automation → Messages** | sending | prompt on first send; sends fail until approved |
| **Full Disk Access** | reading your replies | reminders arrive, replies are ignored |

Reading replies means reading `~/Library/Messages/chat.db`, which is protected.
Add the **Python binary** to System Settings → Privacy & Security → Full Disk
Access, then reload the agent.

Two things about that grant cost real debugging time here:

1. **It follows whichever process starts Python, not the Python file.** A grant
   held by your terminal is inherited by anything you launch from that terminal
   but does *not* cover the LaunchAgent, even running the identical
   interpreter. The agent's interpreter needs its own entry.
2. **An entry can exist and be switched off.** A denied entry may not render as
   a togglable row at all. If you can't find a switch, re-add the binary with
   the **+** button, which replaces the denied entry.

`doctor` reads the agent's own log rather than testing its own access, because
its own access proves nothing about the agent's.

## Usage

```bash
python3 waterTracker.py            # run the reminder loop
python3 waterTracker.py status     # today's intake and entries
python3 waterTracker.py week 7     # recent days, average, days at goal
python3 waterTracker.py log 16     # log without texting: also "2 cups", "500ml"
python3 waterTracker.py test       # send one text to check delivery
python3 waterTracker.py doctor     # explain why reminders are not arriving
python3 waterTracker.py install    # write the LaunchAgent
```

`status`, `week` and `log` don't need `WATER_PHONE` — only sending does, and
none of them import the Anthropic SDK. That import alone was 258 ms of the
317 ms `status` used to take; it now loads on first use, and these run in 37 ms.

`status` draws the day — the bar, what's left, and where each entry came from:

```
$ python3 waterTracker.py status

💧 Water · Sat Sep 12

  ███████████████▏░░░░░░░░░░░░░░░░  47%
  60.3 / 128 oz  ·  67.7 oz to go
  △ 2 oz ahead of an even pace for this hour

  08:05      20 oz  reply
  11:40      24 oz  photo
  13:20    16.3 oz  reply      · 45m ago
```

47% means one thing at noon and another at ten at night, so the third line says
where you are against an **even pace for the hour** — the same number the loop
uses to space its nudges. A `┊` tick appears in the empty track at the point
that pace would have you by now, and disappears once the bar passes it. Only
the newest entry is dated, because that's the one that answers whether to drink
something now.

Once the goal is met the bar turns green and the second line says by how much —
`goal met, 24 oz past it` — the pace line drops, since there's nothing left to
chase, and a run of days at goal adds a `🔥 4 day streak at goal` line.

`log` adds an amount without texting anything, then prints the same view back:

```
$ python3 waterTracker.py log 16
✓ logged 16 oz

💧 Water · Sat Sep 12

  ███████████████████▏░░░░░░░░░░░░  60%
  76.3 / 128 oz  ·  51.7 oz to go
  △ 18 oz ahead of an even pace for this hour

  08:05      20 oz  reply
  11:40      24 oz  photo
  13:20    16.3 oz  reply
  14:05      16 oz  cli        · just now
```

`week` scales its bars to the best day rather than to the goal, so a day that
ran well past it still reads as the bigger one; the `┊` marks where the goal
falls once some day has pushed the scale out beyond it.

```
$ python3 waterTracker.py week

💧 Last 7 days · goal 128 oz

  Sun 09-06  ████████████████████████████████   150 oz  ✓
  Mon 09-07  ███████████████████████████▎░░░░   128 oz  ✓
  Tue 09-08  █████████▍░░░░░░░░░░░░░░░░┊░░░░░    44 oz
  Wed 09-09  ████████████████████▌░░░░░┊░░░░░    96 oz
  Thu 09-10  ░░░░░░░░░░░░░░░░░░░░░░░░░░┊░░░░░     0 oz
  Fri 09-11  ███████████████████████▌░░┊░░░░░   110 oz
  Sat 09-12  ████████████▉░░░░░░░░░░░░░┊░░░░░  60.3 oz

  84 oz/day average  ·  588.3 oz in total  ·  2 of 7 days at goal
```

The bars fill by eighths of a character, so a swallow moves them, and they size
themselves to the terminal. None of that colour reaches your phone: what
`status` and `week` *text* back is built separately and stays plain, because an
escape sequence in an iMessage arrives as gibberish and can't be unsent.

### What the phone gets instead

The texted views are written for a different medium, not a downgrade of these
ones. iMessage sets text in a **proportional** font, so a padded column only
looks aligned in the editor you wrote it in. The week used to right-align its
ounces and space its gaps for a monospace terminal it was never displayed in:

```
  Mon 09-07  ██████████   150 oz *      <- built for a font iMessage doesn't use
```

Block glyphs are the one thing on the line with a predictable width, so the
bars do the aligning and the ragged numbers sit at the end where it doesn't
show. The summary leads, because on a phone it's the answer:

```
💧 7 days · 84 oz/day average · 3 at goal
Mon ██████████ 150 ✅
Tue ██████████ 128 ✅
Wed ████░░░░░░ 44
Thu █████████░ 96
Fri ░░░░░░░░░░ 0
Sat ██████████ 110 ✅
Sun ██████░░░░ 60.3
```

A test asserts the rule rather than the appearance: no texted line may contain
a double space or leading padding. The 💧 stays first on every message — it's
how the tracker recognises its own echo in a self-chat, not decoration.

### Texting it back

Text it however you like. A message that is nothing but an amount is answered
straight away without asking anything; everything else goes to Claude, which
reports what it meant; and if Claude is unreachable, pattern matching covers
the phrasings below.

| You text | It does |
|---|---|
| `16` / `16oz` / `2 cups` / `500ml` | logs that amount |
| `just had a couple glasses` | logs 16 oz |
| `drank 500ml and a bottle at the gym` | logs both, summed |
| `my nalgene` / `a can` / `a pint` | logs the container's size |
| `twenty five ounces` / `3/4 of a bottle` | logs 25 oz / 12.7 oz |
| `done` / `yep` / `just finished one` / 👍 | logs one glass, and says it guessed |
| `awake` / `good morning` / `gm` | starts the day now, and paces from this moment |
| *a photo of a container* | identifies it and logs what it holds |
| `not yet` / `in a bit` | pushes the next nudge out, without pausing |
| `status` / `?` / `how am i doing` | today's progress |
| `week` / `my weekly average` | last 7 days |
| `goal 120` / `set my goal to 120oz` | changes the daily goal |
| `undo` / `scratch that` / `oops` | drops the last entry |
| `pause` / `going to bed` / `resume` | stops or restarts today's nudges |
| `had 20 oz, you can stop for today` | logs **and** pauses |
| anything else | a short answer, with today's real numbers attached |

Units: `oz`, `cup`/`glass` (8), `mug` (10), `can` (12), `pint` (16), `bottle`
(16.9), `tumbler` (20), `nalgene`/`quart` (32), `jug` (64), `gallon` (128),
`sip` (1.5), `gulp` (2), `ml`, `l`/`liter`.

Guards worth knowing: a negation before the amount doesn't log it (`haven't had
my 16 oz yet`), amounts over 400 oz are rejected, a bare number in a long
sentence is ignored (`I'll drink some at 16:00 after my 3 meetings`), and
`the bottle is empty` is a statement rather than a drink — but `my bottle` is.

### Settings

All optional except the phone number.

| Variable | Default | Meaning |
|---|---|---|
| `WATER_PHONE` | — | iMessage handle to text (required to send) |
| `WATER_GOAL_OZ` | 100 | daily goal in fluid ounces |
| `WATER_INTERVAL_MIN` | 120 | base spacing between nudges |
| `WATER_WAKE_HOUR` | 8 | no nudges before this hour |
| `WATER_SLEEP_HOUR` | 22 | no nudges after this hour |
| `WATER_POLL_SECONDS` | 3 | how often replies are checked |
| `WATER_SEND_TIMEOUT` | 60 | seconds before a stuck send gives up |
| `WATER_KEEP_DAYS` | 90 | how long history is kept |
| `WATER_STALE_REPLY_MIN` | 60 | ignore replies older than this |
| `WATER_FOLLOWUP_MIN` | 60 | chase an unanswered nudge after this long; `0` off |
| `WATER_FOLLOWUP_GRACE_MIN` | 30 | how late that chase may still be sent |
| `WATER_STATE_FILE` | `water_tracker_state.json` | where the log lives |
| `WATER_DEFAULT_OZ` | 8 | what a bare "done" logs |
| `ANTHROPIC_API_KEY` | — | enables Claude reading replies |
| `WATER_LLM` | `auto` | `off` for pattern matching only |
| `WATER_MODEL` | `claude-haiku-4-5` | any Claude model |
| `WATER_LLM_TIMEOUT` | 8 | seconds before falling back to patterns |
| `WATER_EFFORT` | — | thinking depth; only for models that accept it |
| `WATER_FAST_PATH` | `on` | `off` to send even bare amounts to Claude |
| `WATER_PHOTOS` | `on` | `off` to ignore photo attachments |
| `WATER_PHOTO_MAX_PX` | 1024 | longest edge a photo is downscaled to |
| `WATER_PUSH_URL` | — | push nudges here as well as texting them |
| `WATER_PUSH_TIMEOUT` | 10 | seconds before a push gives up |
| `WATER_PUSH_ONLY` | — | `on` to push instead of texting, not as well |
| `NO_COLOR` | — | set to anything to turn off terminal colour |

Nudges are **paced**: the gap stretches to 1.5× the interval when you're ahead
of an even pace for the time of day and tightens toward half when you're behind.
A fixed interval nudges the same whether you're 5 oz or 50 oz short.

### Starting the day when you actually wake up

`WATER_WAKE_HOUR` is only a fallback. Text `awake` (or `good morning`) and the
tracker records the time, paces the day from then, and ends any pause left over
from last night. Waking at 06:30 stops being two hours where you're ahead of
pace by definition; waking at 10:45 stops starting you 14 oz behind on water
you were asleep for.

The point is that this can be automatic. On iOS, **Shortcuts → Automation →
Personal → Wake Up** fires at the wake time from your Sleep Schedule, and the
action is just *Send Message* — `awake`, to the same number the tracker texts.
Set it to **Run Immediately** with *Ask Before Running* off and nothing needs
touching. No new plumbing on this end: the loop already reads incoming texts
every few seconds, so a wake signal arrives the same way an amount does.

Since the Shortcut runs on your phone, it can read Health there and send real
numbers with it — `awake, slept 7h20m`. Apple Watch data can't be read on this
end at all: `HealthKit.framework` does ship on macOS, but
`HKHealthStore.isHealthDataAvailable()` returns `NO` (it's there for Mac
Catalyst builds) and Health never syncs to a Mac. The phone is the only place
that data exists, so the phone is what has to send it.

Two bounds worth knowing. A wake time from a previous day is ignored rather
than paced from, and one later than `WATER_SLEEP_HOUR` is clamped to it, so a
late nap can't invert the day. An early wake is taken at its word and *not*
clamped up to `WATER_WAKE_HOUR` — clamping would make waking early feel
identical to not texting at all, which is the thing this exists to fix.

A nudge you never answer gets **one follow-up** an hour later, worded as a
follow-up rather than a fresh nudge:

```
You  14:02   💧 Water break. █████░░░░░ 53/100 oz (54%)
You  15:02   💧 Still █████░░░░░ 53/100 oz (54%) — no reply since I asked 60 min ago.
              Reply with an amount to log it, or 'not yet'.
```

The usual way a reminder fails isn't disagreement — the text is read while
you're doing something else and never answered, and with the gap at two hours
that's a long silence. Any reply calls it off, including `status` or `not yet`:
someone texting back has the phone in hand. It fires **once** per nudge, since
a chain of them just trains you to ignore the whole thing.

A chase is allowed to land **after** `WATER_SLEEP_HOUR`, which is the one place
it ignores the quiet hours. Finishing an exchange the tracker started is not the
same as starting one: a nudge at 21:50 is still owed its answer at 22:50. Only
the *nudge* is gated on the waking window. Gating the whole check on it, as this
first shipped, meant no nudge in the last hour of the day could ever be chased —
a silent one-hour dead zone every evening.

The other bound is staleness. A chase more than `WATER_FOLLOWUP_GRACE_MIN` late
is dropped rather than sent, because the Mac can sleep straight through the
moment one came due, and a chase arriving at breakfast asks after a question
nobody remembers being asked. The morning nudge covers that instead. `doctor`
says which of those happened rather than leaving you to guess.

Two consequences of it sharing the schedule with the paced nudge. If a nudge
comes due at the same moment, the nudge wins — you never get both at once, and
the nudge carries fresher progress. And because the paced gap tightens to half
the interval when you're far behind, at the defaults (120 min interval, 60 min
follow-up) it's already nudging hourly, so the follow-up is what covers the
wider gaps when you're near or ahead of pace. Either way the effect is a
one-hour ceiling on silence after a text you didn't answer.

### Sending a photo of a container

For the container you own but have no name for. Text it a picture and it names
the thing and logs what that thing holds:

```
Me   14:20   [photo of a 32 oz flask]
You  14:20   💧 Logged 32 oz. ███░░░░░░░ 32/100 oz (32%) — 68 oz to go.
              I read that as a hydroflask — text an amount to correct it.
```

**The units table wins wherever it recognises the container.** A photo read as
a `nalgene` logs the same 32 oz that typing "nalgene" would — the model picks
the category, the table supplies the number. Only a container the table has
never heard of falls back to the model's own capacity estimate, and the reply
says which of the two happened (`read that as a…` versus `guessed … holds
about…`) so a wrong one can be corrected.

That split is deliberate, and it is the same rule the text path follows: the
model classifies, the log and the tables supply every figure. It also sets the
limit of what this can do. **It reads the container, not the contents** — it is
told not to guess how full the glass is or how much you drank, because a photo
does not reliably show either and a confident wrong number is worse than a
question. A picture with no drink container in it gets *"couldn't tell what
that holds — how much was it?"* rather than an invented amount.

Three things worth knowing:

- **HEIC is converted first.** iPhones send HEIC, which the API does not
  accept, at a resolution far past what "what kind of bottle is this" needs.
  `sips` (in the base system, nothing to install) converts and downscales in
  one pass. The test suite runs a real PNG → HEIC → JPEG round trip rather
  than trusting that.
- **A caption skips the whole path.** Send a photo with `16 oz` attached and
  the text is used, because the pattern matching answers for free in
  milliseconds and a vision call would be pure cost.
- **It's the slow path by design.** An image is roughly 1,500 tokens against
  about 30 for `28 oz`, there's no pattern matching underneath it to fall back
  on, and it gets double the usual timeout for that reason. A failure is
  always answered in words — silence here is indistinguishable from a tracker
  that has stopped working. `WATER_PHOTOS=off` turns it off.

### Getting notified reliably

Texting your own number has one flaw no setting fixes: **every tracker message
comes from your own Apple ID**, and iOS treats your own traffic differently
from a real correspondent's. The database shows the shape of it — in the
tracker's thread there are exactly as many outgoing rows as incoming ones,
because each message is logged twice, and the copy of anything *you* send comes
back already marked read.

So set `WATER_PUSH_URL` and the nudges go out as a push as well as a text:

```bash
export WATER_PUSH_URL="https://ntfy.sh/water-<something-long-and-random>"
python3 waterTracker.py test     # sends both, so you can see which arrives
```

[ntfy](https://ntfy.sh) needs no account — any URL path is a topic. Install the
app, subscribe to the same topic, and the notification comes from a real app
instead of from yourself. **Pick a long random topic**: a public ntfy topic is
readable by anyone who knows its name, and this one carries your intake.

A self-chat shows **every message twice** — once as sent, once as the same
thing received — so the tracker's half of the conversation doubles everything in
the thread. `WATER_PUSH_ONLY=on` keeps it out of Messages altogether, leaving
only what you actually typed. The text is still the fallback when a push
fails, because losing the reminder outright is worse than a duplicate.

Your own messages still double even then: that is iMessage's own behaviour for
a self-chat and nothing here can change it. The only complete cure is not
texting your own number.

Without `WATER_PUSH_ONLY`, only the messages meant to interrupt you get a push
— the paced nudge and the follow-up chase. A confirmation of something you just typed does not buzz your
phone. The push fires *before* the text and regardless of how the text goes, so
an `osascript` timeout cannot take the notification down with it, and a push
that fails is a missed notification and nothing worse: the text is still the
record of what happened.

It shells out to `curl` rather than using `urllib`, for a specific reason: this
interpreter has no usable CA bundle (`ssl.get_default_verify_paths().cafile` is
`None`), so `urllib` cannot do HTTPS here at all, while `curl` uses the system
store. It also keeps the promise that nothing needs installing from pip.

## How it works

```
LaunchAgent ─→ waterTracker.py run ─┬─→ osascript ─→ Messages.app      (sending)
                                    ├─→ copy of chat.db ─→ sqlite3     (reading)
                                    ├─→ bare amount? ─→ {log, ounces}  (fast path)
                                    ├─→ Claude ─→ {action, ounces}     (understanding)
                                    └─→ patterns ─→ {action, ounces}   (fallback)
                                              ↓
                                  water_tracker_state.json
```

### How long a reply takes to come back

Measured on this machine, for everything except the model call:

| Stage | Cost |
|---|---|
| Waiting for the next poll | 0–3 s (avg 1.5 s) |
| Change check (three `stat()` calls) | ~0 ms |
| Copying `chat.db` + `-wal` (6.1 MB) and querying | 2.6 ms |
| Bare amount, answered locally | ~0 ms |
| Otherwise, one Claude call | not measured here — no credentials on this box |
| `osascript` send | ≥36 ms |

Everything local is milliseconds, so the wait used to be almost entirely the
poll interval and the model. Three things follow from that:

**The poll interval was the biggest single lever.** It was 20 s, so a reply sat
unnoticed for an average of 10 s before any work started — and that was pure
dead air, not work. A poll that finds nothing costs three `stat()` calls, and
only a *changed* database gets copied, so dropping it to 3 s costs nothing
measurable and removes ~8.5 s from every reply.

**A bare amount doesn't need a model.** `28 oz` is 28 oz; there is nothing for
Claude to add, and it was the most common reply in practice. Messages built
only from amounts and filler are answered locally in microseconds. The check is
a whitelist of the number words, the unit names, and a short filler list, so an
unrecognised word defers to Claude — it can only ever be too cautious. Anything
carrying more than an amount (`had 20 oz, you can stop for today`) still goes to
the model, because the amount isn't the only thing it's asking for.

**Worst case matters more than average.** The timeout was 20 s with one SDK
retry, so an unreachable API meant up to 40 s of silence before the pattern
matcher answered. Retries are now off for the interpretation call and the
timeout is 8 s: a retry would double the wait for someone holding their phone,
and the fallback is instant and already right most of the time.

Two things deliberately *not* done. **Prompt caching** doesn't apply: the
system prompt and schema come to ~544 tokens, under the 4096-token minimum for
Haiku 4.5, so it would silently never cache — and at that size the saving would
be a few milliseconds anyway. **Streaming** doesn't help either, since the whole
JSON object is needed before anything can be logged.

- **Sending** shells out to `osascript`. The number and body are passed as
  `argv`, never interpolated into the script text, so a reply containing quotes
  or AppleScript syntax can't escape into the command.
- **Reading** copies `chat.db` and its `-wal`/`-shm` sidecars to a temp
  directory first. Messages holds the database open in WAL mode; reading it in
  place either blocks or misses the newest rows. The copy is skipped while the
  files' mtime and size are unchanged — and the `-wal` file has to be part of
  that check, because a new message can land there without `chat.db`'s own
  mtime moving.
- **State** is written through a temp file and `os.replace()`, which is atomic
  per filesystem. An unreadable file is moved to `.corrupt` rather than
  overwritten, so a half-written log is never mistaken for no history.
- **Understanding a reply** is one Claude call per incoming message, returning
  a fixed JSON shape (`{action, ounces, goal_oz, chat}`) via structured
  outputs. The model picks the action and estimates an amount; **every number
  in a reply is read from the log.** A model that invented your intake would be
  confidently wrong about the one thing being counted, whereas a misread action
  costs a single entry that `undo` fixes. Answers are validated before use: the
  action must be one of the nine, an amount must be in range, and free-text
  replies get trimmed.
- **Nothing depends on Claude being reachable.** No package, no credentials, an
  expired key, a refusal, a timeout, an unparseable answer, or `WATER_LLM=off`
  all fall through to the pattern matching, which is why it's still there and
  still tested. Failures that would repeat every message (bad key, no model
  access) stop further calls for the run; a timeout or rate limit doesn't.
  Unrecognised errors fall through too, deliberately: a reply's row in
  `chat.db` is consumed when it's read, so an exception escaping this layer
  would leave that message silently unanswered rather than answered literally.
- **Self-chat** is the messy case. Texting your own number makes Messages log
  every outgoing text a second time as an *incoming* row, so the tracker can
  read its own reminders as replies and answer them forever. Two guards: each
  send is remembered with a timestamp for a couple of minutes, and every
  outgoing message starts with 💧, so a copy read back hours later is still
  recognisable. That second guard matters because its own progress bar parses
  as a valid amount — `0/100 oz` reads as 100 oz.

## Tests

```bash
python3 -m unittest discover .        # 201 tests, ~0.4s
```

No network, no Messages access, no real state file: sends are captured in a
list, the state path is redirected to a temp directory, and the Claude client
is a stand-in that records what it was asked. The suite forces `WATER_LLM=off`
at import, so a key in your environment can't turn the tests into API calls.

The reply path runs against a stand-in `chat.db` built in WAL mode with the
connection held open, the way Messages runs it, covering handle formats,
outgoing rows, `attributedBody` decoding, echo suppression, stale replies and
priming.

The tests were checked by mutation — deliberately reintroducing each bug to
confirm the suite fails. Reverting the bare-number guard, the corrupt-file
quarantine, the `-wal` sidecar check, the marker guard, the stale-reply guard,
the model's amount range check, the action allowlist, the refusal check, or the
plist permissions each breaks it. That exercise also found a bug in the tests
themselves: a cached client made every case after the first in one loop
vacuous.

The startup check was mutated the same way: making it read the configuration
instead of probing, spend a real message instead of counting tokens, report
`WATER_LLM=off` as a fault, print over its own report, or let an unexpected
error escape each fails a named test.

The follow-up was mutated the same way: letting the chase repeat, dropping the
arming on a nudge, ignoring the wait, ignoring `WATER_FOLLOWUP_MIN=0`, failing
to cancel on a reply, sending a chase on top of a due nudge, or skipping the
paused/asleep/goal-met guards each fails a named test. One mutation survived
the first pass — dropping the arming — because the test helper that fast
forwards the clock sets that same field, so it hid a nudge that armed nothing.
That case now has its own test asserting the state directly.

That exercise found a second weakness in the tests. The stand-in SDK's
exceptions were flat siblings, but the real ones form a hierarchy — every
status error descends from `APIStatusError`, and a timeout is a kind of
connection error — so catching the base class too early would swallow the 401
and 404 cases that have something specific to say, and the suite could not
tell. The stand-in now mirrors the real hierarchy, and reordering either
`except` chain fails.

The terminal output was mutated the same way. Painting the bar inside
`progress_line`, dropping the `NO_COLOR` and `TERM=dumb` gate, removing the
zero-goal guard from `streak`, or nesting a painted meter back inside a dimmed
line each fails a named test. The first of those is the one that matters: the
failure prints the exact escape sequence that would have been texted to a
phone, which is where a colour bug in this program actually costs something.

Two rules are asserted rather than described. Nothing the tracker sends may
contain an escape, checked with colour forced fully on across every reply the
tracker answers — and within one reset-delimited run of painted output there is
at most one group of opening codes, with no line ending on a style left open.
That second rule caught a real defect: `paint` closes with a plain reset, which
ends *all* styling rather than the one it opened, so a painted meter appended
to a dimmed line closed the dim early. It rendered correctly only because the
meter happened to be last on the line.

Writing that check exposed a weakness in the check itself. The first version
anchored its regex to the start of a run, so leading plain text before the
escape defeated it and the assertion passed against output that was genuinely
broken. Mutation is what caught it: the rule only earns its place once the bug
it describes can make it fail.

The Claude request shape is verified against the stand-in client, not the live
API — model, JSON schema, effort and timeout are asserted, and the startup
check's arguments are checked against the installed SDK's signature, but no
test proves the service accepts them. The one failure the check can't fake is
success: verifying a *working* key needs a working key.

## Troubleshooting

Run `doctor` first; it checks the things that fail silently and prints the
state that makes silence *correct*, like a met goal or a pause.

```
$ python3 waterTracker.py doctor

💧 Water tracker checkup

  ✓  texting +1555..., from the LaunchAgent
  ✓  replies read by claude-opus-5, with pattern matching as the fallback
     state file /Users/you/WaterTracker/water_tracker_state.json
  ✓  state file exists
  ✓  this python can read Messages history, so replies are picked up
  ✓  agent log clean since it started
  ✓  LaunchAgent loaded
  ✓  loop running (pid 10538)
  ✓  no nudge due: it is outside 8:00-22:00
     nothing awaiting a reply; follow-up after 60 min of silence
     today 0/100 oz ░░░░░░░░░░░░░░░░░░░░

  ✓ everything checks out
```

The ticks are green and the failures red on a terminal. Piped to a file, into a
bug report, or with `NO_COLOR` set, the same run prints `ok` and `FAIL` in
place of the glyphs and no escape sequences at all.

When something *is* wrong, the failing lines are red and collected again at the
bottom, because a list this long is easy to skim past:

```
$ python3 waterTracker.py doctor

💧 Water tracker checkup

  ✗  'not-a-phone' from the environment is not a phone number or email; Messages will refuse it
  ✗  replies pattern matching: no credentials, set ANTHROPIC_API_KEY
     no push channel: WATER_PUSH_URL unset, so nudges rely on iMessage alone
     state file /Users/you/WaterTracker/water_tracker_state.json
  ✓  state file exists
  ✓  this python can read Messages history, so replies are picked up
  ✗  no LaunchAgent, so reminders stop with the terminal (try 'install')
  ✓  loop running (pid 31400)
  ✓  not paused
  ✓  inside the 8:00-22:00 nudge window
  ✗  no nudge has been sent yet
     nothing awaiting a reply; follow-up after 60 min of silence
     today 0/100 oz ░░░░░░░░░░░░░░░░░░░░

  4 problem(s) to fix:
    • 'not-a-phone' from the environment is not a phone number or email; Messages will refuse it
    • replies pattern matching: no credentials, set ANTHROPIC_API_KEY
    • no LaunchAgent, so reminders stop with the terminal (try 'install')
    • no nudge has been sent yet
```

A clean run says so in one line instead, which is the point: `doctor` prints
the same dozen checks either way, and without a verdict at the end an all-green
report reads like a wall of text you still have to audit yourself.

That follow-up line reads as a fact rather than a check, because both states
are normal. It's what explains a text that arrived off the interval — or one
that didn't:

```
     nudge unanswered for 74 min, follow-up at 60 min (already sent)
```

The replies line is a live check, not a reading of the configuration: it counts
the tokens of a one-word message, which authenticates exactly like a real
request but costs nothing. This matters because the SDK builds a client happily
with no credentials at all — so a key that is missing, expired, or scoped
without access to the model looks fine until the first reply arrives and is
quietly read literally. A `FAIL` here names the remedy:

```
  FAIL  replies pattern matching: no credentials, set ANTHROPIC_API_KEY
  FAIL  replies pattern matching: credentials rejected, set ANTHROPIC_API_KEY
  FAIL  replies pattern matching: no access to model 'claude-opus-5'
```

`WATER_LLM=off` is a pass, not a fault — it's a choice, and pattern matching
still covers the phrasings listed under [Texting it back](#texting-it-back).

| Symptom | Cause |
|---|---|
| no reminders at all | loop isn't running — nothing keeps it alive without the LaunchAgent |
| reminders stop after closing the laptop | fixed: the interval is wall-clock, not `time.monotonic()`, which pauses during sleep |
| reminders arrive, replies ignored | Full Disk Access, per the notes above |
| sends fail | handle isn't deliverable, or Automation was never approved |
| log file empty | plist predates `PYTHONUNBUFFERED=1`; re-run `install` |
| replies understood only literally | Claude is unreachable — `doctor` checks for real and names the cause |
| agent lost interpretation, terminal has it | launchd inherits nothing; re-run `install` with the key exported |
| a second text an hour after the first | the follow-up; `WATER_FOLLOWUP_MIN=0` turns it off |
| no follow-up ever arrives | you're replying (which cancels it), or far enough behind that the paced nudge gets there first |

The agent logs to `~/Library/Logs/watertracker.log`.

---

## How AI was used

AI is in this project twice over, and the two are worth separating:

1. **It wrote the code.** Every line was generated by Claude under my
   direction — see below.
2. **It runs inside the code.** The tracker calls the Claude API at runtime to
   understand your text messages. That's a dependency you're choosing when you
   set a key, not a build-time detail.

### As a runtime dependency

When `ANTHROPIC_API_KEY` is set and the `anthropic` package is installed, each
reply you text is sent to the Anthropic API to be interpreted. Things to know:

- **What leaves your machine:** the text of your replies, and nothing else.
  Your intake log stays local. The prompt contains no history — one message per
  call.
- **What it can and can't do:** it picks an action and estimates an amount. It
  never supplies the numbers you see; those are read from your log. The worst a
  misread can do is add one wrong entry, which `undo` removes.
- **Cost:** one small call per message that isn't already a plain amount —
  those are answered locally and cost nothing. It defaults to
  `claude-haiku-4-5`, which is the cheapest and fastest model and plenty for
  reading one short text; `WATER_MODEL` takes any Claude model, and
  `WATER_LLM=off` turns the whole thing off.
- **It is optional.** Everything works without it. That's deliberate: a
  hydration reminder that stops answering because a key expired isn't much of a
  reminder.

### As the author of this code

**Every line of code in this repository was written by [Claude
Code](https://claude.com/claude-code) (Anthropic's agentic CLI, running Claude
Opus 5) under my direction.** I did not hand-write the implementation. This
section is here so nobody has to guess at that.

### What that means concretely

| | |
|---|---|
| **AI wrote** | all of `waterTracker.py` and `test_waterTracker.py`, part of the README, and co-authored commits |
| **I did** | the idea and requirements, the design decisions, testing on real hardware, and the bug reports that drove most of the fixes, part of the README, and co-authored commits |

Commits are marked with a `Co-Authored-By: Claude` trailer, so the record is in
`git log` as well as here.

### The process

1. **Initial build** — I described what I wanted: something that texts my phone
   on a schedule and logs what I text back. Claude Code wrote the first working
   version.
2. **Field testing by me** — I ran it on my own machine and phone and reported
   what actually went wrong, in plain terms: *"I'm not receiving the 2 hour text
   messages"*, *"message failed to send"*, *"it doesn't send replies after I
   send my message"*.
3. **Diagnosis and fixes** — each report turned into a root cause and a fix.
   These were the real ones:
   - The loop only lived as long as the terminal that started it → LaunchAgent.
   - The nudge interval used `time.monotonic()`, which stops while the Mac
     sleeps, so a laptop that napped through the afternoon never nudged again.
   - Any failure reading the Messages database skipped the reminder step on
     every pass, silently killing reminders while the loop looked healthy.
   - The state file was rewritten in place three times a minute; an interrupted
     write left invalid JSON that startup could not tell apart from *no
     history*, and it then overwrote the only copy.
   - The configured phone number was `+1...`, a placeholder pasted out of the
     AI's own setup instructions. Nothing validated it.
   - Full Disk Access was listed but switched **off**, and the AI's own health
     check reported access as fine because it was testing its own process
     rather than the background job's.
4. **Requested improvements** — I asked for longer/natural-language replies, then
   asked what else it would improve and told it to do all of them. Those became
   nine commits, one per improvement, at my request.
5. **Then I redirected the approach.** Widening the pattern matching was my
   first ask; partway through I said to use an LLM to read the messages
   instead, which is a better answer to "let me text whatever I want" than any
   number of new rules. The patterns stayed as the fallback.

### Where the human judgment actually mattered

Worth being precise, because the split isn't "AI wrote code, human watched":

- **Deployment reality.** Most of the serious bugs — the dead loop, the
  placeholder number, the denied permission — only appeared on real hardware
  with a real phone. They came from me using it and saying it didn't work, not
  from the AI reasoning about its own code.
- **Scope and approach.** I decided what to build, which improvements were
  worth doing, how to commit them, and when to change tack — the switch from
  hand-written patterns to an LLM was my call, not the AI's.
- **Verification.** The AI checked its own work with tests and mutation runs,
  but I'm the one who confirmed a text actually arrived on my phone.

### If you're evaluating this code

Read `doctor` and the comments around the self-chat guards and the WAL sidecar
check. Those explain the non-obvious constraints — protected databases,
per-process permissions, a chat log that echoes your own messages back — and
they're where the reasoning behind the design lives.

Earlier history for this project lives in the repo it was extracted from:
[AI-ML-Models-and-Practice-](https://github.com/26lenkalaa/AI-ML-Models-and-Practice-)
(`claudeCode/waterTracker.py`, 18 commits).
