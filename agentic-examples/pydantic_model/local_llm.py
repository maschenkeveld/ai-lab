"""Run a model with vLLM's Python `LLM` class and use it as a Pydantic AI model.

Copy this file unchanged into other examples. What it provides:

- `ServedModel`: which model to load (and optionally a chat template to override its own).
- `loaded(served, memory_fraction)`: context manager that loads the model in-process and frees it
  afterwards. `memory_fraction` is vLLM-Metal's `gpu_memory_utilization`: a cap on Metal memory
  for everything loaded in this process (weights and KV cache), not per model. Load the model that
  stays resident first with a small fraction (0.3), then the others with a larger one (0.6).
- `local_model(llm, served, temperature)`: a Pydantic AI model backed by `LLM.chat()`.

Limits: text only. Tool calls are not supported (to_chat raises on tool parts), and streaming yields
the whole reply as one chunk. The
model profile makes agents use prompted output (schema in the prompt, reply parsed) by default.
Pydantic AI cannot call an `LLM` object directly, hence the FunctionModel wrapper.
"""

import gc
import glob
import os
import shutil
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# One process: no separate engine process, so MLX/Metal stays on the thread that loaded the model.
os.environ.setdefault("VLLM_ENABLE_V1_MULTIPROCESSING", "0")

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.usage import RequestUsage

VLLM_BIN = Path(shutil.which("vllm") or "/opt/homebrew/opt/vllm-metal/bin/vllm")


def add_vllm_to_path() -> None:
    """vLLM-Metal (and MLX, torch) live in the Homebrew install's own environment.

    Appending (not prepending) its site-packages keeps this folder's packages first, so only
    vllm and its native dependencies come from there. Python minor versions must match (3.12).
    """
    site = glob.glob(str(VLLM_BIN.resolve().parents[1] / "lib" / "python3.*" / "site-packages"))
    if not site:
        raise SystemExit("vLLM-Metal not found; run `task inference:install`")
    if site[0] not in sys.path:
        sys.path.append(site[0])


@dataclass
class ServedModel:
    repo_id: str
    chat_template: Path | None = None  # None: the model's own template


def to_chat(messages: list[ModelMessage], instructions: str | None) -> list[dict[str, str]]:
    """Pydantic AI messages -> chat messages for LLM.chat (text only; no tool calls)."""
    chat = [{"role": "system", "content": instructions}] if instructions else []
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, SystemPromptPart):
                    chat.append({"role": "system", "content": part.content})
                elif isinstance(part, UserPromptPart):
                    assert isinstance(part.content, str), "only text prompts are supported"
                    chat.append({"role": "user", "content": part.content})
                elif isinstance(part, RetryPromptPart):
                    chat.append({"role": "user", "content": part.model_response()})
                else:
                    raise NotImplementedError(f"{type(part).__name__} (tool calls) not supported")
        else:
            text = "".join(p.content for p in message.parts if isinstance(p, TextPart))
            chat.append({"role": "assistant", "content": text})
    return chat


def local_model(llm: Any, served: ServedModel, temperature: float) -> FunctionModel:
    from vllm import SamplingParams

    template = served.chat_template.read_text() if served.chat_template else None
    sampling = SamplingParams(temperature=temperature, max_tokens=500)

    # async on purpose: a sync function may be run in a worker thread, and MLX streams are
    # bound to the thread that loaded the model.
    async def generate(messages: list[ModelMessage], info: AgentInfo) -> tuple[str, RequestUsage]:
        out = llm.chat(
            to_chat(messages, info.instructions),
            sampling,
            chat_template=template,
            use_tqdm=False,
        )[0]
        usage = RequestUsage(
            input_tokens=len(out.prompt_token_ids), output_tokens=len(out.outputs[0].token_ids)
        )
        return out.outputs[0].text, usage

    async def call(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        text, usage = await generate(messages, info)
        return ModelResponse(parts=[TextPart(text)], usage=usage)

    # Some capabilities (for example agento11y's) make Pydantic AI run the agent through the
    # streaming path. LLM.chat cannot stream tokens, so this yields the whole reply as one chunk.
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        text, _ = await generate(messages, info)
        yield text

    # Declares what this adapter can do, so any agent using it (including pydantic_evals' LLM
    # judge) asks for prompted output by default and fails clearly if it needs tool calls.
    profile = ModelProfile(default_structured_output_mode="prompted", supports_tools=False)
    return FunctionModel(call, stream_function=stream, model_name=served.repo_id, profile=profile)


@contextmanager
def loaded(served: ServedModel, memory_fraction: float = 0.3) -> Iterator[tuple[Any, float]]:
    """Load a model with vLLM's LLM class and free it again afterwards (checked: RSS and MLX
    active memory return to ~0 after del + gc + clear_cache)."""
    add_vllm_to_path()
    import mlx.core as mx
    from vllm import LLM

    started = time.monotonic()
    llm = LLM(served.repo_id, max_model_len=4096, gpu_memory_utilization=memory_fraction)
    try:
        yield llm, time.monotonic() - started
    finally:
        del llm
        gc.collect()
        mx.clear_cache()
