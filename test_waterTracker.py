"""Tests for waterTracker.

	python -m unittest discover .

Nothing here touches Messages, the network, or the real state file: sends are
captured in a list, STATE_PATH is redirected into a temp directory, and the
Claude client is a stand-in that records what it was asked.
"""

import base64
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time
import unittest
import zlib
from contextlib import closing, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("WATER_PHONE", "+15551234567")
# Forced, not defaulted: with a key in the environment the suite would
# otherwise send every test reply to the real API.
os.environ["WATER_LLM"] = "off"
_spec = importlib.util.spec_from_file_location("waterTracker", Path(__file__).with_name("waterTracker.py"))
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)


class ExtractOuncesTest(unittest.TestCase):
	def test_reads_amounts_out_of_sentences(self):
		cases = {
			"16": 16.0,
			"16oz": 16.0,
			"2 cups": 16.0,
			"500ml": 16.9,
			"+1L": 33.8,
			"just had a couple glasses": 16.0,
			"drank 500ml and a bottle at the gym": 33.8,
			"half a liter": 16.9,
			"i just finished a glass of water": 8.0,
			"had 20 oz, you can stop for today": 20.0,
			"i had three bottles today": 50.7,
			"12 ounces down": 12.0,
			"a bottle": 16.9,
			"just drank 16": 16.0,
		}
		for text, expected in cases.items():
			with self.subTest(text=text):
				self.assertAlmostEqual(wt.extract_ounces(text), expected, places=1)

	def test_reads_containers_word_numbers_and_fractions(self):
		cases = {
			"a can": 12.0,
			"my nalgene": 32.0,
			"another glass": 8.0,
			"a few sips": 4.5,
			"a couple gulps": 4.0,
			"twenty five ounces": 25.0,
			"fifteen oz": 15.0,
			"two hundred ml": 6.8,
			"3/4 of a bottle": 12.7,
			"half my water bottle": 8.4,
			"a glass and a half": 12.0,
			"a pint": 16.0,
		}
		for text, expected in cases.items():
			with self.subTest(text=text):
				self.assertAlmostEqual(wt.extract_ounces(text), expected, places=1)

	def test_a_negation_before_the_amount_is_not_a_log(self):
		for text in ("i havent had 16 oz yet", "didn't drink my 2 cups", "no water yet, not even a glass"):
			with self.subTest(text=text):
				self.assertTrue(wt.amount_is_negated(text))
		# A negation *after* the amount is a different sentence.
		self.assertFalse(wt.amount_is_negated("had 16 oz but not the second bottle"))

	def test_a_container_without_a_quantity_needs_a_possessive(self):
		# "the bottle is empty" is a statement, not a drink.
		self.assertIsNone(wt.extract_ounces("the bottle is empty"))
		self.assertEqual(wt.extract_ounces("my bottle"), 16.9)

	def test_ignores_text_without_a_usable_amount(self):
		# The bare-number guard matters most here: a time or a count in a long
		# sentence must not be logged as ounces.
		for text in (
			"i'll drink some at 16:00 after my 3 meetings",
			"lol",
			"ok you can stop for today",
			"how much have i had today?",
			"9999999 oz",
			"0 oz",
			"",
			"maybe later today when i get back from the gym around 16",
		):
			with self.subTest(text=text):
				self.assertIsNone(wt.extract_ounces(text))


class DetectIntentTest(unittest.TestCase):
	def test_finds_commands_in_longer_replies(self):
		cases = {
			"undo": "undo",
			"scratch that": "undo",
			"remove the last one": "undo",
			"nevermind": "undo",
			"status": "status",
			"?": "status",
			"how much so far": "status",
			"progress": "status",
			"pause": "pause",
			"quiet please": "pause",
			"ok you can stop for today": "pause",
			"resume": "resume",
			"go": "resume",
			"turn it back on": "resume",
		}
		for text, expected in cases.items():
			with self.subTest(text=text):
				self.assertEqual(wt.detect_intent(text), expected)

	def test_finds_casual_phrasings(self):
		cases = {
			"done": "drank",
			"yep": "drank",
			"just finished one": "drank",
			"👍": "drank",
			"not yet": "later",
			"in a bit": "later",
			"nah": "later",
			"i'm going to bed": "pause",
			"done for the day": "pause",  # a pause, not a glass
			"how am i doing": "status",
			"am i on track": "status",
			"oops": "undo",
			"my bad": "undo",
			"what's the trend": "week",
			"i'm back": "resume",
		}
		for text, expected in cases.items():
			with self.subTest(text=text):
				self.assertEqual(wt.detect_intent(text), expected)

	def test_plain_amounts_are_not_commands(self):
		for text in ("16", "2 cups", "just had a glass"):
			with self.subTest(text=text):
				self.assertIsNone(wt.detect_intent(text))


class FastPathTest(unittest.TestCase):
	"""Which replies are answered without a round trip, and which are not."""

	def test_answers_messages_that_are_only_an_amount(self):
		cases = {
			"28 oz": 28.0,
			"16": 16.0,
			"500ml": 16.9,
			"2 cups": 16.0,
			"a glass": 8.0,
			"had 28 oz": 28.0,
			"just drank a bottle": 16.9,
			"another glass": 8.0,
			"i had 16 oz and a cup": 24.0,
			"finished my nalgene": 32.0,
			# A named container is just a units lookup, fraction included.
			"like half a hydroflask": 16.0,
		}
		for text, expected in cases.items():
			with self.subTest(text=text):
				self.assertAlmostEqual(wt.fast_path_ounces(text), expected, places=1)

	def test_defers_anything_carrying_more_than_an_amount(self):
		# Each of these either means something beyond the amount, or means the
		# opposite of what the number alone suggests. The model has to see them.
		for text in (
			"had 20 oz, you can stop for today",   # also a pause
			"haven't had my 16 oz yet",            # a negation
			"16 oz of coffee",                     # an unknown word
			"the bottle is empty",                 # not a drink
			"undo that 16 oz",                     # an undo
			"set my goal to 120 oz",               # a goal change
			"how am i doing",                      # no amount at all
			"done",                                # a guess, not an amount
			"i'll drink some at 16:00",            # a time, not an amount
			"half a glass of orange juice",        # an unknown word
			# Every word here is filler, but "total" also reads as a request
			# for today's total, so only the intent check defers it. Without
			# that check this logs 16 oz in answer to a question.
			"16 oz total",
		):
			with self.subTest(text=text):
				self.assertIsNone(wt.fast_path_ounces(text))

	def test_no_negation_word_is_treated_as_filler(self):
		# The negation guard and the whitelist overlap today, which is why the
		# guard cannot be caught by a message alone. This pins the overlap: add
		# any of these to the filler list and the guard becomes the only thing
		# standing between "no 16 oz" and a logged 16 oz.
		for word in ("not", "no", "didnt", "havent", "hasnt", "wont", "nope", "forgot"):
			with self.subTest(word=word):
				self.assertNotIn(word, wt.FAST_PATH_WORDS)

	def test_negation_defers_even_when_every_word_is_allowed(self):
		previous = wt.FAST_PATH_WORDS
		wt.FAST_PATH_WORDS = previous | {"not", "havent", "yet"}
		self.addCleanup(setattr, wt, "FAST_PATH_WORDS", previous)
		for text in ("havent had 16 oz yet", "not 16 oz"):
			with self.subTest(text=text):
				self.assertIsNone(wt.fast_path_ounces(text))

	def test_the_switch_turns_it_off(self):
		previous = wt.FAST_PATH
		wt.FAST_PATH = False
		self.addCleanup(setattr, wt, "FAST_PATH", previous)
		self.assertIsNone(wt.fast_path_ounces("28 oz"))

	def test_it_never_disagrees_with_the_pattern_matcher(self):
		# The fast path is only a shortcut, so where it answers at all it has
		# to give the same ounces the slower path would have.
		for text in ("28 oz", "2 cups", "a glass", "500ml", "i had 16 oz and a cup"):
			with self.subTest(text=text):
				self.assertEqual(wt.fast_path_ounces(text), wt.extract_ounces(text))


class MessageTimeTest(unittest.TestCase):
	def test_reads_both_second_and_nanosecond_timestamps(self):
		when = datetime(2026, 9, 9, 12, 0).timestamp()
		apple = when - wt.APPLE_EPOCH
		self.assertAlmostEqual(wt.message_time(int(apple)), when, places=0)
		self.assertAlmostEqual(wt.message_time(int(apple * 1_000_000_000)), when, places=0)

	def test_no_timestamp_is_not_a_time(self):
		self.assertIsNone(wt.message_time(None))
		self.assertIsNone(wt.message_time(0))


