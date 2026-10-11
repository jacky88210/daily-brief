# 在 Synology NAS（Container Manager）或任何 Docker 主機上執行 CNC 進銷存
# 資料（資料庫 cnc.db、圖面 files/）放在 /data，請掛載到 NAS 的資料夾。
FROM python:3.12-slim

ENV TZ=Asia/Taipei \
    CNC_DB=/data/cnc.db \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 時區資料（日期、交期、排程都用台灣時間）
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /data

WORKDIR /app
COPY *.py ./
COPY static ./static

# 用容器預設身分執行：Synology 共用資料夾的擁有者編號（UID）每台不同，
# 換成其他使用者常會沒有權限寫入 /data。容器本身仍與 NAS 系統隔離。
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/auth/status', timeout=4)" || exit 1
CMD ["python", "server.py", "--lan", "--port", "8080"]
