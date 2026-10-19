FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# 代码放进名为 worker 的包目录，入口用 -m 运行
COPY . /app/worker/

CMD ["python", "-m", "worker.main"]
