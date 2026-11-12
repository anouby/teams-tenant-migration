FROM python:3.12-slim
WORKDIR /app
COPY teams_migrate.py ui.py ui.html ./
# config.json and export/ live in the mounted /app/data folder, so they survive the container
ENV BIND=0.0.0.0 PORT=8765 DATA_DIR=/app/data
# 8765: the browser interface; 8799: sign-in return address used by the old-link clean-up
EXPOSE 8765 8799
CMD ["python3", "ui.py", "--no-browser"]
