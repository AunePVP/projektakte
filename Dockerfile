FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000 DATA_DIR=/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd --system --uid 10001 app && mkdir -p /data && chown app /data
USER app
EXPOSE 8000
CMD ["python", "-m", "projektakte"]
