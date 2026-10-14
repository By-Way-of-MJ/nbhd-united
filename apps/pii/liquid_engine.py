"""Pinned LiquidAI PII detector running as a lazy CPU/FP32 singleton."""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LIQUID_MODEL_REPO = "LiquidAI/LFM2.5-Encoder-350M-PII-Detector"
LIQUID_MODEL_REVISION = "b8c9cf3d2d6ae52501b35a27ba46f271449c9ce2"

_MODEL_ROOT = os.environ.get(
    "PII_MODEL_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "pii-model"),
)
_MODEL_PATH = os.path.join(_MODEL_ROOT, "liquid")

_pipeline: object | None = None
_pipeline_load_error: Exception | None = None


MODEL_TOKEN_LIMIT = 2048
WINDOW_OVERLAP_TOKENS = 256
WINDOW_DURATION_FACTOR = 1.25
REPLY_MARGIN_S = 0.25


class _PreparedTokenizer:
    """Serve the pinned decoder an already-tokenized original-string window."""

    def __init__(self, text, encoding):
        self.text = text
        self.encoding = encoding

    def __call__(self, text, **kwargs):
        # This contract is pinned with the decoder revision. Never silently
        # ignore a changed tokenizer request after a future model upgrade.
        if text != self.text or kwargs != {
            "return_offsets_mapping": True,
            "return_tensors": "pt",
            "truncation": True,
            "max_length": MODEL_TOKEN_LIMIT,
        }:
            raise ValueError("Unexpected pinned Liquid tokenizer request")
        import torch

        return {key: torch.tensor([value]) for key, value in self.encoding.items()}


def _token_windows(text, encoded, *, overlap=WINDOW_OVERLAP_TOKENS):
    """Reuse real token IDs and offsets; never decode or re-tokenize a slice."""
    mask = encoded["special_tokens_mask"]
    offsets = encoded["offset_mapping"]
    content = [i for i, special in enumerate(mask) if not special]
    prefix = [i for i, special in enumerate(mask) if special and i < content[0]]
    suffix = [i for i, special in enumerate(mask) if special and i > content[-1]]
    if len(prefix) + len(suffix) != sum(mask):
        raise ValueError("Unexpected Liquid special-token layout")
    capacity = MODEL_TOKEN_LIMIT - len(prefix) - len(suffix)
    if not 0 <= overlap < capacity:
        raise ValueError("Invalid Liquid token overlap")
    begin = 0
    while begin < len(content):
        end = min(begin + capacity, len(content))
        # Byte-level tokens can share a Unicode character. Keep all pieces of
        # that character in one window, including at the overlapping start.
        while end < len(content) and offsets[content[end]][0] < offsets[content[end - 1]][1]:
            end -= 1
        if end <= begin:
            raise ValueError("Liquid character exceeds token window")
        start_char = 0 if begin == 0 else offsets[content[begin]][0]
        end_char = len(text) if end == len(content) else offsets[content[end - 1]][1]
        indices = prefix + content[begin:end] + suffix
        window = {key: [value[i] for i in indices] for key, value in encoded.items() if key != "special_tokens_mask"}
        window["offset_mapping"] = [
            (0, 0) if mask[i] else (offsets[i][0] - start_char, offsets[i][1] - start_char) for i in indices
        ]
        yield start_char, text[start_char:end_char], window
        if end == len(content):
            break
        next_begin = max(begin + 1, end - overlap)
        while next_begin > begin and offsets[content[next_begin]][0] < offsets[content[next_begin - 1]][1]:
            next_begin -= 1
        if next_begin <= begin:
            next_begin = end
        begin = next_begin


def _check_window_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        from apps.pii.shared_client import SharedPiiError

        raise SharedPiiError("Liquid window deadline exceeded", outcome="timeout")


