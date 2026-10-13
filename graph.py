"""
graph.py — LangGraph pipeline.

    User Query
        ├──► [ Vector Retriever ] ──► Ranked List A (IDs)
        └──► [ Graph Traversal ]  ──► Ranked List B (IDs)
                    │
                    ▼
        [ RRF ] ──► [ Expanded Pool ]
                    │
                    ▼
        [ Fetch Content Storage ]
                    │
                    ▼
        [ Cross-Encoder Reranker ] ──► [ Final Top-K ]
                    │
                    ▼
        [ LLM Generation ]
"""

import os
import logging
from typing import TypedDict
from dotenv import load_dotenv

from langchain_neo4j import Neo4jVector, Neo4jGraph
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_classic.retrievers.document_compressors import CrossEncoderReranker
from langchain_community.cross_encoders import HuggingFaceCrossEncoder
from langgraph.graph import StateGraph, END

load_dotenv(override=True)
logging.getLogger("neo4j").setLevel(logging.CRITICAL)

URI = os.getenv("NEO4J_URI")
USER = os.getenv("NEO4J_USERNAME")
PWD = os.getenv("NEO4J_PASSWORD")
DB = os.getenv("NEO4J_DATABASE", "0bd407b0")

VECTOR_K = 20
GRAPH_K = 20
POOL_SIZE = 30
FINAL_K = 5
RRF_K = 60


class State(TypedDict):
    question: str
    list_a_ids: list
    list_b_ids: list
    expanded_pool_ids: list
    docs: list
    final_docs: list
    context: str
    answer: str
    error: str


vector_store = Neo4jVector.from_existing_index(
    embedding=OpenAIEmbeddings(),
    url=URI, username=USER, password=PWD, database=DB,
    index_name="article_embeddings",
    text_node_property="title",
)

graph = Neo4jGraph(
    url=URI, username=USER, password=PWD, database=DB,
    driver_config={"max_connection_lifetime": 300, "keep_alive": True},
)


def vector_retriever(state: State) -> State:
    docs = vector_store.similarity_search(state["question"], k=VECTOR_K)
    state["list_a_ids"] = [d.page_content for d in docs]
    return state


GRAPH_TRAVERSAL = """
UNWIND $titles AS t
MATCH (start:Article {title: t})
OPTIONAL MATCH (start)-[:WRITTEN_BY|COVERS_TOPIC*1..2]-(neighbor:Article)
WHERE neighbor.title <> start.title
WITH neighbor, count(*) AS connections
ORDER BY connections DESC
LIMIT $limit
RETURN neighbor.title AS title
"""


def graph_traversal(state: State) -> State:
    seeds = state.get("list_a_ids", [])[:3]
    if not seeds:
        state["list_b_ids"] = []
        return state
    rows = graph.query(GRAPH_TRAVERSAL, params={"titles": seeds, "limit": GRAPH_K})
    state["list_b_ids"] = [r["title"] for r in rows]
    return state


def rrf(state: State) -> State:
    scores: dict[str, float] = {}
    for ranking in (state.get("list_a_ids", []), state.get("list_b_ids", [])):
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (RRF_K + rank)
    ordered = sorted(scores.items(), key=lambda x: -x[1])
    state["expanded_pool_ids"] = [doc_id for doc_id, _ in ordered[:POOL_SIZE]]
    return state


FETCH_CONTENT = """
UNWIND $titles AS title
MATCH (a:Article {title: title})
OPTIONAL MATCH (a)-[:WRITTEN_BY]->(au:Author)
WITH a, collect(DISTINCT au.name)[..10] AS authors
OPTIONAL MATCH (a)-[:COVERS_TOPIC]->(t:Topic)
RETURN a.title AS title, authors, collect(DISTINCT t.name)[..5] AS topics
"""


def fetch_content(state: State) -> State:
    ids = state.get("expanded_pool_ids", [])
    if not ids:
        state["docs"] = []
        return state
    rows = graph.query(FETCH_CONTENT, params={"titles": ids})
    state["docs"] = [
        Document(page_content=r["title"],
                 metadata={"authors": r["authors"], "topics": r["topics"]})
        for r in rows
    ]
    return state


reranker = CrossEncoderReranker(
    model=HuggingFaceCrossEncoder(model_name="cross-encoder/ms-marco-MiniLM-L-6-v2"),
    top_n=FINAL_K,
)


def rerank(state: State) -> State:
    docs = state.get("docs", [])
    if not docs:
        state["final_docs"] = []
        state["context"] = ""
        return state
    state["final_docs"] = reranker.compress_documents(docs, state["question"])
    state["context"] = "\n".join(
        f"- {d.page_content} | Authors: {', '.join(d.metadata.get('authors', [])) or '—'} "
        f"| Topics: {', '.join(d.metadata.get('topics', [])) or '—'}"
        for d in state["final_docs"]
    )
    return state


PROMPT = ChatPromptTemplate.from_template(
    "You are listing articles retrieved for a query.\n"
    "List EVERY row from the context, one per line.\n"
    "Format: 'N. Title | Authors: ... | Topics: ...'\n"
    "Do NOT skip rows. Do NOT summarize.\n\n"
    "Context:\n{context}\n\n"
    "Question: {question}\n\n"
    "List of articles:"
)
llm_chain = PROMPT | ChatOpenAI(model="gpt-4o-mini", temperature=0) | StrOutputParser()


def generate(state: State) -> State:
    if state.get("error"):
        state["answer"] = state["error"]
        return state
    state["answer"] = llm_chain.invoke({
        "context": state.get("context", ""),
        "question": state["question"],
    })
    return state


wf = StateGraph(State)
for name, fn in [
    ("vector_retriever", vector_retriever),
    ("graph_traversal", graph_traversal),
    ("rrf", rrf),
    ("fetch_content", fetch_content),
    ("rerank", rerank),
    ("generate", generate),
]:
    wf.add_node(name, fn)

wf.set_entry_point("vector_retriever")
wf.add_edge("vector_retriever", "graph_traversal")
wf.add_edge("graph_traversal", "rrf")
wf.add_edge("rrf", "fetch_content")
wf.add_edge("fetch_content", "rerank")
wf.add_edge("rerank", "generate")
wf.add_edge("generate", END)

graph_app = wf.compile()