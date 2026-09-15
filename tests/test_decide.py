"""``decide()``, both providers, no network.

Everything here is a fake standing in for the wire shape a real SDK client
would hand back — no ``anthropic`` client, no ``openai`` client, no key.
The point is the loop and the parsing: the same four tools, the same
stop-at-the-first-proposal rule, and — the one thing structurally different
about OpenAI's shape — a tool call whose arguments are not valid JSON, which
must end the cycle as a hold, with a note, and never as a proposal built from
blank fields.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from merkl_trader import decide as decisions

# --------------------------------------------------------------------------- #
# What every test hands decide(): a situation, and two tools that need no
# fixture of their own.
# --------------------------------------------------------------------------- #


async def _market() -> dict[str, Any]:
    return {"book": "quiet"}


async def _receipts(limit: int) -> list[Any]:
    return []


SWAP_ARGS = {
    "sell_asset": "XRP",
    "sell_max_amount": "4",
    "buy_asset": "RLUSD",
    "buy_amount": "2",
    "reasoning": "the spread was inside my limit",
}


# --------------------------------------------------------------------------- #
# Anthropic-shaped fakes — the path decide() has always spoken.
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class AnthropicBlock:
    type: str
    text: str = ""
    name: str = ""
    input: dict[str, Any] = dataclasses.field(default_factory=dict)
    id: str = "toolu_1"

    def model_dump(self) -> dict[str, Any]:
        if self.type == "text":
            return {"type": "text", "text": self.text}
        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}


@dataclasses.dataclass
class AnthropicUsage:
    input_tokens: int = 10
    output_tokens: int = 5


@dataclasses.dataclass
class AnthropicReply:
    content: list[AnthropicBlock]
    usage: AnthropicUsage = dataclasses.field(default_factory=AnthropicUsage)


class FakeAnthropicClient:
    """Stands in for ``AnthropicModel.create()``. No SDK, no network."""

    def __init__(self, *replies: AnthropicReply) -> None:
        self._replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> AnthropicReply:
        self.requests.append(request)
        return self._replies.pop(0)


# --------------------------------------------------------------------------- #
# OpenAI-shaped fakes.
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class OpenAIFunction:
    name: str
    arguments: str
    """A JSON *string*, exactly as the real SDK hands it back — not an object."""


@dataclasses.dataclass
class OpenAIToolCall:
    id: str
    function: OpenAIFunction
    type: str = "function"

    def model_dump(self, exclude_none: bool = False) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "function": {"name": self.function.name, "arguments": self.function.arguments},
        }


@dataclasses.dataclass
class OpenAIMessage:
    content: str | None = None
    tool_calls: list[OpenAIToolCall] | None = None
    role: str = "assistant"

    def model_dump(self, exclude_none: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            body["tool_calls"] = [call.model_dump() for call in self.tool_calls]
        if exclude_none:
            body = {key: value for key, value in body.items() if value is not None}
        return body


@dataclasses.dataclass
class OpenAIChoice:
    message: OpenAIMessage


@dataclasses.dataclass
class OpenAIUsage:
    prompt_tokens: int = 20
    completion_tokens: int = 8


@dataclasses.dataclass
class OpenAIReply:
    choices: list[OpenAIChoice]
    usage: OpenAIUsage = dataclasses.field(default_factory=OpenAIUsage)


class FakeOpenAIClient:
    """Stands in for ``OpenAIModel.create()``. No SDK, no network."""

    def __init__(self, *replies: OpenAIReply) -> None:
        self._replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> OpenAIReply:
        self.requests.append(request)
        return self._replies.pop(0)


class FailingOpenAIClient:
    """Raises on ``create()`` — a bad key or no network, not a crash to propagate."""

    async def create(self, **request: Any) -> Any:
        raise RuntimeError("401 Unauthorized")


def swap_call(call_id: str = "call_1", *, arguments: str | None = None) -> OpenAIToolCall:
    return OpenAIToolCall(
        id=call_id,
        function=OpenAIFunction(name="propose_swap", arguments=arguments or json.dumps(SWAP_ARGS)),
    )


# --------------------------------------------------------------------------- #
# The Anthropic path — untouched, still exercised.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_anthropic_swap_is_still_decided() -> None:
    reply = AnthropicReply(
        content=[
            AnthropicBlock(type="text", text="Checked the book."),
            AnthropicBlock(type="tool_use", name="propose_swap", id="t1", input=SWAP_ARGS),
        ]
    )
    client = FakeAnthropicClient(reply)

    decision = await decisions.decide(
        model=client,
        model_name="claude-x",
        max_tokens=256,
        situation={"ok": True},
        market=_market,
        receipts=_receipts,
    )

    assert decision.kind == "swap"
    assert decision.sell_max_amount == "4"
    assert decision.usage.input_tokens == 10
    assert decision.usage.output_tokens == 5
    # the default provider is unchanged, and this request never mentions OpenAI's shape
    assert "tool_choice" not in client.requests[0]


@pytest.mark.asyncio
async def test_anthropic_hold_when_nothing_is_proposed() -> None:
    reply = AnthropicReply(content=[AnthropicBlock(type="text", text="quiet market, holding")])
    client = FakeAnthropicClient(reply)

    decision = await decisions.decide(
        model=client,
        model_name="claude-x",
        max_tokens=256,
        situation={},
        market=_market,
        receipts=_receipts,
    )

    assert decision.kind == "hold"
    assert decision.reasoning == "quiet market, holding"


# --------------------------------------------------------------------------- #
# The OpenAI path.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_openai_swap_is_decided() -> None:
    reply = OpenAIReply(choices=[OpenAIChoice(message=OpenAIMessage(tool_calls=[swap_call()]))])
    client = FakeOpenAIClient(reply)

    decision = await decisions.decide(
        model=client,
        model_name="gpt-x",
        max_tokens=256,
        situation={"ok": True},
        market=_market,
        receipts=_receipts,
        provider="openai",
    )

    assert decision.kind == "swap"
    assert decision.sell_max_amount == "4"
    assert decision.reasoning == SWAP_ARGS["reasoning"]
    assert decision.usage.input_tokens == 20
    assert decision.usage.output_tokens == 8
    request = client.requests[0]
    assert request["tool_choice"] == "auto"
    assert request["max_completion_tokens"] == 256
    assert "temperature" not in request, "left at the API default"
    assert [tool["function"]["name"] for tool in request["tools"]] == [
        tool["name"] for tool in decisions.TOOLS
    ]


@pytest.mark.asyncio
async def test_openai_hold_when_nothing_is_proposed() -> None:
    reply = OpenAIReply(choices=[OpenAIChoice(message=OpenAIMessage(content="quiet market"))])
    client = FakeOpenAIClient(reply)

    decision = await decisions.decide(
        model=client,
        model_name="gpt-x",
        max_tokens=256,
        situation={},
        market=_market,
        receipts=_receipts,
        provider="openai",
    )

    assert decision.kind == "hold"
    assert decision.reasoning == "quiet market"


@pytest.mark.asyncio
async def test_openai_reads_the_market_before_proposing() -> None:
    """Two round-trips: the read tool's result reaches the model as a ``tool`` message."""
    look = OpenAIReply(
        choices=[
            OpenAIChoice(
                message=OpenAIMessage(
                    tool_calls=[
                        OpenAIToolCall(
                            id="call_market",
                            function=OpenAIFunction(name="get_market", arguments="{}"),
                        )
                    ]
                )
            )
        ]
    )
    swap = OpenAIReply(
        choices=[OpenAIChoice(message=OpenAIMessage(tool_calls=[swap_call("call_swap")]))]
    )
    client = FakeOpenAIClient(look, swap)

    decision = await decisions.decide(
        model=client,
        model_name="gpt-x",
        max_tokens=256,
        situation={},
        market=_market,
        receipts=_receipts,
        provider="openai",
    )

    assert decision.kind == "swap"
    assert len(client.requests) == 2
    second_messages = client.requests[1]["messages"]
    assert any(
        message.get("role") == "tool" and message.get("tool_call_id") == "call_market"
        for message in second_messages
    )


