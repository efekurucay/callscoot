# AI agent runbook

CallScoot now ships with an optional `callscoot-agent` helper.

It is responsible for:

- reading phone-call audio from `callscoot.agent.rx.monitor`
- transcribing caller speech
- generating an LLM reply
- synthesizing the reply back to audio
- playing that reply into `callscoot.agent.tx`
- writing transcript + summary files into the call session directory

## Supported providers

### STT

- `mock`
- `openai`
- `whisper_cli`

### LLM

- `mock`
- `openai`
- `ollama`

### TTS

- `mock`
- `openai`
- `espeak`

That means you can mix providers, for example:

- OpenAI STT + OpenAI LLM + OpenAI TTS
- OpenAI STT + Ollama LLM + espeak TTS
- whisper.cpp STT + Ollama LLM + espeak TTS
- mock/mock/mock for local smoke tests

## 1) Create the virtual devices

```bash
callscoot-agent bootstrap-audio
systemctl --user restart callscoot-daemon.service
```

This creates:

- `callscoot.agent.rx`
- `callscoot.agent.rx.monitor`
- `callscoot.agent.tx`
- `callscoot.agent.tx.monitor`

and points CallScoot at them.

## 2) Configure the provider stack

### Fully local-ish example

```bash
callscoot-agent configure \
  --stt-provider whisper_cli \
  --whisper-command whisper-cli \
  --whisper-model-path /path/to/ggml-base.bin \
  --llm-provider ollama \
  --llm-model llama3.1:8b \
  --tts-provider espeak
```

### OpenAI example

```bash
export OPENAI_API_KEY=...

callscoot-agent configure \
  --stt-provider openai \
  --stt-model whisper-1 \
  --llm-provider openai \
  --llm-model gpt-4o-mini \
  --tts-provider openai \
  --tts-model gpt-4o-mini-tts \
  --tts-voice alloy
```

## 3) Test the pipeline without a real phone call

```bash
callscoot-agent reply "Merhaba, bugün saat kaç?"
```

That exercises the configured LLM/TTS stack once and returns JSON.

## 4) Run the continuous agent

```bash
callscoot-agent run
```

What it does:

1. waits for an active call session from `callscoot-daemon`
2. starts recording from `callscoot.agent.rx.monitor`
3. runs a simple energy-based VAD
4. transcribes completed utterances
5. asks the LLM for a short reply
6. synthesizes that reply
7. plays it into `callscoot.agent.tx`
8. writes transcript + summary files to the session directory

## Optional systemd service

The repo also installs:

```text
~/.config/systemd/user/callscoot-agent.service
```

Enable it if you want the AI loop to always be available:

```bash
systemctl --user enable --now callscoot-agent.service
```

## Transcript output

Transcripts are appended to:

```text
~/.local/state/callscoot/calls/<session-id>/transcript.jsonl
```

Summaries are written to:

```text
~/.local/state/callscoot/calls/<session-id>/summary.txt
```

## Notes

- `mock` providers are useful for smoke tests
- `espeak` requires `espeak-ng`
- `whisper_cli` expects a working whisper.cpp/whisper-cli installation and model path
- for a pure AI path, keep CallScoot echo cancellation off on the virtual sinks
