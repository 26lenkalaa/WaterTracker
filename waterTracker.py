"""Text yourself water reminders over iMessage and log the replies you send back.

Setup:
	export WATER_PHONE="+15551234567"   # the iMessage handle to text, for run/test
	export WATER_GOAL_OZ="100"          # optional daily goal in oz, default 100
	export WATER_INTERVAL_MIN="120"     # optional nudge spacing, default 120
	export WATER_WAKE_HOUR="8"          # optional, no nudges before this hour
	export WATER_SLEEP_HOUR="22"        # optional, no nudges after this hour
	export WATER_KEEP_DAYS="90"         # optional, how long history is kept
	export WATER_STALE_REPLY_MIN="60"   # optional, ignore replies older than this

	Claude reads your replies when it can, which is what lets you text whatever
	you like instead of a fixed vocabulary. It needs `pip install anthropic` and
	credentials (ANTHROPIC_API_KEY, or an `ant auth login` profile); without
	either, pattern matching handles the common phrasings on its own.

	export ANTHROPIC_API_KEY="sk-ant-..."   # enables interpretation
	export WATER_LLM="off"                  # optional, patterns only
	export WATER_MODEL="claude-opus-5"      # optional, any Claude model
	export WATER_DEFAULT_OZ="8"             # optional, a bare "done" logs this

	Your replies are sent to the Anthropic API to be interpreted. Nothing else
	is: your intake log stays on this machine, and every number in a reply is
	read from that log rather than written by the model.

	Sending needs Automation access for Messages; macOS prompts on the first send.
	Reading your replies needs Full Disk Access, because the Messages database is
	protected: System Settings > Privacy & Security > Full Disk Access, then add
	the terminal you run this from and restart it. Without it, reminders still
	send and you can log from the command line instead.

	That grant follows whichever process starts python, so a terminal's access
	is inherited by a run started from it but not by the LaunchAgent: the agent
	needs its own interpreter added. 'doctor' reads the agent's log and reports
	what the agent itself found.

	python waterTracker.py            # run the reminder loop
	python waterTracker.py status     # print today's intake
	python waterTracker.py week 7     # print recent days and the average
	python waterTracker.py log 16     # log 16 oz without texting
	python waterTracker.py test       # send one text to check delivery
	python waterTracker.py doctor     # explain why reminders are not arriving
	python waterTracker.py install    # keep the loop running via launchd
	python -m unittest discover .     # run the tests

	Reminders only go out while the loop is running, so closing the terminal
	stops them. 'install' writes a LaunchAgent that starts it at login and
	restarts it if it dies; grant Full Disk Access to the python binary it
	points at, not just your terminal, or replies will be ignored. 'doctor'
	checks all of that and prints what it finds.

	Nudges are spaced by WATER_INTERVAL_MIN, stretched when you are ahead of an
	even pace for the time of day and tightened when you are behind.

Replies can be whole sentences — amounts and commands are picked out of the text:
	16 / 16oz / 2 cups / 500ml         log that amount
	"just had a couple glasses"        log 16 oz
	"drank 500ml and a bottle"         log both, summed
	status / ? / "how much so far"     get today's progress
	week / "my weekly average"         get the last 7 days
	goal 120 / "set my goal to 120oz"  change the daily goal
	undo / "scratch that"              drop the last entry
	pause / resume                     stop or restart today's nudges
"""

import base64
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

GOAL_OZ = float(os.getenv("WATER_GOAL_OZ", "100"))
INTERVAL_MIN = int(os.getenv("WATER_INTERVAL_MIN", "120"))
WAKE_HOUR = int(os.getenv("WATER_WAKE_HOUR", "8"))
SLEEP_HOUR = int(os.getenv("WATER_SLEEP_HOUR", "22"))
# The dominant part of the wait for a reply, since everything after it is
# milliseconds: a poll that finds nothing costs three stat() calls, and only a
# changed database is copied. At 20s this was averaging 10s of dead air before
# a reply was even noticed, which was more than the model ever took.
POLL_SECONDS = int(os.getenv("WATER_POLL_SECONDS", "3"))
SEND_TIMEOUT = int(os.getenv("WATER_SEND_TIMEOUT", "60"))
KEEP_DAYS = int(os.getenv("WATER_KEEP_DAYS", "90"))
STALE_REPLY_MIN = int(os.getenv("WATER_STALE_REPLY_MIN", "60"))
# A nudge nobody answered gets one follow-up after this long. The usual way a
# reminder fails is not disagreement: the text is read while doing something
# else and never answered, and the next nudge is hours out. 0 turns it off.
FOLLOWUP_MIN = int(os.getenv("WATER_FOLLOWUP_MIN", "60"))
# How late that follow-up may be and still be worth sending. It is allowed to
# land after SLEEP_HOUR, because finishing an exchange the nudge started is not
# the same as starting one — but only just after. A Mac asleep at the moment
# one came due must not wake up and chase last night's nudge over breakfast.
FOLLOWUP_GRACE_MIN = int(os.getenv("WATER_FOLLOWUP_GRACE_MIN", "30"))
STATE_PATH = Path(os.getenv("WATER_STATE_FILE", "water_tracker_state.json"))
CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"

# A reply that only confirms drinking, with no amount, counts as this much.
DEFAULT_SERVING_OZ = float(os.getenv("WATER_DEFAULT_OZ", "8"))

# Claude reads each reply and says what it meant, which is how "finished the
# one on my desk before heading out" becomes an amount without anyone writing
# a pattern for it. Optional in every sense: the pattern matching below still
# runs whenever the model is unreachable, so no key, no network, or no
# package all degrade to the behaviour that existed before this.
LLM_MODEL = os.getenv("WATER_MODEL", "claude-haiku-4-5")
# Short, because a person is waiting on the answer with their phone in hand and
# the pattern matching below is instant. Retries are off for the same reason
# (see llm_plan), so this is the whole worst case rather than a third of it.
LLM_TIMEOUT = float(os.getenv("WATER_LLM_TIMEOUT", "8"))
LLM_MODE = os.getenv("WATER_LLM", "auto").lower()  # auto or off
# Thinking depth, for the models that take it. Left unset because Haiku 4.5 is
# the default here and rejects output_config.effort with a 400; point
# WATER_MODEL at an Opus or Sonnet model and WATER_EFFORT=low gets the same
# cheap, shallow read this used to ask Opus for.
LLM_EFFORT = os.getenv("WATER_EFFORT", "").strip().lower()
# Claude writes the wording for conversation only. Every number in a reply
# comes from the log, because a model that invents your intake is worse than
# no tracker: it would be confidently wrong about the one thing being counted.
MAX_CHAT_CHARS = 300

try:
	import anthropic
except ModuleNotFoundError:  # pip install anthropic to turn interpretation on
	anthropic = None

# How long a message we sent stays recognisable as our own self-chat echo.
# Comfortably more than a poll, or the echo would arrive after its record had
# expired and get answered as though it were a reply.
ECHO_WINDOW_SEC = max(120, POLL_SECONDS * 3)

# Every message we send starts with this, and an incoming one that starts with
# it is our own. Timestamped echo records handle the normal case; this catches
# a copy read back long afterwards, which matters because our own progress bar
# ("0/100 oz") parses as a perfectly good amount.
MARKER = "\U0001f4a7"

# Messages stores dates against Apple's own epoch, in seconds on older rows and
# nanoseconds on newer ones.
APPLE_EPOCH = 978307200

# Everything is normalised to fluid ounces. Containers are here because people
# text what they drank out of, not a measurement: "a can", "my nalgene".
UNITS = {
	"": 1.0,
	"oz": 1.0,
	"ounce": 1.0,
	"ounces": 1.0,
	"cup": 8.0,
	"cups": 8.0,
	"glass": 8.0,
	"glasses": 8.0,
	"bottle": 16.9,
	"bottles": 16.9,
	"water bottle": 16.9,
	"water bottles": 16.9,
	"ml": 0.033814,
	"l": 33.814,
	"liter": 33.814,
	"liters": 33.814,
	"litre": 33.814,
	"litres": 33.814,
	"sip": 1.5,
	"sips": 1.5,
	"gulp": 2.0,
	"gulps": 2.0,
	"mug": 10.0,
	"mugs": 10.0,
	"can": 12.0,
	"cans": 12.0,
	"pint": 16.0,
	"pints": 16.0,
	"tumbler": 20.0,
	"tumblers": 20.0,
	"shaker": 24.0,
	"shakers": 24.0,
	"nalgene": 32.0,
	"nalgenes": 32.0,
	"hydroflask": 32.0,
	"hydro flask": 32.0,
	"quart": 32.0,
	"quarts": 32.0,
	"jug": 64.0,
	"jugs": 64.0,
	"gallon": 128.0,
	"gallons": 128.0,
}

# The container words worth offering a vision model, singular only. Measures are
# left out: "oz" and "ml" are not things you can point a camera at, and "sip" and
# "gulp" describe a mouthful rather than the object being photographed. Naming
# these in the prompt is what lets a recognised container resolve to the table's
# figure instead of the model's guess at it.
UNITS_FOR_PROMPT = frozenset({
	"cup", "glass", "bottle", "water bottle", "mug", "can", "pint",
	"tumbler", "shaker", "nalgene", "hydroflask", "quart", "jug", "gallon",
})

# argv keeps the phone number and body out of the script source, so a reply
# containing quotes or AppleScript syntax cannot escape into the command.
SEND_SCRIPT = """
on run argv
	tell application "Messages"
		set svc to 1st account whose service type = iMessage
		send (item 2 of argv) to participant (item 1 of argv) of svc
	end tell
end run
"""

NUDGES = [
	"Time to drink water.",
	"Water break.",
	"Hydrate.",
	"Grab a glass of water.",
	"Sip check.",
]


