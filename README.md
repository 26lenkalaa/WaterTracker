# WaterTracker

Texts you water reminders over iMessage and logs the replies you text back. It
runs on your Mac, talks to Messages, and keeps no account and no server — your
intake log is a JSON file on your own disk.

```
You  22:14   💧 Water break. ███░░░░░░░ 32/100 oz (32%) 28 oz behind pace.
              Reply with an amount to log it.
Me   22:16   just had a couple glasses
You  22:16   💧 Logged 16 oz. ████░░░░░░ 48/100 oz (48%) — 52 oz to go.
```

Replies are read as sentences, so `drank 500ml and a bottle at the gym` and
`ok you can stop for today` both work.

---

## Requirements

- macOS with Messages signed in
- Python 3.10+ (uses `X | None` type syntax)
- No third-party packages — standard library only

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

`status`, `week` and `log` don't need `WATER_PHONE` — only sending does.

### Texting it back

| You text | It does |
|---|---|
| `16` / `16oz` / `2 cups` / `500ml` | logs that amount |
| `just had a couple glasses` | logs 16 oz |
| `drank 500ml and a bottle at the gym` | logs both, summed |
| `half a liter` | logs 16.9 oz |
| `status` / `?` / `how much so far` | today's progress |
| `week` / `my weekly average` | last 7 days |
| `goal 120` / `set my goal to 120oz` | changes the daily goal |
| `undo` / `scratch that` | drops the last entry |
| `pause` / `resume` | stops or restarts today's nudges |
| `had 20 oz, you can stop for today` | logs **and** pauses |

Units: `oz`, `cup`/`glass` (8 oz), `bottle` (16.9 oz), `ml`, `l`/`liter`.
Amounts over 400 oz are rejected, and a bare number in a long sentence is
ignored — `I'll drink some at 16:00 after my 3 meetings` logs nothing.

### Settings

All optional except the phone number.

| Variable | Default | Meaning |
|---|---|---|
| `WATER_PHONE` | — | iMessage handle to text (required to send) |
| `WATER_GOAL_OZ` | 100 | daily goal in fluid ounces |
| `WATER_INTERVAL_MIN` | 120 | base spacing between nudges |
| `WATER_WAKE_HOUR` | 8 | no nudges before this hour |
| `WATER_SLEEP_HOUR` | 22 | no nudges after this hour |
| `WATER_POLL_SECONDS` | 20 | how often replies are checked |
| `WATER_SEND_TIMEOUT` | 60 | seconds before a stuck send gives up |
| `WATER_KEEP_DAYS` | 90 | how long history is kept |
| `WATER_STALE_REPLY_MIN` | 60 | ignore replies older than this |
| `WATER_STATE_FILE` | `water_tracker_state.json` | where the log lives |

Nudges are **paced**: the gap stretches to 1.5× the interval when you're ahead
of an even pace for the time of day and tightens toward half when you're behind.
A fixed interval nudges the same whether you're 5 oz or 50 oz short.

## How it works

```
LaunchAgent ─→ waterTracker.py run ─┬─→ osascript ─→ Messages.app     (sending)
                                    └─→ copy of chat.db ─→ sqlite3    (reading)
                                              ↓
                                  water_tracker_state.json
```

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
- **Self-chat** is the messy case. Texting your own number makes Messages log
  every outgoing text a second time as an *incoming* row, so the tracker can
  read its own reminders as replies and answer them forever. Two guards: each
  send is remembered with a timestamp for a couple of minutes, and every
  outgoing message starts with 💧, so a copy read back hours later is still
  recognisable. That second guard matters because its own progress bar parses
  as a valid amount — `0/100 oz` reads as 100 oz.

## Tests

```bash
python3 -m unittest discover .        # 57 tests, ~0.04s
```

No network, no Messages access, no real state file: sends are captured in a
list and the state path is redirected to a temp directory. The reply path runs
against a stand-in `chat.db` built in WAL mode with the connection held open,
the way Messages runs it, covering handle formats, outgoing rows,
`attributedBody` decoding, echo suppression, stale replies and priming.

The tests were checked by mutation — deliberately reintroducing each bug to
confirm the suite fails. Reverting the bare-number guard, the corrupt-file
quarantine, the `-wal` sidecar check, the marker guard or the stale-reply guard
each breaks it.

## Troubleshooting

Run `doctor` first; it checks the things that fail silently and prints the
state that makes silence *correct*, like a met goal or a pause.

```
$ python3 waterTracker.py doctor
Water tracker checkup
  ok    texting +1555..., from the LaunchAgent
  ok    this python can read Messages history, so replies are picked up
  ok    agent log clean since it started
  ok    LaunchAgent loaded
  ok    loop running (pid 10538)
  ok    no nudge due: it is outside 8:00-22:00
        today 0/100 oz
```

| Symptom | Cause |
|---|---|
| no reminders at all | loop isn't running — nothing keeps it alive without the LaunchAgent |
| reminders stop after closing the laptop | fixed: the interval is wall-clock, not `time.monotonic()`, which pauses during sleep |
| reminders arrive, replies ignored | Full Disk Access, per the notes above |
| sends fail | handle isn't deliverable, or Automation was never approved |
| log file empty | plist predates `PYTHONUNBUFFERED=1`; re-run `install` |

The agent logs to `~/Library/Logs/watertracker.log`.

---

## How AI was used

**Every line of code in this repository was written by [Claude
Code](https://claude.com/claude-code) (Anthropic's agentic CLI, running Claude
Opus 5) under my direction.** I did not hand-write the implementation. This
section is here so nobody has to guess at that.

### What that means concretely

| | |
|---|---|
| **AI wrote** | all of `waterTracker.py` (990 lines) and `test_waterTracker.py` (581 lines), this README, and every commit message |
| **I did** | the idea and requirements, the design decisions, testing on real hardware, and the bug reports that drove most of the fixes |

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

### Where the human judgment actually mattered

Worth being precise, because the split isn't "AI wrote code, human watched":

- **Deployment reality.** Most of the serious bugs — the dead loop, the
  placeholder number, the denied permission — only appeared on real hardware
  with a real phone. They came from me using it and saying it didn't work, not
  from the AI reasoning about its own code.
- **Scope.** I decided what to build, which improvements were worth doing, and
  how to commit them.
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