class InstallTest(unittest.TestCase):
	def setUp(self):
		folder = tempfile.TemporaryDirectory()
		self.addCleanup(folder.cleanup)
		self.home = Path(folder.name)
		for name, value in (
			("PLIST_PATH", self.home / "agent.plist"),
			("LOG_PATH", self.home / "agent.log"),
			("STATE_PATH", self.home / "state.json"),
		):
			previous = getattr(wt, name)
			setattr(wt, name, value)
			self.addCleanup(setattr, wt, name, previous)

	def install_with(self, **environment):
		for key, value in environment.items():
			previous = os.environ.get(key)
			os.environ[key] = value
			self.addCleanup(lambda k=key, v=previous: os.environ.pop(k) if v is None else os.environ.__setitem__(k, v))
		wt.install_agent()
		import plistlib
		return plistlib.loads(wt.PLIST_PATH.read_bytes())["EnvironmentVariables"]

	def test_carries_the_settings_the_agent_cannot_inherit(self):
		settings = self.install_with(WATER_PHONE="+15551234567", WATER_MODEL="claude-haiku-4-5")
		self.assertEqual(settings["WATER_PHONE"], "+15551234567")
		self.assertEqual(settings["WATER_MODEL"], "claude-haiku-4-5")
		self.assertEqual(settings["PYTHONUNBUFFERED"], "1")
		self.assertTrue(Path(settings["WATER_STATE_FILE"]).is_absolute(), "launchd runs from /")

	def test_a_copied_key_is_not_left_world_readable(self):
		settings = self.install_with(WATER_PHONE="+15551234567", ANTHROPIC_API_KEY="sk-ant-test")
		self.assertEqual(settings["ANTHROPIC_API_KEY"], "sk-ant-test")
		self.assertEqual(wt.PLIST_PATH.stat().st_mode & 0o077, 0, "the key is readable by others")

	def test_every_setting_reaches_the_agent(self):
		# launchd inherits nothing, so a setting missing from this list is one
		# you can export, see work in the foreground, and never get from the
		# background job. Six were missing when this test was written.
		import re
		source = Path(wt.__file__).read_text()
		declared = set(re.findall(r'os\.getenv\("(WATER_[A-Z_]+)"', source))
		block = re.search(r"for name in \(\s*(.*?)\s*\):", source, re.S).group(1)
		carried = set(re.findall(r'"([A-Z_]+)"', block))
		# Both of these are written explicitly above the loop, not through it.
		carried |= {"WATER_PHONE", "WATER_STATE_FILE"}
		self.assertEqual(declared - carried, set(), "settings the LaunchAgent never sees")

	def test_no_key_no_key_in_the_plist(self):
		os.environ.pop("ANTHROPIC_API_KEY", None)
		settings = self.install_with(WATER_PHONE="+15551234567")
		self.assertNotIn("ANTHROPIC_API_KEY", settings)

	def stub_check(self, working, how):
		previous = wt.llm_check
		wt.llm_check = lambda: (working, how)
		self.addCleanup(setattr, wt, "llm_check", previous)

	def test_a_key_that_does_not_work_is_called_out(self):
		# install is the one moment credentials get set, so a broken one has to
		# be obvious here rather than discovered from the agent log hours later.
		self.stub_check(False, "pattern matching: credentials rejected, set ANTHROPIC_API_KEY")
		printed = io.StringIO()
		with redirect_stdout(printed):
			self.install_with(WATER_PHONE="+15551234567", ANTHROPIC_API_KEY="sk-ant-...")
		output = printed.getvalue()
		self.assertIn("credentials rejected", output)
		self.assertIn("re-run install", output, "wrote a bad key with no warning")

	def test_a_working_key_is_not_second_guessed(self):
		self.stub_check(True, "read by claude-haiku-4-5, with pattern matching as the fallback")
		printed = io.StringIO()
		with redirect_stdout(printed):
			self.install_with(WATER_PHONE="+15551234567")
		self.assertNotIn("re-run install", printed.getvalue())


class HandleTest(unittest.TestCase):
	def test_accepts_phone_numbers_and_emails(self):
		for handle in ("+15551234567", "5551234567", "(555) 123-4567", "me@example.com"):
			with self.subTest(handle=handle):
				self.assertTrue(wt.looks_like_handle(handle))

	def test_rejects_placeholders_and_fragments(self):
		# "+1..." is what got pasted out of a setup example, sending every
		# reminder into a handle Messages could not deliver to.
		for handle in ("+1...", "", None, "your number here", "+1", "555-1234", "me@localhost", "@example.com"):
			with self.subTest(handle=handle):
				self.assertFalse(wt.looks_like_handle(handle))


class TrackerTestCase(unittest.TestCase):
	"""Base class: a tracker on a temp state file with sends captured."""

	def setUp(self):
		folder = tempfile.TemporaryDirectory()
		self.addCleanup(folder.cleanup)
		self.state_path = Path(folder.name) / "state.json"
		self.patch(wt, "STATE_PATH", self.state_path)
		# Pattern matching is what these tests are about, so the model is out
		# of the way regardless of what is installed or configured.
		self.patch(wt, "llm_plan", lambda text: None)
		self.tracker = wt.WaterTracker()
		self.sent = []
		self.tracker.send = self.sent.append

	def patch(self, module, name, value):
		"""Swap a module-level setting for the duration of one test."""
		previous = getattr(module, name)
		setattr(module, name, value)
		self.addCleanup(setattr, module, name, previous)

	def set_day(self, day, ounces):
		self.tracker.state["days"][day.isoformat()] = [{"at": f"{day}T09:00:00", "oz": ounces, "via": "test"}]


class DoctorTest(TrackerTestCase):
	"""doctor describes the agent, not the shell it happens to run in."""

	def stub_check(self, working, how):
		self.patch(wt, "llm_check", lambda: (working, how))

	def test_the_agents_llm_setting_beats_the_shells(self):
		# WATER_LLM=off lives in the plist, so doctor run from a terminal
		# without it exported must not report a failure the agent is not
		# having. Same mistake as reporting a nudge window the loop is not
		# using -- doctor describes the agent, not itself.
		self.stub_check(False, "pattern matching: no credentials, set ANTHROPIC_API_KEY")
		self.patch(wt, "LLM_MODE", "auto")
		self.patch(wt, "installed_agent", lambda: {"EnvironmentVariables": {"WATER_LLM": "off"}})
		printed = io.StringIO()
		with redirect_stdout(printed):
			wt.doctor()
		output = printed.getvalue()
		self.assertIn("WATER_LLM=off in the LaunchAgent", output)
		self.assertNotIn("FAIL  replies", output, "reported the shell's failure as the agent's")

	def test_a_real_credential_problem_is_still_reported(self):
		# The other side of it: with no WATER_LLM=off anywhere, a bad key has
		# to keep showing up as a failure.
		self.stub_check(False, "pattern matching: credentials rejected, set ANTHROPIC_API_KEY")
		self.patch(wt, "LLM_MODE", "auto")
		self.patch(wt, "installed_agent", lambda: {"EnvironmentVariables": {}})
		printed = io.StringIO()
		with redirect_stdout(printed):
			wt.doctor()
		self.assertIn("credentials rejected", printed.getvalue())


class StateTest(TrackerTestCase):
	def test_write_is_atomic_and_leaves_no_scratch_file(self):
		self.tracker.add(16, "cli")
		self.assertEqual(json.loads(self.state_path.read_text())["days"][self.tracker.today()][0]["oz"], 16)
		self.assertFalse(self.state_path.with_name(self.state_path.name + ".tmp").exists())

	def test_unreadable_state_is_kept_not_overwritten(self):
		self.tracker.add(16, "cli")
		original = self.state_path.read_text()
		self.state_path.write_text(original[:20])  # a write cut short

		fresh = wt.WaterTracker()
		self.assertEqual(fresh.total(), 0)
		spoiled = self.state_path.with_name(self.state_path.name + ".corrupt")
		self.assertEqual(spoiled.read_text(), original[:20])

	def test_inspecting_a_bad_file_does_not_move_it(self):
		self.state_path.write_text("{not json")
		self.assertIsNone(wt.load_state(quarantine=False))
		self.assertTrue(self.state_path.exists())

	def test_missing_file_starts_empty(self):
		self.assertEqual(wt.load_state(), {})

	def test_reading_the_log_does_not_need_a_phone_number(self):
		self.patch(self.tracker, "phone", None)
		self.tracker.add(16, "cli")
		self.assertIn("16", self.tracker.progress_line())
		self.assertEqual(len(self.tracker.week_lines(7)), 8)

	def test_sending_without_a_phone_number_stops_with_a_clear_error(self):
		tracker = wt.WaterTracker()
		tracker.phone = None
		with self.assertRaises(SystemExit) as caught:
			tracker.send("hello")
		self.assertIn("WATER_PHONE", str(caught.exception))

	def test_sending_to_a_placeholder_handle_stops_before_messages_sees_it(self):
		tracker = wt.WaterTracker()
		tracker.phone = "+1..."
		with self.assertRaises(SystemExit) as caught:
			tracker.send("hello")
		self.assertIn("not a phone number", str(caught.exception))


class FakeClient:
	"""Stands in for anthropic.Anthropic, recording what it was asked.

	Interpretation and the startup probe get separate outcomes, since the
	point of the probe is to fail on its own before any message arrives.
	"""

	def __init__(self, outcome=None, count_outcome=None):
		self.outcome = outcome
		self.count_outcome = count_outcome or SimpleNamespace(input_tokens=3)
		self.requests = []
		self.counts = []
		self.options = []
		self.messages = SimpleNamespace(create=self._create, count_tokens=self._count)

	def with_options(self, **options):
		self.options.append(options)
		return self

	def _create(self, **request):
		self.requests.append(request)
		if isinstance(self.outcome, Exception):
			raise self.outcome
		return self.outcome

	def _count(self, **request):
		self.counts.append(request)
		if isinstance(self.count_outcome, Exception):
			raise self.count_outcome
		return self.count_outcome


def fake_sdk(client, broken=False):
	"""A stand-in for the anthropic module: exception classes and a factory.

	The classes mirror the real hierarchy, which the `except` chains depend on:
	every status error descends from APIStatusError, and a timeout is a kind of
	connection error. A flat set of siblings would let a wrongly ordered chain
	pass its tests — catching APIStatusError first silently swallows the 401
	and 404 cases that have something specific to say.

	With broken=True the factory itself raises, standing in for an SDK that
	cannot be constructed at all.
	"""

	class AnthropicError(Exception):
		pass

	class APIError(AnthropicError):
		pass

	class APIStatusError(APIError):
		status_code = 503

	class APIConnectionError(APIError):
		pass

	class APITimeoutError(APIConnectionError):
		pass

	def status(name, code, message=""):
		return type(name, (APIStatusError,), {"status_code": code, "message": message or name})

	def factory(**kwargs):
		if broken:
			raise RuntimeError("no credentials")
		return client

	return SimpleNamespace(
		Anthropic=factory,
		AnthropicError=AnthropicError,
		APIError=APIError,
		APIStatusError=APIStatusError,
		APIConnectionError=APIConnectionError,
		APITimeoutError=APITimeoutError,
		BadRequestError=status("BadRequestError", 400, "bad request"),
		AuthenticationError=status("AuthenticationError", 401),
		PermissionDeniedError=status("PermissionDeniedError", 403),
		NotFoundError=status("NotFoundError", 404),
		RateLimitError=status("RateLimitError", 429),
	)


