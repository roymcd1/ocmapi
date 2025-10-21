# Use a small official Python base image
FROM python:3.11-slim

WORKDIR /app
COPY ocm_app.py .

RUN pip install --no-cache-dir flask requests

EXPOSE 8080
ENV PORT=8080
ENV PYTHONUNBUFFERED=1

CMD ["python", "ocm_app.py"]

