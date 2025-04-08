FROM python:3.13-slim

WORKDIR /app

ENV PORT 8080
ENV HOST 0.0.0.0

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

# CMD ["gunicorn", "--bind", "0.0.0.0:$PORT", "--workers", "1", "--threads", "8", "--timeout", "0", "app:app"]
CMD [ "python", "app.py" ]