def fake_answer(payload, stop_reason="end_turn"):
	text = payload if isinstance(payload, str) else json.dumps(payload)
	return SimpleNamespace(
		content=[SimpleNamespace(type="text", text=text)],
		stop_reason=stop_reason,
	)


class LlmTestCase(unittest.TestCase):
	"""Swaps in a stand-in SDK, and puts the module globals back afterwards."""

	def setUp(self):
		self.previous = (wt.LLM_MODE, wt.anthropic, wt._llm_client, wt._llm_broken)
		wt.LLM_MODE = "auto"
		wt._llm_client = None
		wt._llm_broken = False
		self.addCleanup(self.restore)

	def restore(self):
		wt.LLM_MODE, wt.anthropic, wt._llm_client, wt._llm_broken = self.previous

	def install(self, outcome=None, count_outcome=None, broken=False):
		# Resetting the cached client matters: without it a second install in
		# one test silently keeps answering with the first fake's response,
		# which made every payload after the first in a loop vacuous.
		wt._llm_client = None
		wt._llm_broken = False
		client = FakeClient(outcome, count_outcome)
		wt.anthropic = fake_sdk(client, broken=broken)
		return client


class LlmPlanTest(LlmTestCase):
	"""The interpretation call: request shape, validation, and failure modes."""

	def test_reads_a_valid_answer(self):
		self.install(fake_answer({"action": "log", "ounces": 24, "goal_oz": None, "chat": None}))
		self.assertEqual(wt.llm_plan("finished the one on my desk"), {
			"action": "log", "ounces": 24.0, "goal_oz": None, "chat": None,
		})

	def test_asks_for_json_from_the_configured_model(self):
		client = self.install(fake_answer({"action": "status", "ounces": None, "goal_oz": None, "chat": None}))
		wt.llm_plan("how am i doing")

		request = client.requests[0]
		self.assertEqual(request["model"], wt.LLM_MODEL)
		self.assertEqual(request["messages"], [{"role": "user", "content": "how am i doing"}])
		self.assertIn("water-intake tracker", request["system"])
		self.assertEqual(request["output_config"]["format"]["type"], "json_schema")
		self.assertEqual(request["output_config"]["format"]["schema"], wt.PLAN_SCHEMA)
		self.assertEqual(client.options[0]["timeout"], wt.LLM_TIMEOUT)
		# Off, so a timeout is not silently doubled while someone waits.
		self.assertEqual(client.options[0]["max_retries"], 0)

	def test_no_thinking_or_effort_is_asked_for_by_default(self):
		# The default model rejects output_config.effort outright, and asking
		# for thinking on a nine-way classification only adds delay.
		client = self.install(fake_answer({"action": "status", "ounces": None, "goal_oz": None, "chat": None}))
		wt.llm_plan("how am i doing")

		request = client.requests[0]
		self.assertNotIn("effort", request["output_config"])
		self.assertNotIn("thinking", request)

	def test_effort_is_sent_when_configured_for_a_model_that_takes_it(self):
		self.patch_effort("low")
		client = self.install(fake_answer({"action": "status", "ounces": None, "goal_oz": None, "chat": None}))
		wt.llm_plan("how am i doing")
		self.assertEqual(client.requests[0]["output_config"]["effort"], "low")

	def patch_effort(self, value):
		previous = wt.LLM_EFFORT
		wt.LLM_EFFORT = value
		self.addCleanup(setattr, wt, "LLM_EFFORT", previous)

	def test_turned_off_by_configuration_without_calling_out(self):
		client = self.install(fake_answer({"action": "status", "ounces": None, "goal_oz": None, "chat": None}))
		wt.LLM_MODE = "off"
		self.assertIsNone(wt.llm_plan("16 oz"))
		self.assertEqual(client.requests, [], "called the API while switched off")

	def test_a_refusal_is_not_a_plan(self):
		self.install(fake_answer({"action": "log", "ounces": 16, "goal_oz": None, "chat": None}, "refusal"))
		self.assertIsNone(wt.llm_plan("16 oz"))

	def test_unusable_answers_are_rejected(self):
		for payload in (
			"not json at all",
			{"action": "log", "ounces": None, "goal_oz": None, "chat": None},      # no amount
			{"action": "log", "ounces": 9000, "goal_oz": None, "chat": None},      # implausible
			{"action": "log", "ounces": -5, "goal_oz": None, "chat": None},        # negative
			{"action": "goal", "ounces": None, "goal_oz": None, "chat": None},     # no goal
			{"action": "chat", "ounces": None, "goal_oz": None, "chat": "  "},     # nothing said
			{"action": "teleport", "ounces": None, "goal_oz": None, "chat": None}, # invented
		):
			with self.subTest(payload=payload):
				self.install(fake_answer(payload))
				self.assertIsNone(wt.llm_plan("16 oz"))

	def test_long_chat_replies_are_trimmed(self):
		self.install(fake_answer({
			"action": "chat", "ounces": None, "goal_oz": None, "chat": "word " * 500,
		}))
		plan = wt.llm_plan("tell me about water")
		self.assertEqual(len(plan["chat"]), wt.MAX_CHAT_CHARS)

	def test_a_permanent_failure_stops_further_calls(self):
		client = self.install(None)
		client.outcome = wt.anthropic.AuthenticationError("401")
		self.assertIsNone(wt.llm_plan("16 oz"))
		self.assertFalse(wt.llm_ready(), "kept calling an API that rejected the key")

	def test_a_passing_failure_keeps_the_model_in_play(self):
		client = self.install(None)
		client.outcome = wt.anthropic.APITimeoutError("slow")
		self.assertIsNone(wt.llm_plan("16 oz"))
		self.assertTrue(wt.llm_ready(), "gave up after one timeout")

	def test_every_failure_mode_returns_none(self):
		for name in (
			"AuthenticationError", "NotFoundError", "BadRequestError", "RateLimitError",
			"APITimeoutError", "APIConnectionError", "APIStatusError",
		):
			with self.subTest(error=name):
				client = self.install(None)
				client.outcome = getattr(wt.anthropic, name)("boom")
				self.assertIsNone(wt.llm_plan("16 oz"))

	def test_an_unexpected_error_returns_none_rather_than_escaping(self):
		# The SDK raises a bare TypeError at request time when it cannot
		# resolve credentials. Letting that out would leave the reply
		# unanswered with its row already consumed.
		client = self.install(None)
		client.outcome = TypeError("could not resolve authentication method")
		self.assertIsNone(wt.llm_plan("16 oz"))
		self.assertFalse(wt.llm_ready(), "kept calling an SDK that cannot authenticate")


class LlmCheckTest(LlmTestCase):
	"""The startup probe: does it report what will actually happen?"""

	def test_a_working_setup_names_the_model(self):
		self.install()
		working, how = wt.llm_check()
		self.assertTrue(working)
		self.assertIn(wt.LLM_MODEL, how)

	def test_probes_by_counting_tokens_rather_than_sending_a_message(self):
		# Counting authenticates exactly like a real request but is free, and
		# must not be mistaken for interpretation work.
		client = self.install()
		wt.llm_check()
		self.assertEqual(len(client.counts), 1)
		self.assertEqual(client.counts[0]["model"], wt.LLM_MODEL)
		self.assertEqual(client.requests, [], "spent a real message on the probe")
		self.assertEqual(client.options[0]["timeout"], wt.LLM_TIMEOUT)

	def test_switched_off_is_a_pass_without_a_round_trip(self):
		client = self.install()
		wt.LLM_MODE = "off"
		working, how = wt.llm_check()
		self.assertTrue(working, "reported a configured choice as a fault")
		self.assertIn("WATER_LLM=off", how)
		self.assertEqual(client.counts, [], "probed the API while switched off")

	def test_a_missing_package_is_a_failure(self):
		wt.anthropic = None
		working, how = wt.llm_check()
		self.assertFalse(working)
		self.assertIn("not installed", how)

	def test_each_rejection_is_reported_with_its_remedy(self):
		# The specific cases are all subclasses of APIStatusError, so this also
		# pins the order of the except chain: catch the base first and every
		# one of these degrades to a bare status code with no remedy.
		for name, expected in (
			("AuthenticationError", "ANTHROPIC_API_KEY"),
			("PermissionDeniedError", "permission"),
			("NotFoundError", wt.LLM_MODEL),
			("APIConnectionError", "cannot reach"),
			("APITimeoutError", "cannot reach"),
			("APIStatusError", "503"),
		):
			with self.subTest(error=name):
				client = self.install()
				client.count_outcome = getattr(wt.anthropic, name)("boom")
				working, how = wt.llm_check()
				self.assertFalse(working)
				self.assertIn(expected, how)
				if name != "APIStatusError":
					self.assertNotIn("API error", how, "fell through to the generic branch")

	def test_no_credentials_anywhere_gets_the_remedy_not_the_traceback(self):
		# The SDK signals this with a bare TypeError at request time, and it is
		# the ordinary case: nothing exported. Reporting the type name here
		# would bury the one thing worth saying.
		client = self.install()
		client.count_outcome = TypeError(
			"Could not resolve authentication method. Expected one of api_key, "
			"auth_token, or credentials to be set."
		)
		working, how = wt.llm_check()
		self.assertFalse(working)
		self.assertIn("ANTHROPIC_API_KEY", how)
		self.assertNotIn("TypeError", how)

	def test_an_unexpected_error_is_still_a_clean_failure(self):
		client = self.install()
		client.count_outcome = ValueError("something new")
		working, how = wt.llm_check()
		self.assertFalse(working)
		self.assertIn("ValueError", how)

	def test_a_client_that_cannot_be_built_is_reported_once(self):
		# llm_client would otherwise print its own fallback notice, which the
		# probe is about to phrase better itself.
		self.install(broken=True)
		captured = io.StringIO()
		with redirect_stdout(captured):
			working, how = wt.llm_check()
		self.assertFalse(working)
		self.assertIn("no credentials", how)
		self.assertEqual(captured.getvalue(), "", "printed over its own report")

	def test_a_failed_probe_leaves_interpretation_in_play(self):
		# A probe can fail for reasons a message never will, so a bad network
		# moment at startup must not switch Claude off for the whole run.
		client = self.install()
		client.count_outcome = wt.anthropic.APIConnectionError("offline")
		self.assertFalse(wt.llm_check()[0])
		self.assertTrue(wt.llm_ready(), "gave up on Claude over one failed probe")


