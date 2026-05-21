# Architecture Diagrams

Visual reference for the Corporate Bot platform architecture. All diagrams use [Mermaid](https://mermaid.js.org/) syntax.

---

## 1. System Architecture

High-level view of containers, external services, and data flow.

```mermaid
graph TD
    subgraph External Services
        AzureAD[Azure AD<br/>Authentication + SSO]
        GitHub[GitHub<br/>Knowledge Repos]
        GraphAPI[Microsoft Graph API<br/>SharePoint + Teams Files]
        GeminiAPI[Gemini API<br/>Embeddings + DocAI + Media]
        VapiAPI[Vapi<br/>Voice Calls]
    end

    subgraph Microsoft Teams
        TeamsClient[Teams Desktop/Web/Mobile]
    end

    subgraph Messaging Channels
        TG[Telegram<br/>MTProto Userbot]
    end

    subgraph Docker Host
        subgraph bot-core Container
            FastAPI[FastAPI Server<br/>main.py]
            AgentPool[Client Pool<br/>bot/agent.py]
            Hooks[Security Hooks<br/>bot/hooks.py<br/>4-Tier Access]
            Harness[Harness System<br/>bot/harness.py]
            MCP[MCP Tool Servers<br/>bot/mcp/ - 84 tools]
            Prompts[Prompt Builder<br/>bot/prompts.py]
            Scheduler[Task Scheduler<br/>bot/scheduler.py]
            DomainSync[Domain Sync<br/>github_sync.py + graph_sync.py]
            TokenVault[Token Vault<br/>bot/auth/token_vault.py]
            Audit[Audit Logger<br/>bot/audit.py]
        end

        subgraph Storage Volumes
            BotData[(bot-data<br/>SQLite DBs)]
            MediaCache[(media-cache<br/>Cached Files)]
            ClaudeSessions[(claude-sessions<br/>SDK State)]
        end

        subgraph Optional Services
            Camofox[Camoufox Browser<br/>Anti-detect Firefox]
            Playwright[Playwright<br/>Chromium SSE Server]
        end
    end

    subgraph Claude SDK
        ClaudeAPI[Claude API<br/>Sonnet / Opus]
    end

    TeamsClient -->|Webhook POST| FastAPI
    TG -->|MTProto| FastAPI

    FastAPI --> AgentPool
    AgentPool --> Hooks
    AgentPool --> Harness
    AgentPool -->|Query| ClaudeAPI
    AgentPool --> MCP
    AgentPool --> Prompts

    FastAPI --> Scheduler
    FastAPI -->|JWT Validation| AzureAD

    MCP --> BotData
    MCP --> Camofox
    MCP --> Playwright
    MCP --> GeminiAPI
    MCP --> VapiAPI

    DomainSync -->|Fetch Content| GitHub
    DomainSync -->|Fetch Content| GraphAPI
    DomainSync -->|Store Chunks| BotData

    TokenVault -->|OBO Exchange| AzureAD
    TokenVault -->|Encrypted Storage| BotData

    Audit --> BotData

    AgentPool --> ClaudeSessions
    AgentPool --> MediaCache
```

---

## 2. Knowledge Domain Data Flow

How documentation flows from source repositories (GitHub or Microsoft Graph API) through the indexing pipeline to search results. Gemini Flash handles document parsing (PDF, DOCX, XLSX, PPTX, images) and contextual chunk enrichment.

```mermaid
sequenceDiagram
    participant GH as GitHub Repo
    participant MS as Graph API<br/>(SharePoint/Teams)
    participant WH as Webhook Handler
    participant Sync as github_sync.py /<br/>graph_sync.py
    participant Parser as doc_parser.py<br/>(Gemini Flash)
    participant Enrich as Contextual Enrichment<br/>(Gemini Flash)
    participant Chunk as Chunking Engine
    participant Embed as Gemini Embeddings
    participant DB as SQLite<br/>(rag_chunks + domain_documents)
    participant User as Employee
    participant Bot as Claude Agent
    participant Search as search_knowledge()

    Note over GH,WH: 1. Content Change Detection
    GH->>WH: Push webhook (HMAC-SHA256 signed)
    WH->>WH: Verify GITHUB_WEBHOOK_SECRET
    WH->>Sync: Trigger sync for domain_id

    Note over MS,Sync: 1b. Graph API Source (alternative)
    MS->>Sync: Delta query / poll for changes
    Sync->>MS: GET /drives/{id}/root/children
    MS-->>Sync: File list + eTag hashes

    Note over Sync,DB: 2. Incremental Fetch
    Sync->>Sync: Compare hash vs domain_documents.content_hash
    Sync->>GH: GET raw content (changed files only)
    GH-->>Sync: File content

    Note over Parser,Embed: 3. Parse + Enrich + Chunk + Embed
    Sync->>Parser: Parse file (MD / PDF / DOCX / XLSX / PPTX / images)
    Parser->>Parser: Gemini multimodal extraction (binary formats)
    Parser-->>Sync: Extracted text
    Sync->>Chunk: Apply chunking_strategy
    Chunk-->>Sync: Text chunks with metadata
    Sync->>Enrich: Contextual Retrieval enrichment
    Enrich->>Enrich: Gemini generates context prefix per chunk
    Enrich-->>Sync: Enriched chunks
    Sync->>Embed: Batch embed chunks
    Embed-->>Sync: 3072-dim vectors

    Note over DB: 4. Store
    Sync->>DB: INSERT rag_chunks (domain_id, embedding, text, source_file)
    Sync->>DB: UPDATE domain_documents (content_hash, last_indexed)
    Sync->>DB: UPDATE domain_registry (chunk_count, last_sync_at)

    Note over User,Search: 5. Query Time
    User->>Bot: "How do I deploy to staging?"
    Bot->>Search: search_knowledge(query, domains)
    Search->>Embed: Embed query text
    Embed-->>Search: Query vector
    Search->>DB: SELECT FROM rag_chunks<br/>WHERE domain_id IN (allowed_domains)<br/>ORDER BY cosine_similarity DESC
    DB-->>Search: Top-K chunks with scores
    Search-->>Bot: Ranked results with source attribution
    Bot-->>User: Answer with citations
```

---

## 3. Permission Model

Four-tier access control from Bot Admin down to Guest, showing how permissions flow through harnesses and enforcement layers.

```mermaid
graph TD
    subgraph User Identification
        Msg[Incoming Message] --> Extract[Extract AAD Object ID<br/>from Teams Activity]
        Extract --> AdminCheck{In BOT_ADMIN_AAD_IDS?}
    end

    subgraph Tier Resolution
        AdminCheck -->|Yes| T1[Tier 1: BOT ADMIN<br/>Full access to everything]
        AdminCheck -->|No| TenantCheck{Tenant matches<br/>TEAMS_TENANT_ID?}
        TenantCheck -->|No| T4[Tier 4: GUEST<br/>search_knowledge only]
        TenantCheck -->|Yes| T3[Tier 3: EMPLOYEE<br/>Sandboxed, domain-scoped]
        T3 --> DomainCheck{In domain.yaml<br/>admins list?}
        DomainCheck -->|Yes for domain D| T2[Tier 2: DOMAIN ADMIN<br/>Manage domain D content]
    end

    subgraph Enforcement Layers
        T1 --> L1[Layer 1: Tier Resolution<br/>bot/core/roles.py]
        T2 --> L1
        T3 --> L1
        T4 --> L1

        L1 --> L2[Layer 2: Harness Tool Whitelist<br/>hooks.py - allowed_tools prefix match]
        L2 --> L3[Layer 3: Email Allowlist<br/>hooks.py - allowed_emails per harness]
        L3 --> L4[Layer 4: Domain Scoping<br/>access.py - allowed_domains filter]
        L4 --> L5[Layer 5: Chat Isolation<br/>access.py - allowed_chat_ids]
        L5 --> L6[Layer 6: Kernel Sandbox<br/>hooks.py - bubblewrap namespace]
        L6 --> L7[Layer 7: Domain Auth<br/>mcp/domains.py - is_domain_admin]
    end

    subgraph Harness Config
        HarnessDB[(harnesses.json)] --> HC{Harness Lookup}
        HC --> KD[domains<br/>Domain access scope]
        HC --> WD[write_domain<br/>Fact routing]
        HC --> DR[default_role<br/>RBAC auto-assign]
        HC --> AT[allowed_tools<br/>Tool whitelist]
        HC --> AC[allowed_credentials<br/>Credential access]

        KD --> L4
        AT --> L2
        AC --> L3
    end

    subgraph Access Examples
        T1 --- Ex1[Bot Admin:<br/>All domains, all tools,<br/>deploy, code edit]
        T2 --- Ex2[Domain Admin:<br/>Reindex own domain,<br/>manage domain facts]
        T3 --- Ex3[Employee:<br/>Search assigned domains,<br/>use harness tools,<br/>personal MS365 in DM]
        T4 --- Ex4[Guest:<br/>search_knowledge only,<br/>no personal data]
    end

    style T1 fill:#e74c3c,color:#fff
    style T2 fill:#e67e22,color:#fff
    style T3 fill:#3498db,color:#fff
    style T4 fill:#95a5a6,color:#fff
```

---

## 4. Message Pipeline

End-to-end flow from an incoming Teams message through agent processing to the response.

```mermaid
sequenceDiagram
    participant Teams as MS Teams
    participant API as FastAPI<br/>main.py
    participant Auth as JWT Validator<br/>+ Tier Resolution
    participant Queue as Task Queue<br/>(SQLite-backed)
    participant Pool as Client Pool<br/>bot/agent.py
    participant Hooks as Security Hooks<br/>bot/hooks.py
    participant SDK as Claude Agent SDK
    participant MCP as MCP Tool Servers
    participant TV as Token Vault
    participant Audit as Audit Logger

    Note over Teams,API: 1. Message Receipt
    Teams->>API: POST /api/teams-messages<br/>(Activity JSON + Bearer token)
    API->>Auth: Validate JWT signature<br/>(Azure AD public keys)
    Auth-->>API: Valid + user identity

    Note over API,Queue: 2. Triage and Enqueue
    API->>Auth: get_user_tier(aad_id, tenant_id)
    Auth-->>API: Tier 3 (Employee)
    API->>API: Resolve harness<br/>(rules engine or assignment)
    API->>Queue: Enqueue task<br/>(chat_id, text, user_ctx)
    API-->>Teams: HTTP 200 (accepted)

    Note over Queue,Pool: 3. Dispatch
    Queue->>Pool: Dispatch task<br/>(sequential per chat)

    Note over Pool,SDK: 4. Agent Processing
    Pool->>Pool: Build system prompt<br/>(metadata + facts + domains)
    Pool->>Pool: Build AccessContext<br/>(chat_ids, domains)

    alt SSO Token Available
        Pool->>TV: Get user token<br/>(encrypted vault)
        TV-->>Pool: Decrypted MS365 token
    end

    Pool->>SDK: query(prompt, model, budget)

    loop Tool Calls
        SDK->>Hooks: PreToolUse check
        Hooks->>Hooks: Tier gate + tool whitelist<br/>+ email allowlist<br/>+ domain scope<br/>+ sandbox check
        Hooks-->>SDK: Allow / Block

        alt Tool Allowed
            SDK->>MCP: Execute tool<br/>(search_knowledge, Bash, etc.)
            MCP-->>SDK: Tool result
        end

        SDK->>Hooks: PostToolUse<br/>(audit, streaming status)
    end

    Note over SDK,Teams: 5. Response
    SDK-->>Pool: ResultMessage<br/>(text + usage)

    Pool->>Pool: Extract FACTS_UPDATE<br/>(from all text blocks)
    Pool->>Audit: Log activity<br/>(user, tools, cost, domains)
    Pool->>Teams: Proactive message<br/>(via stored serviceUrl)

    Note over Teams: 6. Employee sees response<br/>in Teams channel or DM
```

---

## Additional References

- [CORPORATE_ARCHITECTURE.md](CORPORATE_ARCHITECTURE.md) -- Module-level architecture details
- [KNOWLEDGE_DOMAINS_ARCHITECTURE.md](KNOWLEDGE_DOMAINS_ARCHITECTURE.md) -- 9-layer domain stack
- [ACCESS_MANAGEMENT.md](ACCESS_MANAGEMENT.md) -- Detailed permission model and credential tiers
- [ARCHITECTURE.md](ARCHITECTURE.md) -- Full component reference and tuning parameters
