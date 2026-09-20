FROM python:3.11-slim

# Install system dependencies (FFmpeg is required for audio extraction/conversion)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files
COPY . .

# Expose Render PORT
EXPOSE 8080

# Run the bot
CMD ["python", "bot.py"]