# Replies are usually sentences rather than bare amounts, so quantities and
# commands are scanned out of free text instead of matched against it whole.
NUMBER_WORDS = {
	"a": 1.0,
	"an": 1.0,
	"one": 1.0,
	"two": 2.0,
	"three": 3.0,
	"four": 4.0,
	"five": 5.0,
	"six": 6.0,
	"seven": 7.0,
	"eight": 8.0,
	"nine": 9.0,
	"ten": 10.0,
	"eleven": 11.0,
	"twelve": 12.0,
	"thirteen": 13.0,
	"fourteen": 14.0,
	"fifteen": 15.0,
	"sixteen": 16.0,
	"seventeen": 17.0,
	"eighteen": 18.0,
	"nineteen": 19.0,
	"twenty": 20.0,
	"thirty": 30.0,
	"forty": 40.0,
	"fifty": 50.0,
	"sixty": 60.0,
	"seventy": 70.0,
	"eighty": 80.0,
	"ninety": 90.0,
	"hundred": 100.0,
	"couple": 2.0,
	"few": 3.0,
	"several": 4.0,
	"dozen": 12.0,
	"half": 0.5,
	"quarter": 0.25,
	"three quarters": 0.75,
	# Determiners standing in for "one of": "my nalgene", "another glass".
	# "the" is deliberately absent — "the bottle is empty" is not a drink.
	"my": 1.0,
	"another": 1.0,
}
# "third" is deliberately absent: "my third bottle" means the third one, not a
# third of one, and there is no way to tell those apart here.

