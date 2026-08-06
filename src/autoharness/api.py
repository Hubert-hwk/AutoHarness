"""FastAPI transport for AutoHarness."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from autoharness import __version__
from autoharness.models import AgentTrace, AnalysisRequest, AnalysisResult, FailureDiagnosis
from autoharness.service import AutoHarness

app = FastAPI(
    title="AutoHarness",
    version=__version__,
    description="Agent trace diagnosis and code localization API",
)
service = AutoHarness()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.post("/v1/diagnose", response_model=FailureDiagnosis)
def diagnose(trace: AgentTrace) -> FailureDiagnosis:
    return service.diagnoser.diagnose(trace)


@app.post("/v1/analyze", response_model=AnalysisResult)
def analyze(request: AnalysisRequest | AgentTrace) -> AnalysisResult:
    if isinstance(request, AgentTrace):
        return service.analyze(request)
    try:
        return service.analyze(
            request.trace,
            repository_path=request.repository_path,
            candidate_limit=request.candidate_limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
