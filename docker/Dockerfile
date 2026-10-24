# network-checker dashboard (+ netdiag) in a container.
#
#   docker build -f docker/Dockerfile -t network-check-tool .
#   docker run --network host -v nct-data:/data network-check-tool
#
# Use host networking: inside a bridged container, pings and ARP describe the
# container's virtual network, not the plant network you want to watch.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY netdiag ./netdiag
COPY network_check_tool ./network_check_tool
RUN pip install --no-cache-dir .

# SQLite database outside the image so history survives container updates.
ENV DATABASE_URL=sqlite:////data/network_check_tool.db
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 9991
# The dashboard has no authentication: keep it on the host's loopback
# (default) or put an authenticating reverse proxy in front of it.
CMD ["network-checker", "run-server", "--host", "127.0.0.1", "--port", "9991"]
