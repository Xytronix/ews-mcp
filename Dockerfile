# ews-mcp with SmarterMail REST tools, Graph/IMAP/DAV mailbox backends and
# Liteparse attachment parsing. Serves streamable-http on :8000.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libreoffice-writer \
    libreoffice-calc \
    libreoffice-impress \
    tesseract-ocr \
    tesseract-ocr-eng \
    imagemagick \
    libheif1 \
    unar \
    openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# 0.0.0.0 so a sibling container (e.g. an MCP gateway) can reach /mcp. A
# non-loopback bind refuses to start without either MCP_API_KEY or an explicit
# MCP_ALLOW_UNAUTHENTICATED=true, so publishing this port needs a decision.
ENV MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

EXPOSE 8000
ENTRYPOINT []
CMD ["python", "-m", "src.main"]