FROM python:3.12-slim

# System dependencies + Node.js 22 (Claude Agent SDK + Playwright MCP)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git openssh-client bubblewrap \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Non-root user (UID 10001 for volume ownership consistency)
RUN groupadd -g 999 hostdocker 2>/dev/null || true && \
    useradd -m -s /bin/bash -u 10001 -G 999 botuser

# Pre-install Playwright MCP as botuser
USER botuser
RUN npx -y @playwright/mcp@0.0.68 --help > /dev/null 2>&1 || true
USER root

# Document creation tools (pptx, diagrams, docx)
RUN PUPPETEER_SKIP_DOWNLOAD=true npm install -g pptxgenjs@3.12.0 @mermaid-js/mermaid-cli@11.4.2 docx@9.6.1
ENV NODE_PATH=/usr/lib/node_modules

# Install Chromium for Playwright
RUN PLAYWRIGHT_NPX_DIR=$(find /home/botuser/.npm/_npx -name "playwright" -path "*/node_modules/playwright" -type d 2>/dev/null | head -1) && \
    echo "Using Playwright from: $PLAYWRIGHT_NPX_DIR (version: $(node -e "console.log(require('$PLAYWRIGHT_NPX_DIR/package.json').version)"))" && \
    PLAYWRIGHT_BROWSERS_PATH=/home/botuser/.cache/ms-playwright \
    node "$PLAYWRIGHT_NPX_DIR/cli.js" install --with-deps chromium && \
    chmod -R 755 /home/botuser/.cache/ms-playwright

# Timezone
ARG TZ=Europe/London
ENV TZ=${TZ}
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# External MCP server dependencies
RUN pip install --no-cache-dir mcp==1.26.0 requests==2.32.5 python-dotenv==1.2.2 workspace-mcp==1.14.2

# Application code
COPY . .

# Git commit baked at build time
ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT}

# Create directories and set ownership
RUN mkdir -p data/tmp data/prompts data/domains data/browser-profile data/config \
    data/credentials data/credentials/google-workspace-creds \
    data/credentials/oauth_profiles data/media_cache data/reports logs \
    /home/botuser/.claude && \
    chown -R botuser:botuser /app /home/botuser

# Entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000 8210

# Run as non-root
USER botuser
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1
ENTRYPOINT ["/entrypoint.sh"]
