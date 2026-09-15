from __future__ import annotations

from fastapi import Request

from backend.services.benchmarks import BenchmarkService
from backend.services.engine import NimbusEngine


def get_engine(request: Request) -> NimbusEngine:
    return request.app.state.engine


def get_benchmarks(request: Request) -> BenchmarkService:
    return request.app.state.benchmarks