class FollowPlanTest(TrackerTestCase):
	"""Acting on a plan, and falling back when there is none."""

	def plan(self, action, **fields):
		full = {"action": action, "ounces": None, "goal_oz": None, "chat": None}
		full.update(fields)
		self.patch(wt, "llm_plan", lambda text: full)

	def test_a_log_plan_records_the_amount(self):
		self.plan("log", ounces=24)
		self.tracker.handle_reply("finished the one on my desk")
		self.assertEqual(self.tracker.total(), 24)
		self.assertIn("Logged 24 oz", self.sent[-1])

	def test_a_goal_plan_changes_the_goal(self):
		self.plan("goal", goal_oz=120)
		self.tracker.handle_reply("lets aim higher from now on")
		self.assertEqual(self.tracker.goal, 120)

	def test_chat_uses_the_models_words_and_the_logs_numbers(self):
		self.plan("chat", chat="Coffee counts, and so does tea.")
		self.tracker.handle_reply("does coffee count")
		self.assertIn("Coffee counts", self.sent[-1])
		self.assertIn("0/100 oz", self.sent[-1], "the progress line comes from the log")
		self.assertEqual(self.tracker.total(), 0)

	def test_each_action_reaches_its_handler(self):
		for action, expected in (
			("status", "Today:"),
			("week", "oz/day average"),
			("pause", "Paused"),
			("resume", "Reminders back on"),
			("later", "check back later"),
			("undo", "Nothing logged"),
		):
			with self.subTest(action=action):
				self.plan(action)
				self.tracker.handle_reply("something")
				self.assertIn(expected, self.sent[-1])

	def test_no_plan_falls_back_to_pattern_matching(self):
		self.patch(wt, "llm_plan", lambda text: None)
		self.tracker.handle_reply("just had a couple glasses")
		self.assertEqual(self.tracker.total(), 16, "patterns did not take over")

	def test_a_bare_amount_never_reaches_the_model(self):
		asked = []
		self.patch(wt, "llm_plan", lambda text: asked.append(text) or None)
		self.tracker.handle_reply("28 oz")
		self.assertEqual(asked, [], "spent a round trip on a plain amount")
		self.assertEqual(self.tracker.total(), 28)

	def test_the_model_sees_the_message_as_typed(self):
		# Deliberately not a bare amount: that would be answered by the fast
		# path and never reach the model at all.
		seen = []
		self.patch(wt, "llm_plan", lambda text: seen.append(text) or None)
		self.tracker.handle_reply("  Finished My Coffee  ")
		self.assertEqual(seen, ["Finished My Coffee"], "case and padding should survive")


class MarkerTest(TrackerTestCase):
	"""Outgoing messages have to be self-identifying, late echoes included."""

	def setUp(self):
		super().setUp()
		self.osascript = []

		class Result:
			returncode = 0
			stderr = ""

		def fake_run(command, **kwargs):
			self.osascript.append(command[-1])
			return Result()

		self.patch(wt.subprocess, "run", fake_run)
		# The real send, not the capturing stub the base class installs.
		self.tracker.send = lambda message: wt.WaterTracker.send(self.tracker, message)

	def test_every_outgoing_message_carries_the_marker(self):
		self.tracker.handle_reply("16 oz")
		self.tracker.handle_reply("status")
		self.tracker.handle_reply("lol")
		self.assertEqual(len(self.osascript), 3)
		for message in self.osascript:
			self.assertTrue(message.startswith(wt.MARKER), f"unmarked: {message!r}")

	def test_the_marker_is_not_doubled(self):
		self.tracker.send(f"{wt.MARKER} already marked")
		self.assertEqual(self.osascript[-1].count(wt.MARKER), 1)


class EchoTest(TrackerTestCase):
	"""Texting your own number means reading your own sends back as replies."""

	def test_our_own_message_is_recognised_once(self):
		self.tracker.state["sent_echoes"].append([time.time(), "💧 Hydrate."])
		self.assertTrue(self.tracker.is_echo("💧 Hydrate."))
		self.assertFalse(self.tracker.is_echo("💧 Hydrate."), "one send matched two incoming rows")

	def test_a_real_reply_is_not_mistaken_for_an_echo(self):
		self.tracker.state["sent_echoes"].append([time.time(), "💧 Hydrate."])
		self.assertFalse(self.tracker.is_echo("16 oz"))

	def test_records_expire_instead_of_accumulating(self):
		self.tracker.state["sent_echoes"] = [
			[time.time() - wt.ECHO_WINDOW_SEC - 1, "old"],
			[time.time(), "new"],
		]
		self.tracker.forget_stale_echoes()
		self.assertEqual([message for _, message in self.tracker.state["sent_echoes"]], ["new"])

	def test_a_burst_of_sends_does_not_evict_a_pending_record(self):
		# The old 20-message cap dropped the oldest record regardless of age.
		self.tracker.state["sent_echoes"].append([time.time(), "first"])
		for number in range(30):
			self.tracker.state["sent_echoes"].append([time.time(), f"nudge {number}"])
		self.assertTrue(self.tracker.is_echo("first"))

	def test_state_written_in_the_old_format_is_discarded(self):
		self.tracker.state["sent_echoes"] = ["a bare string from an older version"]
		self.tracker.save()
		self.assertEqual(wt.WaterTracker().state["sent_echoes"], [])


class HandleReplyTest(TrackerTestCase):
	def test_logs_an_amount_and_reports_what_is_left(self):
		self.tracker.handle_reply("just had a couple glasses")
		self.assertEqual(self.tracker.total(), 16)
		self.assertIn("84 oz to go", self.sent[-1])

	def test_sets_the_goal_without_logging_it(self):
		self.tracker.handle_reply("set my goal to 64 oz")
		self.assertEqual(self.tracker.goal, 64)
		self.assertEqual(self.tracker.total(), 0)

	def test_goal_word_next_to_an_amount_still_logs(self):
		# "my goal, i drank 16 oz" is a log, not a goal change: the comma ends
		# the goal phrase.
		self.tracker.handle_reply("my goal, i drank 16 oz")
		self.assertEqual(self.tracker.goal, 100)
		self.assertEqual(self.tracker.total(), 16)

	def test_one_reply_can_log_and_pause(self):
		self.tracker.handle_reply("had 20 oz, you can stop for today")
		self.assertEqual(self.tracker.total(), 20)
		self.assertEqual(self.tracker.state["paused_on"], self.tracker.today())
		self.assertIn("Paused", self.sent[-1])

	def test_undo_drops_the_last_entry_only(self):
		self.tracker.handle_reply("16 oz")
		self.tracker.handle_reply("8 oz")
		self.tracker.handle_reply("scratch that")
		self.assertEqual(self.tracker.total(), 16)

	def test_undo_on_an_empty_day_is_harmless(self):
		self.tracker.handle_reply("undo")
		self.assertIn("Nothing logged", self.sent[-1])
		self.assertEqual(self.tracker.total(), 0)

	def test_resume_clears_a_pause(self):
		self.tracker.handle_reply("pause")
		self.tracker.handle_reply("resume")
		self.assertIsNone(self.tracker.state["paused_on"])

	def test_goal_is_celebrated_once_a_day(self):
		self.tracker.handle_reply("100 oz")
		self.assertIn("Goal hit", self.sent[-1])
		self.tracker.handle_reply("8 oz")
		self.assertNotIn("Goal hit", self.sent[-1])
		self.assertIn("past your goal", self.sent[-1])

	def test_raising_the_goal_allows_a_fresh_celebration(self):
		self.tracker.handle_reply("100 oz")
		self.tracker.handle_reply("goal 120")
		self.tracker.handle_reply("20 oz")
		self.assertIn("Goal hit", self.sent[-1])

	def test_confirming_without_an_amount_logs_a_default_serving(self):
		self.tracker.handle_reply("done")
		self.assertEqual(self.tracker.total(), wt.DEFAULT_SERVING_OZ)
		self.assertIn("text an amount to be exact", self.sent[-1])

	def test_an_amount_beats_a_bare_confirmation(self):
		self.tracker.handle_reply("yep, drank 20 oz")
		self.assertEqual(self.tracker.total(), 20)

	def test_a_negated_amount_is_not_logged(self):
		self.tracker.handle_reply("i havent had 16 oz yet")
		self.assertEqual(self.tracker.total(), 0)
		self.assertIn("check back later", self.sent[-1])

	def test_not_yet_pushes_the_next_nudge_out_without_pausing(self):
		before = self.tracker.state["last_nudge_at"]
		self.tracker.handle_reply("not yet")
		self.assertGreater(self.tracker.state["last_nudge_at"], before)
		self.assertIsNone(self.tracker.state["paused_on"], "a snooze is not a pause")
		self.assertEqual(self.tracker.total(), 0)

	def test_unparseable_reply_asks_again_without_logging(self):
		self.tracker.handle_reply("lol")
		self.assertIn("Didn't catch", self.sent[-1])
		self.assertEqual(self.tracker.total(), 0)


