FROM python:3.12-slim
WORKDIR /bot
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV HOST=0.0.0.0 PORT=8000 DB_PATH=/bot/data/bot.db PYTHONUNBUFFERED=1 NO_BROWSER=1
CMD ["python", "-m", "app"]
