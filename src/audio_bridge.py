from __future__ import annotations

import base64
import logging
import queue
import subprocess
import threading

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
CHUNK_MS = 100
CHUNK_FRAMES = SAMPLE_RATE * CHUNK_MS // 1000


class PulseAudioCapture(threading.Thread):
    def __init__(self, source: str, audio_queue: queue.Queue[str], stop_event: threading.Event) -> None:
        super().__init__(daemon=True)
        self.source = source
        self.audio_queue = audio_queue
        self.stop_event = stop_event
        self.process: subprocess.Popen | None = None

    def run(self) -> None:
        bytes_per_chunk = CHUNK_FRAMES * CHANNELS * SAMPLE_WIDTH
        while not self.stop_event.is_set():
            cmd = [
                "pacat",
                "--record",
                f"--device={self.source}",
                f"--rate={SAMPLE_RATE}",
                f"--channels={CHANNELS}",
                "--format=s16le",
                "--raw",
                "--latency-msec=50",
            ]
            logger.info("[Capture] starting: %s", " ".join(cmd))
            self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                while not self.stop_event.is_set() and self.process.stdout:
                    raw = self.process.stdout.read(bytes_per_chunk)
                    if not raw:
                        logger.info("[Capture] stream ended, retrying")
                        break
                    encoded = base64.b64encode(raw).decode("utf-8")
                    try:
                        self.audio_queue.put_nowait(encoded)
                    except queue.Full:
                        continue
            finally:
                if self.process and self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                self.process = None
            if not self.stop_event.is_set():
                self.stop_event.wait(0.25)
        logger.info("[Capture] stopped")

    def stop(self) -> None:
        self.stop_event.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()


class PulseAudioPlayback:
    def __init__(self, sink: str) -> None:
        self.sink = sink
        self.process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        cmd = [
            "pacat",
            "--playback",
            f"--device={self.sink}",
            f"--rate={SAMPLE_RATE}",
            f"--channels={CHANNELS}",
            "--format=s16le",
            "--latency-msec=50",
        ]
        logger.info("[Playback] starting: %s", " ".join(cmd))
        self.process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def _write_raw(self, raw: bytes) -> None:
        self.start()
        if not self.process or not self.process.stdin or self.process.stdin.closed:
            return
        try:
            self.process.stdin.write(raw)
            self.process.stdin.flush()
        except BrokenPipeError:
            logger.warning("[Playback] pacat pipe closed, restarting")
            self.stop()
            self.start()
            if self.process and self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.write(raw)
                self.process.stdin.flush()

    def write(self, audio_base64: str) -> None:
        raw = base64.b64decode(audio_base64)
        with self._lock:
            self._write_raw(raw)

    def silence(self) -> None:
        silence_frames = SAMPLE_RATE // 10
        silence_bytes = b"\x00" * silence_frames * CHANNELS * SAMPLE_WIDTH
        with self._lock:
            self._write_raw(silence_bytes)

    def stop(self) -> None:
        if not self.process:
            return
        try:
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
        except Exception:
            pass
        if self.process.poll() is None:
            self.process.terminate()
        self.process = None
        logger.info("[Playback] stopped")


def drain_audio_queue(audio_queue: queue.Queue[str]) -> None:
    while True:
        try:
            audio_queue.get_nowait()
            audio_queue.task_done()
        except queue.Empty:
            return
