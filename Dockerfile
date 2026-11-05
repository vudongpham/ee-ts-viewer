FROM python:3.12-slim

WORKDIR /app

# print log lines immediately (stdout is not a terminal in docker without -t)
ENV PYTHONUNBUFFERED=1

# libexpat: needed by the GDAL library inside the rasterio wheel
# (python:*-slim images do not include it)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY . /app

RUN python -m pip install --no-cache-dir .

EXPOSE 2742

CMD ["eetsviewer"]
