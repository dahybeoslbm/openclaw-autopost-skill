# Stage 1: Get openclaw-cli from the official image
FROM ghcr.io/openclaw/openclaw:latest AS openclaw_base

# Stage 2: Build the auto-travel-blogger environment
FROM python:3.10-slim-bullseye

# Thêm 3 dòng này ngay sau FROM
ENV PYTHONIOENCODING=utf-8
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

# Install Node.js and npm
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Install gemini-cli globally
RUN npm install -g @google/gemini-cli

# Copy openclaw-cli executable from the openclaw_base stage
COPY --from=openclaw_base /usr/local/bin/openclaw /usr/local/bin/openclaw
RUN chmod +x /usr/local/bin/openclaw


WORKDIR /app

# Copy project files
COPY . /app/

# Install Python dependencies (remove beautifulsoup4 if not used; add markdown for WordPress)
RUN pip install --no-cache-dir requests markdown beautifulsoup4

# Set executable permissions
RUN chmod +x /app/scripts/blogger.py

# Setup output directory
RUN mkdir -p /app/output
ENV OUTPUT_DIR=/app/output

ENTRYPOINT ["python", "/app/scripts/blogger.py"]
