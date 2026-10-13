"""
app.py — FastAPI wrapper.

Endpoints:
    GET  /health   — liveness probe
    GET  /me       — authenticated user's claims
    POST /ask      — runs the pipeline
    GET  /metrics/ — Prometheus metrics
"""

import logging
from contextlib import asynccontextmanager
from dotenv import load_dotenv

from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from prometheus_client import Counter, Histogram, make_asgi_app
from langchain_core.callbacks import BaseCallbackHandler

from auth import get_current_user, require_role
from graph import graph_app, State

from fastapi.middleware.cors import CORSMiddleware


load_dotenv(override=True)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("graphrag-api")


QUERIES = Counter("graphrag_queries_total", "Queries", ["status", "user"])
LATENCY = Histogram("graphrag_query_seconds", "Latency",
                    buckets=(0.5, 1, 2, 5, 10, 20, 60))
TOKENS = Counter("graphrag_llm_tokens_total", "LLM tokens consumed", ["kind"])
LLM_CALLS = Counter("graphrag_llm_calls_total", "Total LLM API calls")


class TokenTracker(BaseCallbackHandler):
    def on_llm_end(self, response, **kwargs):
        usage = (response.llm_output or {}).get("token_usage", {})
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        if prompt:
            TOKENS.labels(kind="prompt").inc(prompt)
        if completion:
            TOKENS.labels(kind="completion").inc(completion)
        LLM_CALLS.inc()
        logger.info(f"tokens prompt={prompt} completion={completion}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("GraphRAG API starting")
    yield
    logger.info("GraphRAG API shutting down")


app = FastAPI(title="GraphRAG API", version="1.0.0", lifespan=lifespan)
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
...

app = FastAPI(title="GraphRAG API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3001",
        "http://127.0.0.1:3001",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/metrics", make_asgi_app())


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=1000)


class AskResponse(BaseModel):
    answer: str
    documents: list = []
    error: str = ""


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/me")
async def me(user: dict = Depends(get_current_user)):
    return {
        "sub": user.get("sub"),
        "preferred_username": user.get("preferred_username"),
        "email": user.get("email"),
        "roles": user.get("realm_access", {}).get("roles", []),
    }


@app.post("/ask", response_model=AskResponse)
async def ask(req: AskRequest, user: dict = Depends(require_role("graphrag-user"))):
    username = user.get("preferred_username", "anonymous")
    logger.info(f"ask user={username} q={req.question[:80]}")

    state: State = {
        "question": req.question,
        "list_a_ids": [], "list_b_ids": [], "expanded_pool_ids": [],
        "docs": [], "final_docs": [],
        "context": "", "answer": "", "error": "",
    }

    tracker = TokenTracker()

    with LATENCY.time():
        try:
            out = graph_app.invoke(state, config={"callbacks": [tracker]})
        except Exception as e:
            logger.exception("graph invoke failed")
            QUERIES.labels(status="error", user=username).inc()
            raise HTTPException(status_code=500, detail=str(e))

    QUERIES.labels(
        status="error" if out.get("error") else "success",
        user=username,
    ).inc()

    return AskResponse(
        answer=out.get("answer", ""),
        documents=[
            {
                "title": d.page_content,
                "authors": d.metadata.get("authors", []),
                "topics": d.metadata.get("topics", []),
            }
            for d in out.get("final_docs", [])
        ],
        error=out.get("error", ""),
    )


@app.exception_handler(Exception)
async def unhandled(request, exc):
    logger.exception("unhandled error")
    return JSONResponse(status_code=500, content={"detail": "Internal error"})