"""LangSmith tracing for the hospital agent.

Wraps the key agent / tool / LLM functions so their runs appear in LangSmith.
The module degrades to a no-op when LangSmith is not configured
(no LANGSMITH_API_KEY / LANGSMITH_TRACING), so it is always safe to import
even if the `langsmith` package is absent.
"""
import os
import functools

_LANGSMITH_AVAILABLE = False
_traceable = None
_Client = None

try:
    from langsmith import traceable as _traceable
    from langsmith import Client as _Client
    _LANGSMITH_AVAILABLE = True
except Exception:
    _LANGSMITH_AVAILABLE = False


def _tracing_enabled() -> bool:
    return bool(os.getenv("LANGSMITH_API_KEY")) or os.getenv(
        "LANGSMITH_TRACING", ""
    ).lower() in ("1", "true", "yes", "on")


def traced(name: str, run_type: str = "chain", tags: list = None):
    """Decorator that traces a function in LangSmith when configured."""
    def decorator(func):
        if _LANGSMITH_AVAILABLE and _tracing_enabled():
            return _traceable(name=name, run_type=run_type, tags=tags or [])(func)
        return func
    return decorator


def add_metadata(**kwargs):
    """Attach metadata to the currently active LangSmith run."""
    if not (_LANGSMITH_AVAILABLE and _tracing_enabled()):
        return
    try:
        client = _Client()
        client.update_current_run(metadata={**_current_metadata(), **kwargs})
    except Exception:
        pass


def _current_metadata() -> dict:
    try:
        client = _Client()
        run = client.read_current_run()
        return dict(getattr(run, "metadata", {}) or {})
    except Exception:
        return {}


def record_usage(completion, model: str = ""):
    """Record token usage + cost onto the active LangSmith run.

    Raw OpenAI clients don't populate usage automatically; record_usage writes
    it to run.metadata["usage_metadata"] so LangSmith computes tokens/cost.
    """
    if not (_LANGSMITH_AVAILABLE and _tracing_enabled()):
        return
    try:
        usage = getattr(completion, "usage", None)
        if usage is None and isinstance(completion, dict):
            usage = completion.get("usage")
        if usage is None:
            return
        add_metadata(**{
            "usage_metadata": {
                "input_tokens": getattr(usage, "prompt_tokens", None),
                "output_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
                "model": model or getattr(usage, "model", ""),
            }
        })
    except Exception:
        pass