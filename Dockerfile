FROM python:3.12-slim
WORKDIR /app
COPY pa ./pa
COPY web ./web
COPY config.json .
ENV PYTHONUNBUFFERED=1 PA_DATA_DIR=/data
EXPOSE 8080
CMD ["python", "-m", "pa.server"]