class _LiquidPiiPipeline:
    """Adapt Liquid's hybrid decoder to the existing HF-pipeline span shape."""

    def __init__(self, tokenizer: object, model: object, decoder: object):
        self._tokenizer = tokenizer
        self._model = model
        self._decoder = decoder

    def __call__(
        self,
        text: str,
        *,
        deadline: float | None = None,
        on_window_deadline: Callable[[int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        encoded = self._tokenizer(
            text,
            return_offsets_mapping=True,
            return_special_tokens_mask=True,
            truncation=False,
        )
        if len(encoded["input_ids"]) <= MODEL_TOKEN_LIMIT:
            # Exactly the original model IDs, masks, offsets and full-context
            # hybrid decoder, with no span assembly on fitting input.
            return self._predict(text, {k: v for k, v in encoded.items() if k != "special_tokens_mask"})
        from apps.pii.span_merge import _preserve_baseline

        _check_window_deadline(deadline)
        # Origin truncates the neural encoding, NOT the text passed to hybrid
        # rules. Preserve its exact first 2048 IDs (including a partial Unicode
        # character), and run full-text hybrid rules in this same inference.
        # The pinned tokenizer adds only a leading BOS, no trailing special.
        if encoded["special_tokens_mask"] != [1] + [0] * (len(encoded["input_ids"]) - 1):
            raise ValueError("Unexpected pinned Liquid special-token layout")
        first = {k: v[:MODEL_TOKEN_LIMIT] for k, v in encoded.items() if k != "special_tokens_mask"}
        window_started = time.monotonic()
        baseline = self._predict(text, first)
        max_window_s = time.monotonic() - window_started
        spans = []
        windows = list(_token_windows(text, encoded))
        completed = 1  # The origin prefix was already computed exactly once.
        exhausted = False
        for start, part, window in windows[1:]:
            # Budget for another inference using the slowest observation, plus
            # merge/serialization headroom. Equality is sufficient for admission.
            if deadline is not None and deadline - time.monotonic() < (
                WINDOW_DURATION_FACTOR * max_window_s + REPLY_MARGIN_S
            ):
                exhausted = True
                break
            window_started = time.monotonic()
            window_spans = self._predict(part, window)
            max_window_s = max(max_window_s, time.monotonic() - window_started)
            for span in window_spans:
                if not 0 <= span["start"] < span["end"] <= len(part):
                    raise ValueError("Invalid Liquid window span")
                spans.append(
                    {
                        **span,
                        "start": start + span["start"],
                        "end": start + span["end"],
                        "_window": start,
                        "_context": min(span["start"], len(part) - span["end"]),
                    }
                )
            completed += 1
        # Once origin's result exists, budget exhaustion must never discard it.
        # Only completed windows contribute; no new inference starts on expiry.
        result = _preserve_baseline(baseline, spans, text)
        if exhausted or (deadline is not None and time.monotonic() >= deadline):
            if on_window_deadline is not None:
                on_window_deadline(completed, len(windows))
        return result

    def _predict(self, text, encoded):
        spans = self._decoder.predict(text, _PreparedTokenizer(text, encoded), self._model, hybrid=True)
        return [
            {
                "entity_group": str(span["type"]),
                "word": text[int(span["start"]) : int(span["end"])],
                # The pinned hybrid decoder exposes accepted spans, no scores.
                "score": 1.0,
                "start": int(span["start"]),
                "end": int(span["end"]),
            }
            for span in spans
        ]


def _load_decoder(model_source: str):
    if os.path.isdir(model_source):
        helper_path = Path(model_source) / "pii_hybrid_decode.py"
        context_path = Path(model_source) / "context_cued.py"
        if not helper_path.is_file() or not context_path.is_file():
            raise FileNotFoundError("Liquid model artifact is missing pii_hybrid_decode.py or context_cued.py")
    else:
        from huggingface_hub import hf_hub_download

        helper_path = Path(
            hf_hub_download(
                LIQUID_MODEL_REPO,
                "pii_hybrid_decode.py",
                revision=LIQUID_MODEL_REVISION,
            )
        )
        context_path = Path(
            hf_hub_download(
                LIQUID_MODEL_REPO,
                "context_cued.py",
                revision=LIQUID_MODEL_REVISION,
            )
        )

    helper_dir = str(helper_path.parent)
    if helper_dir not in sys.path:
        sys.path.insert(0, helper_dir)

    context_spec = importlib.util.spec_from_file_location("context_cued", context_path)
    if context_spec is None or context_spec.loader is None:
        raise RuntimeError(f"Could not load Liquid context helper from {context_path}")
    context_module = importlib.util.module_from_spec(context_spec)
    # pii_hybrid_decode.py imports this exact top-level name. Install the module
    # from the same pinned snapshot before executing the decoder so an unrelated
    # context_cued module elsewhere on sys.path can never win.
    sys.modules["context_cued"] = context_module
    context_spec.loader.exec_module(context_module)

    spec = importlib.util.spec_from_file_location(
        "pinned_liquid_pii_hybrid_decode",
        helper_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load Liquid decoder from {helper_path}")
    decoder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(decoder)
    return decoder


def _build_liquid_pii_pipeline() -> _LiquidPiiPipeline:
    import torch
    from transformers import AutoModelForTokenClassification, AutoTokenizer

    model_source = _MODEL_PATH if os.path.isdir(_MODEL_PATH) else LIQUID_MODEL_REPO
    remote_kwargs = {}
    if model_source == LIQUID_MODEL_REPO:
        remote_kwargs["revision"] = LIQUID_MODEL_REVISION

    decoder = _load_decoder(model_source)
    tokenizer = AutoTokenizer.from_pretrained(
        model_source,
        trust_remote_code=True,
        **remote_kwargs,
    )
    model = AutoModelForTokenClassification.from_pretrained(
        model_source,
        trust_remote_code=True,
        **remote_kwargs,
    )
    model = model.to(device="cpu", dtype=torch.float32).eval()
    logger.info("Liquid PII detection model loaded from %s", model_source)
    return _LiquidPiiPipeline(tokenizer, model, decoder)


def get_liquid_pii_pipeline():
    """Return the shared Liquid callable, caching success or load failure.

    The remote fallback pins weights, tokenizer, custom model code, and hybrid
    decode helpers to ``LIQUID_MODEL_REVISION``. Production uses the identically
    pinned artifact baked beneath ``PII_MODEL_PATH/liquid``.
    """
    global _pipeline, _pipeline_load_error
    if _pipeline_load_error is not None:
        raise _pipeline_load_error
    if _pipeline is not None:
        return _pipeline

    try:
        _pipeline = _build_liquid_pii_pipeline()
    except Exception as exc:
        logger.error(
            "Liquid PII model failed to initialize — disabling neural PII detection "
            "for this process; falling back to pattern recognizers only. Restart the "
            "container after fixing the dependency to retry.",
            exc_info=True,
        )
        _pipeline_load_error = exc
        raise
    return _pipeline