@pytest.mark.asyncio
async def test_openai_only_the_first_proposal_in_a_turn_is_taken() -> None:
    greedy = OpenAIReply(
        choices=[
            OpenAIChoice(
                message=OpenAIMessage(
                    tool_calls=[
                        swap_call("call_1"),
                        swap_call("call_2", arguments=json.dumps(SWAP_ARGS)),
                    ]
                )
            )
        ]
    )
    client = FakeOpenAIClient(greedy)

    decision = await decisions.decide(
        model=client,
        model_name="gpt-x",
        max_tokens=256,
        situation={},
        market=_market,
        receipts=_receipts,
        provider="openai",
    )

    assert decision.kind == "swap"
    assert "one action per cycle" in decision.note


@pytest.mark.asyncio
async def test_openai_malformed_tool_call_is_a_hold_not_a_blank_proposal() -> None:
    """The one thing structurally different about OpenAI: arguments are a string
    the model can hand back broken. A broken propose_payment must never become
    a payment with an empty destination and amount."""
    reply = OpenAIReply(
        choices=[
            OpenAIChoice(
                message=OpenAIMessage(
                    tool_calls=[
                        OpenAIToolCall(
                            id="call_1",
                            function=OpenAIFunction(
                                name="propose_payment", arguments="{not valid json"
                            ),
                        )
                    ]
                )
            )
        ]
    )
    client = FakeOpenAIClient(reply)

    decision = await decisions.decide(
        model=client,
        model_name="gpt-x",
        max_tokens=256,
        situation={},
        market=_market,
        receipts=_receipts,
        provider="openai",
    )

    assert decision.kind == "hold"
    assert not decision.acts
    assert decision.destination == ""
    assert decision.amount == ""
    assert "not valid JSON" in decision.note


@pytest.mark.asyncio
async def test_openai_a_failed_model_call_is_a_clean_hold() -> None:
    """A bad key or no network ends the cycle as a hold with a note — never a
    traceback the caller was never asked to handle."""
    client = FailingOpenAIClient()

    decision = await decisions.decide(
        model=client,
        model_name="gpt-x",
        max_tokens=256,
        situation={},
        market=_market,
        receipts=_receipts,
        provider="openai",
    )

    assert decision.kind == "hold"
    assert "the model call failed" in decision.note


def test_openai_tools_mirror_the_same_names_descriptions_and_schemas() -> None:
    assert [tool["function"]["name"] for tool in decisions.OPENAI_TOOLS] == [
        tool["name"] for tool in decisions.TOOLS
    ]
    for original, converted in zip(decisions.TOOLS, decisions.OPENAI_TOOLS, strict=True):
        function = converted["function"]
        assert converted["type"] == "function"
        assert function["description"] == original["description"]
        assert function["parameters"] == original["input_schema"]
        assert function["strict"] is True
