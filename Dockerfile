FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY app.py ./app.py

RUN useradd --create-home --uid 10001 phonebook
USER phonebook

EXPOSE 8080

CMD ["python", "app.py"]