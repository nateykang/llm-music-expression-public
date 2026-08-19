"""Generation modes. Each turns an LLM response into a renderable artifact."""

from . import abc, codegen

MODES = {"codegen": codegen, "abc": abc}

__all__ = ["MODES", "abc", "codegen"]
