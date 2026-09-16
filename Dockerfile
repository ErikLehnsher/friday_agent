FROM node:20-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY . .

RUN mkdir -p /data/sessions /data/control

ENV AGENT_PROJECT_DIR=/data/sessions \
    BOT_DATA_DIR=/data/control \
    CLAUDE_BIN=claude

CMD ["python3", "bot.py"]
