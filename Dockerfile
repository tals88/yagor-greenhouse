FROM python:3.13-slim

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Install gws CLI globally
RUN npm install -g @googleworkspace/cli

# Install uv
RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy dependency files first (cache layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application
COPY lib/ ./lib/
COPY agent.py scheduler.py admin.py skill.md ./

# gws config mounted at runtime: .gws-config/
# .env mounted at runtime

CMD ["uv", "run", "python", "scheduler.py"]