_TENS = ("twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_ONES = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
# Longest alternative first, so "liters" wins over "l" and "cups" over "cup".
_UNIT_RE = "|".join(sorted((unit for unit in UNITS if unit), key=len, reverse=True))
_WORDS_RE = "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
_QTY_RE = (
	# Compounds before single words, so "twenty five" is 25 and not 20 then 5.
	rf"(?:{'|'.join(_ONES)}|a)[-\s]hundred(?:[-\s](?:{_WORDS_RE}))?"
	rf"|(?:{'|'.join(_TENS)})[-\s](?:{'|'.join(_ONES)})"
	r"|\d+\s*/\s*\d+"
	r"|\d+(?:\.\d+)?"
	rf"|{_WORDS_RE}"
)
AMOUNT_RE = re.compile(rf"(?<![\w.])({_QTY_RE})\s*(?:of\s+)?(?:an?\s+|my\s+|the\s+)?({_UNIT_RE})\b")
BARE_NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")
# "a glass and a half": the half belongs to whatever unit came before it.
AND_A_HALF_RE = re.compile(r"\band a half\b")
MAX_LOG_OZ = 400.0

# A negation ahead of the amount means it has not happened yet, so
# "haven't had 16 oz" must not log 16 oz.
NEGATION_RE = re.compile(r"\b(not|no|didn'?t|haven'?t|hasn'?t|won'?t|nope|forgot)\b")

# Checked in order, so "undo that" is an undo before "that" matters, and
# "I'm done for the day" stops reminders instead of logging a glass. Each is a
# search, not a match, to catch commands wrapped in a sentence.
INTENT_PATTERNS = (
	("undo", re.compile(
		r"\b(undo|scratch that|(remove|delete|drop|take)( that| the)?( back| last| one)?"
		r"|never ?mind|oops|my (bad|mistake)|that was wrong|wrong)\b"
	)),
	("week", re.compile(r"\b(week|weekly|last \d+ days|history|average|trend)\b")),
	("status", re.compile(
		r"\b(status|progress|total|how much|how many|where am i|how am i|how'?s my"
		r"|on track|am i (good|behind|ahead|close)|what'?s my count)\b|^\?+$"
	)),
	("pause", re.compile(
		r"\b(pause|snooze|quiet|shush|stop|leave me alone|going to bed|off to bed"
		r"|done for (the day|today)|no more (today|tonight)|busy)\b"
	)),
	("resume", re.compile(
		r"\b(resume|unpause|start again|back on|i'?m back|wake up)\b|^(go|start)$"
	)),
	# Softer than a pause: not now, ask me again next time round.
	("later", re.compile(
		r"\b(not yet|later|in a (bit|min|minute|sec|while)|hold on|soon|nope|no thanks"
		r"|didn'?t|haven'?t|hasn'?t|forgot)\b|^(no|nah)$"
	)),
	# Deliberately below every command above, and checked below the amount in
	# handle_reply: "had a glass this morning" is a log that happens to mention
	# the morning, not an announcement of waking up.
	("wake", re.compile(
		r"\b(awake|i'?m up|im up|just woke|woke up|good morning|gm)\b"
		r"|^(morning|awake|up)$"
	)),
	# No amount given, just confirmation that some water happened.
	("drank", re.compile(
		r"\b(done|did it|drank|drinking|drunk|finished|chugged|gulped|sipped|refill(ed)?"
		r"|topped off|another|one more|same again|got some|had some|yes|yep|yeah|yup|sure)\b"
		r"|^[\U0001f44d✅\U0001f964\U0001f4a6\U0001f6b0]+$"
	)),
)


# Words that add nothing to "I drank an amount". Anything outside this set,
# the number words, and the unit names sends the message to Claude, so the
# list can only ever be too cautious — never too eager.
FAST_PATH_FILLER = frozenset({
	"i", "im", "ive", "just", "had", "have", "has", "got", "drank", "drink",
	"drunk", "finished", "chugged", "downed", "of", "water", "and", "plus",
	"more", "another", "about", "roughly", "around", "approx", "like", "maybe",
	"so", "far", "total", "ok", "okay", "yep", "yes",
})
FAST_PATH_WORDS = FAST_PATH_FILLER | set(NUMBER_WORDS) | {unit for unit in UNITS if unit}
FAST_PATH = os.getenv("WATER_FAST_PATH", "on").strip().lower() not in ("0", "off", "no")

# Photos of a container, for the case the units table cannot cover: a bottle you
# own but have no name for. Off is a supported answer — this is the slow path by
# definition, since an image is no use to the pattern matching underneath.
PHOTOS = os.getenv("WATER_PHOTOS", "on").strip().lower() not in ("0", "off", "no")
# iPhone photos arrive as HEIC, which the API does not take, and at a resolution
# far past anything useful for "what kind of bottle is this". sips converts and
# downscales in one call.
PHOTO_MAX_PX = int(os.getenv("WATER_PHOTO_MAX_PX", "1024"))
PHOTO_TYPES = ("image/heic", "image/heif", "image/png", "image/jpeg", "image/webp", "image/gif")


def fast_path_ounces(text: str) -> float | None:
	"""Ounces from a message that says nothing but the amount, else None.

	"28 oz" is 28 oz — there is nothing for a model to add, and asking anyway
	spends a round trip on the most common reply there is while someone waits.
	So a message built only from amounts and filler is answered here, and
	everything else goes to Claude exactly as before.

	Deliberately strict: an unrecognised word is enough to defer. "had 20 oz,
	you can stop for today" has to reach the interpreter, because the amount is
	not the only thing it is asking for.
	"""
	if not FAST_PATH:
		return None
	ounces = extract_ounces(text)
	if ounces is None or amount_is_negated(text):
		return None
	# "drank" is the one intent that means no more than the amount already does.
	if detect_intent(text) not in (None, "drank"):
		return None
	if any(word not in FAST_PATH_WORDS for word in re.findall(r"[a-z']+", text)):
		return None
	return ounces


def quantity(raw: str) -> float:
	"""A quantity written as digits, a fraction, or words: 'twenty five' is 25."""
	if raw in NUMBER_WORDS:
		return NUMBER_WORDS[raw]
	if "/" in raw:
		top, _, bottom = raw.partition("/")
		return float(top.strip()) / float(bottom.strip())
	try:
		return float(raw)
	except ValueError:
		pass
	words = re.split(r"[-\s]+", raw)
	if "hundred" in words:
		# "two hundred" is 200, and a bare "hundred" is still 100.
		split = words.index("hundred")
		before = sum(NUMBER_WORDS.get(word, 0.0) for word in words[:split]) or 1.0
		after = sum(NUMBER_WORDS.get(word, 0.0) for word in words[split + 1:])
		return before * 100.0 + after
	return sum(NUMBER_WORDS.get(word, 0.0) for word in words)


def amount_is_negated(text: str) -> bool:
	"""True when a negation comes before the first amount.

	"haven't had 16 oz" is not 16 oz. A negation *after* the amount is a
	different sentence — "had 16 oz but not the second bottle" still counts.
	"""
	amount = AMOUNT_RE.search(text) or BARE_NUMBER_RE.search(text)
	negation = NEGATION_RE.search(text)
	return bool(amount and negation and negation.start() < amount.start())


def extract_ounces(text: str) -> float | None:
	"""Total fluid ounces mentioned anywhere in a reply, else None.

	Handles '16', '2 cups', '500 ml', 'half a liter', 'twenty five ounces',
	'3/4 of a bottle', 'a glass and a half', and sums every amount in a
	sentence like 'a bottle at the gym and 500ml after'.
	"""
	body = text.strip().lower()
	total = 0.0
	last_unit = None
	for match in AMOUNT_RE.finditer(body):
		last_unit = match.group(2)
		total += quantity(match.group(1)) * UNITS[last_unit]
	if last_unit and AND_A_HALF_RE.search(body):
		total += 0.5 * UNITS[last_unit]
	if not total:
		# No unit anywhere: trust a lone number ('16', 'just drank 16') but not
		# one buried in a longer sentence, where it is usually a time or a date.
		numbers = BARE_NUMBER_RE.findall(body)
		if len(numbers) != 1 or len(body.split()) > 6:
			return None
		total = float(numbers[0])
	total = round(total, 1)
	# Reject nonsense so a stray long number cannot wreck the day's total.
	return total if 0 < total <= MAX_LOG_OZ else None


def detect_intent(text: str) -> str | None:
	"""Command hidden in a longer reply, e.g. 'ok you can stop for today'."""
	for name, pattern in INTENT_PATTERNS:
		if pattern.search(text):
			return name
	return None


def decode_body(blob: bytes | None) -> str:
	"""Pull the plain text out of a message's attributedBody archive."""
	if not blob:
		return ""
	try:
		tail = blob.split(b"NSString")[1][5:]
		if tail[0] == 0x81:
			length = int.from_bytes(tail[1:3], "little")
			tail = tail[3:]
		else:
			length = tail[0]
			tail = tail[1:]
		return tail[:length].decode("utf-8", "replace").strip()
	except (IndexError, UnicodeDecodeError):
		return ""


def digits(handle: str) -> str:
	"""Last 10 digits of a phone number, for comparing handle formats."""
	return re.sub(r"\D", "", handle)[-10:]


PLAN_ACTIONS = ("log", "status", "week", "goal", "undo", "pause", "resume", "later", "wake", "chat")

PLAN_SCHEMA = {
	"type": "object",
	"properties": {
		"action": {"type": "string", "enum": list(PLAN_ACTIONS)},
		"ounces": {
			"type": ["number", "null"],
			"description": "Fluid ounces to log. Only for action 'log'.",
		},
		"goal_oz": {
			"type": ["number", "null"],
			"description": "New daily goal in fluid ounces. Only for action 'goal'.",
		},
		"chat": {
			"type": ["string", "null"],
			"description": "One or two sentences answering the message. Only for action 'chat'.",
		},
	},
	"required": ["action", "ounces", "goal_oz", "chat"],
	"additionalProperties": False,
}

INTERPRETER_PROMPT = f"""\
You read one text message sent to a water-intake tracker and report what the \
sender meant. The sender is texting their own tracker, casually, usually while \
doing something else.

Pick exactly one action:
- log: they drank something. Put the total in `ounces`, converted to US fluid \
ounces. Estimate sensibly from containers and vague amounts: a glass or cup 8, \
a mug 10, a can 12, a pint 16, a standard water bottle 16.9, a tumbler 20, a \
large bottle or nalgene 32, a jug 64, a sip 1.5, a gulp 2. A bare confirmation \
with no amount at all ("done", "yep", "just had some") is {DEFAULT_SERVING_OZ:g}. \
Any drink counts, not only water.
- status: they want today's progress.
- week: they want recent days, an average, or a trend.
- goal: they want to change the daily goal. Put it in `goal_oz`.
- undo: they want the last entry removed, including "oops" and "that was wrong".
- pause: they want reminders to stop for the day, including "going to bed".
- resume: they want reminders to start again.
- wake: they are telling you they just got up — "awake", "good morning", "just woke up". Today's pacing starts from now. Not for a message that merely mentions the morning while reporting a drink.
- later: they have not drunk anything yet and want to be asked again soon. Use \
this for "not yet", "in a bit", and for anything they say they did NOT drink.
- chat: none of the above. Put a friendly reply of at most two sentences in \
`chat`, in the voice of a terse, encouraging tracker.

Rules:
- A negation means it did not happen: "haven't had my 16 oz yet" is later, not log.
- Never invent numbers for status, week, or chat. The tracker fills those in.
- Prefer log when they clearly drank something, even if the amount is vague.
- Amounts above 400 oz are a mistake; treat those as chat and ask.
- Unused fields must be null."""

PHOTO_SCHEMA = {
	"type": "object",
	"properties": {
		"container": {
			"type": ["string", "null"],
			"description": (
				"What the container is, in one or two words, lowercase. Use one of "
				f"these words when it fits: {', '.join(sorted(UNITS_FOR_PROMPT))}. "
				"Null if the picture shows no drink container."
			),
		},
		"ounces": {
			"type": ["number", "null"],
			"description": (
				"How much the container holds when full, in US fluid ounces. Your "
				"best estimate of its capacity, not of how much is left in it. "
				"Null if you cannot tell."
			),
		},
		"note": {
			"type": ["string", "null"],
			"description": "One short clause on what you saw, or why you could not tell.",
		},
	},
	"required": ["container", "ounces", "note"],
	"additionalProperties": False,
}

PHOTO_PROMPT = """\
You are shown a photo sent to a water-intake tracker. The sender is telling you \
what they drank from, usually because it is a container they have no name for.

Report what the container is and how much it holds when full. Capacity is a \
property of the object, so estimate it from the kind of thing it is — a standard \
single-serve water bottle is about 17 oz, a pint glass 16, a large insulated \
flask 32, a mug 10, a soda can 12.

Do not estimate how much liquid is currently in it, and do not estimate how much \
the sender drank. You cannot see either reliably, and a wrong number there is \
worse than no number: say what the container is and let them correct the amount.

If the photo has no drink container in it, set container and ounces to null and \
say so in the note."""

_llm_client = None
_llm_broken = False


def llm_ready() -> bool:
	"""Whether interpretation should be attempted at all."""
	return LLM_MODE != "off" and anthropic is not None and not _llm_broken


def llm_client(quiet: bool = False):
	"""The shared client, or None when one cannot be built.

	The SDK resolves an API key, an auth token, or an `ant auth login` profile
	on its own, so this only has to notice that none of them worked. Note that
	construction succeeds with no credentials at all — that only surfaces on
	the first request, which is why llm_check() exists.
	"""
	global _llm_client, _llm_broken
	if _llm_client is None:
		try:
			_llm_client = anthropic.Anthropic(max_retries=1)
		except Exception as error:
			if not quiet:
				print(f"   Claude interpretation off ({error}); using pattern matching")
			_llm_broken = True
			return None
	return _llm_client


def llm_check() -> tuple[bool, str]:
	"""Whether interpretation will actually work, and how it is set up.

	Counting tokens authenticates exactly like a real request but is free, so
	this answers "will Claude read my replies" rather than the much weaker
	"is a key configured". Worth the round trip: the SDK builds a client
	happily with no credentials, so everything looks fine until the first
	message arrives and quietly falls back.
	"""
	if LLM_MODE == "off":
		return True, "pattern matching only (WATER_LLM=off)"
	if anthropic is None:
		return False, "pattern matching: the anthropic package is not installed"
	client = llm_client(quiet=True)
	if client is None:
		return False, "pattern matching: no credentials the SDK can find"
	try:
		client.with_options(timeout=LLM_TIMEOUT).messages.count_tokens(
			model=LLM_MODEL, messages=[{"role": "user", "content": "ping"}]
		)
	except anthropic.AuthenticationError:
		return False, "pattern matching: credentials rejected, set ANTHROPIC_API_KEY"
	except anthropic.PermissionDeniedError:
		return False, "pattern matching: those credentials lack permission"
	except anthropic.NotFoundError:
		return False, f"pattern matching: no access to model {LLM_MODEL!r}"
	except anthropic.APIConnectionError:
		return False, "pattern matching: cannot reach the API right now"
	except anthropic.APIStatusError as error:
		return False, f"pattern matching: API error {error.status_code}"
	except TypeError:
		# What the SDK raises at request time when it could not resolve
		# credentials from anywhere. This is the ordinary "no key set" case, so
		# it gets the remedy rather than a stack-trace-shaped sentence. Only
		# the arguments above reach it, and they are fixed and correct, so a
		# TypeError here is about authentication and nothing else.
		return False, "pattern matching: no credentials, set ANTHROPIC_API_KEY"
	except Exception as error:
		return False, f"pattern matching: {type(error).__name__}: {error}"
	return True, f"read by {LLM_MODEL}, with pattern matching as the fallback"


def normalise_photo(path: Path) -> bytes | None:
	"""A photo as JPEG bytes the API will accept, or None if it cannot be.

	Two problems at once. iPhone photos are HEIC, which the API does not take,
	and they are several megabytes at a resolution far beyond what "what kind of
	bottle is this" needs. sips is in the base system and fixes both in one
	pass, so nothing has to be installed to make photos work.
	"""
	try:
		with tempfile.TemporaryDirectory() as tmp:
			out = Path(tmp) / "photo.jpg"
			done = subprocess.run(
				["sips", "-s", "format", "jpeg", "-Z", str(PHOTO_MAX_PX),
				 str(path), "--out", str(out)],
				capture_output=True, text=True, timeout=20,
			)
			if done.returncode != 0 or not out.exists():
				detail = done.stderr.strip().splitlines()
				print(f"   could not convert {path.name}: {detail[-1] if detail else 'sips failed'}")
				return None
			return out.read_bytes()
	except subprocess.TimeoutExpired:
		print(f"   gave up converting {path.name} after 20s")
		return None
	except OSError as error:
		print(f"   could not read {path.name}: {error}")
		return None


def llm_photo_plan(path: Path) -> dict | None:
	"""Ask Claude what container is in a photo. None whenever it cannot say."""
	if not llm_ready() or not PHOTOS:
		return None
	client = llm_client()
	if client is None:
		return None
	jpeg = normalise_photo(path)
	if jpeg is None:
		return None
	block = {
		"type": "image",
		"source": {
			"type": "base64",
			"media_type": "image/jpeg",
			"data": base64.standard_b64encode(jpeg).decode("ascii"),
		},
	}
	output_config: dict = {"format": {"type": "json_schema", "schema": PHOTO_SCHEMA}}
	if LLM_EFFORT:
		output_config["effort"] = LLM_EFFORT
	try:
		response = client.with_options(timeout=LLM_TIMEOUT * 2, max_retries=0).messages.create(
			model=LLM_MODEL,
			max_tokens=512,
			system=PHOTO_PROMPT,
			# A longer timeout than the text path: an image is a far bigger
			# request, and there is no pattern matching to fall back to, so
			# giving up early here just loses the message.
			messages=[{"role": "user", "content": [block]}],
			output_config=output_config,
		)
	except anthropic.AuthenticationError:
		print("   photo reading failed: credentials rejected")
		_disable_llm()
		return None
	except anthropic.NotFoundError:
		print(f"   photo reading failed: no access to model {LLM_MODEL!r}")
		_disable_llm()
		return None
	except anthropic.BadRequestError as error:
		# The likely one: a model without vision, or an image the API rejects.
		print(f"   photo reading failed: bad request ({error.message})")
		return None
	except anthropic.APITimeoutError:
		print(f"   photo reading failed: no answer in {LLM_TIMEOUT * 2:g}s")
		return None
	except anthropic.APIConnectionError:
		print("   photo reading failed: network unreachable")
		return None
	except anthropic.APIStatusError as error:
		print(f"   photo reading failed: API error {error.status_code}")
		return None
	except Exception as error:
		print(f"   photo reading failed: {type(error).__name__}: {error}")
		return None
	if response.stop_reason == "refusal":
		print("   Claude declined to read that photo")
		return None
	try:
		answer = next(b.text for b in response.content if b.type == "text")
		return json.loads(answer)
	except (StopIteration, json.JSONDecodeError, TypeError, AttributeError) as error:
		print(f"   photo answer unusable: {error}")
		return None


def photo_amount(plan: dict) -> tuple[float, str] | None:
	"""Ounces and how they were arrived at, or None if the photo said nothing.

	The units table wins wherever it recognises the container, so a photo of a
	nalgene logs the same 32 oz that typing "nalgene" would. Only a container
	the table has never heard of falls back to the model's own figure, and the
	caller says which of the two happened so a wrong one can be corrected.
	"""
	container = (plan.get("container") or "").strip().lower()
	singular = container.rstrip("s")
	# Matched against the container words only, never the whole units table.
	# That table also carries bare measures, and an empty key standing for "a
	# number with no unit means ounces" — which matched a photo with no
	# container in it at all and logged one ounce for a picture of nothing.
	if container and (container in UNITS_FOR_PROMPT or singular in UNITS_FOR_PROMPT):
		known = UNITS.get(container) or UNITS.get(singular)
		if known:
			return known, f"read that as a {container}"
	ounces = plan.get("ounces")
	if not isinstance(ounces, (int, float)) or not 0 < ounces <= MAX_LOG_OZ:
		return None
	label = container or "that"
	return round(float(ounces), 1), f"guessed {label} holds about {ounces:g} oz"


def sane_plan(plan: dict) -> dict | None:
	"""Check a model's answer before acting on it.

	Structured output guarantees the shape, not the sense: an action still has
	to come with the field it needs, and an amount still has to be plausible.
	"""
	action = plan.get("action")
	if action not in PLAN_ACTIONS:
		return None
	if action == "log":
		ounces = plan.get("ounces")
		if not isinstance(ounces, (int, float)) or not 0 < ounces <= MAX_LOG_OZ:
			return None
		plan["ounces"] = round(float(ounces), 1)
	if action == "goal":
		goal = plan.get("goal_oz")
		if not isinstance(goal, (int, float)) or not 0 < goal <= MAX_LOG_OZ:
			return None
		plan["goal_oz"] = round(float(goal), 1)
	if action == "chat":
		chat = (plan.get("chat") or "").strip()
		if not chat:
			return None
		plan["chat"] = chat[:MAX_CHAT_CHARS]
	return plan


def llm_plan(text: str) -> dict | None:
	"""Ask Claude what a reply meant. None whenever that cannot be answered."""
	if not llm_ready():
		return None
	client = llm_client()
	if client is None:
		return None
	# Structured output only. No thinking is asked for: reading one short text
	# into one of nine actions is not reasoning work, the default model does not
	# think unless told to, and a person is waiting on the answer. (The failure
	# modes of switching thinking *off* — a tool call written into visible text,
	# leaked tags — are specific to the Opus family and to tool use, and this
	# call uses neither.) Retries are off too: the SDK would double the wait on
	# a timeout, and falling back to pattern matching is faster than a retry.
	output_config: dict = {"format": {"type": "json_schema", "schema": PLAN_SCHEMA}}
	if LLM_EFFORT:
		output_config["effort"] = LLM_EFFORT
	try:
		response = client.with_options(timeout=LLM_TIMEOUT, max_retries=0).messages.create(
			model=LLM_MODEL,
			max_tokens=512,
			system=INTERPRETER_PROMPT,
			messages=[{"role": "user", "content": text}],
			output_config=output_config,
		)
	except anthropic.AuthenticationError:
		print("   Claude interpretation failed: credentials rejected")
		_disable_llm()
		return None
	except anthropic.NotFoundError:
		print(f"   Claude interpretation failed: no access to model {LLM_MODEL!r}")
		_disable_llm()
		return None
	except anthropic.BadRequestError as error:
		print(f"   Claude interpretation failed: bad request ({error.message})")
		_disable_llm()
		return None
	except anthropic.RateLimitError:
		print("   Claude interpretation failed: rate limited")
		return None
	except anthropic.APITimeoutError:
		print(f"   Claude interpretation failed: no answer in {LLM_TIMEOUT:g}s")
		return None
	except anthropic.APIConnectionError:
		print("   Claude interpretation failed: network unreachable")
		return None
	except anthropic.APIStatusError as error:
		print(f"   Claude interpretation failed: API error {error.status_code}")
		return None
	except Exception as error:
		# Anything else, including the TypeError the SDK raises at request time
		# when it cannot resolve credentials. Falling back is the whole promise
		# of this layer, so nothing here may reach the caller: an exception
		# would leave the message unanswered and its row already consumed.
		print(f"   Claude interpretation failed: {type(error).__name__}: {error}")
		_disable_llm()
		return None
	if response.stop_reason == "refusal":
		print("   Claude declined to interpret that message")
		return None
	try:
		answer = next(block.text for block in response.content if block.type == "text")
		return sane_plan(json.loads(answer))
	except (StopIteration, json.JSONDecodeError, TypeError, AttributeError) as error:
		print(f"   Claude interpretation unusable: {error}")
		return None


def _disable_llm() -> None:
	"""Stop trying after a failure that will repeat on every message."""
	global _llm_broken
	_llm_broken = True
	print("   falling back to pattern matching for the rest of this run")


def clock(hour: float) -> str:
	"""An hour-as-float rendered as a time: 7.5 becomes '7:30'."""
	whole = int(hour)
	return f"{whole}:{round((hour - whole) * 60):02d}"


def message_time(raw: int | None) -> float | None:
	"""Unix seconds for a Messages timestamp, None when there is none."""
	if not raw:
		return None
	seconds = raw / 1_000_000_000 if raw > 10**11 else raw
	return seconds + APPLE_EPOCH


def looks_like_handle(handle: str | None) -> bool:
	"""True if this could be an iMessage address: a phone number or an email.

	Worth checking because a bad handle fails inside Messages, where the only
	symptom is reminders that never arrive. "+1..." pasted out of a setup
	example is the case that prompted this.
	"""
	if not handle:
		return False
	if "@" in handle:
		name, _, domain = handle.partition("@")
		return bool(name) and "." in domain
	return len(re.sub(r"\D", "", handle)) >= 10


def load_state(quarantine: bool = True) -> dict | None:
	"""Read the saved log. {} when there is none, None when it is unreadable.

	A half-written file used to be indistinguishable from having no history at
	all: the empty state was saved straight back over the only copy, so every
	logged ounce was gone. The bad file is moved aside instead of overwritten.
	"""
	try:
		return json.loads(STATE_PATH.read_text())
	except FileNotFoundError:
		return {}
	except json.JSONDecodeError as error:
		if not quarantine:
			return None
		spoiled = STATE_PATH.with_name(STATE_PATH.name + ".corrupt")
		STATE_PATH.replace(spoiled)
		print(f"State file was unreadable ({error}).\nKept it at {spoiled}; starting a fresh log.")
		return {}


class WaterTracker:
	def __init__(self) -> None:
		# Not required here: status, week and log only touch the state file, and
		# needing a phone number to read your own intake is pure friction.
		self.phone = os.getenv("WATER_PHONE")
		self.state = load_state() or {}
		self.state.setdefault("goal_oz", GOAL_OZ)
		self.state.setdefault("days", {})
		self.state.setdefault("last_rowid", None)
		self.state.setdefault("paused_on", None)
		self.state.setdefault("congratulated_on", None)
		self.state.setdefault("sent_echoes", [])
		self.state.setdefault("last_nudge_at", 0.0)
		self.state.setdefault("pruned_on", None)
		# When the outstanding nudge went out, or None once it has been
		# answered. Kept in state so a restart mid-wait neither forgets an
		# unanswered nudge nor follows up on one twice.
		self.state.setdefault("awaiting_reply_since", None)
		self.state.setdefault("followed_up", False)
		# When today actually began, ISO, or None to fall back to WAKE_HOUR.
		# Set by texting 'awake' — a Shortcuts automation on the Wake Up
		# trigger can do that without anyone touching the phone.
		self.state.setdefault("woke_at", None)
		# Echoes were a bare list of message strings before they carried a
		# timestamp. Anything written in the old shape is long stale.
		if not all(isinstance(echo, list) and len(echo) == 2 for echo in self.state["sent_echoes"]):
			self.state["sent_echoes"] = []
		self.reply_warning_shown = False
		self.photo_warning_shown = False
		self.last_db_stamp = None

	# ----- state -----

	def save(self) -> None:
		"""Write the log through a temp file, so a crash cannot truncate it.

		os.replace is atomic within a filesystem: readers see either the old
		file or the new one, never a partial write. Without this, being killed
		mid-write (a launchd restart, a power cut) left invalid JSON behind.
		"""
		scratch = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
		with scratch.open("w") as handle:
			json.dump(self.state, handle, indent=2)
			handle.flush()
			os.fsync(handle.fileno())
		os.replace(scratch, STATE_PATH)

	@property
	def goal(self) -> float:
		return float(self.state["goal_oz"])

	def today(self) -> str:
		return date.today().isoformat()

	def entries(self) -> list[dict]:
		return self.state["days"].setdefault(self.today(), [])

	def total(self) -> float:
		return sum(entry["oz"] for entry in self.entries())

	def add(self, ounces: float, source: str) -> None:
		self.entries().append(
			{"at": datetime.now().isoformat(timespec="seconds"), "oz": round(ounces, 1), "via": source}
		)
		self.save()

	def prune_days(self) -> int:
		"""Forget days past the retention window. Returns how many went.

		Every reply is kept forever otherwise, and the whole file is rewritten
		on each save. ISO dates sort chronologically as strings, so a plain
		comparison finds the stale keys.
		"""
		cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
		stale = [day for day in self.state["days"] if day < cutoff]
		for day in stale:
			del self.state["days"][day]
		return len(stale)

	def day_total(self, day: date) -> float:
		return sum(entry["oz"] for entry in self.state["days"].get(day.isoformat(), []))

	def bar(self, total: float) -> str:
		filled = min(10, int(10 * total / self.goal)) if self.goal else 0
		return "█" * filled + "░" * (10 - filled)

	def week_lines(self, days: int = 7) -> list[str]:
		"""One line per recent day, oldest first, against today's goal."""
		lines, totals = [], []
		for offset in range(days - 1, -1, -1):
			day = date.today() - timedelta(days=offset)
			total = self.day_total(day)
			totals.append(total)
			met = "*" if self.goal and total >= self.goal else " "
			lines.append(f"  {day.strftime('%a %m-%d')}  {self.bar(total)} {total:>5g} oz {met}")
		met_count = sum(1 for total in totals if self.goal and total >= self.goal)
		average = sum(totals) / len(totals)
		lines.append(f"  {average:.0f} oz/day average, {met_count}/{days} days at goal")
		return lines

	def streak(self) -> int:
		"""Consecutive days up to yesterday that met the goal, plus today if met."""
		count = 0
		day = date.today()
		if self.total() < self.goal:
			day -= timedelta(days=1)
		while True:
			if self.day_total(day) < self.goal:
				return count
			count += 1
			day -= timedelta(days=1)

	# ----- messaging -----

	def require_phone(self) -> None:
		"""Fail before doing any work when there is nowhere to send."""
		if not self.phone:
			raise SystemExit('Missing WATER_PHONE, e.g. export WATER_PHONE="+15551234567"')
		if not looks_like_handle(self.phone):
			raise SystemExit(
				f"WATER_PHONE is {self.phone!r}, which is not a phone number or email "
				"address. Messages will refuse it and no reminder will arrive.\n"
				"Set your own number, digits included: export WATER_PHONE=\"+15551234567\""
			)

	def send(self, message: str) -> None:
		self.require_phone()
		if not message.startswith(MARKER):
			message = f"{MARKER} {message}"
		print(f"-> {message}")
		try:
			result = subprocess.run(
				["osascript", "-", self.phone, message],
				input=SEND_SCRIPT,
				capture_output=True,
				text=True,
				# An unanswered Automation prompt blocks osascript indefinitely,
				# which would otherwise wedge the whole reminder loop.
				timeout=SEND_TIMEOUT,
			)
		except subprocess.TimeoutExpired:
			print(f"   send timed out after {SEND_TIMEOUT}s; approve the Messages prompt")
			return
		if result.returncode != 0:
			print(f"   send failed: {result.stderr.strip()}")
			return
		# Texting your own number creates a self-chat, where Messages logs each
		# outgoing text a second time as an *incoming* row carrying the body.
		# Without remembering what we sent, we would read our own reminders as
		# replies, answer them, and loop forever.
		self.state["sent_echoes"].append([time.time(), message])
		self.forget_stale_echoes()
		self.save()

	def forget_stale_echoes(self) -> None:
		"""Drop echo records too old to still be waiting in the database.

		The previous cap was the last 20 messages with no expiry, so records
		that never matched sat there forever and a burst of sends could evict
		one that was still pending. Age is the honest criterion.
		"""
		cutoff = time.time() - ECHO_WINDOW_SEC
		self.state["sent_echoes"] = [
			echo for echo in self.state["sent_echoes"] if echo[0] >= cutoff
		]

	def is_echo(self, body: str) -> bool:
		"""True if we sent this exact text ourselves a moment ago."""
		self.forget_stale_echoes()
		for index, (_, message) in enumerate(self.state["sent_echoes"]):
			if message == body:
				# Consumed, so a genuine reply repeating the text is not eaten.
				del self.state["sent_echoes"][index]
				return True
		return False

	def progress_line(self) -> str:
		total, goal = self.total(), self.goal
		if not goal:
			return f"{total:g} oz"
		return f"{self.bar(total)} {total:g}/{goal:g} oz ({total / goal * 100:.0f}%)"

	# ----- reply reading -----

	def db_stamp(self) -> tuple | None:
		"""Change marker for the Messages database, None if it cannot be read.

		The -wal sidecar has to be part of this: a new message can land there
		without chat.db's own mtime moving, so keying on the main file alone
		would sit on unread replies until something else touched it.
		"""
		marks = []
		for suffix in ("", "-wal", "-shm"):
			path = CHAT_DB.with_name(CHAT_DB.name + suffix)
			try:
				info = path.stat()
			except OSError:
				continue
			marks.append((suffix, info.st_mtime_ns, info.st_size))
		return tuple(marks) or None

	def read_replies(self) -> list[tuple[int, str, list[Path]]]:
		"""Incoming messages from WATER_PHONE that we have not processed yet.

		Each entry is the row id, the text, and any photo attachments — a photo
		arrives as a message with no body at all, so the text alone is not
		enough to tell an empty message from a picture of a water bottle.

		The live database is copied first: Messages holds it open in WAL mode, so
		reading it in place either blocks or misses the newest rows.
		"""
		if not CHAT_DB.exists():
			return []
		# Copying the database three times a minute all day is wasted work when
		# no message has arrived. An unreadable stamp skips the check rather
		# than the read, so a stat failure cannot quietly stop replies.
		stamp = self.db_stamp()
		if stamp is not None and stamp == self.last_db_stamp:
			return []
		with tempfile.TemporaryDirectory() as tmp:
			copy = Path(tmp) / "chat.db"
			try:
				shutil.copy2(CHAT_DB, copy)
				for suffix in ("-wal", "-shm"):
					sidecar = CHAT_DB.with_name(CHAT_DB.name + suffix)
					if sidecar.exists():
						shutil.copy2(sidecar, copy.with_name(copy.name + suffix))
			except PermissionError:
				if not self.reply_warning_shown:
					print(
						"Cannot read Messages history, so replies will be ignored.\n"
						"Grant Full Disk Access to this terminal and restart it, or log "
						"with: python waterTracker.py log 16"
					)
					self.reply_warning_shown = True
				return []
			try:
				# closing(), not the connection's own context manager, which only
				# scopes transactions and would leave the handle open.
				with closing(sqlite3.connect(f"file:{copy}?mode=ro", uri=True)) as db:
					rows = db.execute(
						"SELECT m.ROWID, m.text, m.attributedBody, h.id, m.date "
						"FROM message m JOIN handle h ON m.handle_id = h.ROWID "
						"WHERE m.is_from_me = 0 AND m.ROWID > ? ORDER BY m.ROWID",
						(self.state["last_rowid"] or 0,),
					).fetchall()
					# Photos live outside the message row: the body is empty (or
					# just an object-replacement character) and the file is on
					# disk, reachable only through this join.
					#
					# Caught separately from the message query on purpose. Losing
					# photos is a missing feature; letting that failure escape
					# would stop every text reply being read too, which is the
					# whole product.
					attachments: dict[int, list[Path]] = {}
					if PHOTOS and rows:
						try:
							first = rows[0][0] - 1
							for message_id, filename, mime in db.execute(
								"SELECT j.message_id, a.filename, a.mime_type "
								"FROM message_attachment_join j "
								"JOIN attachment a ON a.ROWID = j.attachment_id "
								"WHERE j.message_id > ?",
								(first,),
							):
								if not filename or (mime or "").lower() not in PHOTO_TYPES:
									continue
								# Stored with a literal ~ for the home directory.
								path = Path(filename).expanduser()
								if path.exists():
									attachments.setdefault(message_id, []).append(path)
						except sqlite3.Error as error:
							if not self.photo_warning_shown:
								print(f"   could not read photo attachments: {error}")
								self.photo_warning_shown = True
			except sqlite3.Error as error:
				print(f"   could not query Messages: {error}")
				return []

		mine = digits(self.phone)
		replies, stale = [], 0
		for rowid, text, blob, handle, raw_date in rows:
			self.state["last_rowid"] = rowid
			if digits(handle) != mine and handle != self.phone:
				continue
			body = (text or "").strip() or decode_body(blob)
			# U+FFFC is the placeholder Messages puts where an attachment sits.
			# Left in, it would read as a body and hide the photo behind it.
			body = body.replace("\ufffc", "").strip()
			photos = attachments.get(rowid, [])
			if not body and not photos:
				continue
			if body and (self.is_echo(body) or body.startswith(MARKER)):
				continue
			# A reply read long after it was sent is not safe to act on: the
			# amount may belong to a previous day, and after any gap in reading
			# there is a whole backlog of them waiting.
			sent_at = message_time(raw_date)
			if sent_at and time.time() - sent_at > STALE_REPLY_MIN * 60:
				stale += 1
				continue
			replies.append((rowid, body, photos))
		if stale:
			print(f"   skipped {stale} reply(s) older than {STALE_REPLY_MIN} min")
		# Recorded only after a clean read, so a failed copy is retried on the
		# next poll instead of being treated as already seen.
		self.last_db_stamp = stamp
		# Only when the cursor actually moved. This used to write the log three
		# times a minute all day, every write another chance to be interrupted.
		if rows:
			self.save()
		return replies

	def prime_replies(self) -> None:
		"""Skip existing history on first run so old texts are not replayed."""
		if self.state["last_rowid"] is not None:
			return
		self.state["last_rowid"] = 0
		self.read_replies()
		print(f"Watching for replies after message {self.state['last_rowid']}.")

	# ----- reply handling -----

	def pause(self) -> str:
		self.state["paused_on"] = self.today()
		self.save()
		return "Paused for today. Reply 'resume' to start again."

	def resume(self) -> str:
		self.state["paused_on"] = None
		self.save()
		return "Reminders back on."

	def wake(self) -> str:
		"""Start the day now rather than at WAKE_HOUR.

		Waking up also ends a pause: yesterday's 'going to bed' should not keep
		today quiet. The nudge clock is reset too, so the acknowledgement below
		counts as the morning's first contact and the next nudge is a full gap
		away — being texted twice in the first minute of being awake is not a
		good introduction to a hydration tracker.
		"""
		now = datetime.now()
		self.state["woke_at"] = now.isoformat(timespec="seconds")
		self.state["paused_on"] = None
		self.state["last_nudge_at"] = time.time()
		self.state["awaiting_reply_since"] = None
		self.save()
		left = SLEEP_HOUR - self.day_start()
		return (
			f"Morning. Goal {self.goal:g} oz by {SLEEP_HOUR}:00 — "
			f"{left:.0f}h to drink it. {self.progress_line()}"
		)

	def snooze(self) -> str:
		"""Push the next nudge out a full gap without pausing the whole day."""
		self.state["last_nudge_at"] = time.time()
		self.save()
		return "No problem, I'll check back later."

	def undo(self) -> str:
		entries = self.entries()
		if not entries:
			return "Nothing logged today yet."
		dropped = entries.pop()
		self.save()
		return f"Removed {dropped['oz']:g} oz. Now {self.progress_line()}"

	def set_goal(self, ounces: float) -> str:
		self.state["goal_oz"] = round(ounces, 1)
		self.state["congratulated_on"] = None
		self.save()
		return f"Daily goal set to {self.goal:g} oz. {self.progress_line()}"

	def log_photo(self, path: Path) -> None:
		"""Log from a picture of a container, saying where the number came from.

		There is no pattern matching underneath this one, so a failure has to be
		answered in words rather than fallen through: a photo that goes
		unacknowledged looks identical to a tracker that has stopped working.
		"""
		if not PHOTOS:
			self.send("Photos are switched off here. Text an amount instead.")
			return
		if not llm_ready():
			self.send("Can't read photos right now — Claude is unavailable. Text an amount instead.")
			return
		plan = llm_photo_plan(path)
		if plan is None:
			self.send("Couldn't read that photo. Text an amount instead.")
			return
		amount = photo_amount(plan)
		if amount is None:
			note = (plan.get("note") or "").strip()
			tail = f" {note[:MAX_CHAT_CHARS]}" if note else ""
			self.send(f"Couldn't tell what that holds.{tail} How much was it?")
			return
		ounces, how = amount
		print(f"   photo read as {ounces:g} oz ({how})")
		# Always invites a correction, because the container is identified
		# rather than measured: the number is the container's capacity, not a
		# reading of what was actually swallowed.
		self.log_reply(ounces, f" I {how} — text an amount to correct it.")

	def log_reply(self, ounces: float, note: str = "") -> None:
		self.add(ounces, "reply")
		if self.total() < self.goal:
			remaining = self.goal - self.total()
			self.send(f"Logged {ounces:g} oz. {self.progress_line()} — {remaining:g} oz to go.{note}")
			return
		# congratulated_on was recorded and never read, so every drink after
		# the goal was met got the same "Goal hit" fanfare and streak count.
		if self.state["congratulated_on"] == self.today():
			self.send(f"Logged {ounces:g} oz. {self.total():g} oz today, past your goal.{note}")
			return
		self.state["congratulated_on"] = self.today()
		self.save()
		streak = self.streak()
		suffix = f" {streak}-day streak." if streak > 1 else ""
		self.send(f"Logged {ounces:g} oz. Goal hit at {self.total():g} oz.{suffix}{note}")

	def follow_plan(self, plan: dict) -> None:
		"""Act on Claude's reading of a message.

		The action comes from the model; every number in the reply comes from
		the log, so a misread costs one wrong entry that 'undo' fixes rather
		than a confidently invented total.
		"""
		action = plan["action"]
		if action == "log":
			self.log_reply(plan["ounces"])
		elif action == "goal":
			self.send(self.set_goal(plan["goal_oz"]))
		elif action == "status":
			self.send(f"\U0001f4a7 Today: {self.progress_line()}")
		elif action == "week":
			self.send("Last 7 days:\n" + "\n".join(line.strip() for line in self.week_lines()))
		elif action == "undo":
			self.send(self.undo())
		elif action == "pause":
			self.send(self.pause())
		elif action == "resume":
			self.send(f"{self.resume()} {self.progress_line()}")
		elif action == "wake":
			self.send(self.wake())
		elif action == "later":
			self.send(self.snooze())
		else:
			self.send(f"{plan['chat']}\n{self.progress_line()}")

	def handle_reply(self, body: str, photos: list[Path] | None = None) -> None:
		print(f"<- {body}" + (f" [{len(photos)} photo(s)]" if photos else ""))
		# Any reply answers the outstanding nudge, whatever it turns out to
		# mean. Someone texting 'status' or 'not yet' has the phone in hand, so
		# a follow-up would be chasing a person who is plainly already there.
		# Cleared before interpreting, so a message the tracker cannot parse
		# still counts as having been answered.
		self.state["awaiting_reply_since"] = None
		self.save()
		if photos and not body:
			self.log_photo(photos[0])
			return

		text = body.strip().lower()

		# Answered without the network when the message is only an amount, so
		# the commonest reply comes back in milliseconds instead of a round
		# trip. Anything less clear-cut falls through to Claude below.
		quick = fast_path_ounces(text)
		if quick is not None:
			print(f"   read as log, {quick:g} oz, without asking Claude")
			self.log_reply(quick)
			return

		# Claude next, since it reads sentences no pattern anticipates, and
		# the patterns below as the fallback when it cannot answer.
		plan = llm_plan(body.strip())
		if plan:
			print(f"   read as {plan['action']}")
			self.follow_plan(plan)
			return

		# Bounded gap before the number so "my goal, drank 16 oz" is not a goal
		# change; punctuation ends the phrase.
		goal_match = re.search(r"\bgoals?\b[^\d,.!?]{0,15}(\d+(?:\.\d+)?)\s*([a-z]*)", text)
		if goal_match:
			factor = UNITS.get(goal_match.group(2), 1.0)
			self.send(self.set_goal(float(goal_match.group(1)) * factor))
			return

		intent = detect_intent(text)
		if intent == "undo":
			self.send(self.undo())
			return

		ounces = extract_ounces(text)
		if ounces is not None and not amount_is_negated(text):
			# One sentence can do both: "had 20 oz, you can stop for today".
			note = ""
			if intent == "pause":
				note = " " + self.pause()
			elif intent == "resume":
				note = " " + self.resume()
			self.log_reply(ounces, note)
			return

		if intent == "status":
			self.send(f"\U0001f4a7 Today: {self.progress_line()}")
			return
		if intent == "week":
			self.send("Last 7 days:\n" + "\n".join(line.strip() for line in self.week_lines()))
			return
		if intent == "pause":
			self.send(self.pause())
			return
		if intent == "resume":
			self.send(f"{self.resume()} {self.progress_line()}")
			return
		if intent == "later":
			self.send(self.snooze())
			return
		if intent == "wake":
			self.send(self.wake())
			return
		if intent == "drank":
			# No amount, just confirmation. A glass is the safest guess, and
			# saying so invites a correction rather than hiding it.
			self.log_reply(
				DEFAULT_SERVING_OZ,
				f" Counted as {DEFAULT_SERVING_OZ:g} oz — text an amount to be exact.",
			)
			return

		self.send("Didn't catch an amount. Try 'had 2 cups', '500ml', 'done', or 'status'.")

	# ----- reminders -----

	def awake(self) -> bool:
		now = datetime.now()
		return self.day_start() <= now.hour + now.minute / 60 < SLEEP_HOUR

	def day_start(self) -> float:
		"""The hour the day began, as a float: 7.5 means 07:30.

		WATER_WAKE_HOUR is only a fallback. Texting 'awake' records the real
		time, which matters more than it sounds: a fixed 8:00 means waking at
		06:00 leaves two hours where you are ahead of pace by definition, and
		waking at 10:00 starts you already behind on water you were asleep for.

		Only today's record counts. Yesterday's wake time says nothing about
		this morning, and a stale one would skew the pace all day.
		"""
		woke = self.state.get("woke_at")
		if woke:
			try:
				at = datetime.fromisoformat(woke)
			except (TypeError, ValueError):
				return float(WAKE_HOUR)
			if at.date() == date.today():
				# Taken as given, even before WAKE_HOUR: saying you are up is
				# explicit, and clamping it up to 8:00 would make an early
				# riser wait exactly as long as before, which is the thing this
				# exists to fix. Only the top is clamped, so a late nap cannot
				# leave SLEEP_HOUR behind it and invert the day.
				return min(at.hour + at.minute / 60, float(SLEEP_HOUR))
		return float(WAKE_HOUR)

	def expected_by_now(self, now: datetime | None = None) -> float:
		"""Ounces you would have drunk at an even pace across the waking day."""
		start = self.day_start()
		hours = SLEEP_HOUR - start
		if hours <= 0:
			return self.goal
		now = now or datetime.now()
		elapsed = now.hour + now.minute / 60 - start
		return self.goal * min(1.0, max(0.0, elapsed / hours))

	def nudge_gap(self, now: datetime | None = None) -> float:
		"""Seconds to wait before the next nudge, tightened when behind pace.

		A fixed interval nudges just as often whether you are 5 oz or 50 oz
		short, which is either nagging or useless. Ahead of pace it backs off,
		and at a quarter of the goal behind it halves the wait.
		"""
		deficit = self.expected_by_now(now) - self.total()
		if deficit <= 0:
			return INTERVAL_MIN * 60 * 1.5
		share = deficit / self.goal if self.goal else 0.0
		return INTERVAL_MIN * 60 * max(0.5, 1 - 2 * share)

	def waiting_minutes(self) -> float | None:
		"""How long the outstanding nudge has gone unanswered, if one has."""
		waiting_since = self.state["awaiting_reply_since"]
		if not waiting_since:
			return None
		return (time.time() - float(waiting_since)) / 60

	def follow_up_due(self) -> bool:
		"""Whether an unanswered nudge has earned its one follow-up.

		Bounded at both ends. Too early is nagging. Too late is noise: the Mac
		can sleep straight through the moment a chase came due, and one that
		surfaces at breakfast about last night's nudge is asking after a
		question nobody remembers — the morning nudge covers that instead.
		"""
		if not FOLLOWUP_MIN or self.state["followed_up"]:
			return False
		waiting = self.waiting_minutes()
		if waiting is None:
			return False
		return FOLLOWUP_MIN <= waiting < FOLLOWUP_MIN + FOLLOWUP_GRACE_MIN

	def maybe_follow_up(self) -> None:
		"""Chase a nudge nobody answered, once, then stay quiet.

		Stopping after one is the point: a chain of them trains you to ignore
		the whole thing, and the paced nudge is still coming. An explicit
		'not yet' counts as an answer, so it lands here as silence rather than
		as another prod.
		"""
		if not self.follow_up_due():
			return
		# Recorded before sending, like last_nudge_at below, so a failed send
		# does not retry on every poll for the rest of the gap.
		self.state["followed_up"] = True
		self.save()
		waiting = self.waiting_minutes() or 0
		# Phrased as not having heard back, which is what is actually measured.
		# "Nothing logged" would be a guess: the log can also be added to from
		# the command line, without a reply.
		self.send(
			f"\U0001f4a7 Still {self.progress_line()} — no reply since I asked "
			f"{waiting:.0f} min ago.\nReply with an amount to log it, or 'not yet'."
		)

	def maybe_remind(self) -> None:
		if self.state["paused_on"] == self.today():
			return
		if self.total() >= self.goal:
			return
		# A nudge goes out only inside the waking window, and only once the
		# paced gap has passed. A chase is judged separately and deliberately
		# outlives that window: it finishes an exchange a nudge already started,
		# and a question asked at 21:50 is still owed its answer at 22:50.
		# Nothing ever *starts* after SLEEP_HOUR — this is why awake() gates the
		# nudge here rather than the whole method, which used to mean a nudge in
		# the last hour of the day could never be chased at all.
		#
		# Wall clock, not time.monotonic(): monotonic stops while the Mac is
		# asleep, so a laptop that naps through the afternoon would wake up
		# thinking no time had passed and never nudge. Kept in state so a
		# restart neither loses the spacing nor fires a duplicate.
		nudge_due = self.awake() and (
			time.time() - float(self.state["last_nudge_at"]) >= self.nudge_gap()
		)
		if not nudge_due:
			# Too early for the next nudge, which is exactly when chasing the
			# last one is worth it: that gap is hours and the follow-up is an
			# hour, so this fires in between rather than on top of a nudge.
			self.maybe_follow_up()
			return
		# Recorded before sending, so a failed send waits instead of retrying
		# every poll.
		self.state["last_nudge_at"] = time.time()
		# A nudge is unanswered by definition, and arms one follow-up. Armed on
		# the attempt rather than on a confirmed send, matching last_nudge_at:
		# send() reports failures without raising, and a nudge that never
		# arrived is worth chasing too.
		self.state["awaiting_reply_since"] = time.time()
		self.state["followed_up"] = False
		self.save()
		nudge = NUDGES[int(time.time() // 60) % len(NUDGES)]
		deficit = self.expected_by_now() - self.total()
		behind = f" {deficit:.0f} oz behind pace." if deficit >= 1 else ""
		self.send(
			f"\U0001f4a7 {nudge} {self.progress_line()}{behind}\nReply with an amount to log it."
		)

	def run(self) -> None:
		self.require_phone()
		print(
			f"Water tracker running for {self.phone}. Goal {self.goal:g} oz, "
			f"nudge every {INTERVAL_MIN} min between {WAKE_HOUR}:00 and {SLEEP_HOUR}:00."
		)
		print(
			f"Follow-up: one chase after {FOLLOWUP_MIN} min without a reply."
			if FOLLOWUP_MIN
			else "Follow-up: off (WATER_FOLLOWUP_MIN=0)."
		)
		# The real check, not just the configuration: this log is where anyone
		# looks when replies come back read literally.
		print(f"Replies: {llm_check()[1]}")
		self.prime_replies()
		while True:
			try:
				# Once a day rather than every poll, and keyed on the date so a
				# loop left running for months still prunes after midnight.
				if self.state["pruned_on"] != self.today():
					self.state["pruned_on"] = self.today()
					dropped = self.prune_days()
					if dropped:
						print(f"   forgot {dropped} day(s) older than {KEEP_DAYS} days")
					self.save()
				try:
					for _, body, photos in self.read_replies():
						self.handle_reply(body, photos)
				except Exception as error:  # keep the loop alive across transient failures
					print(f"   error reading replies: {error}")
				# Separate from the block above: a failure while reading replies
				# used to skip maybe_remind() on every pass, silently killing
				# reminders for as long as the failure lasted.
				try:
					self.maybe_remind()
				except Exception as error:
					print(f"   error sending reminder: {error}")
				time.sleep(POLL_SECONDS)
			except KeyboardInterrupt:
				print("\nStopped.")
				return


PLIST_LABEL = "com.watertracker.reminders"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"
LOG_PATH = Path.home() / "Library" / "Logs" / "watertracker.log"

PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>{label}</string>
	<key>ProgramArguments</key>
	<array>
		<string>{python}</string>
		<string>{script}</string>
		<string>run</string>
	</array>
	<key>EnvironmentVariables</key>
	<dict>
{environment}	</dict>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
	<key>StandardOutPath</key>
	<string>{log}</string>
	<key>StandardErrorPath</key>
	<string>{log}</string>
</dict>
</plist>
"""


def install_agent() -> None:
	"""Write a LaunchAgent, so reminders do not stop with the terminal."""
	phone = os.getenv("WATER_PHONE")
	if not phone:
		raise SystemExit('Missing WATER_PHONE, e.g. export WATER_PHONE="+15551234567"')
	if not looks_like_handle(phone):
		raise SystemExit(
			f"WATER_PHONE is {phone!r}, which is not a phone number or email address.\n"
			"Nothing was installed. Set your own number: export WATER_PHONE=\"+15551234567\""
		)
	settings = {
		"WATER_PHONE": phone,
		# launchd starts the job from /, where a relative state path would
		# silently become a second, empty log.
		"WATER_STATE_FILE": str(STATE_PATH.resolve()),
		# Python buffers stdout when it is a file rather than a terminal, so
		# without this the log stays empty for hours and a failing send leaves
		# no trace anywhere.
		"PYTHONUNBUFFERED": "1",
	}
	for name in (
		"WATER_GOAL_OZ",
		"WATER_INTERVAL_MIN",
		"WATER_WAKE_HOUR",
		"WATER_SLEEP_HOUR",
		"WATER_POLL_SECONDS",
		"WATER_SEND_TIMEOUT",
		"WATER_KEEP_DAYS",
		"WATER_STALE_REPLY_MIN",
		"WATER_FOLLOWUP_MIN",
		"WATER_FOLLOWUP_GRACE_MIN",
		"WATER_DEFAULT_OZ",
		"WATER_LLM",
		"WATER_MODEL",
		"WATER_LLM_TIMEOUT",
		"WATER_EFFORT",
		"WATER_FAST_PATH",
		"WATER_PHOTOS",
		"WATER_PHOTO_MAX_PX",
		# launchd jobs inherit nothing from your shell, so the key has to be
		# written into the plist or the agent quietly loses interpretation.
		"ANTHROPIC_API_KEY",
		"ANTHROPIC_AUTH_TOKEN",
	):
		value = os.getenv(name)
		if value:
			settings[name] = value
	environment = "".join(
		f"\t\t<key>{escape(key)}</key>\n\t\t<string>{escape(value)}</string>\n"
		for key, value in settings.items()
	)
	PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
	LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
	PLIST_PATH.write_text(
		PLIST_TEMPLATE.format(
			label=PLIST_LABEL,
			python=escape(sys.executable),
			script=escape(str(Path(__file__).resolve())),
			environment=environment,
			log=escape(str(LOG_PATH)),
		)
	)
	secret = next((name for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN") if name in settings), None)
	if secret:
		# The plist would otherwise be world-readable, and it now holds a key.
		PLIST_PATH.chmod(0o600)
	print(f"Wrote {PLIST_PATH}")
	print(f"State:  {settings['WATER_STATE_FILE']}")
	# The real check, not llm_status(): install is where credentials get set, so
	# it is the worst possible place to print an unverified claim that Claude is
	# reading replies. The key being written is the one in this environment, so
	# this tests exactly what the agent will run with.
	working, how = llm_check()
	print(f"Replies: {how}")
	if secret:
		print(f"        {secret} was copied into the plist, so it is now chmod 600")
	if not working:
		print("        ^ fix that and re-run install, or the agent reads replies literally")
	print(f"Start:  launchctl bootstrap gui/$(id -u) {PLIST_PATH}")
	print(f"Stop:   launchctl bootout gui/$(id -u)/{PLIST_LABEL}")
	print(f"Log:    {LOG_PATH}")
	print(
		f"Give {sys.executable} Full Disk Access as well as your terminal, "
		"otherwise the background job cannot read your replies."
	)


def installed_agent() -> dict:
	"""The LaunchAgent's own configuration, {} when it is not installed.

	The agent carries its own copy of the settings, so the shell running
	doctor tells you nothing about what the background job is doing.
	"""
	try:
		return plistlib.loads(PLIST_PATH.read_bytes())
	except (FileNotFoundError, plistlib.InvalidFileException, ValueError):
		return {}


# Opening the database is the only real test of Full Disk Access, so this runs
# under whichever interpreter is in question.
READ_PROBE = "import sys; open(sys.argv[1], 'rb').close()"


def agent_log_since_start(limit: int = 40) -> list[str]:
	"""The LaunchAgent's log for its latest run, empty when there is none."""
	try:
		lines = LOG_PATH.read_text(errors="replace").splitlines()
	except OSError:
		return []
	for index in range(len(lines) - 1, -1, -1):
		if lines[index].startswith("Water tracker running for"):
			return lines[index:]
	return lines[-limit:]


def doctor() -> None:
	"""Report on everything that can stop reminders without saying so.

	Written after an afternoon of silence that took a look at ps, the state
	file and launchctl to explain: nothing was running.
	"""

	def check(good: bool, note: str) -> None:
		print(f"  {'ok  ' if good else 'FAIL'}  {note}")

	print("Water tracker checkup")
	installed = installed_agent()
	agent = installed.get("EnvironmentVariables", {})
	phone = os.getenv("WATER_PHONE")
	source = "the environment"
	if not phone:
		phone, source = agent.get("WATER_PHONE"), "the LaunchAgent"
	if not phone:
		check(False, "WATER_PHONE is unset and no LaunchAgent is installed, so nothing can send")
	elif not looks_like_handle(phone):
		check(False, f"{phone!r} from {source} is not a phone number or email; Messages will refuse it")
	else:
		check(True, f"texting {phone}, from {source}")
	agent_phone = agent.get("WATER_PHONE")
	if agent_phone and os.getenv("WATER_PHONE") and agent_phone != os.getenv("WATER_PHONE"):
		check(False, f"the LaunchAgent texts {agent_phone}, not the {os.getenv('WATER_PHONE')} set here")

	working, how = llm_check()
	check(working, f"replies {how}")
	if LLM_MODE != "off" and anthropic is None:
		print("        python3 -m pip install anthropic to have Claude read them")
	print(f"        state file {STATE_PATH.resolve()}")
	state = load_state(quarantine=False)
	if state is None:
		check(False, "state file is unreadable; 'run' will set it aside and start fresh")
		state = {}
	else:
		check(STATE_PATH.exists(), "state file exists" if STATE_PATH.exists() else "no state file yet")

	# Full Disk Access follows whatever launched python, not the binary alone: a
	# grant held by this terminal is inherited by anything started from it and
	# says nothing about the launchd job, even on the same interpreter. So this
	# probe covers foreground runs, and the agent's log covers the agent.
	interpreters = {sys.executable: "this python"}
	arguments = installed.get("ProgramArguments") or []
	if arguments and arguments[0] != sys.executable:
		interpreters[arguments[0]] = "the LaunchAgent's python"
	for interpreter, label in interpreters.items():
		probe = subprocess.run(
			[interpreter, "-c", READ_PROBE, str(CHAT_DB)], capture_output=True, text=True
		)
		if probe.returncode == 0:
			check(True, f"{label} can read Messages history, so replies are picked up")
		elif "PermissionError" in probe.stderr:
			check(False, f"{label} has no Full Disk Access, so replies are ignored: grant it to {interpreter}")
		else:
			tail = probe.stderr.strip().splitlines()
			check(False, f"{label} cannot read {CHAT_DB}: {tail[-1] if tail else 'unknown error'}")

	log = agent_log_since_start()
	if any("Cannot read Messages history" in line for line in log):
		target = arguments[0] if arguments else sys.executable
		check(False, f"the running agent reports it cannot read replies: add {target} to Full Disk Access, then reload")
	elif log:
		check(True, f"agent log clean since it started ({len(log)} line(s) in {LOG_PATH})")
	for line in log:
		if "send failed" in line or "send timed out" in line:
			check(False, f"agent log: {line.strip()}")

	if PLIST_PATH.exists():
		loaded = subprocess.run(["launchctl", "list", PLIST_LABEL], capture_output=True).returncode == 0
		check(loaded, f"LaunchAgent {'loaded' if loaded else 'installed but not loaded'}")
		if "PYTHONUNBUFFERED" not in agent:
			check(False, f"LaunchAgent predates unbuffered logging, so {LOG_PATH} stays empty; re-run install")
	else:
		check(False, "no LaunchAgent, so reminders stop with the terminal (try 'install')")

	# pgrep -f matches the launchd job and a hand-started loop alike; this
	# process is running 'doctor', so it cannot match itself.
	pids = subprocess.run(
		["pgrep", "-f", "waterTracker.py run"], capture_output=True, text=True
	).stdout.split()
	check(bool(pids), f"loop running (pid {', '.join(pids)})" if pids else "no reminder loop is running")

	today = date.today().isoformat()
	goal = float(state.get("goal_oz") or GOAL_OZ)
	total = sum(entry["oz"] for entry in state.get("days", {}).get(today, []))
	paused = state.get("paused_on") == today
	check(not paused, "paused for today, reply 'resume'" if paused else "not paused")
	# Read the same way the loop reads it, or doctor reports a window the
	# tracker is not actually using once 'awake' has been texted.
	start = float(WAKE_HOUR)
	woke = state.get("woke_at")
	if woke:
		try:
			at = datetime.fromisoformat(woke)
			if at.date() == date.today():
				start = min(max(at.hour + at.minute / 60, float(WAKE_HOUR)), float(SLEEP_HOUR))
				print(f"        day started {at:%H:%M} (you texted that you were up)")
		except (TypeError, ValueError):
			print(f"        woke_at is unreadable ({woke!r}); falling back to {WAKE_HOUR}:00")
	else:
		print(f"        no wake time texted today; pacing from {WAKE_HOUR}:00")
	right_now = datetime.now()
	awake = start <= right_now.hour + right_now.minute / 60 < SLEEP_HOUR
	if awake:
		check(True, f"inside the {clock(start)}-{SLEEP_HOUR}:00 nudge window")
	else:
		# The time of day is not a fault, so it reads as a fact, not a failure.
		print(f"        outside the {WAKE_HOUR}:00-{SLEEP_HOUR}:00 nudge window")

	# Only a gap the tracker cannot explain is worth reporting. Overnight, or
	# once the goal is met, silence is the design and flagging it is noise.
	last_nudge = float(state.get("last_nudge_at") or 0)
	quiet_because = None
	if not awake:
		quiet_because = f"it is outside {WAKE_HOUR}:00-{SLEEP_HOUR}:00"
	elif paused:
		quiet_because = "it is paused for today"
	elif goal and total >= goal:
		quiet_because = "the goal is met"
	if quiet_because:
		check(True, f"no nudge due: {quiet_because}")
	elif not last_nudge:
		check(False, "no nudge has been sent yet")
	else:
		minutes = (time.time() - last_nudge) / 60
		# The gap stretches to 1.5x the interval when you are ahead of pace.
		check(
			minutes <= INTERVAL_MIN * 1.5,
			f"last nudge {minutes:.0f} min ago, interval is {INTERVAL_MIN} min",
		)
	# Both states are normal, so they read as facts. Which one it is explains a
	# text that arrived off the interval, or one that never came.
	waiting = state.get("awaiting_reply_since")
	if not FOLLOWUP_MIN:
		print("        no follow-ups: WATER_FOLLOWUP_MIN=0")
	elif not waiting:
		print(f"        nothing awaiting a reply; follow-up after {FOLLOWUP_MIN} min of silence")
	else:
		held = (time.time() - float(waiting)) / 60
		if state.get("followed_up"):
			why = " (already sent)"
		elif held >= FOLLOWUP_MIN + FOLLOWUP_GRACE_MIN:
			# The case that looks like a missing text but is not: nothing was
			# running when it came due, and a chase this old is deliberately
			# dropped rather than sent late.
			why = f" (missed its moment by more than {FOLLOWUP_GRACE_MIN} min, dropped)"
		elif held >= FOLLOWUP_MIN:
			why = " (due now)"
		else:
			why = f" (in {FOLLOWUP_MIN - held:.0f} min)"
		print(f"        nudge unanswered for {held:.0f} min, follow-up at {FOLLOWUP_MIN} min{why}")
	print(f"        today {total:g}/{goal:g} oz")
	print("Send a text with 'test' to confirm the Messages Automation prompt was approved.")


def main(argv: list[str]) -> None:
	command = argv[0] if argv else "run"
	if command == "install":
		install_agent()
		return
	if command == "doctor":
		doctor()
		return

	tracker = WaterTracker()

	if command == "status":
		print(f"Today: {tracker.progress_line()}")
		for entry in tracker.entries():
			print(f"  {entry['at'][11:16]}  {entry['oz']:>5g} oz  ({entry['via']})")
		streak = tracker.streak()
		if streak:
			print(f"Streak: {streak} day(s) at goal.")
	elif command == "week":
		days = int(argv[1]) if len(argv) > 1 else 7
		print(f"Last {days} days against a {tracker.goal:g} oz goal:")
		print("\n".join(tracker.week_lines(days)))
	elif command == "log":
		if len(argv) < 2:
			raise SystemExit("Usage: python waterTracker.py log 16")
		ounces = extract_ounces(" ".join(argv[1:]))
		if ounces is None:
			raise SystemExit("Could not read that amount, try '16' or '2 cups'.")
		tracker.add(ounces, "cli")
		print(f"Logged {ounces:g} oz. {tracker.progress_line()}")
	elif command == "test":
		tracker.send(f"\U0001f4a7 Water tracker is connected. {tracker.progress_line()}")
	elif command == "run":
		tracker.run()
	else:
		raise SystemExit(
			f"Unknown command {command!r}. "
			"Use run, status, week, log, test, doctor, or install."
		)


if __name__ == "__main__":
	main(sys.argv[1:])
