"""The one place that talks to the model. To swap providers, rewrite reply() and keep its signature."""
import anthropic

MODEL = "claude-opus-5-5"
client = anthropic.Anthropic()


def reply(system: str, tools: list[dict], messages: list, text: str, run_tool) -> str:
    """Append the user's words to `messages`, run tools until the model answers, return the answer.

    `messages` is the session history. It is only ever appended to, and a failed turn is removed whole,
    so the next request never carries a half-finished turn."""
    start = len(messages)
    messages.append({"role": "user", "content": text})
    try:
        while True:
            resp = client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=system,
                tools=tools,
                messages=messages,
                output_config={"effort": "low"},  # short spoken replies; raise it if orders come out wrong
                cache_control={"type": "ephemeral"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            if resp.stop_reason == "refusal":
                del messages[start:]
                return "Sorry, I can't help with that. Is there something else you need?"
            messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason != "tool_use":
                words = " ".join(b.text for b in resp.content if b.type == "text").strip()
                return words or "Sorry, could you say that again?"
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": run_tool(b.name, b.input)}
                for b in resp.content if b.type == "tool_use"
            ]})
    except Exception:
        del messages[start:]
        raise


def sentence(system: str, text: str) -> str:
    """One short answer with no tools and no history. Empty if the model declines."""
    resp = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": text}],
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if resp.stop_reason == "refusal":
        return ""
    return " ".join(b.text for b in resp.content if b.type == "text").strip()