class StreakTest(TrackerTestCase):
	def test_counts_back_from_the_last_day_at_goal(self):
		today = date.today()
		for offset in (1, 2, 3):
			self.set_day(today - timedelta(days=offset), 100)
		self.assertEqual(self.tracker.streak(), 3)

	def test_today_counts_once_the_goal_is_met(self):
		self.set_day(date.today() - timedelta(days=1), 100)
		self.tracker.add(100, "test")
		self.assertEqual(self.tracker.streak(), 2)

	def test_a_short_day_breaks_the_streak(self):
		today = date.today()
		self.set_day(today - timedelta(days=1), 100)
		self.set_day(today - timedelta(days=2), 20)
		self.set_day(today - timedelta(days=3), 100)
		self.assertEqual(self.tracker.streak(), 1)


class WeekTest(TrackerTestCase):
	def test_lists_recent_days_oldest_first_with_an_average(self):
		today = date.today()
		self.set_day(today - timedelta(days=1), 100)
		self.set_day(today - timedelta(days=2), 50)
		lines = self.tracker.week_lines(3)

		self.assertEqual(len(lines), 4, "3 days plus a summary")
		self.assertIn("50 oz", lines[0])
		self.assertIn("100 oz", lines[1])
		self.assertIn("*", lines[1], "a day at goal is marked")
		self.assertNotIn("*", lines[0])
		self.assertIn("50 oz/day average, 1/3 days at goal", lines[-1])

	def test_days_with_nothing_logged_count_as_zero(self):
		lines = self.tracker.week_lines(7)
		self.assertIn("0 oz/day average, 0/7 days at goal", lines[-1])
		self.assertEqual(len(lines), 8)

	def test_asking_by_text_answers_with_the_week(self):
		self.set_day(date.today() - timedelta(days=1), 64)
		self.tracker.handle_reply("what was my weekly average")
		self.assertIn("oz/day average", self.sent[-1])
		self.assertEqual(self.tracker.total(), 0, "a question logged an amount")


class NormalisePhotoTest(unittest.TestCase):
	"""The sips conversion, run for real: HEIC is what iPhones actually send."""

	def setUp(self):
		folder = tempfile.TemporaryDirectory()
		self.addCleanup(folder.cleanup)
		self.folder = Path(folder.name)

	def seed_png(self, width=64, height=64):
		"""A valid PNG, built here so the suite carries no binary fixtures.

		Written by hand rather than pasted as base64: a 2x2 image is a legal
		PNG that sips refuses to read (pixelWidth comes back nil), which
		skipped the HEIC test — the one thing in this class worth testing.
		"""
		raw = b"".join(b"\x00" + bytes((70, 130, 180)) * width for _ in range(height))

		def chunk(tag, data):
			body = tag + data
			return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

		path = self.folder / "seed.png"
		path.write_bytes(
			b"\x89PNG\r\n\x1a\n"
			+ chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
			+ chunk(b"IDAT", zlib.compress(raw, 9))
			+ chunk(b"IEND", b"")
		)
		return path

	def make_image(self, name, fmt):
		"""A real image on disk, converted by sips so no library is needed."""
		target = self.folder / name
		done = subprocess.run(
			["sips", "-s", "format", fmt, str(self.seed_png()), "--out", str(target)],
			capture_output=True, text=True,
		)
		if done.returncode != 0 or not target.exists():
			self.skipTest(f"this machine's sips cannot write {fmt}")
		return target

	def test_converts_heic_to_jpeg(self):
		# The whole reason this function exists: the API takes JPEG, PNG, GIF
		# and WebP, and an iPhone sends HEIC.
		heic = self.make_image("IMG_0001.HEIC", "heic")
		data = wt.normalise_photo(heic)
		self.assertIsNotNone(data, "HEIC came back unconverted")
		self.assertTrue(data.startswith(b"\xff\xd8\xff"), "not JPEG bytes")

	def test_converts_png_too(self):
		data = wt.normalise_photo(self.make_image("shot.png", "png"))
		self.assertTrue(data.startswith(b"\xff\xd8\xff"))

	def test_a_missing_file_is_not_fatal(self):
		self.assertIsNone(wt.normalise_photo(self.folder / "nope.heic"))

	def test_a_file_that_is_not_an_image_is_not_fatal(self):
		junk = self.folder / "notes.heic"
		junk.write_bytes(b"this is not an image at all")
		self.assertIsNone(wt.normalise_photo(junk))


class PhotoAmountTest(unittest.TestCase):
	"""Turning a container into ounces, and where that number comes from."""

	def test_a_known_container_uses_the_units_table(self):
		# The design rule: a recognised container resolves to the same figure
		# typing its name would, not to whatever the model guessed.
		for container, expected in (("nalgene", 32.0), ("pint", 16.0), ("mug", 10.0),
		                            ("can", 12.0), ("hydroflask", 32.0), ("glass", 8.0)):
			with self.subTest(container=container):
				ounces, how = wt.photo_amount(
					{"container": container, "ounces": 999, "note": None}
				)
				self.assertEqual(ounces, expected, "used the model's number over the table")
				self.assertIn(container, how)

	def test_plurals_and_capitals_still_match_the_table(self):
		for container in ("Nalgene", "PINTS", " mugs "):
			with self.subTest(container=container):
				ounces, _ = wt.photo_amount({"container": container, "ounces": 999, "note": None})
				self.assertIn(ounces, (32.0, 16.0, 10.0))

	def test_an_unknown_container_falls_back_to_the_estimate(self):
		ounces, how = wt.photo_amount(
			{"container": "insulated growler", "ounces": 40, "note": None}
		)
		self.assertEqual(ounces, 40.0)
		self.assertIn("guessed", how, "an estimate was not labelled as one")
		self.assertIn("growler", how)

	def test_a_bare_measure_is_not_treated_as_a_container(self):
		# "oz" is in UNITS at 1.0. Matching it would log one ounce for a photo
		# of a bottle, which is worse than falling back to the estimate.
		ounces, how = wt.photo_amount({"container": "oz", "ounces": 24, "note": None})
		self.assertEqual(ounces, 24.0)
		self.assertIn("guessed", how)

	def test_nothing_usable_is_none(self):
		for plan in (
			{"container": None, "ounces": None, "note": "no container here"},
			{"container": "growler", "ounces": 0, "note": None},
			{"container": "growler", "ounces": -5, "note": None},
			{"container": "growler", "ounces": 9000, "note": None},
			{"container": "growler", "ounces": "big", "note": None},
			{"container": None, "ounces": None, "note": None},
		):
			with self.subTest(plan=plan):
				self.assertIsNone(wt.photo_amount(plan))


class LogPhotoTest(TrackerTestCase):
	"""Answering a photo, including when it cannot be read."""

	def setUp(self):
		super().setUp()
		self.patch(wt, "PHOTOS", True)
		self.photo = self.state_path.with_name("IMG.HEIC")
		self.photo.write_bytes(b"stand-in")

	def stub(self, plan):
		self.patch(wt, "llm_photo_plan", lambda path: plan)
		self.patch(wt, "llm_ready", lambda: True)

	def test_a_recognised_container_is_logged_with_its_table_value(self):
		self.stub({"container": "nalgene", "ounces": 30, "note": "a big blue flask"})
		self.tracker.log_photo(self.photo)
		self.assertEqual(self.tracker.total(), 32)
		self.assertIn("correct it", self.sent[-1], "logged a guess without inviting a fix")

	def test_an_unreadable_photo_is_answered_not_ignored(self):
		# There is no pattern matching under this path, so silence would look
		# exactly like a tracker that had stopped working.
		self.stub(None)
		self.tracker.log_photo(self.photo)
		self.assertEqual(self.tracker.total(), 0)
		self.assertIn("Couldn't read", self.sent[-1])

	def test_a_photo_with_no_container_asks_rather_than_guesses(self):
		self.stub({"container": None, "ounces": None, "note": "that is a cat"})
		self.tracker.log_photo(self.photo)
		self.assertEqual(self.tracker.total(), 0)
		self.assertIn("How much", self.sent[-1])
		self.assertIn("cat", self.sent[-1], "dropped the model's explanation")

	def test_switched_off_says_so(self):
		self.patch(wt, "PHOTOS", False)
		self.tracker.log_photo(self.photo)
		self.assertIn("switched off", self.sent[-1])
		self.assertEqual(self.tracker.total(), 0)

	def test_claude_unavailable_says_so(self):
		self.patch(wt, "llm_ready", lambda: False)
		self.tracker.log_photo(self.photo)
		self.assertIn("unavailable", self.sent[-1])
		self.assertEqual(self.tracker.total(), 0)

	def test_a_photo_with_a_caption_is_read_as_text(self):
		# A caption means the pattern matching can do the job for free, so the
		# photo path is skipped entirely.
		self.stub({"container": "nalgene", "ounces": 32, "note": None})
		self.tracker.handle_reply("16 oz", [self.photo])
		self.assertEqual(self.tracker.total(), 16, "spent a vision call on a captioned amount")


