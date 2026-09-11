FROM python:3.11-slim

# Install system dependencies for audio and web server
RUN apt-get update && apt-get install -y \
    alsa-utils \
    libasound2 \
    libasound2-dev \
    portaudio19-dev \
    build-essential \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy application files
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Expose web interface port
EXPOSE 5000

# Run the Flask app
CMD ["python", "app.py"]
