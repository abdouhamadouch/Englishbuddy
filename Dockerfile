FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN which ffmpeg
RUN which ffprobe

CMD ["python", "bot.py"]
