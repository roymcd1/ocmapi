# Use a small official Python base image
FROM python:3.11-slim

# Create app directory
WORKDIR /app

# Copy app code
COPY ocm_app.py .

# Install dependencies
RUN pip install --no-cache-dir flask requests

# Expose Flask port
EXPOSE 5000

# Environment variables (overridden by Code Engine secrets)
ENV PORT=5000
ENV PYTHONUNBUFFERED=1

# Run the app
CMD ["python", "ocm_app.py"]

