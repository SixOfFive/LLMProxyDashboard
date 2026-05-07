# LLM Proxy Dashboard

A single-file Python TCP proxy for **Ollama** and **llama.cpp** servers, with a
live terminal dashboard. Watch traffic, rewrite the requested model on the fly,
or switch the upstream server without restarting your client.

No third-party dependencies. Pure stdlib. Works on Windows, macOS, and Linux as
long as the terminal supports ANSI escape codes.

## What it does

- Listens on a local port and forwards every connection to an upstream Ollama
  or llama.cpp server.
- Parses HTTP traffic in flight to extract the model, context size, and other
  request parameters from each call (Ollama clients change these per call).
- Sniffs streaming responses (NDJSON for Ollama, SSE for the OpenAI-compatible
  endpoints) and reconstructs the assistant's reply.
- Renders a fixed status bar at the bottom of the terminal showing live token
  and byte rates over the last 5 minutes, the current request metadata, and a
  preview of the in-flight response.
- Accepts slash commands at the prompt to change the upstream server, override
  the requested model, or toggle visibility of request/response bodies.

## Requirements

- Python 3.9 or newer.
- A terminal that supports ANSI escape codes. On Windows 10/11 this is enabled
  automatically by the script.

## Usage

```
python llm_proxy.py --host <upstream> [--to <port>] [--bind <host:port>]
```

| flag      | default            | meaning                                          |
|-----------|--------------------|--------------------------------------------------|
| `--host`  | required           | Upstream host. Accepts `host`, `host:port`, or `[v6]:port`. |
| `--to`    | `11434`            | Upstream port. Overrides any port given in `--host`. |
| `--bind`  | `0.0.0.0:11434`    | Local bind. Accepts `host:port`, bare port, or just host. |
| `--verbose`, `-v` | off          | Log `[conn]` open/close lines. Hidden by default. |

When `--bind` uses a wildcard host (`0.0.0.0`, `*`, or empty), the proxy listens
on both IPv4 and IPv6 so `localhost` resolves correctly regardless of which
family the client tries first.

### Examples

Forward `localhost:11434` to a remote Ollama server:

```
python llm_proxy.py --host 192.168.1.10
```

Listen on a different local port:

```
python llm_proxy.py --host 192.168.1.10 --bind 11435
```

Use a non-default upstream port (e.g. llama.cpp on 8080):

```
python llm_proxy.py --host 192.168.1.10:8080
```

Point your Ollama client at the proxy by setting `OLLAMA_HOST=http://localhost:11434`
(or whichever local port you bound to).

## Slash commands

Type at the `>` prompt:

| command                   | effect                                                            |
|---------------------------|-------------------------------------------------------------------|
| `/help`                   | List commands.                                                    |
| `/model <name>`           | Rewrite the `model` field in every outgoing JSON request.         |
| `/model`                  | Clear the model override.                                         |
| `/server <host[:port]>`   | Change the upstream destination for new connections.              |
| `/showfrom` / `/hidefrom` | Show or hide the prompt content sent to the server. Default: hide. |
| `/showto` / `/hideto`     | Show or hide the assistant's reply text. Default: show.            |
| `/quit`                   | Exit.                                                             |

When content is hidden, the proxy logs a one-line summary instead — body size,
estimated tokens, message count for requests; size, tokens, duration, and
tokens-per-second for responses.

## Keys

| key       | effect                                                          |
|-----------|-----------------------------------------------------------------|
| `↑` / `↓` | Navigate command history.                                       |
| `Ctrl-E`  | Toggle single-line ↔ multi-line input. In multi-line mode `Enter` inserts a newline; `Ctrl-E` again collapses the buffer back so `Enter` will submit it. |
| `Esc`     | Clear the current input.                                        |
| `Ctrl-C`  | Quit.                                                           |

## Status bar layout

```
─────────────────  → 192.168.1.10:11434   override=(none)   from=off to=on   graph=42s/300s (←5m..now→)
 req /api/chat       model=qwen2.5:14b      ctx=8192   max=512    temp=0.7
 rsp │ …reply text streaming in real time
 tok/s   42  prompt    1234   reply     567 │▁▂▃▅▆█▇▅▃▂▁│ peak    89  avg   12.4
 B/s   1.2K  prompt   34.5K   reply   12.3K │▁▂▃▅▆█▇▅▃▂▁│ peak  4.5K  avg  800.0
> _
```

- **Footer**: upstream target, model override, toggle states, sparkline window length.
- **Request row**: endpoint, model, context size, max tokens, temperature for
  the most recent request. Fields not specified in the request body show as
  `(default)` since the upstream server falls back to its own defaults.
- **Response row**: live tail of the assistant's text as it streams. Newlines
  are shown as `⏎` so the row stays single-line.
- **Token / byte rows**: current rate, totals, sparkline of the last 5 minutes,
  peak, and average.
- **Input row**: prompt for slash commands.

The scrolling area above the status bar logs each request and response. Lines
are color-coded: `[conn]` in grey, `[asst]` in green, `[user]` in orange.

## How token counts are computed

- **Input tokens**: estimated by walking the request body's `messages` /
  `system` / `prompt` fields and dividing total characters by 4.
- **Output tokens**: estimated per-chunk from the streamed text. When the
  upstream emits authoritative counts (`prompt_eval_count` / `eval_count` for
  Ollama, `tokens_predicted` for llama.cpp, `usage` for the OpenAI-compatible
  endpoints), they are logged on the `[done]` line for cross-reference.

The estimate exists because clients want a live `tok/s` number that updates
continuously, not just a final total. Treat it as approximate.

## Caveats

- **Plaintext HTTP only.** This is not a TLS-aware proxy. Ollama and llama.cpp
  use plaintext HTTP by default, which is the intended target.
- **Chunked request bodies pass through unmodified.** If a client sends the
  request body using HTTP chunked transfer encoding, the model rewriter and the
  request logger fall back to passthrough mode. Most Ollama clients use
  `Content-Length`, so this is rarely an issue.
- **Model rewriting changes the request, not the response.** If you point a
  client at the proxy and use `/model` to swap models, the response will reflect
  the substituted model, not the one the client asked for. Some clients may
  notice and behave oddly.
- **Existing connections keep their original upstream.** `/server <new>` only
  affects connections opened after the change.

## License

MIT. Use it however you like.
