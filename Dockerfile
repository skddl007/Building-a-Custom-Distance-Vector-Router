FROM python:3.11-slim

# Install iproute2 so router.py can manage Linux routes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY router.py /app/router.py

CMD ["python", "router.py"]
