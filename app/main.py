from fastapi import FastAPI

app = FastAPI(title="Flowdesk", description="LLM ticket triage with human review.")


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
