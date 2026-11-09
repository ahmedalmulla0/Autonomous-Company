# FreeLLMAPI evaluation

- **Subject:** [tashfeenahmed/freellmapi](https://github.com/tashfeenahmed/freellmapi) (MIT, reviewed at `7f3fece`, 2026-11-08)
- **Question:** Can it lower model costs for the company loop, and can products use it?
- **Status:** Evaluated. Not wired in. The isolation boundary blocks the default setup (see Blocker).

## What it is

FreeLLMAPI is a self-hosted Node 20 router. It combines the free tiers of about 34 providers (Gemini, Groq, Cerebras, Mistral, OpenRouter, Cloudflare and others) behind one OpenAI-compatible `/v1` endpoint. It also serves Anthropic `/v1/messages`, Gemini `/v1beta` and Ollama-style surfaces.

- **Routing:** `model: "auto"`, `auto:fast`, `auto:smart`, `auto:coding` or a named profile. On a 429 or 5xx, the router tries the next model in the chain. It tracks request and token limits for each key.
- **Keys:** Provider keys are encrypted (AES-256-GCM) in SQLite. Clients see one `freellmapi-…` bearer token.
- **Tool calling:** OpenAI `tools` work across providers, and plain-text tool calls are converted into real `tool_calls`. Our `openai-compatible` adapter needs this.
- **Deployment:** Docker, a desktop app or `npm run dev`. The dashboard runs on `:3001`.

## Fit with our engine adapters

Our `openai-compatible` engine ([engine adapters](../ENGINE_ADAPTERS.md)) is the natural place to connect it:

```bash
ENGINE=openai-compatible \
OPENAI_COMPATIBLE_ADAPTER_ENABLED=1 \
OPENAI_COMPATIBLE_ENDPOINT="https://<operator-host>/v1/chat/completions" \
OPENAI_COMPATIBLE_MODEL="auto:coding" \
OPENAI_COMPATIBLE_API_KEY="$FREELLMAPI_UNIFIED_KEY" \
make start
```

The adapter's checks pass with this setup. The endpoint has no query string and no embedded credentials, and the key is read from the environment only.

## Blocker: isolation rejects local routers

FreeLLMAPI is meant to run locally (`http://localhost:3001`). Every model cycle runs inside the project isolation namespace. Its egress proxy **rejects loopback, private and link-local addresses** ([project isolation](project-isolation.md): "host-local/private model servers need a separately designed broker and are currently unavailable"). A local FreeLLMAPI therefore cannot be reached from a cycle, even though `engine-adapters.sh` accepts the loopback URL.

Options, ranked:

1. **Host-side broker (recommended, needs design).** A broker on the host forwards only `POST /v1/chat/completions` from the namespace to a fixed local upstream and adds the key on the host side. This fills the "separately designed broker" gap that the isolation doc names. Other local model servers (Ollama, vLLM, LM Studio) would also benefit.
2. **Public HTTPS deployment.** This works today without code changes. However, the upstream project says "Local-first … don't expose it to the internet". It also has no multi-tenant auth, so a leaked unified key gives anyone the whole pool. This is acceptable only behind an access layer such as Cloudflare Access or mTLS, plus a strong, rotated key.
3. **Do not bypass the isolation proxy.** Under our guardrails, bypassing it is not an option.

## Risks (Munger check)

| Risk | Severity | Note |
|---|---|---|
| Provider terms of service | **High** | This loop is a commercial operation that runs unattended. Upstream's own ToS review flags Cohere ("avoid": personal use forbidden), NVIDIA NIM (evaluation only) and Gemini/Z.ai (caution). Rules: one account per provider, no resale, no shared endpoint, no production backend use. Disable Cohere and NVIDIA in the chain. |
| Quality drift during the day | High | Top models hit their daily caps first. Late-day cycles fall back to weaker models, which makes autonomous decisions and code noticeably worse. |
| Tool-call reliability | Medium | Smaller fallback models often produce malformed tool calls. Expect failed cycles. Sticky sessions (30 minutes) help within one cycle. |
| Using it as a product backend | **Do not** | Upstream: "not production … swap in a paid API before you ship." Revenue products must use paid APIs with an SLA. |
| Supply chain | Low/Med | The catalog auto-updates from freellmapi.co (Ed25519-signed). Pin a release or image tag and review it before upgrading. |

## Decision

- **Products:** No. Never use FreeLLMAPI as the inference backend of anything we sell.
- **Internal loop:** Possibly, for low-stakes cycles such as research, drafting and docs. Do not use it for code that ships or for decisions that cost money. It requires option 1 (the broker). Until the broker exists, Claude remains the default engine.
- **Next action:** `cto-vogels` and `devops-hightower` design the host-side model broker (allowlisted path, fixed upstream, host-injected key, request logging). Then run a 1-day A/B test: 10 research cycles on `auto:smart` against the default engine, comparing cycle success rate and output quality.
