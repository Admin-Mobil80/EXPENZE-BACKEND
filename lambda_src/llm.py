"""OpenAI model access for ExpensifyAI.

Two calls, mirroring the two passes the handler makes:

1. `extract_receipt` - perception, constrained by a strict `json_schema`
   response format so the result is schema-valid rather than hopefully-valid.
2. `explain` - turns the computed verdict into employee-facing prose.

There is deliberately no tool loop. Everything the verdict needs - the expense
type, the lines, the total - already comes back from pass 1, so a tool call
would only hand the same values straight back for us to act on: a round trip
that decides nothing. (It is also rejected outright
by the API: reasoning models cannot combine function tools with reasoning on
chat completions, and turning reasoning off would remove exactly the judgment
this model was chosen for.)

`policy.py` sits underneath and is provider-agnostic: it decides every
reimbursement outcome, and nothing here can override it.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from openai import OpenAI

from secrets_loader import get_secret_value

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAX_OUTPUT_CHARS = 2000


class ProviderError(RuntimeError):
    """The model provider is misconfigured and cannot be used at all."""


def _log_usage(call: str, model: str, response: Any) -> None:
    """What that call cost, in the only unit the provider bills in.

    Nothing recorded this, so the only answer to "what does a receipt cost to
    process" was the monthly invoice divided by a guess - and the first time
    the question was asked in earnest, the honest answer was that we could not
    tell. Worse: a fault that turned one receipt into thousands of audits was
    invisible here, because an invocation that succeeds at calling the model
    and fails afterwards looks like nothing at all from the billing side.

    One line per call, parseable. A log filter over `openai_usage` counts calls
    and sums tokens per day without anybody exporting anything.
    """
    try:
        u = getattr(response, "usage", None)
        if not u:
            return
        cached = getattr(getattr(u, "prompt_tokens_details", None),
                         "cached_tokens", 0) or 0
        reasoning = getattr(getattr(u, "completion_tokens_details", None),
                            "reasoning_tokens", 0) or 0
        logger.info(
            "openai_usage call=%s model=%s prompt=%s cached=%s completion=%s "
            "reasoning=%s total=%s",
            call, model, getattr(u, "prompt_tokens", 0), cached,
            getattr(u, "completion_tokens", 0), reasoning,
            getattr(u, "total_tokens", 0),
        )
    except Exception:
        # Accounting must never be able to fail an audit.
        logger.exception("could not record usage for %s", call)


def _note_part(receipt_input: dict[str, Any]) -> list[dict[str, Any]]:
    """The sender's own words, fenced off from the instruction around them.

    Appended last and clearly delimited. The rules for reading it are in the
    system prompt, which the sender cannot write into - so a caption saying
    "ignore the above" arrives as the contents of a labelled block rather than
    as a sentence sitting among our own.
    """
    note = str(receipt_input.get("sender_note", "") or "").strip()
    if not note:
        return []
    # A caption cannot be allowed to close its own fence and start writing
    # outside it.
    note = note.replace("<<<", "<").replace(">>>", ">")
    return [{"type": "text",
             "text": f"<<<SENDER_NOTE>>>\n{note}\n<<<END_SENDER_NOTE>>>"}]


def _figures(verdict: dict[str, Any]) -> str:
    """The money, once, in plain text.

    Amounts travel as decimal strings so nothing is lost to binary floats, which
    means JSON hands them to the model already quoted - and it copied the quotes
    straight into a customer-facing sentence: `INR "5500.00"`. Restating them
    unquoted is a surer fix than an instruction not to reproduce what it sees.
    """
    ccy = str(verdict.get("currency", "") or "")
    # One figure now. `reimbursable_total` is always the receipt total, and
    # the other two - a provisional amount pending an answer, and a disallowed
    # amount - belong to an engine that could strike lines out and ask the
    # submitter questions. Neither exists; both rows were always absent, and a
    # label offering to explain a deduction invites the model to invent one.
    rows = [("Receipt total", verdict.get("receipt_total"))]
    lines = [f"  {label}: {ccy} {value}".rstrip()
             for label, value in rows if value not in (None, "")]
    if not lines:
        return ""
    return ("\n\nThe figures, written as they should appear in your answer "
            "- never in quotation marks:\n" + "\n".join(lines))


def _content(receipt_input: dict[str, Any], instruction: str) -> list[dict[str, Any]]:
    """Format the neutral receipt input as OpenAI content parts."""
    kind = receipt_input["kind"]
    if kind == "image_base64":
        url = f"data:{receipt_input['media_type']};base64,{receipt_input['data']}"
        return [
            {"type": "image_url", "image_url": {"url": url}},
            {"type": "text", "text": instruction},
        ] + _note_part(receipt_input)
    if kind == "image_url":
        return [
            {"type": "image_url", "image_url": {"url": receipt_input["url"]}},
            {"type": "text", "text": instruction},
        ] + _note_part(receipt_input)
    if kind == "images":
        # A multi-page receipt - almost always a rendered PDF. Sent as one
        # message rather than one call per page, because the total is
        # frequently on a different page from the line items and reading them
        # separately would produce two half-answers instead of one whole one.
        parts: list[dict[str, Any]] = []
        for page in receipt_input["pages"]:
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{page['media_type']};base64,{page['data']}"},
            })
        parts.append({"type": "text", "text": (
            f"This receipt has {len(receipt_input['pages'])} page(s), in order. "
            "Read them as one document: line items and the total may be on "
            f"different pages.\n\n{instruction}")})
        parts.extend(_note_part(receipt_input))
        return parts
    return [{"type": "text", "text": f"Receipt:\n{receipt_input['text']}\n\n{instruction}"}]


class OpenAIClient:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = OpenAI(api_key=api_key)
        self._model = model

    def extract_receipt(
        self, receipt_input: dict[str, Any], schema: dict[str, Any], system: str
    ) -> dict[str, Any]:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": _content(
                        receipt_input, "Extract this receipt into the required schema."
                    ),
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "receipt", "schema": schema, "strict": True},
            },
        )
        _log_usage("extract", self._model, response)
        return json.loads(response.choices[0].message.content)

    def explain(
        self, receipt: dict[str, Any], verdict: dict[str, Any], system: str
    ) -> str:
        """Turn a computed verdict into something an employee can read.

        The model writes prose about numbers it is given. It never produces
        them: `verdict` arrives already computed by policy.py, so nothing the
        model says here can change what anyone is paid.
        """
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": (
                        "Receipt:\n" + json.dumps(receipt, indent=2) +
                        "\n\nVerdict from the policy engine:\n" + json.dumps(verdict, indent=2) +
                        _figures(verdict)
                    ),
                },
            ],
        )
        _log_usage("explain", self._model, response)
        return (response.choices[0].message.content or "").strip()


def build_client() -> OpenAIClient:
    """Construct the client once per Lambda container."""
    secret_arn = os.environ.get("OPENAI_SECRET_ARN")
    if not secret_arn:
        raise ProviderError("OPENAI_SECRET_ARN is unset")

    api_key = get_secret_value(secret_arn)
    if not api_key:
        raise ProviderError(
            f"secret {secret_arn} has no value yet - put the OpenAI API key in it"
        )

    return OpenAIClient(api_key=api_key, model=os.environ["OPENAI_MODEL"])
