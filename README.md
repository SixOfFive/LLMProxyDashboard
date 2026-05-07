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
- Captures every request body in memory so you can press `Ctrl-E` to expand
  the most recent prompt — even when prompt content is hidden by default.

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
| `--verbose`, `-v` | off          | Log `[conn]` open/close lines and noise-endpoint requests (`/api/show`, `/api/tags`, `/api/version`, `/api/ps`, `/api/embed`, `/api/embeddings`). Hidden by default since clients poll these constantly. |

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

## Log lines

Lines that scroll above the status bar are tagged so you can grep / scan them.

| tag           | color  | when                                                  |
|---------------|--------|-------------------------------------------------------|
| `[boot]`      | white  | Server bound and ready.                               |
| `[conn]`      | grey   | Connection opened or closed (only with `--verbose`).  |
| `[req]`       | white  | One-line header for each request: endpoint, model, ctx, max, temp. |
| `[req-stats]` | green  | Hidden-mode summary of the request body (size, est. tokens, message count). |
| `[system]` / `[user]` / `[assistant]` / `[prompt]` | white / orange / white | Prompt content lines when `/showfrom` is on. `[user]` is highlighted because it's usually the part you care about. |
| `[asst]`      | green  | Assistant reply text on done (when `/showto` is on, the default). |
| `[resp-stats]`| white  | Hidden-mode summary of the response (size, est. tokens, duration, tps). |
| `[done]`      | white  | Authoritative counts from the upstream when available — `prompt_eval_count`, `eval_count` for Ollama, `tokens_predicted` for llama.cpp, `usage` for OpenAI-compat. |
| `[cfg]`       | white  | Acknowledgement after a slash command changed something. |

### Example: `/api/chat` with default toggles (`from=off to=on`)

```
[req] /api/chat model=qwen2.5:14b ctx=8192 max=512 temp=0.7
[req-stats] body=4.2K  ~987t in  msgs=3
[asst] Paris is the capital of France.
[done] prompt_eval=987 eval=8
```

Same call after `/showfrom`:

```
[req] /api/chat model=qwen2.5:14b ctx=8192 max=512 temp=0.7
[system] You are a helpful assistant.
[user] What's the capital of France?
[asst] Paris is the capital of France.
[done] prompt_eval=987 eval=8
```

Housekeeping calls (`/api/show`, `/api/tags`, `/api/version`, `/api/ps`,
`/api/embed`, `/api/embeddings`) are filtered out of the log entirely unless
you pass `--verbose`. They produce no useful prompt content and clients hit
them on every render.

## Keys

| key       | effect                                                          |
|-----------|-----------------------------------------------------------------|
| `↑` / `↓` | Navigate command history.                                       |
| `Ctrl-E`  | Toggle single-line ↔ multi-line input. If the input is empty when expanding, the most recent request (system / user / assistant turns) is loaded into the buffer so you can read what was just sent. In multi-line mode `Enter` inserts a newline; `Ctrl-E` again collapses the buffer back so `Enter` will submit it. |
| `PgUp` / `PgDn` | Scroll the multi-line input view by a page when the buffer is larger than the visible area. The header shows the visible line range, e.g. `[1-5/12]`. Editing or pressing Esc snaps back to the bottom. |
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

- **Footer**: upstream target, model override, the `from`/`to` toggle states, and
  how much of the 5-minute sparkline window has been filled (`graph=NNs/300s`).
- **Request row**: endpoint, model, context size, max tokens, temperature for
  the most recent request. Fields not specified in the request body show as
  `(default)` — the upstream falls back to its own defaults. Ollama clients
  change these per call, so the row reflects whatever the most recent request
  asked for.
- **Response row**: live tail of the assistant's text as it streams. Newlines
  are shown as `⏎` so the row stays single-line.
- **Token / byte rows**: current rate, totals, sparkline of the last 5 minutes,
  plus `peak` (max in window) and `avg` (window mean) so the bars are anchored
  to a known scale instead of just being abstract bars.
- **Input row**: prompt for slash commands and free text.

### Multi-line input view

Pressing `Ctrl-E` expands the input to six rows — one header, five content rows.
If the buffer is empty when expanding, the most recent request body
(`[req]`, `[system]`, `[user]`, `[assistant]`, `[prompt]`) is loaded so you can
read what was just sent, even with `/hidefrom` on:

```
 ┄ multi-line ┄ Enter=newline  Ctrl-E=collapse  PgUp/PgDn=scroll  Esc=clear  [1-5/12]
↑ [req] /api/chat model=qwen2.5:14b ctx=8192 max=512 temp=0.7
  [system] You are a helpful assistant.
  [user] What's the capital of France?
  [user] And what's its population?
↓ [user] One more thing — anything notable about it?
> _
```

`[1-5/12]` shows which lines are visible out of the total. The `↑` / `↓`
markers on the edge rows indicate that more content sits above or below.
`PgUp` / `PgDn` scroll one page at a time. Editing or `Esc` snaps back to the
bottom. Press `Ctrl-E` again to collapse — newlines become `⏎` in the
single-line buffer; `Enter` then submits the whole thing.

## How token counts are computed

There are two numbers floating around: a **live estimate** (what drives the
`tok/s` rate and sparkline) and the **authoritative count** the upstream
reports at the end of a request.

- **Live estimate** — characters divided by 4. Computed per request body
  (input) and per streamed chunk (output) so the dashboard updates every
  second instead of only on completion. Treat it as approximate; the divisor
  is a reasonable default for English text and many models.
- **Authoritative count** — pulled from the final response chunk and emitted
  on the `[done]` line for cross-reference:
  - Ollama: `prompt_eval_count`, `eval_count`
  - llama.cpp `/completion`: `tokens_evaluated`, `tokens_predicted`
  - OpenAI-compat: `usage.prompt_tokens`, `usage.completion_tokens`

The estimate is not corrected to the authoritative count — that would cause
the running totals and sparklines to jump backwards on each completion. If
you need exact numbers, read the `[done]` line; if you want a live feel for
throughput, watch the `tok/s` row.

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
