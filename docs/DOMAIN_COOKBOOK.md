# Knowledge Domain Cookbook

Practical recipes for configuring knowledge domains in Corporate Bot. Each recipe includes the complete YAML config, harness setup, and verification steps.

For architecture details, see [KNOWLEDGE_DOMAINS_ARCHITECTURE.md](KNOWLEDGE_DOMAINS_ARCHITECTURE.md).

---

## Table of Contents

1. [Engineering Domain](#1-engineering-domain)
2. [HR Domain](#2-hr-domain)
3. [Sales Domain](#3-sales-domain)
4. [SharePoint Document Source](#4-sharepoint-document-source)
5. [Teams Channel Files Source](#5-teams-channel-files-source)
6. [Multi-Domain Harness](#6-multi-domain-harness)
7. [Fact Routing Examples](#7-fact-routing-examples)
8. [Search Optimization](#8-search-optimization)
9. [Access Control Patterns](#9-access-control-patterns)
10. [Domain Health Monitoring](#10-domain-health-monitoring)
11. [Knowledge Stack Configuration](#11-knowledge-stack-configuration)

---

## 1. Engineering Domain

Runbooks from a GitHub repository with automatic sync, coding skills pre-loaded, and engineering-specific fact categories.

### Domain Config

```yaml
# data/domains/eng-runbooks.yaml
domain:
  id: eng-runbooks
  name: Engineering Runbooks
  description: >
    Deployment guides, operational runbooks, incident response procedures,
    and infrastructure documentation for the engineering team.
    Contains: deploy pipelines, monitoring setup, database operations,
    scaling procedures, security protocols.

  owner: platform-team@company.com

  source:
    type: github
    repo: your-org/eng-runbooks
    branch: main
    paths:
      - docs/
      - runbooks/
      - architecture/
    exclude:
      - drafts/
      - .github/
      - node_modules/

  indexing:
    chunking_strategy: markdown_headers    # Splits on ## and ### headers
    chunk_size: 1500                        # Chars per chunk
    chunk_overlap: 200                      # Overlap for context continuity
    poll_interval_minutes: 5                # Fallback if webhook misses

  access:
    teams:
      - engineering
      - sre
      - devops
    admins:
      - platform-lead@company.com
      - cto@company.com

  knowledge:
    prompt_modules:
      - deploy                              # Inject deploy context into system prompt
      - staging
      - pitfalls
    skills:
      - browser-automation                  # Useful for checking dashboards
      - coding-principles                   # Code review capabilities
    auto_load_skills:
      - coding-principles                   # Pre-loaded into CLAUDE.md
    facts_categories:
      - infrastructure
      - deploy
      - monitoring
    essential_docs:                         # Always in hot summary
      - docs/deploy-pipeline.md
      - runbooks/incident-response.md
    hot_context_budget: 3000                # Max chars for hot summary
```

### Registration and Sync

```
# Register the domain
manage_domain(action="register", domain_id="eng-runbooks")

# Trigger initial indexing
manage_domain(action="reindex", domain_id="eng-runbooks")

# Verify
manage_domain(action="health", domain_id="eng-runbooks")
# Expected: status=active, chunk_count > 0
```

### GitHub Webhook

Set up automatic re-indexing when runbooks change:

1. Go to `your-org/eng-runbooks` > Settings > Webhooks > Add
2. Payload URL: `https://bot.your-company.example.com/webhooks/github`
3. Content type: `application/json`
4. Secret: your `GITHUB_WEBHOOK_SECRET`
5. Events: Just the push event

### Harness

```
manage_harness(action="create", label="engineering",
  fields='{"domains": ["eng-runbooks", "shared-policies"],
           "write_domain": "eng-runbooks",
           "default_role": "teamwork"}')
```

### Example Queries

```
"How do I deploy the payment service to staging?"
"What's the incident response procedure for database outages?"
"Show me the monitoring setup for the API gateway"
```

---

## 2. HR Domain

Employee handbook, leave policies, and compliance documents. Read-only access for all employees, writable by HR admins only. Larger chunk sizes for policy documents that should not be split mid-section.

### Domain Config

```yaml
# data/domains/hr-policies.yaml
domain:
  id: hr-policies
  name: HR Policies & Guidelines
  description: >
    Employee handbook, leave policies, performance review processes,
    compensation guidelines, benefits documentation, and compliance training.
    Contains: PTO policy, expense reports, onboarding guides, code of conduct.

  owner: hr@company.com

  source:
    type: github
    repo: your-org/hr-docs
    branch: main
    paths:
      - policies/
      - handbook/
      - benefits/
      - compliance/
    exclude:
      - internal-only/                      # HR-internal documents
      - salary-bands/                       # Confidential compensation data

  indexing:
    chunking_strategy: markdown_headers
    chunk_size: 2000                        # Larger chunks -- policy sections
    chunk_overlap: 300                      # More overlap for legal context
    poll_interval_minutes: 30               # HR docs change less frequently

  access:
    teams:
      - all-employees                       # Everyone can search
    admins:
      - hr-lead@company.com
      - hr-manager@company.com

  knowledge:
    prompt_modules: []                      # No special prompt modules needed
    skills:
      - email-operations                    # HR may need to send confirmation emails
    auto_load_skills: []
    facts_categories:
      - hr
      - benefits
      - compliance
      - onboarding
```

### Harness

```
manage_harness(action="create", label="hr-team",
  fields='{"domains": ["hr-policies"],
           "write_domain": "hr-policies",
           "default_role": "teamwork",
           "allowed_credentials": ["hr-inbox@company.com"]}')
```

### Example Queries

```
"What is the PTO policy for employees with less than 2 years of tenure?"
"How do I submit an expense report?"
"When is the next performance review cycle?"
```

---

## 3. Sales Domain

CRM playbooks, pitch materials, and pricing guides. Includes email access for the shared sales inbox.

### Domain Config

```yaml
# data/domains/sales-playbooks.yaml
domain:
  id: sales-playbooks
  name: Sales Playbooks & Materials
  description: >
    CRM best practices, sales playbooks, pitch deck templates, pricing guides,
    competitive analysis, and customer success stories.
    Contains: discovery call scripts, objection handling, ROI calculators,
    contract templates, quarterly business review templates.

  owner: sales-ops@company.com

  source:
    type: github
    repo: your-org/sales-enablement
    branch: main
    paths:
      - playbooks/
      - pricing/
      - competitive/
      - templates/
    exclude:
      - confidential-deals/

  indexing:
    chunking_strategy: markdown_headers
    chunk_size: 1500
    chunk_overlap: 200
    poll_interval_minutes: 15

  access:
    teams:
      - sales
      - account-management
      - solutions-engineering
    admins:
      - sales-director@company.com
      - sales-ops-lead@company.com

  knowledge:
    prompt_modules: []
    skills:
      - email-operations                    # Follow-up emails
      - pptx-creation                       # Generate pitch decks
      - docx-creation                       # Generate proposals
    auto_load_skills: []
    facts_categories:
      - sales
      - pricing
      - competitive-intel
      - customer-stories
```

### Harness

```
manage_harness(action="create", label="sales",
  fields='{"domains": ["sales-playbooks", "shared-policies"],
           "write_domain": "sales-playbooks",
           "default_role": "teamwork",
           "allowed_credentials": ["sales-team@company.com"]}')
```

### Example Queries

```
"What is our pricing for the Enterprise tier?"
"Help me prepare for a discovery call with a fintech company"
"Draft a proposal for the Acme Corp renewal"
"What are our main differentiators vs CompetitorX?"
```

---

## 4. SharePoint Document Source

Index corporate policies and compliance documents from a SharePoint site via Microsoft Graph API. Requires Azure AD permissions: `Files.Read.All` and `Sites.Read.All`.

### Domain Config

```yaml
# data/domains/company-policies.yaml
domain:
  id: company-policies
  name: Company Policies
  description: >
    Corporate policies, compliance documents, and HR guidelines
    stored in the company SharePoint site.
  owner: compliance@company.com

  source:
    type: graph_api                           # Microsoft Graph API source
    site_id: "contoso.sharepoint.com,guid1,guid2"  # SharePoint site ID
    paths:                                    # Folders within the document library
      - Policies
      - Compliance
    exclude:
      - "*/drafts/*"

  indexing:
    chunking_strategy: markdown_headers
    chunk_size: 2000                          # Larger chunks for policy documents
    chunk_overlap: 300
    poll_interval_minutes: 60                 # Less frequent — policies change slowly

  access:
    teams: [all-employees]
    admins: [compliance-lead@company.com]

  knowledge:
    prompt_modules: []
    skills: [email-operations]
    auto_load_skills: []
    facts_categories: [compliance, hr]
```

### Finding the SharePoint Site ID

The site ID format is `hostname,site-guid,web-guid`. Find it via:

```
# From bot admin chat (requires Graph API access):
# Using Microsoft Graph Explorer: GET https://graph.microsoft.com/v1.0/sites?search=contoso
# Or: GET https://graph.microsoft.com/v1.0/sites/{hostname}:/{server-relative-path}
```

### Registration

```
manage_domain(action="register", domain_id="company-policies")
manage_domain(action="reindex", domain_id="company-policies")
```

Documents (PDF, DOCX, XLSX, PPTX, images) are automatically parsed via Gemini Flash multimodal extraction. Markdown and text files are indexed directly.

---

## 5. Teams Channel Files Source

Index documents shared in a specific Teams channel. Files uploaded to Teams channels are stored in a SharePoint document library behind the scenes.

### Domain Config

```yaml
# data/domains/eng-channel-docs.yaml
domain:
  id: eng-channel-docs
  name: Engineering Channel Files
  description: >
    Documents shared in the Engineering team's General channel.
    Architecture docs, design specs, meeting notes.
  owner: eng-lead@company.com

  source:
    type: graph_api                           # Microsoft Graph API source
    team_id: "team-guid-here"                 # Teams team ID
    channel_id: "channel-guid-here"           # Teams channel ID
    paths: [""]                               # Root of channel files
    exclude:
      - "*.tmp"                               # Skip temp files
      - "~$*"                                 # Skip Office lock files

  indexing:
    chunking_strategy: markdown_headers
    chunk_size: 1500
    chunk_overlap: 200
    poll_interval_minutes: 30

  access:
    teams: [engineering]
    admins: [eng-lead@company.com]

  knowledge:
    prompt_modules: [deploy, staging]
    skills: [coding-principles, browser-automation]
    auto_load_skills: [coding-principles]
    facts_categories: [infrastructure, deploy]
```

### Finding Team and Channel IDs

```
# Team ID: Teams Admin Center > Teams > select team > copy the group ID
# Channel ID: Teams Admin Center > Teams > select team > Channels > copy channel ID
# Or via Graph API:
#   GET /teams → find your team
#   GET /teams/{team-id}/channels → find the channel
```

---

## 6. Multi-Domain Harness

Give teams access to multiple knowledge domains. Modules, skills, and fact categories are composed (deduplicated union) from all referenced domains plus the harness's own configuration.

### Composition Logic

When a harness references domains D1, D2, D3:

```
composed_modules = dedupe(
    harness.claude_md_modules +       # Harness's own modules
    D1.prompt_modules +                # Domain 1's prompt modules
    D2.prompt_modules +                # Domain 2's prompt modules
    D3.prompt_modules                  # Domain 3's prompt modules
)

composed_categories = dedupe(
    harness.facts_categories +
    D1.facts_categories +
    D2.facts_categories +
    D3.facts_categories
)
```

### Example: Platform Engineering Team

The platform team needs access to engineering runbooks, shared company policies, and the infrastructure monitoring domain.

```
manage_harness(action="create", label="platform-eng",
  fields='{"domains": ["eng-runbooks", "shared-policies", "infra-monitoring"],
           "write_domain": "eng-runbooks",
           "default_role": "teamwork",
           "claude_md_modules": ["architecture"]}')
```

**Result**: The platform engineering channel gets:
- Search across all three domains
- System prompt modules from `eng-runbooks` (deploy, staging, pitfalls) + harness (architecture)
- Skills from all domains (coding-principles, browser-automation) available for on-demand loading
- Facts scoped to categories from all three domains + any chat-local facts

### Example: Executive Team

Executives get read access to all domains but no code execution:

```
manage_harness(action="create", label="executive",
  fields='{"domains": ["eng-runbooks", "hr-policies", "sales-playbooks",
                        "finance-reports", "shared-policies"],
           "default_role": "employee"}')
```

---

## 7. Fact Routing Examples

When the bot learns new information in a multi-domain session, it decides which domain to tag the fact with based on domain descriptions in the system prompt.

### How It Works

The model sees an `ACTIVE KNOWLEDGE DOMAINS:` section in its system prompt listing each domain's ID, name, and description. When it emits a `FACTS_UPDATE`, it includes a `domain` field:

```
FACTS_UPDATE: {"deploy_pipeline_url": {
    "value": "https://ci.company.com/pipelines",
    "category": "infrastructure",
    "domain": "eng-runbooks"
}}
```

### Routing Rules

| Scenario | Routing |
|----------|---------|
| Fact clearly belongs to one domain | Tag with that domain's `domain_id` |
| Fact spans multiple domains | Model emits the same fact with multiple domain tags |
| Fact has no domain relevance | Omit the `domain` field (fact stays chat-local) |
| Session has no domains configured | All facts are chat-local (existing behavior) |

### Examples

**Engineering fact** -- tagged with `eng-runbooks`:
```
User: "The staging cluster was upgraded to K8s 1.30 last Tuesday"
Bot stores: {subject: "staging_cluster", key: "k8s_version",
             value: "1.30, upgraded last Tuesday",
             category: "infrastructure", domain: "eng-runbooks"}
```

**HR fact** -- tagged with `hr-policies`:
```
User: "The new PTO rollover limit is 5 days starting January"
Bot stores: {subject: "pto_policy", key: "rollover_limit",
             value: "5 days, effective January",
             category: "hr", domain: "hr-policies"}
```

**Chat-local fact** -- no domain tag:
```
User: "My preferred email format is plain text, no HTML"
Bot stores: {subject: "user_preference", key: "email_format",
             value: "plain text", category: "preferences"}
```

**Multi-domain fact** -- stored twice:
```
User: "The compliance audit deadline is March 15 for both HR and Engineering"
Bot stores two facts:
  {subject: "compliance_audit", key: "deadline", value: "March 15",
   category: "compliance", domain: "hr-policies"}
  {subject: "compliance_audit", key: "deadline", value: "March 15",
   category: "compliance", domain: "eng-runbooks"}
```

---

## 8. Search Optimization

### Chunking Strategy Selection

| Strategy | Best For | Chunk Quality |
|----------|----------|---------------|
| `markdown_headers` | Markdown docs with `##`/`###` headers | High -- preserves logical sections |
| `fixed_size` | Plain text, code files, unstructured content | Medium -- may split mid-paragraph |
| `paragraph` | Well-formatted prose documents | High -- preserves paragraph boundaries |

### Chunk Size Tuning

| Content Type | Recommended `chunk_size` | Recommended `chunk_overlap` |
|-------------|--------------------------|----------------------------|
| Technical runbooks | 1500 | 200 |
| Policy documents | 2000 | 300 |
| Code documentation | 1200 | 150 |
| FAQ / Q&A style | 800 | 100 |
| Long-form articles | 2000 | 400 |

**Rule of thumb**: Larger chunks preserve more context but reduce search precision. Smaller chunks improve precision but may lose context. Start with the recommended sizes and adjust based on search quality.

### Improving Search Results

1. **Write better descriptions**: The domain `description` field helps the model route queries to the right domain. Include specific keywords and content types.

2. **Use `exclude` patterns**: Exclude drafts, test fixtures, generated files, and CI configs to reduce noise.

3. **Verify chunk counts**: After indexing, check `manage_domain(action="health")`. A domain with 0 chunks means the paths/branch/exclude config is wrong.

4. **Test with real queries**: After registering a domain, run 5-10 real queries that employees would ask. If results are poor, try:
   - Smaller `chunk_size` for more precision
   - Larger `chunk_overlap` for better context continuity
   - Different `chunking_strategy` for the content type

5. **Check for stale content**: If `last_sync_at` is old, verify the webhook is firing and `GITHUB_TOKEN` is valid.

---

## 9. Access Control Patterns

### Pattern 1: Team-Based Access

The simplest pattern. Each team gets a harness with specific domains.

```
# Engineering: eng + shared
manage_harness(action="create", label="engineering",
  fields='{"domains": ["eng-runbooks", "shared-policies"],
           "write_domain": "eng-runbooks", "default_role": "teamwork"}')

# HR: hr + shared
manage_harness(action="create", label="hr-team",
  fields='{"domains": ["hr-policies", "shared-policies"],
           "write_domain": "hr-policies", "default_role": "teamwork"}')

# Sales: sales + shared
manage_harness(action="create", label="sales",
  fields='{"domains": ["sales-playbooks", "shared-policies"],
           "write_domain": "sales-playbooks", "default_role": "teamwork"}')
```

### Pattern 2: Role-Based Access

Use domain-level admin lists to give specific users management capabilities.

```yaml
# In the domain YAML:
access:
  teams: [engineering]
  admins:
    - senior-dev-aad-id         # Can reindex, manage domain content
    - team-lead-aad-id          # Can reindex, manage domain content
```

Standard employees can search the domain but cannot trigger reindexing or modify domain configuration. Domain admins (Tier 2) can manage their own domains but cannot access other domains or bot-level configuration.

### Pattern 3: Shared Credentials per Team

Attach shared email accounts to specific harnesses:

```
# HR team can access the shared HR inbox
manage_harness(action="update", label="hr-team",
  fields='{"allowed_credentials": ["hr-inbox@company.com"]}')

# Sales team can access the shared sales inbox
manage_harness(action="update", label="sales",
  fields='{"allowed_credentials": ["sales-team@company.com"]}')

# Engineering has no shared inbox (["*"] = unrestricted for admins, [] = deny-all for employees)
manage_harness(action="update", label="engineering",
  fields='{"allowed_credentials": []}')
```

### Pattern 4: Read-Only External Access

For external consultants or partners who need to search documentation but not execute code or access email:

```
manage_harness(action="create", label="guest-docs",
  fields='{"domains": ["public-api-docs"],
           "default_role": "external"}')
```

### Pattern 5: Cross-Department Project Team

A temporary project team spanning multiple departments:

```
manage_harness(action="create", label="project-atlas",
  fields='{"domains": ["eng-runbooks", "sales-playbooks", "shared-policies"],
           "write_domain": "eng-runbooks",
           "default_role": "teamwork"}')
```

Assign to the project channel: `/set-harness project-atlas`

When the project ends, delete the harness:
```
manage_harness(action="delete", label="project-atlas")
```

---

## 10. Domain Health Monitoring

### Quick Health Check

```
manage_domain(action="health", domain_id="eng-runbooks")
```

Returns:
```
Domain: eng-runbooks (Engineering Runbooks)
Status: active
Chunks: 847
Last sync: 2026-05-10T14:30:00Z (2 hours ago)
Errors: none
```

### Health Indicators

| Indicator | Healthy | Needs Attention |
|-----------|---------|-----------------|
| Status | `active` | `error` or `syncing` for > 30 minutes |
| Chunk count | > 0 | 0 (indexing failed or paths are wrong) |
| Last sync | Within `poll_interval_minutes` | More than 2x the poll interval |
| Errors | Empty | Non-empty error_message |

### Monitoring All Domains

```
manage_domain(action="list")
```

Lists all registered domains with status, chunk counts, and last sync times.

### Common Issues

| Issue | Symptom | Fix |
|-------|---------|-----|
| GitHub token expired | `error_message: "401 Unauthorized"` | Regenerate `GITHUB_TOKEN` PAT, update `.env`, restart |
| Wrong branch | `chunk_count: 0` | Check `source.branch` in domain YAML matches the actual branch |
| Exclude too aggressive | `chunk_count` lower than expected | Review `source.exclude` patterns |
| Large repo timeout | `status: syncing` for hours | Narrow `source.paths` to specific directories |
| Webhook not firing | `last_sync` stuck at initial sync time | Check GitHub webhook delivery log for HTTP errors |
| Graph API 403 | `error_message: "403 Forbidden"` | Ensure `Files.Read.All` and `Sites.Read.All` permissions are granted and admin-consented |
| Graph API wrong site_id | `chunk_count: 0` | Verify site_id format: `hostname,site-guid,web-guid` |

### Forcing a Full Resync

If content-hash gating is preventing an update (e.g., after fixing chunking config):

```
manage_domain(action="reindex", domain_id="eng-runbooks")
```

This ignores content hashes and re-processes all files from scratch.

---

## 11. Knowledge Stack Configuration

Each domain's `knowledge:` section declares its full knowledge stack. When a harness references multiple domains, these stacks are composed (deduplicated union).

### Per-Domain Knowledge Fields

| Field | Purpose | Example |
|-------|---------|---------|
| `prompt_modules` | System prompt modules injected for this domain | `[deploy, staging, pitfalls]` |
| `skills` | Skills relevant to this domain (model awareness) | `[coding-principles, browser-automation]` |
| `auto_load_skills` | Skills pre-loaded into CLAUDE.md | `[coding-principles]` |
| `facts_categories` | Fact categories owned by this domain | `[infrastructure, deploy]` |
| `essential_docs` | File paths always included in hot summary | `[deploy/pipeline.md]` |
| `hot_context_budget` | Max chars for this domain's hot summary (default: 3000) | `5000` |

### Hot Summaries and Knowledge Maps

Each domain gets a hot summary -- a compressed knowledge map injected into the system prompt so the model has immediate awareness without search tool calls. Generated by Gemini Flash with this structure:

```
TOPICS: [comma-separated list of main topics covered]

KEY INFORMATION:
  - [most important fact or procedure, 1 line each]
  - [up to 10 key items]

DIVE DEEPER:
  - [topic] -> search_knowledge("[suggested query]")
  - [up to 5 search suggestions for common questions]
```

Hot summaries are cached in the database and regenerated on domain re-sync. Manual refresh:

```
manage_domain(action="refresh_summary", domain_id="eng-runbooks")
```

When Gemini is unavailable, falls back to a file-list summary (document inventory + key facts).

### Composition Example

Harness with domains `[eng-runbooks, hr-policies]`:

```
Final prompt_modules = harness.claude_md_modules + eng-runbooks.prompt_modules + hr-policies.prompt_modules
Final facts_categories = harness.facts_categories + eng-runbooks.facts_categories + hr-policies.facts_categories
```

All lists are deduplicated. Order: harness-level first, then domain-level in domain list order.

---

## Quick Reference

### Domain Lifecycle

```
1. Create YAML config in data/domains/
2. Register: manage_domain(action="register", domain_id="...")
3. Initial sync: manage_domain(action="reindex", domain_id="...")
4. Set up GitHub webhook (GitHub source) or configure poll interval (Graph API source)
5. Create harness with domains + write_domain + default_role
6. Assign harness to Teams channel: /set-harness <label>
7. Verify: search_knowledge(query="...", domains=["..."])
```

### Essential MCP Tools

| Tool | Purpose |
|------|---------|
| `manage_domain(action="register")` | Register a new domain from YAML config |
| `manage_domain(action="reindex")` | Force full re-indexing |
| `manage_domain(action="health")` | Check sync status and chunk counts |
| `manage_domain(action="list")` | List all domains with status |
| `manage_domain(action="refresh_summary")` | Regenerate hot summary for a domain |
| `search_knowledge(query, domains)` | Search across specified domains |
| `manage_harness(action="create")` | Create a harness with domain access |
