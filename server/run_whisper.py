"""Start WhisperLive (faster-whisper backend) for the caption API.

One shared large-v3-turbo model on the GPU, bound to localhost only: the
caption API on :8000 is the single public entry point.

``--beam-size`` (or WHISPER_BEAM_SIZE) changes faster-whisper's decoding beam,
which WhisperLive does not expose to clients. Every update re-decodes the
unfinished audio, so a smaller beam shortens each pass and with it how soon
interim and final captions come out (see bench_latency.py).

``--min-chunk`` (or WHISPER_MIN_CHUNK) is how much new audio WhisperLive waits
for before decoding (its built-in 1.0 s), which bounds how soon the first
interim caption of a sentence can appear.
"""
import argparse
import functools
import logging
import os

from whisper_live.server import TranscriptionServer
from whisper_live.transcriber.transcriber_faster_whisper import WhisperModel


def set_beam_size(beam_size: int):
    """Make ``beam_size`` the default for every transcribe() call WhisperLive makes."""
    original = WhisperModel.transcribe

    @functools.wraps(original)
    def transcribe(self, audio, *args, **kwargs):
        kwargs.setdefault("beam_size", beam_size)
        kwargs.setdefault("best_of", beam_size)
        return original(self, audio, *args, **kwargs)

    WhisperModel.transcribe = transcribe
    logging.info("faster-whisper beam size: %d", beam_size)


def set_min_chunk(seconds: float):
    """Start decoding once ``seconds`` of new audio are buffered instead of WhisperLive's 1.0 s.

    The threshold is a literal inside ServeClientBase.speech_to_text, so the method is rebuilt
    from its source with the number swapped; if the source ever changes shape, nothing is patched.
    """
    import inspect
    import textwrap
    from whisper_live.backend import base
    source = textwrap.dedent(inspect.getsource(base.ServeClientBase.speech_to_text))
    if source.count("duration < 1.0") != 1:
        logging.warning("WhisperLive changed; minimum chunk left at 1.0 s")
        return
    namespace = {}
    exec(compile(source.replace("duration < 1.0", f"duration < {float(seconds)!r}"),
                 inspect.getsourcefile(base), "exec"), vars(base), namespace)
    base.ServeClientBase.speech_to_text = namespace["speech_to_text"]
    logging.info("WhisperLive minimum chunk: %.2f s", seconds)


def main():
    parser = argparse.ArgumentParser(description="WhisperLive for LiveCaption")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9095)
    # WhisperLive only accepts a path or HF repo id here; this is faster-whisper's "large-v3-turbo".
    parser.add_argument("--model", default=os.environ.get(
        "WHISPER_MODEL_REPO", "mobiuslabsgmbh/faster-whisper-large-v3-turbo"))
    parser.add_argument("--max-clients", type=int, default=4)
    parser.add_argument("--beam-size", type=int, default=int(os.environ.get("WHISPER_BEAM_SIZE", "1")))
    parser.add_argument("--min-chunk", type=float, default=float(os.environ.get("WHISPER_MIN_CHUNK", "1.0")))
    args = parser.parse_args()
    if args.beam_size != 5:
        set_beam_size(args.beam_size)
    if args.min_chunk != 1.0:
        set_min_chunk(args.min_chunk)
    TranscriptionServer().run(
        args.host,
        port=args.port,
        backend="faster_whisper",
        faster_whisper_custom_model_path=args.model,
        single_model=True,
        max_clients=args.max_clients,
        max_connection_time=24 * 3600,   # live streams run for hours; default is 600 s
        cache_path=os.path.expanduser("~/.cache/whisper-live/"),
    )


if __name__ == "__main__":
    main()
