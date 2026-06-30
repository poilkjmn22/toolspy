"""cable_engine.pipeline — Stage / Context pipeline for cable-match.

A Stage takes a Context, mutates it, and returns it. The pipeline is
a list of Stages. Each Stage is independently testable and swappable
(borrowed from FFmpeg's filter pattern).

See stage.py for the core abstractions (Context, Stage, Pipeline).
"""

from .stage import Context, Pipeline, Stage

__all__ = ['Context', 'Pipeline', 'Stage']

