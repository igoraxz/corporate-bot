---
description: "Professional corporate diagram creation via Mermaid CLI (mmdc). TRIGGER when: user asks to draw a diagram, create a flowchart, make an architecture diagram, visualize a business process, create an org chart, ER diagram, sequence diagram, mind map, timeline, or any visual representation of organizational relationships/flows."
---

# Corporate Diagram Creation

Create professional corporate diagrams (org charts, process flows, architecture) using Mermaid syntax rendered via mmdc (Mermaid CLI).
The CLI is globally installed. Write .mmd file, render via Bash, send the image.

## Workflow

1. Determine the right diagram type for the content
2. Write Mermaid syntax to a .mmd file (via Write tool)
3. Render: mmdc -i diagram.mmd -o diagram.png -t dark --scale 2
4. Send the PNG via telegram_send_photo or telegram_send_document

## Output Path

- Admin/organization chats: /app/data/tmp/diagram.png
- Sandbox/coding chats: ./diagram.png (current directory)
- Relay chats (Mac): /tmp/diagram.png, use [RELAY_FILE: /tmp/diagram.png]

## Render Command

PNG (default, best for chat delivery):
  mmdc -i /app/data/tmp/diagram.mmd -o /app/data/tmp/diagram.png -t dark --scale 2 -p /app/data/tmp/puppeteer-config.json

Note: If mmdc fails to locate Chrome, create puppeteer-config.json with:
  {"executablePath": "/home/botuser/.cache/ms-playwright/chromium-XXXX/chrome-linux64/chrome"}
  (check actual path with: ls /home/botuser/.cache/ms-playwright/)

SVG (vector, best for documents/editing):
  mmdc -i /app/data/tmp/diagram.mmd -o /app/data/tmp/diagram.svg -t dark

Available themes: default, dark, forest, neutral, base
Always use --scale 2 for retina-quality output.

## Sandbox Limitation

mmdc uses Chromium internally for rendering. In bwrap-sandboxed coding chats,
Chromium may fail due to missing /proc. If mmdc fails in sandbox:
1. Try adding --puppeteerConfigFile with sandbox-compatible settings
2. As fallback, write raw Mermaid syntax and note the user can render it at mermaid.live
3. Admin/team chats have no such limitation

## Diagram Types and When to Use Each

flowchart       - Processes, decisions, workflows (flowchart TD or flowchart LR)
sequenceDiagram - API calls, interactions over time
classDiagram    - Object models, data structures
erDiagram       - Database schemas, relationships
stateDiagram-v2 - State machines, lifecycles
gantt           - Project timelines, schedules
mindmap         - Brainstorming, topic exploration
timeline        - Historical events, milestones
pie             - Simple proportions
quadrantChart   - Priority matrices, positioning
architecture-beta - System architecture (C4-like)
gitGraph        - Branch strategies
sankey-beta     - Flow volumes, energy diagrams
xychart-beta    - Data charts (bar, line)
block-beta      - Block diagrams, containers

## Direction Selection

TD (top-down): Hierarchies, org charts, decision trees
LR (left-right): Processes, timelines, data flows
BT (bottom-up): Rarely used, for convergent flows
RL (right-left): Rarely used, for reverse processes

## Syntax Examples

### Flowchart

    flowchart LR
        A[Start] --> B{Decision}
        B -->|Yes| C[Action 1]
        B -->|No| D[Action 2]
        C --> E[End]
        D --> E
        style A fill:#4ECDC4,color:#fff
        classDef highlight fill:#E8B84B,color:#000
        class B highlight

### Sequence Diagram

    sequenceDiagram
        participant U as User
        participant A as API Gateway
        participant S as Service
        participant D as Database
        U->>A: Request
        activate A
        A->>S: Process
        activate S
        S->>D: Persist data
        D-->>S: Confirmation
        S-->>A: 201 Created
        deactivate S
        A-->>U: Response
        deactivate A

### Architecture (subgraph-based)

    flowchart TB
        subgraph Client["Client Layer"]
            Web[Web App]
            Mobile[Mobile App]
        end
        subgraph API["API Layer"]
            GW[API Gateway]
            Auth[Auth Service]
        end
        subgraph Services["Microservices"]
            Orders[Orders]
            Payments[Payments]
        end
        subgraph Data["Data Layer"]
            PG[(PostgreSQL)]
            Redis[(Redis Cache)]
        end
        Web & Mobile --> GW
        GW --> Auth
        GW --> Orders & Payments
        Orders --> PG
        GW --> Redis
        style Client fill:#f0f4f8,stroke:#4A90A4
        style API fill:#e8f4e8,stroke:#7BA68C
        style Services fill:#fff3e0,stroke:#E8B84B
        style Data fill:#fce4ec,stroke:#E74C3C

### ER Diagram

    erDiagram
        USER ||--o{ ORDER : places
        ORDER ||--|{ LINE_ITEM : contains
        USER { int id PK  string email  string name }
        ORDER { int id PK  int user_id FK  decimal total }

### Mind Map

    mindmap
      root((Project))
        Frontend
          React
          TypeScript
        Backend
          Python
          FastAPI
        Infrastructure
          Docker
          AWS

### Gantt Chart

    gantt
        title Project Timeline
        dateFormat YYYY-MM-DD
        section Planning
        Requirements :done, p1, 2024-01-01, 14d
        section Development
        Backend API  :active, d1, after p1, 21d
        Frontend UI  :        d2, after p1, 28d

## Error Prevention Rules (CRITICAL)

1. Special characters in labels: Wrap in quotes
   WRONG: A[Text (with) parens]
   RIGHT: A["Text (with) parens"]

2. Reserved word end: Capitalize or quote it
   RIGHT: End --> A (not end --> A)

3. Node IDs starting with o or x: Use descriptive names
   RIGHT: orderNode --> exitNode

4. Semicolons in messages: Use HTML entity #59;

5. Comments: Use double percent %%

6. HTML in subgraph titles: Quote the title

7. Long labels: Break with <br/> inside quotes

## Styling Best Practices

### Color Coding Conventions
Green (#27AE60, #7BA68C): Success, approved, completed
Blue (#4A90A4, #4ECDC4): Information, data, neutral flow
Red/Orange (#E74C3C, #F39C12): Errors, warnings, blockers
Purple (#8E44AD): External systems, third-party
Gray (#95A5A6): Inactive, deprecated, optional

### classDef for Consistent Styling
    classDef primary fill:#1B2838,color:#fff,stroke:none
    classDef secondary fill:#4A90A4,color:#fff,stroke:none
    classDef accent fill:#E8B84B,color:#000,stroke:none
    A[Start]:::primary --> B[Process]:::secondary --> C[Result]:::accent

### Shape Semantics
[Rectangle]    - Standard process/step
{Diamond}      - Decision point
([Stadium])    - Start/end
[(Cylinder)]   - Database/storage
((Circle))     - Junction/connector
[[Subroutine]] - Sub-process reference

## Quality Checklist

- Correct diagram type chosen for the content
- Direction (TD/LR) matches natural reading flow
- Labels are concise but clear
- Color coding is consistent and meaningful
- No syntax errors (validate mentally before rendering)
- Appropriate detail level
- Subgraphs used to group related elements
- Rendered at 2x scale for clarity