class PhotoRequestTest(LlmTestCase):
	"""The vision request itself, against the stand-in client."""

	def setUp(self):
		super().setUp()
		self.patch_photos(True)
		folder = tempfile.TemporaryDirectory()
		self.addCleanup(folder.cleanup)
		self.jpeg = Path(folder.name) / "x.jpg"
		self.jpeg.write_bytes(b"\xff\xd8\xff" + b"0" * 40)
		self.patch(wt, "normalise_photo", lambda path: self.jpeg.read_bytes())

	def patch(self, module, name, value):
		previous = getattr(module, name)
		setattr(module, name, value)
		self.addCleanup(setattr, module, name, previous)

	def patch_photos(self, value):
		self.patch(wt, "PHOTOS", value)

	def test_sends_the_image_as_base64_jpeg(self):
		client = self.install(fake_answer({"container": "pint", "ounces": 16, "note": None}))
		wt.llm_photo_plan(self.jpeg)

		request = client.requests[0]
		block = request["messages"][0]["content"][0]
		self.assertEqual(block["type"], "image")
		self.assertEqual(block["source"]["type"], "base64")
		self.assertEqual(block["source"]["media_type"], "image/jpeg")
		self.assertEqual(base64.b64decode(block["source"]["data"]), self.jpeg.read_bytes())
		self.assertEqual(request["output_config"]["format"]["schema"], wt.PHOTO_SCHEMA)
		self.assertIn("container", request["system"])

	def test_allows_longer_than_the_text_path(self):
		# An image is a much bigger request and has no fallback beneath it.
		client = self.install(fake_answer({"container": "pint", "ounces": 16, "note": None}))
		wt.llm_photo_plan(self.jpeg)
		self.assertGreater(client.options[0]["timeout"], wt.LLM_TIMEOUT)
		self.assertEqual(client.options[0]["max_retries"], 0)

	def test_switched_off_makes_no_request(self):
		client = self.install(fake_answer({"container": "pint", "ounces": 16, "note": None}))
		self.patch_photos(False)
		self.assertIsNone(wt.llm_photo_plan(self.jpeg))
		self.assertEqual(client.requests, [])

	def test_a_model_without_vision_is_reported_not_raised(self):
		client = self.install(None)
		client.outcome = wt.anthropic.BadRequestError("no vision on this model")
		self.assertIsNone(wt.llm_photo_plan(self.jpeg))

	def test_every_failure_mode_returns_none(self):
		for name in ("AuthenticationError", "NotFoundError", "BadRequestError",
		             "APITimeoutError", "APIConnectionError", "APIStatusError"):
			with self.subTest(error=name):
				client = self.install(None)
				self.patch(wt, "normalise_photo", lambda path: b"\xff\xd8\xfftest")
				client.outcome = getattr(wt.anthropic, name)("boom")
				self.assertIsNone(wt.llm_photo_plan(self.jpeg))

	def test_an_unconvertible_photo_never_reaches_the_api(self):
		client = self.install(fake_answer({"container": "pint", "ounces": 16, "note": None}))
		self.patch(wt, "normalise_photo", lambda path: None)
		self.assertIsNone(wt.llm_photo_plan(self.jpeg))
		self.assertEqual(client.requests, [], "uploaded an image that failed to convert")


class ReadRepliesTest(TrackerTestCase):
	"""The reply path against a stand-in for the Messages database."""

	def setUp(self):
		super().setUp()
		self.db_path = self.state_path.with_name("chat.db")
		self.patch(wt, "CHAT_DB", self.db_path)
		# WAL mode with the connection held open, as Messages runs it: commits
		# land in the -wal sidecar and are not checkpointed into chat.db, which
		# is what makes copying the sidecars necessary.
		self.db = sqlite3.connect(self.db_path)
		self.addCleanup(self.db.close)
		self.db.execute("PRAGMA journal_mode=WAL")
		self.db.execute("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT)")
		self.db.execute(
			"CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, "
			"attributedBody BLOB, handle_id INTEGER, is_from_me INTEGER, date INTEGER)"
		)
		self.db.execute(
			"CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT, mime_type TEXT)"
		)
		self.db.execute(
			"CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER)"
		)
		self.db.execute("INSERT INTO handle VALUES (1, ?)", (self.tracker.phone,))
		self.db.execute("INSERT INTO handle VALUES (2, '+15559998888')")
		self.db.commit()

	def add_message(self, text, handle=1, from_me=0, blob=None, minutes_ago=0):
		apple_time = int((time.time() - minutes_ago * 60 - wt.APPLE_EPOCH) * 1_000_000_000)
		self.db.execute(
			"INSERT INTO message (text, attributedBody, handle_id, is_from_me, date) "
			"VALUES (?, ?, ?, ?, ?)",
			(text, blob, handle, from_me, apple_time),
		)
		self.db.commit()

	def attach(self, name="IMG_0001.HEIC", mime="image/heic", make_file=True):
		"""Hang an attachment off the most recent message, as Messages does."""
		message_id = self.db.execute("SELECT MAX(ROWID) FROM message").fetchone()[0]
		path = self.state_path.with_name(name)
		if make_file:
			path.write_bytes(b"not really an image")
		self.db.execute("INSERT INTO attachment (filename, mime_type) VALUES (?, ?)",
		                (str(path), mime))
		attachment_id = self.db.execute("SELECT MAX(ROWID) FROM attachment").fetchone()[0]
		self.db.execute("INSERT INTO message_attachment_join VALUES (?, ?)",
		                (message_id, attachment_id))
		self.db.commit()
		return path

	def test_a_broken_attachment_table_does_not_stop_text_replies(self):
		# Photos are a feature; reading replies is the product. A failure in
		# the attachment join has to cost the photos and nothing else.
		self.db.execute("DROP TABLE message_attachment_join")
		self.db.commit()
		self.add_message("16 oz")
		self.assertEqual(self.bodies(), ["16 oz"], "lost every reply over a photo query")

	def test_a_photo_is_read_as_an_attachment_not_an_empty_message(self):
		self.add_message("")
		path = self.attach()
		self.assertEqual(self.photos(), [[path]])

	def test_the_attachment_placeholder_character_is_not_a_body(self):
		# Messages puts U+FFFC where the attachment sits; left in, it reads as
		# a body and hides the photo behind it.
		self.add_message("￼")
		self.attach()
		rows = self.tracker.read_replies()
		self.assertEqual(rows[0][1], "", "the placeholder was treated as text")
		self.assertEqual(len(rows[0][2]), 1)

	def test_a_video_is_not_offered_as_a_photo(self):
		self.add_message("")
		self.attach(name="clip.mov", mime="video/quicktime")
		self.assertEqual(self.photos(), [], "queued a video for the vision model")

	def test_an_attachment_whose_file_is_gone_is_skipped(self):
		self.add_message("")
		self.attach(make_file=False)
		self.assertEqual(self.photos(), [], "queued a path that does not exist")

	def test_a_captioned_photo_keeps_both_halves(self):
		self.add_message("half of this")
		path = self.attach()
		rows = self.tracker.read_replies()
		self.assertEqual(rows[0][1], "half of this")
		self.assertEqual(rows[0][2], [path])

	def bodies(self):
		return [body for _, body, _ in self.tracker.read_replies()]

	def photos(self):
		return [paths for _, _, paths in self.tracker.read_replies()]

	def count_copies(self):
		"""Start counting database copies; returns the growing list."""
		copies = []
		original = shutil.copy2

		class CountingShutil:
			def copy2(self, source, target):
				copies.append(source)
				return original(source, target)

		self.patch(wt, "shutil", CountingShutil())
		return copies

	def test_reads_each_new_message_once(self):
		self.add_message("16 oz")
		self.assertEqual(self.bodies(), ["16 oz"])
		self.add_message("8 oz")
		self.assertEqual(self.bodies(), ["8 oz"], "an already-handled message came back")

	def test_ignores_other_senders_and_our_own_outgoing_rows(self):
		self.add_message("16 oz", handle=2)
		self.add_message("32 oz", from_me=1)
		self.add_message("64 oz")
		self.assertEqual(self.bodies(), ["64 oz"])

	def test_matches_a_handle_written_in_another_format(self):
		with closing(sqlite3.connect(self.db_path)) as db:
			db.execute("UPDATE handle SET id = '(555) 123-4567' WHERE ROWID = 1")
			db.commit()
		self.add_message("16 oz")
		self.assertEqual(self.bodies(), ["16 oz"])

	def test_our_own_nudge_read_back_is_not_a_reply(self):
		# Texting your own number: the send shows up again as an incoming row.
		nudge = "💧 Hydrate. ░░░░░░░░░░ 0/100 oz (0%)"
		self.tracker.state["sent_echoes"].append([time.time(), nudge])
		self.add_message(nudge)
		self.assertEqual(self.bodies(), [])

	def test_our_own_message_read_back_late_is_still_not_a_reply(self):
		# The echo record expires but the copy stays in the database forever,
		# and our own text is full of amounts.
		own = f"{wt.MARKER} Logged 16 oz. ████░░░░░░ 48/100 oz (48%) — 52 oz to go."
		self.assertIsNotNone(wt.extract_ounces(own), "premise: this text parses as an amount")
		self.add_message(own)
		self.assertEqual(self.bodies(), [])

	def test_a_reply_from_hours_ago_is_not_logged_today(self):
		self.add_message("16 oz", minutes_ago=wt.STALE_REPLY_MIN + 1)
		self.add_message("8 oz", minutes_ago=1)
		self.assertEqual(self.bodies(), ["8 oz"])

	def test_a_message_without_a_timestamp_is_still_read(self):
		self.db.execute("INSERT INTO message (text, handle_id, is_from_me) VALUES ('16 oz', 1, 0)")
		self.db.commit()
		self.assertEqual(self.bodies(), ["16 oz"])

	def test_falls_back_to_the_attributed_body(self):
		text = "24 oz"
		blob = b"streamtyped\x81\xe8\x03NSString\x01\x94\x84\x01+" + bytes([len(text)]) + text.encode()
		self.add_message(None, blob=blob)
		self.assertEqual(self.bodies(), [text])

	def test_history_is_skipped_on_first_run(self):
		self.add_message("16 oz from last week")
		fresh = wt.WaterTracker()
		fresh.prime_replies()
		self.assertEqual(fresh.read_replies(), [])

	def test_unchanged_database_is_not_copied_again(self):
		copies = self.count_copies()
		self.add_message("16 oz")
		self.assertEqual(self.bodies(), ["16 oz"])
		after_read = len(copies)
		self.assertGreater(after_read, 0)

		self.assertEqual(self.bodies(), [])
		self.assertEqual(len(copies), after_read, "copied a database that had not changed")

		# The insert above sits in the -wal sidecar, so this only works if the
		# stamp watches the sidecars and not chat.db alone.
		self.add_message("8 oz")
		self.assertEqual(self.bodies(), ["8 oz"], "missed a message after skipping a copy")
		self.assertGreater(len(copies), after_read)

	def test_an_unreadable_stamp_does_not_skip_the_read(self):
		self.add_message("16 oz")
		self.patch(self.tracker, "db_stamp", lambda: None)
		self.assertEqual(self.bodies(), ["16 oz"])
		self.assertEqual(self.bodies(), [], "the cursor did not advance")


