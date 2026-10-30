import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import agent_board as ab
import dictate

NOW = 1_800_000_000.0


def board():
    return ab.Board(ab.Meta(Path(tempfile.mkdtemp()) / "m.json"), "/srv/www/p", now=lambda: NOW)


class FakeRecorder:
    """Stands in for arecord: writes the wav it was asked for when it is stopped, as the real one does."""

    def __init__(self, args, wav=b"RIFF" + b"\0" * 2000, **kwargs):
        self.args, self.kwargs, self.wav, self.signals, self.returncode = args, kwargs, wav, [], None

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        Path(self.args[-1]).write_bytes(self.wav)
        self.returncode = 0

    def communicate(self, timeout=None):
        return b"", b""


def said(text, code=0, err=""):
    return lambda args, **kw: SimpleNamespace(returncode=code, stdout=text, stderr=err)


class DictationKey(unittest.TestCase):
    def test_ctrl_n_toggles_and_ctrl_v_stays_paste(self):
        self.assertEqual(ab.DICTATE_KEY, "ctrl+n")
        self.assertEqual(ab.KEYS[14], "ctrl+n")
        b = board()
        self.assertEqual(b.handle("ctrl+n").kind, "dictate_start")
        b.dictation = "recording"
        self.assertEqual(b.handle("ctrl+n").kind, "dictate_stop")

    def test_ignored_while_transcribing(self):
        b = board()
        b.dictation = "transcribing"
        self.assertIsNone(b.handle("ctrl+n"))
        self.assertIn("still turning", b.msg)

    def test_not_in_the_pr_views(self):
        b = board()
        b.set_tab("review")
        self.assertIsNone(b.handle("ctrl+n"))

    def test_footer_says_it_is_recording(self):
        b = board()
        b.dictation = "recording"
        footer = "".join(t for t, _ in ab._hint_segs(b))
        self.assertIn("recording", footer)
        self.assertIn("ctrl+n stops", footer)


def wav(path, samples):
    Path(path).write_bytes(b"\0" * ab.WAV_HEADER + struct.pack(f"<{len(samples)}h", *samples))


class Waveform(unittest.TestCase):
    def setUp(self):
        self.path = str(Path(tempfile.mkdtemp()) / "speech.wav")

    def test_silence_is_zero_and_loud_speech_is_high(self):
        wav(self.path, [0] * 4000)
        self.assertEqual(ab.audio_level(self.path), 0.0)
        wav(self.path, [8000, -8000] * 2000)
        self.assertEqual(ab.audio_level(self.path), 1.0)

    def test_only_the_last_moments_count(self):
        wav(self.path, [20000, -20000] * 4000 + [0] * 1600)  # loud, then 100 ms of quiet
        self.assertEqual(ab.audio_level(self.path), 0.0)

    def test_quiet_speech_still_moves_the_bars(self):
        wav(self.path, [600, -600] * 2000)
        self.assertGreater(ab.audio_level(self.path), 0.2)

    def test_unreadable_or_header_only_files_are_silent(self):
        self.assertEqual(ab.audio_level(self.path), 0.0)  # not created yet
        Path(self.path).write_bytes(b"\0" * ab.WAV_HEADER)
        self.assertEqual(ab.audio_level(self.path), 0.0)

    def test_the_footer_draws_the_newest_levels_on_the_right(self):
        b = board()
        b.dictation = "recording"
        for level in (0.0, 0.5, 1.0):
            b.add_level(level)
        footer = "".join(t for t, _ in ab._hint_segs(b))
        self.assertIn("▁" * (ab.WAVE_WIDTH - 3) + "▁▅█", footer)

    def test_the_meter_keeps_only_what_fits(self):
        b = board()
        for _ in range(ab.WAVE_WIDTH + 10):
            b.add_level(0.3)
        self.assertEqual(len(b.levels), ab.WAVE_WIDTH)

    def test_a_new_recording_starts_with_an_empty_meter(self):
        b = board()
        b.levels = [1.0] * 5
        ab.start_dictation(b, popen=lambda args, **kw: FakeRecorder(args, **kw))
        self.assertEqual(b.levels, [])
        ab.abort_dictation(b)


class DictatedText(unittest.TestCase):
    def test_lands_at_the_caret_on_the_next_tick(self):
        b = board()
        for ch in "fix":
            b.handle("char", ch)
        b.dictated = "the login bug"
        self.assertEqual(b.input, "fix")  # nothing happens until the main loop ticks
        b.handle("tick")
        self.assertEqual(b.input, "fix the login bug")
        self.assertIn("dictated 3 words", b.msg)

    def test_no_extra_space_after_whitespace_or_in_an_empty_prompt(self):
        b = board()
        b.dictated = "hello"
        b.handle("tick")
        self.assertEqual(b.input, "hello")
        b.handle("ctrl+j")
        b.dictated = "again"
        b.handle("tick")
        self.assertEqual(b.input, "hello\nagain")


