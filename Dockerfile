FROM python:3.14-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY sleepnumber_rf ./sleepnumber_rf
RUN pip install --no-cache-dir .
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8768
ENTRYPOINT ["python", "-m", "sleepnumber_rf.server"]
CMD ["--config", "/data/config.json", "--port", "8768"]
