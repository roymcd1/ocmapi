FROM python:3.11-slim

# Create app directory
WORKDIR /app

# Copy files
COPY . /app

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose port (optional; Code Engine sets it automatically)
EXPOSE 8080

# Run the app
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "ocm_app:app"]

