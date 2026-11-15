FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libzbar0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY app.py products.py index.html ./
CMD ["python","-u","app.py"]
