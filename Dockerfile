FROM python:3.10-slim

WORKDIR /app

COPY . /app

RUN python -m pip install --no-cache-dir .

EXPOSE 2742

CMD ["eetsviewer"]