class Recording(unittest.TestCase):
    def test_start_uses_the_recorder_setting_and_puts_the_file_last_without_a_placeholder(self):
        b = board()
        b.meta.set_setting("dictate_record", "my-recorder --rate 16000")
        made = []
        ab.start_dictation(b, popen=lambda args, **kw: made.append(args) or FakeRecorder(args, **kw))
        self.assertEqual(made[0][:3], ["my-recorder", "--rate", "16000"])
        self.assertTrue(made[0][3].endswith("speech.wav"))
        self.assertEqual(b.dictation, "recording")
        ab.abort_dictation(b)

    def test_default_recorder_is_arecord(self):
        b = board()
        made = []
        ab.start_dictation(b, popen=lambda args, **kw: made.append(args) or FakeRecorder(args, **kw))
        self.assertEqual(made[0][0], "arecord")
        self.assertTrue(made[0][-1].endswith("speech.wav"))
        ab.abort_dictation(b)

    def test_a_recorder_that_cannot_start_is_reported(self):
        b = board()

        def boom(args, **kw):
            raise FileNotFoundError("arecord")

        ab.start_dictation(b, popen=boom)
        self.assertIsNone(b.dictation)
        self.assertTrue(b.msg.startswith("✗ could not start recording"))

    def test_stop_signals_the_recorder_and_transcribes(self):
        b = board()
        ab.start_dictation(b, popen=lambda args, **kw: FakeRecorder(args, **kw))
        proc, folder = b.dictate["proc"], b.dictate["folder"]
        t = ab.stop_dictation(b, run=said("  open the\nsettings  "))
        t.join(5)
        self.assertEqual(proc.signals, [ab.signal.SIGINT])
        self.assertEqual(b.dictated, "open the settings")
        self.assertIsNone(b.dictation)
        self.assertFalse(Path(folder).exists())  # the recording is not kept

    def test_an_empty_recording_is_reported_without_transcribing(self):
        b = board()
        ab.start_dictation(b, popen=lambda args, **kw: FakeRecorder(args, wav=b"RIFF", **kw))
        ran = []
        t = ab.stop_dictation(b, run=lambda *a, **k: ran.append(a))
        self.assertIsNone(t)
        self.assertEqual(ran, [])
        self.assertTrue(b.msg.startswith("✗ nothing recorded"))
        self.assertIsNone(b.dictation)

    def test_closing_the_board_stops_the_microphone(self):
        b = board()
        ab.start_dictation(b, popen=lambda args, **kw: FakeRecorder(args, **kw))
        proc, folder = b.dictate["proc"], b.dictate["folder"]
        ab.abort_dictation(b)
        self.assertEqual(proc.signals, [ab.signal.SIGINT])
        self.assertFalse(Path(folder).exists())
        self.assertIsNone(b.dictation)


class Transcribing(unittest.TestCase):
    def test_the_command_setting_replaces_the_default(self):
        b = board()
        b.meta.set_setting("dictate_command", "my-stt --lang pl {audio} --plain")
        seen = []
        ab.run_transcription(b, "/srv/www/a.wav", run=lambda args, **kw: seen.append(args) or SimpleNamespace(returncode=0, stdout="cześć", stderr=""))
        self.assertEqual(seen[0], ["my-stt", "--lang", "pl", "/srv/www/a.wav", "--plain"])
        self.assertEqual(b.dictated, "cześć")

    def test_default_command_runs_the_bundled_script(self):
        seen = []
        ab.run_transcription(board(), "/srv/www/a.wav", run=lambda args, **kw: seen.append(args) or SimpleNamespace(returncode=0, stdout="x", stderr=""))
        self.assertTrue(seen[0][1].endswith("dictate.py"))
        self.assertEqual(seen[0][-1], "/srv/www/a.wav")

    def test_failure_shows_the_last_line_of_stderr(self):
        b = board()
        ab.run_transcription(b, "a.wav", run=said("", code=1, err="Traceback\nImportError: no faster_whisper"))
        self.assertEqual(b.msg, "✗ speech to text failed: ImportError: no faster_whisper")
        self.assertEqual(b.dictated, "")

    def test_silence_is_reported(self):
        b = board()
        ab.run_transcription(b, "a.wav", run=said("  \n"))
        self.assertEqual(b.msg, "✗ no speech heard")

    def test_a_missing_command_is_reported(self):
        b = board()

        def boom(*a, **k):
            raise FileNotFoundError("my-stt")

        ab.run_transcription(b, "a.wav", run=boom)
        self.assertTrue(b.msg.startswith("✗ could not turn your speech into text"))


class Settings(unittest.TestCase):
    def test_both_commands_are_in_settings_and_shown_with_their_defaults(self):
        b = board()
        names = dict(b.settings_fields())
        self.assertEqual(names["dictate_record"], "Dictation recorder")
        self.assertEqual(names["dictate_cmd"], "Dictation command")
        self.assertEqual(b.setting_text("dictate_record"), ab.DEFAULT_DICTATE_RECORD)
        self.assertEqual(b.setting_text("dictate_cmd"), ab.DEFAULT_DICTATE_COMMAND)

    def test_editing_saves_and_the_default_text_clears_the_setting(self):
        b = board()
        b._commit_setting("dictate_cmd", "my-stt {audio}")
        self.assertEqual(b.meta.settings["dictate_command"], "my-stt {audio}")
        b._commit_setting("dictate_cmd", ab.DEFAULT_DICTATE_COMMAND)
        self.assertNotIn("dictate_command", b.meta.settings)
        b._commit_setting("dictate_record", "")
        self.assertNotIn("dictate_record", b.meta.settings)

    def test_every_new_key_is_in_the_help(self):
        self.assertTrue(any(k == "ctrl+n" for k, _ in ab.HELP_BY_VIEW["sessions"]))


class DefaultTranscriber(unittest.TestCase):
    def test_usage_error_without_a_file(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(dictate.main(["dictate.py"]), 2)

    def test_missing_faster_whisper_says_how_to_install_it(self):
        with mock.patch.object(dictate, "transcribe", side_effect=ImportError), mock.patch("sys.stderr") as err:
            self.assertEqual(dictate.main(["dictate.py", "a.wav"]), 1)
        self.assertIn("pip install faster-whisper", "".join(c.args[0] for c in err.write.call_args_list))

    def test_prints_the_text(self):
        with mock.patch.object(dictate, "transcribe", return_value="hello"), mock.patch("sys.stdout") as out:
            self.assertEqual(dictate.main(["dictate.py", "a.wav"]), 0)
        self.assertIn("hello", "".join(c.args[0] for c in out.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
