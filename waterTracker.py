"""Text yourself water reminders over iMessage and log the replies you send back.

Setup:
	export WATER_PHONE="+15551234567"   # the iMessage handle to text, for run/test
	export WATER_GOAL_OZ="100"          # optional daily goal in oz, default 100
	export WATER_INTERVAL_MIN="120"     # optional nudge spacing, default 120
	export WATER_WAKE_HOUR="8"          # optional, no nudges before this hour
	export WATER_SLEEP_HOUR="22"        # optional, no nudges after this hour
	export WATER_KEEP_DAYS="90"         # optional, how long history is kept
	export WATER_STALE_REPLY_MIN="60"   # optional, ignore replies older than this

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
POLL_SECONDS = int(os.getenv("WATER_POLL_SECONDS", "20"))
SEND_TIMEOUT = int(os.getenv("WATER_SEND_TIMEOUT", "60"))
KEEP_DAYS = int(os.getenv("WATER_KEEP_DAYS", "90"))
STALE_REPLY_MIN = int(os.getenv("WATER_STALE_REPLY_MIN", "60"))
STATE_PATH = Path(os.getenv("WATER_STATE_FILE", "water_tracker_state.json"))
CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"

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

# Everything is normalised to fluid ounces.
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
	"ml": 0.033814,
	"l": 33.814,
	"liter": 33.814,
	"liters": 33.814,
	"litre": 33.814,
	"litres": 33.814,
}

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
	"couple": 2.0,
	"half": 0.5,
}
# Longest alternative first, so "liters" wins over "l" and "cups" over "cup".
_UNIT_RE = "|".join(sorted((unit for unit in UNITS if unit), key=len, reverse=True))
_QTY_RE = r"\d+(?:\.\d+)?|" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
AMOUNT_RE = re.compile(rf"(?<![\w.])({_QTY_RE})\s*(?:of\s+)?(?:an?\s+)?({_UNIT_RE})\b")
BARE_NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")
MAX_LOG_OZ = 400.0

# Checked in order, so "undo that" is an undo before "that" matters. Each is a
# search, not a match, to catch commands wrapped in a sentence.
INTENT_PATTERNS = (
	("undo", re.compile(r"\b(undo|scratch that|(remove|delete|drop)( the)? last|never ?mind)\b")),
	("week", re.compile(r"\b(week|weekly|last \d+ days|history|average)\b")),
	("status", re.compile(r"\b(status|progress|total|how much|how many|where am i)\b|^\?+$")),
	("pause", re.compile(r"\b(pause|snooze|quiet|shush|stop|leave me alone)\b")),
	("resume", re.compile(r"\b(resume|unpause|start again|back on)\b|^(go|start)$")),
)


def extract_ounces(text: str) -> float | None:
	"""Total fluid ounces mentioned anywhere in a reply, else None.

	Handles '16', '2 cups', '500 ml', 'half a liter', and sums every amount in
	a sentence like 'a bottle at the gym and 500ml after'.
	"""
	body = text.strip().lower()
	total = 0.0
	for match in AMOUNT_RE.finditer(body):
		quantity = match.group(1)
		scale = NUMBER_WORDS[quantity] if quantity in NUMBER_WORDS else float(quantity)
		total += scale * UNITS[match.group(2)]
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
		# Echoes were a bare list of message strings before they carried a
		# timestamp. Anything written in the old shape is long stale.
		if not all(isinstance(echo, list) and len(echo) == 2 for echo in self.state["sent_echoes"]):
			self.state["sent_echoes"] = []
		self.reply_warning_shown = False
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

	def read_replies(self) -> list[tuple[int, str]]:
		"""Incoming messages from WATER_PHONE that we have not processed yet.

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
			if not body:
				continue
			if self.is_echo(body) or body.startswith(MARKER):
				continue
			# A reply read long after it was sent is not safe to act on: the
			# amount may belong to a previous day, and after any gap in reading
			# there is a whole backlog of them waiting.
			sent_at = message_time(raw_date)
			if sent_at and time.time() - sent_at > STALE_REPLY_MIN * 60:
				stale += 1
				continue
			replies.append((rowid, body))
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

	def handle_reply(self, body: str) -> None:
		print(f"<- {body}")
		text = body.strip().lower()

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
		if ounces is not None:
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

		self.send("Didn't catch an amount. Try 'had 2 cups', '500ml', or 'status'.")

	# ----- reminders -----

	def awake(self) -> bool:
		return WAKE_HOUR <= datetime.now().hour < SLEEP_HOUR

	def expected_by_now(self, now: datetime | None = None) -> float:
		"""Ounces you would have drunk at an even pace across the waking day."""
		hours = SLEEP_HOUR - WAKE_HOUR
		if hours <= 0:
			return self.goal
		now = now or datetime.now()
		elapsed = now.hour + now.minute / 60 - WAKE_HOUR
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

	def maybe_remind(self) -> None:
		if not self.awake():
			return
		if self.state["paused_on"] == self.today():
			return
		if self.total() >= self.goal:
			return
		# Wall clock, not time.monotonic(): monotonic stops while the Mac is
		# asleep, so a laptop that naps through the afternoon would wake up
		# thinking no time had passed and never nudge. Kept in state so a
		# restart neither loses the spacing nor fires a duplicate.
		if time.time() - float(self.state["last_nudge_at"]) < self.nudge_gap():
			return
		# Recorded before sending, so a failed send waits instead of retrying
		# every poll.
		self.state["last_nudge_at"] = time.time()
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
					for _, body in self.read_replies():
						self.handle_reply(body)
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
	print(f"Wrote {PLIST_PATH}")
	print(f"State:  {settings['WATER_STATE_FILE']}")
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
	awake = WAKE_HOUR <= datetime.now().hour < SLEEP_HOUR
	if awake:
		check(True, f"inside the {WAKE_HOUR}:00-{SLEEP_HOUR}:00 nudge window")
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
