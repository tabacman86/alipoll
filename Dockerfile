FROM python:3.12-slim

WORKDIR /app

# System deps: Chromium runtime + VNC stack
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libx11-6 libx11-xcb1 \
    libxcb1 libxext6 libxshmfence1 libdrm2 libgtk-3-0 \
    xvfb x11vnc novnc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install chromium

COPY . .

RUN mkdir -p data logs

VOLUME ["/app/data", "/app/logs"]

EXPOSE 6080

CMD ["python", "main.py"]
