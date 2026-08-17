"""独立重要性判定包。

为保持  可直接运行，这里采用惰性导出。
"""
from __future__ import annotations

__all__ = ["ActivityEvent", "ImportanceJudge", "JudgeResult"]


def __getattr__(name):
    if name in __all__:
        from . import importance
        return getattr(importance, name)
    raise AttributeError(name)
