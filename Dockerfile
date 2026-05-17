FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install required dependencies for Chromium rendering engine
# FIX: Replaced librandr2 with libxrandr2 for ARM64 compatibility
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl fonts-noto-color-emoji fonts-freefont-ttf fonts-unifont \
    fonts-ipafont-gothic fonts-wqy-zenhei fonts-tlwg-loma-otf \
    libglib2.0-0 libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxext6 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# Pre-install CloakBrowser binary during build to speed up cold-starts
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m cloakbrowser install

COPY main.py .

EXPOSE 8000

CMD ["uvicorn", "main.py:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "asyncio"]
