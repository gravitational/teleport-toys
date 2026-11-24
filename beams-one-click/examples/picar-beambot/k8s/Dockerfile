# The PiCar-X MCP server in simulator mode, as a container. Build context: ../robot
#   docker build -f k8s/Dockerfile -t picar-sim:dev robot
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN grep -vE '^\s*#' requirements.txt | pip install --no-cache-dir -r /dev/stdin
COPY robot_mcp.py robot_hat_lite.py ./
# COPY keeps the source's file modes. Files saved 0600 would be unreadable to the non-root user below.
RUN chmod -R a+rX /app
# SIMULATE=1: no hardware, a fake room with desks. SAY_CMD=true: no speaker in a container, `say` just succeeds.
ENV SIMULATE=1 ROBOT_HOST=0.0.0.0 ROBOT_PORT=8765 SAY_CMD=true PYTHONUNBUFFERED=1
USER 10001
EXPOSE 8765
CMD ["python3", "robot_mcp.py"]
