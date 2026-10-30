"""The board's default speech-to-text command: `python3 dictate.py recording.wav` prints what was said.

It needs `pip install faster-whisper`, and is only a default: the board runs whatever command you set under *Dictation command*
(`ctrl+s`), so any client that takes an audio file and prints the text on stdout works instead.
"""

from __future__ import annotations

import os
import sys

MODEL = os.environ.get("AGENT_BOARD_WHISPER_MODEL", "base")  # tiny / base / small / medium / large-v3: bigger is slower and more exact
LANGUAGE = os.environ.get("AGENT_BOARD_WHISPER_LANGUAGE") or None  # None lets the model detect it


def transcribe(path: str) -> str:
    from faster_whisper import WhisperModel  # imported here so the board itself never needs it

    model = WhisperModel(MODEL, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(path, language=LANGUAGE, vad_filter=True)  # the VAD filter drops the silence around the speech
    return " ".join(seg.text.strip() for seg in segments).strip()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: dictate.py AUDIO_FILE", file=sys.stderr)
        return 2
    try:
        print(transcribe(argv[1]))
    except ImportError:
        print("faster-whisper is not installed: pip install faster-whisper", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
