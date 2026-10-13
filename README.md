# Neo4j-GraphRag

╔══════════════════════════════════════════════════════════════════════════╗
║                          USER'S BROWSER                                  ║
║                     http://localhost:3001                                ║
║                                                                          ║
║  ┌────────────────────────────────────────────────────────────────────┐  ║
║  │  index.html + app.js + style.css  (served by nginx :3001)          │  ║
║  │                                                                    │  ║
║  │  1. Page loads                                                     │  ║
║  │  2. keycloak.esm.js imported → exposes globalThis.Keycloak         │  ║
║  │  3. initKeycloak() runs → keycloak.init({onLoad: "check-sso"})     │  ║
║  └────────────────────────┬───────────────────────────────────────────┘  ║
╚═══════════════════════════│══════════════════════════════════════════════╝
                            │
                            │ (no session)
                            ▼
╔══════════════════════════════════════════════════════════════════════════╗
║                       KEYCLOAK :8080                                     ║
║                                                                          ║
║  ┌────────────────────────────────────────────────────────────────────┐  ║
║  │  /realms/graphrag/protocol/openid-connect/auth                     │  ║
║  │                                                                    │  ║
║  │  • Shows login form                                                │  ║
║  │  • User enters: alice / alice-pass                                 │  ║
║  │  • Validates credentials                                           │  ║
║  │  • Issues authorization code                                       │  ║
║  │  • Redirects back to http://localhost:3001/?code=...               │  ║
║  └────────────────────────┬───────────────────────────────────────────┘  ║
╚═══════════════════════════│══════════════════════════════════════════════╝
                            │
                            │ (redirect with code)
                            ▼
╔══════════════════════════════════════════════════════════════════════════╗
║                    BROWSER (again)                                       ║
║                                                                          ║
║  app.js:                                                                ║
║    • Detects ?code= in URL                                              ║
║    • POSTs to /realms/graphrag/protocol/openid-connect/token            ║
║      with code + code_verifier (PKCE)                                   ║
║    • Receives access_token (JWT) + refresh_token                        ║
║    • Stores in memory                                                   ║
║    • renderUser() → header shows "alice"                                ║
║    • scheduleTokenRefresh() runs every 20s                              ║
╚═══════════════════════════│══════════════════════════════════════════════╝
                            │
                            │ User types a question and clicks Send
                            ▼
╔══════════════════════════════════════════════════════════════════════════╗
║                    BROWSER → API :8000                                   ║
║                                                                          ║
║  fetch("http://localhost:8000/ask", {                                   ║
║    method: "POST",                                                      ║
║    headers: {                                                           ║
║      "Authorization": "Bearer <JWT>",                                   ║
║      "Content-Type": "application/json"                                 ║
║    },                                                                   ║
║    body: JSON.stringify({question: "..."})                              ║
║  })                                                                     ║
╚═══════════════════════════│══════════════════════════════════════════════╝
                            │
                            │ 1. OPTIONS /ask (CORS preflight)
                            ▼
╔══════════════════════════════════════════════════════════════════════════╗
║                     FASTRAG API :8000                                    ║
║                     (app.py)                                             ║
║                                                                          ║
║  ┌────────────────────────────────────────────────────────────────────┐  ║
║  │  CORSMiddleware                                                    │  ║
║  │  • Responds to OPTIONS with 200 + Access-Control-Allow-*           │  ║
║  └────────────────────────┬───────────────────────────────────────────┘  ║
║                           │                                              ║
║  ┌────────────────────────▼───────────────────────────────────────────┐  ║
║  │  auth.py — JWT validation                                          │  ║
║  │  • Fetches JWKS from http://keycloak:8080/.../certs (cached 1h)    │  ║
║  │  • Verifies signature (RS256)                                      │  ║
║  │  • Verifies issuer = http://localhost:8080/realms/graphrag         │  ║
║  │  • Verifies expiry                                                 │  ║
║  │  • Checks realm_access.roles contains "graphrag-user"              │  ║
║  └────────────────────────┬───────────────────────────────────────────┘  ║
║                           │                                              ║
║  ┌────────────────────────▼───────────────────────────────────────────┐  ║
║  │  TokenTracker attached via config={"callbacks": [tracker]}         │  ║
║  │  graph_app.invoke(state)                                           │  ║
║  └────────────────────────┬───────────────────────────────────────────┘  ║
╚═══════════════════════════│══════════════════════════════════════════════╝
                            │
                            │ 2. LangGraph pipeline (graph.py)
                            ▼