class PruneTest(TrackerTestCase):
	def test_drops_days_past_the_window_and_keeps_the_rest(self):
		today = date.today()
		self.set_day(today - timedelta(days=wt.KEEP_DAYS + 5), 100)
		self.set_day(today - timedelta(days=wt.KEEP_DAYS - 5), 100)
		self.set_day(today, 20)

		self.assertEqual(self.tracker.prune_days(), 1)
		remaining = sorted(self.tracker.state["days"])
		self.assertEqual(remaining, sorted([
			(today - timedelta(days=wt.KEEP_DAYS - 5)).isoformat(),
			today.isoformat(),
		]))

	def test_nothing_to_prune_is_a_no_op(self):
		self.set_day(date.today(), 20)
		self.assertEqual(self.tracker.prune_days(), 0)
		self.assertEqual(self.tracker.total(), 20)


class WakeTest(TrackerTestCase):
	"""Texting 'awake' starts the day, and paces it from then."""

	def iso(self, hour, minute=0, day_offset=0):
		when = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
		return (when + timedelta(days=day_offset)).isoformat(timespec="seconds")

	def test_wake_words_are_recognised(self):
		for text in ("awake", "good morning", "morning", "i'm up", "im up",
		             "just woke up", "woke up", "gm"):
			with self.subTest(text=text):
				self.assertEqual(wt.detect_intent(text), "wake")

	def test_mentioning_the_morning_while_logging_is_not_a_wake_signal(self):
		# The whole reason the pattern sits below the amount check.
		self.tracker.handle_reply("had a glass this morning")
		self.assertEqual(self.tracker.total(), 8, "logged nothing; read as a wake signal")
		self.assertIsNone(self.tracker.state["woke_at"])

	def test_texting_awake_records_the_time_and_answers(self):
		self.tracker.handle_reply("awake")
		self.assertIsNotNone(self.tracker.state["woke_at"])
		self.assertIn("Morning", self.sent[-1])

	def test_waking_up_ends_a_pause(self):
		# Yesterday's 'going to bed' must not keep today quiet.
		self.tracker.state["paused_on"] = self.tracker.today()
		self.tracker.handle_reply("awake")
		self.assertIsNone(self.tracker.state["paused_on"])

	def test_waking_up_does_not_nudge_in_the_same_breath(self):
		self.patch(wt, "WAKE_HOUR", 0)
		self.patch(wt, "SLEEP_HOUR", 24)
		self.tracker.state["last_nudge_at"] = 0
		self.tracker.handle_reply("awake")
		before = len(self.sent)
		self.tracker.maybe_remind()
		self.assertEqual(len(self.sent), before, "nudged immediately after saying good morning")

	def test_the_day_starts_when_told_not_at_wake_hour(self):
		self.patch(wt, "WAKE_HOUR", 8)
		self.patch(wt, "SLEEP_HOUR", 22)
		cases = {None: 8.0, self.iso(6, 30): 6.5, self.iso(10, 45): 10.75}
		for woke, expected in cases.items():
			with self.subTest(woke=woke):
				self.tracker.state["woke_at"] = woke
				self.assertAlmostEqual(self.tracker.day_start(), expected, places=2)

	def test_an_early_riser_is_taken_at_their_word(self):
		# Clamping this up to WAKE_HOUR would make waking early feel identical
		# to not texting at all, which is the thing the feature exists to fix.
		self.patch(wt, "WAKE_HOUR", 8)
		self.tracker.state["woke_at"] = self.iso(6, 0)
		self.assertEqual(self.tracker.day_start(), 6.0)

	def test_a_late_nap_cannot_invert_the_day(self):
		self.patch(wt, "WAKE_HOUR", 8)
		self.patch(wt, "SLEEP_HOUR", 22)
		self.tracker.state["woke_at"] = self.iso(23, 30)
		self.assertEqual(self.tracker.day_start(), 22.0, "day_start overtook SLEEP_HOUR")

	def test_yesterdays_wake_time_is_ignored(self):
		self.patch(wt, "WAKE_HOUR", 8)
		self.tracker.state["woke_at"] = self.iso(6, 30, day_offset=-1)
		self.assertEqual(self.tracker.day_start(), 8.0, "paced off a stale wake time")

	def test_an_unreadable_wake_time_falls_back(self):
		self.patch(wt, "WAKE_HOUR", 8)
		for junk in ("not-a-date", "", 12345, None):
			with self.subTest(junk=junk):
				self.tracker.state["woke_at"] = junk
				self.assertEqual(self.tracker.day_start(), 8.0)

	def test_waking_late_lowers_the_pace_target(self):
		self.patch(wt, "WAKE_HOUR", 8)
		self.patch(wt, "SLEEP_HOUR", 22)
		noon = datetime.now().replace(hour=12, minute=0)
		self.tracker.state["woke_at"] = None
		default = self.tracker.expected_by_now(noon)
		self.tracker.state["woke_at"] = self.iso(10, 45)
		late = self.tracker.expected_by_now(noon)
		self.assertLess(late, default, "a late start still expected a full day's water")

	def test_the_window_opens_early_when_told(self):
		# awake() has to follow day_start too, or the pace moves but the
		# nudges still wait for WAKE_HOUR.
		hour = datetime.now().hour
		self.patch(wt, "WAKE_HOUR", (hour + 1) % 24)
		self.patch(wt, "SLEEP_HOUR", 23 if hour < 23 else 24)
		self.tracker.state["woke_at"] = None
		if not self.tracker.awake():
			self.tracker.state["woke_at"] = datetime.now().isoformat(timespec="seconds")
			self.assertTrue(self.tracker.awake(), "still shut out after saying I was up")

	def test_claude_can_report_a_wake_too(self):
		self.assertIn("wake", wt.PLAN_ACTIONS)
		plan = wt.sane_plan({"action": "wake", "ounces": None, "goal_oz": None, "chat": None})
		self.assertEqual(plan["action"], "wake")
		self.tracker.follow_plan(plan)
		self.assertIsNotNone(self.tracker.state["woke_at"])


class RemindTest(TrackerTestCase):
	def setUp(self):
		super().setUp()
		self.patch(wt, "WAKE_HOUR", 0)
		self.patch(wt, "SLEEP_HOUR", 24)

	def test_nudges_then_waits_out_the_interval(self):
		self.tracker.maybe_remind()
		self.assertEqual(len(self.sent), 1)
		self.tracker.maybe_remind()
		self.assertEqual(len(self.sent), 1, "nudged twice inside one interval")

		# Well past any pace-adjusted gap.
		self.tracker.state["last_nudge_at"] = time.time() - wt.INTERVAL_MIN * 60 * 10
		self.tracker.maybe_remind()
		self.assertEqual(len(self.sent), 2)

	def test_pace_target_grows_across_the_day(self):
		self.patch(wt, "WAKE_HOUR", 8)
		self.patch(wt, "SLEEP_HOUR", 20)
		expected = {6: 0, 8: 0, 14: 50, 20: 100, 23: 100}
		for hour, ounces in expected.items():
			with self.subTest(hour=hour):
				at = datetime(2026, 9, 9, hour)
				self.assertAlmostEqual(self.tracker.expected_by_now(at), ounces, places=1)

	def test_being_behind_shortens_the_wait_and_being_ahead_lengthens_it(self):
		self.patch(wt, "WAKE_HOUR", 8)
		self.patch(wt, "SLEEP_HOUR", 20)
		midday = datetime(2026, 9, 9, 14)  # 50 oz of a 100 oz goal due by now
		interval = wt.INTERVAL_MIN * 60

		self.assertEqual(self.tracker.nudge_gap(midday), interval * 0.5)  # 50 oz behind, floored
		self.tracker.add(25, "test")
		self.assertAlmostEqual(self.tracker.nudge_gap(midday), interval * 0.5)  # 25 oz behind
		self.tracker.add(15, "test")
		self.assertAlmostEqual(self.tracker.nudge_gap(midday), interval * 0.8)  # 10 oz behind
		self.tracker.add(20, "test")
		self.assertAlmostEqual(self.tracker.nudge_gap(midday), interval * 1.5)  # ahead of pace

	def test_nudge_says_how_far_behind_you_are(self):
		# maybe_remind reads the real clock, so the deficit is whatever the
		# time of day makes it; the point is that the text agrees with it.
		deficit = self.tracker.expected_by_now()
		self.tracker.maybe_remind()
		if deficit >= 1:
			self.assertIn(f"{deficit:.0f} oz behind pace", self.sent[-1])
		else:
			self.assertNotIn("behind pace", self.sent[-1])

	def test_interval_survives_a_restart(self):
		self.tracker.maybe_remind()
		restarted = wt.WaterTracker()
		restarted.send = self.sent.append
		restarted.maybe_remind()
		self.assertEqual(len(self.sent), 1, "a restart re-nudged immediately")

	def test_quiet_when_paused_asleep_or_done(self):
		self.tracker.state["paused_on"] = self.tracker.today()
		self.tracker.maybe_remind()
		self.assertEqual(self.sent, [])

		self.tracker.state["paused_on"] = None
		self.patch(wt, "WAKE_HOUR", 23)
		self.patch(wt, "SLEEP_HOUR", 24)
		self.tracker.state["last_nudge_at"] = 0
		if not self.tracker.awake():
			self.tracker.maybe_remind()
			self.assertEqual(self.sent, [], "nudged outside the waking window")

		self.patch(wt, "WAKE_HOUR", 0)
		self.patch(wt, "SLEEP_HOUR", 24)
		self.tracker.add(self.tracker.goal, "test")
		self.tracker.maybe_remind()
		self.assertEqual(self.sent, [], "nudged after the goal was met")


