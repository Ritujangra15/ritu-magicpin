# Vera — magicpin AI Challenge

## Approach

This submission uses a deterministic, context-grounded decision engine rather than a generic LLM prompt.

### 1. Four-context composition
Every message is composed from:
- `CategoryContext`: category voice, vocabulary, peer statistics, offers, digest/research and seasonal signals.
- `MerchantContext`: identity, locality, subscription, performance, active offers, conversation history and customer aggregates.
- `TriggerContext`: the immediate reason for contact, including urgency and trigger payload.
- `CustomerContext` when the message is sent on the merchant's behalf.

### 2. Decision before wording
The engine routes triggers into intent families:
- research/compliance
- performance movement
- renewal/dormancy
- seasonal/event opportunities
- merchant planning and curiosity
- customer recall/winback/refill/appointment
- operational alerts and review themes
- unseen/generated trigger fallback

The composer deliberately does not fill missing fields with guesses. Placeholder or injected contexts are handled from whatever facts are actually available.

### 3. Engagement strategy
Messages preferentially combine:
1. one concrete fact,
2. one merchant/customer-specific anchor,
3. the immediate trigger,
4. one useful next step.

CTAs are kept to one primary action. For example, instead of asking a merchant to "improve marketing", the bot offers a specific draft/checklist/comparison based on the current signal.

### 4. Category voice
The engine uses the category vocabulary and avoids generic promotional language. Customer-facing messages also respect relationship state and consent.

### 5. Multi-turn handling
`respond()` detects:
- opt-outs / disinterest -> `end`
- delay requests -> `wait`
- affirmative intent -> `send`
- questions -> grounded follow-up
- ambiguous/no-action replies -> `wait`

Conversation state is retained in memory by conversation ID.

## Operational design

`bot.py` exposes all required endpoints:
- `POST /v1/context`
- `POST /v1/tick`
- `POST /v1/reply`
- `GET /v1/healthz`
- `GET /v1/metadata`

The implementation has no dependency on external merchant/customer APIs and does not transmit context outside the process.

## Tradeoffs

I chose deterministic rules over an LLM because:
- the challenge requires deterministic behavior for identical inputs;
- context grounding is easier to guarantee;
- latency remains comfortably below the 30-second limit;
- unseen contexts can still be handled through structured fallbacks.

The main tradeoff is that nuanced open-ended conversation is less flexible than a strong LLM, but the replay handler covers the highest-value conversation transitions without hallucinating facts.

## Running

```bash
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Health:
```bash
curl http://localhost:8080/v1/healthz
```