╔══════════════════════════════════════════════════════════════════════════╗
║                    LANGGRAPH PIPELINE                                    ║
║                                                                          ║
║   ┌──────────────────┐                                                   ║
║   │ vector_retriever │  (embed question → vector index)                  ║
║   └────────┬─────────┘                                                   ║
║            │ list_a_ids (20 titles)                                      ║
║            ▼                                                             ║
║   ┌──────────────────┐                                                   ║
║   │ graph_traversal  │  (2-hop expansion from top 3 seeds)               ║
║   └────────┬─────────┘                                                   ║
║            │ list_b_ids (20 titles)                                      ║
║            ▼                                                             ║
║   ┌──────────────────┐                                                   ║
║   │       rrf        │  (Reciprocal Rank Fusion, k=60)                   ║
║   └────────┬─────────┘                                                   ║
║            │ expanded_pool_ids (30 titles)                               ║
║            ▼                                                             ║
║   ┌──────────────────┐                                                   ║
║   │  fetch_content   │  (attach authors + topics via Cypher)             ║
║   └────────┬─────────┘                                                   ║
║            │ docs[] with metadata                                        ║
║            ▼                                                             ║
║   ┌──────────────────┐                                                   ║
║   │      rerank      │  (CrossEncoderReranker, top_n=5)                  ║
║   └────────┬─────────┘                                                   ║
║            │ final_docs (top 5)                                          ║
║            ▼                                                             ║
║   ┌──────────────────┐                                                   ║
║   │     generate     │  (LLM writes answer from context)                 ║
║   └────────┬─────────┘                                                   ║
║            │ answer                                                      ║
╚════════════│═════════════════════════════════════════════════════════════╝
             │
             ├──────────────────────┬──────────────────────┐
             │                      │                      │
             ▼                      ▼                      ▼
    ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
    │ Neo4j Vector    │   │  Neo4j Graph    │   │  OpenAI API     │
    │                 │   │                 │   │                 │
    │ article_        │   │ :Article        │   │ • embeddings    │
    │ embeddings      │   │ :Author         │   │ • chat completion│
    │ (HNSW index)    │   │ :Topic          │   │ • token usage   │
    │                 │   │ WRITTEN_BY      │   │                 │
    │                 │   │ COVERS_TOPIC    │   │                 │
    └─────────────────┘   └─────────────────┘   └─────────────────┘
             ▲                      ▲                      ▲
             │                      │                      │
             └──────────────────────┴──────────────────────┘
                                    │
                                    │ Cypher queries + HTTP
                                    │
╔═══════════════════════════════════│══════════════════════════════════════╗
║                    OBSERVABILITY  │                                      ║
║                                   │                                      ║
║   Prometheus :9090                │                                      ║
║   • Scrapes graphrag-api:8000/metrics/ every 15s                        ║
║   • Scrapes keycloak:9000/metrics every 15s                             ║
║                                                                          ║
║   Grafana :3000                                                          ║
║   • Reads from Prometheus                                                ║
║   • Dashboards: queries/min, latency p95, tokens/min, cost/hour         ║
╚══════════════════════════════════════════════════════════════════════════╝
                                    │
                                    │ 3. Response
                                    ▼
╔══════════════════════════════════════════════════════════════════════════╗
║                    BROWSER (final)                                       ║
║                                                                          ║
║  JSON response:                                                         ║
║  {                                                                      ║
║    "answer": "1. Embedding Search Pipelines | Authors: ...",            ║
║    "documents": [                                                       ║
║      {title, authors, topics},                                          ║
║      ...5 documents                                                     ║
║    ],                                                                   ║
║    "error": ""                                                          ║
║  }                                                                      ║
║                                                                          ║
║  app.js renders:                                                        ║
║  • The question as a user bubble                                        ║
║  • The answer as a bot bubble                                           ║
║  • The 5 documents with authors + topics below                          ║
╚══════════════════════════════════════════════════════════════════════════╝
