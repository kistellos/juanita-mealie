FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir '.[web]'

RUN useradd --create-home --shell /usr/sbin/nologin juanita
USER juanita

EXPOSE 8000
CMD ["juanita-web"]
