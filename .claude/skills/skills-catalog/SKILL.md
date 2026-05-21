---
description: "Skills catalog: complete list of all available skills with ASSESS-WHEN triggers and dependency rules. Always in system prompt."
---

AVAILABLE SKILLS — Reference Catalog:

• phone-calls — Outbound AI voice assistant & helpdesk calls via Vapi. ASSESS when: task
  involves contacting a vendor/client/partner by phone, scheduling by phone, getting a
  quote verbally, checking a call transcript.
  Also: "drop"/"hang up" for active calls.

• browser-automation — Headless browser (Playwright + Camoufox anti-detect). ASSESS when:
  task involves visiting a website, checking availability/prices online, filling forms,
  taking screenshots, extracting page data, logging into portals, checking links.
  Russian: "посмотри на сайте", "открой ссылку", "проверь на сайте".

• third-party-correspondence — Messaging external contacts via Telegram or Teams.
  ASSESS when: communicating with external contacts (vendors, partners,
  clients, service providers) via Telegram or Teams. ALWAYS invoke for
  external/non-team chats.

• email-operations — Email sending + Gmail attachments. ASSESS when: task involves
  composing, sending, forwarding, replying to email, or downloading attachments.
  Russian: "напиши письмо", "отправь email", "скачай вложение".

• image-operations — AI image generation and editing for corporate visual assets.
  ASSESS when: task involves creating, drawing, generating images, or editing/retouching
  corporate photos and visual content.

• file-sending — Sending files/documents to chat. ASSESS when: delivering any
  file to the user — attachments, screenshots, generated images, corporate documents,
  reports.

• self-upgrade — Bot code changes + deployment (admin only). ASSESS when: admin asks to
  modify code, fix bugs, add features, deploy, or discusses implementation changes.
  CRITICAL: invoke for "deploy", "задеплой", "ship it", "push and deploy".

• travel-search — Corporate travel & expense management: flights, hotels, car hire via
  APIs + browser. ASSESS when: ANY corporate travel request — finding flights, hotels,
  accommodation, car rental, trip planning, route optimization, cheapest dates, T&E.

• relay-chats — Employee Workstation Control: Mac Claude Code sessions via TG channels.
  ASSESS when: creating/managing employee workstation channels, Mac status, TOTP auth,
  workstation session management.

• mcp-authentication — Auth flows + credential management for ALL MCP servers.
  ASSESS when: AUTH_ERROR from any tool, re-authentication needed, credential troubleshooting.

• coding-principles — Code quality + review pipeline (3 tiers: SMALL/MEDIUM/SIGNIFICANT).
  ASSESS when: ANY programming task — code changes, code discussions, planning architecture,
  reviewing diffs. Load FIRST when using Bash/Edit/Write on source files.

• project-management — Tickets, sprints, kanban board, task tracking. ASSESS when:
  creating or managing tickets, viewing boards, organizing development work, ANY coding task.

• qa-testing — QA testing methodology for staging validation. ASSESS when: running
  staging QA tests, generating test scenarios, pre-deploy QA gate.

• pptx-creation — Professional corporate PowerPoint presentation creation via pptxgenjs.
  ASSESS when: user asks to create a presentation, make slides, build a deck, generate
  PowerPoint, make a pitch deck, prepare meeting slides, create a corporate slideshow.

• docx-creation — Professional corporate Word document creation via docx (npm).
  ASSESS when: user asks to create a document, write a business report, generate a
  Word file, make a proposal, format a corporate letter.

• diagram-creation — Professional corporate diagrams via Mermaid CLI (mmdc).
  ASSESS when: user asks to draw a diagram, create a flowchart, make an architecture
  diagram, visualize a business process, create an org chart, ER diagram, sequence
  diagram.

• html-report — Professional corporate HTML reports and dashboards with Chart.js +
  Tailwind. ASSESS when: user asks to create an HTML report, make a business dashboard,
  generate a corporate infographic, build a data visualization, create a formatted report.
