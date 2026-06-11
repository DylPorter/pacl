FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libffi-dev nodejs npm && \
    rm -rf /var/lib/apt/lists/*

# The intermediary spawns the Arize Phoenix MCP server over stdio (`npx -y
# @arizeai/phoenix-mcp@latest`) for its self-eval trace reads. Prime the npx
# cache at build time with the exact spawn spec so the first runtime spawn is a
# cache hit, not a multi-MB download that would blow the connect timeout on a
# cold container. The `|| true` swallows the server's stdio-wait exit.
RUN node -v && (timeout 120 npx -y @arizeai/phoenix-mcp@latest --help >/dev/null 2>&1 || true)

COPY pyproject.toml uv.lock /app/
RUN pip install --no-cache-dir uv && \
    uv pip install --system .

COPY src /app/src
COPY README.md LICENSE /app/

ENV PYTHONPATH=/app/src
ENV PORT=8080

EXPOSE 8080

# Pure-MCP PACL. Hackathon demo: run `pacl.demo.app:app` instead (PACL + the /demo UI); see demo/.
CMD exec uvicorn pacl.server:app --host 0.0.0.0 --port ${PORT}