class FollowUpTest(TrackerTestCase):
	"""The second text when a nudge goes unanswered."""

	def setUp(self):
		super().setUp()
		self.patch(wt, "WAKE_HOUR", 0)
		self.patch(wt, "SLEEP_HOUR", 24)

	def waited(self, minutes):
		"""Backdate the outstanding nudge as though it had gone unanswered."""
		self.tracker.state["awaiting_reply_since"] = time.time() - minutes * 60

	def chases(self):
		return [text for text in self.sent if "no reply since" in text]

	def test_a_nudge_starts_the_clock_on_a_reply(self):
		# Asserted on the state rather than through waited(), which sets this
		# field itself and would hide a nudge that never armed anything.
		self.assertIsNone(self.tracker.state["awaiting_reply_since"])
		self.tracker.maybe_remind()
		armed = self.tracker.state["awaiting_reply_since"]
		self.assertIsNotNone(armed, "a nudge left nothing awaiting a reply")
		self.assertGreaterEqual(armed, time.time() - 5)
		self.assertFalse(self.tracker.state["followed_up"])

	def test_chases_a_nudge_that_went_unanswered(self):
		self.tracker.maybe_remind()
		self.waited(wt.FOLLOWUP_MIN)
		self.tracker.maybe_remind()
		self.assertEqual(len(self.chases()), 1)
		self.assertIn("Reply with an amount", self.sent[-1])

	def test_stays_quiet_until_the_hour_is_up(self):
		self.tracker.maybe_remind()
		self.waited(wt.FOLLOWUP_MIN - 1)
		self.tracker.maybe_remind()
		self.assertEqual(self.chases(), [], "chased before the wait was over")

	def test_chases_only_once(self):
		self.tracker.maybe_remind()
		self.waited(wt.FOLLOWUP_MIN)
		for _ in range(4):
			self.tracker.maybe_remind()
		self.assertEqual(len(self.chases()), 1, "a chase became a chain")

	def test_any_reply_cancels_the_chase(self):
		# The reply lands after a full hour of silence, so the chase was due
		# and was called off rather than never being armed.
		for reply in ("status", "not yet", "asdf gibberish"):
			with self.subTest(reply=reply):
				self.sent.clear()
				self.tracker.state["awaiting_reply_since"] = None
				self.tracker.state["last_nudge_at"] = 0
				self.tracker.maybe_remind()
				self.waited(wt.FOLLOWUP_MIN)
				self.assertTrue(self.tracker.follow_up_due(), "the chase was never armed")
				self.tracker.handle_reply(reply)
				self.tracker.maybe_remind()
				self.assertEqual(self.chases(), [], "chased someone who had replied")

	def test_a_reply_clears_the_wait_even_when_unparseable(self):
		self.tracker.maybe_remind()
		self.tracker.handle_reply("asdf gibberish")
		self.assertIsNone(self.tracker.state["awaiting_reply_since"])

	def test_the_next_nudge_rearms_the_chase(self):
		self.tracker.maybe_remind()
		self.waited(wt.FOLLOWUP_MIN)
		self.tracker.maybe_remind()
		self.assertEqual(len(self.chases()), 1)

		# Past any pace-adjusted gap, so the paced nudge comes round again.
		self.tracker.state["last_nudge_at"] = time.time() - wt.INTERVAL_MIN * 60 * 10
		self.tracker.maybe_remind()
		self.waited(wt.FOLLOWUP_MIN)
		self.tracker.maybe_remind()
		self.assertEqual(len(self.chases()), 2, "the second nudge was never chased")

	def test_a_due_nudge_wins_over_a_chase(self):
		# Both are due at once when the gap has tightened to the follow-up
		# time. The nudge carries fresh progress, so it should be the one sent
		# and it should not be doubled up with a chase.
		self.tracker.maybe_remind()
		self.tracker.state["last_nudge_at"] = time.time() - wt.INTERVAL_MIN * 60 * 10
		self.waited(wt.FOLLOWUP_MIN * 5)
		self.sent.clear()
		self.tracker.maybe_remind()
		self.assertEqual(len(self.sent), 1)
		self.assertEqual(self.chases(), [], "sent a chase on top of a nudge")

	def test_it_survives_a_restart(self):
		self.tracker.maybe_remind()
		self.waited(wt.FOLLOWUP_MIN)
		self.tracker.save()

		restarted = wt.WaterTracker()
		restarted.send = self.sent.append
		restarted.maybe_remind()
		self.assertEqual(len(self.chases()), 1, "a restart forgot the unanswered nudge")

		# And the restart must not re-chase what it already chased.
		restarted.save()
		again = wt.WaterTracker()
		again.send = self.sent.append
		again.maybe_remind()
		self.assertEqual(len(self.chases()), 1, "a restart chased the same nudge twice")

	def arm_a_chase(self):
		"""Nudge, then let a full hour of silence pass, leaving a chase due."""
		self.tracker.maybe_remind()
		self.waited(wt.FOLLOWUP_MIN)
		self.sent.clear()
		self.assertTrue(self.tracker.follow_up_due(), "the chase was never armed")

	def test_no_chase_while_paused(self):
		self.arm_a_chase()
		self.tracker.state["paused_on"] = self.tracker.today()
		self.tracker.maybe_remind()
		self.assertEqual(self.chases(), [], "chased while paused")

	def test_no_chase_once_the_goal_is_met(self):
		self.arm_a_chase()
		self.tracker.add(self.tracker.goal, "test")
		self.tracker.maybe_remind()
		self.assertEqual(self.chases(), [], "chased after the goal was met")

	def shut_the_window(self):
		"""A waking window that excludes now, whatever the clock says."""
		hour = datetime.now().hour
		self.patch(wt, "WAKE_HOUR", (hour + 1) % 24)
		self.patch(wt, "SLEEP_HOUR", (hour + 2) % 24)
		self.assertFalse(self.tracker.awake(), "the window still contains now")

	def test_a_chase_outlives_the_window_that_started_it(self):
		# The 21:50 case: the nudge went out inside the window and the chase
		# falls after SLEEP_HOUR. Gating the whole of maybe_remind() on awake()
		# meant no nudge in the final hour of the day could ever be chased.
		self.arm_a_chase()
		self.shut_the_window()
		self.tracker.maybe_remind()
		self.assertEqual(len(self.chases()), 1, "a late chase was swallowed by the window")

	def test_a_shut_window_still_blocks_a_fresh_nudge(self):
		# The other half of that split: a chase may finish an exchange after
		# hours, but nothing may start one.
		self.tracker.state["last_nudge_at"] = 0
		self.tracker.state["awaiting_reply_since"] = None
		self.shut_the_window()
		self.tracker.maybe_remind()
		self.assertEqual(self.sent, [], "nudged outside the waking window")

	def test_a_chase_that_missed_its_moment_is_dropped(self):
		# The Mac can sleep straight through the due moment. Waking up hours
		# later to chase last night's nudge is noise, not diligence.
		self.arm_a_chase()
		self.waited(wt.FOLLOWUP_MIN + wt.FOLLOWUP_GRACE_MIN)
		self.assertFalse(self.tracker.follow_up_due())
		self.tracker.maybe_remind()
		self.assertEqual(self.chases(), [], "chased about a question hours old")

	def test_a_chase_inside_the_grace_still_goes(self):
		self.arm_a_chase()
		self.waited(wt.FOLLOWUP_MIN + wt.FOLLOWUP_GRACE_MIN - 1)
		self.tracker.maybe_remind()
		self.assertEqual(len(self.chases()), 1, "a slightly late chase was dropped")

	def test_zero_turns_it_off(self):
		self.patch(wt, "FOLLOWUP_MIN", 0)
		self.tracker.maybe_remind()
		self.waited(600)
		self.tracker.maybe_remind()
		self.assertEqual(self.chases(), [], "chased with follow-ups switched off")

	def test_the_chase_reports_the_wait_and_the_progress(self):
		self.tracker.add(32, "test")
		self.tracker.maybe_remind()
		self.waited(75)
		self.tracker.maybe_remind()
		chase = self.chases()[0]
		self.assertIn("75 min ago", chase)
		self.assertIn(f"32/{self.tracker.goal:g} oz", chase)


if __name__ == "__main__":
	unittest.main()
