FROM python:3.11-slim

# Install ffmpeg + mediainfo system packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    mediainfo \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# If you have a cookies.txt file, place it in the project root
# and it will be available at /app/cookies.txt
# Set COOKIE_FILE=/app/cookies.txt in your environment variables

CMD ["python", "bot.py"]
