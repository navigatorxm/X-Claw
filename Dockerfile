FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Create memory directories
RUN mkdir -p memory/logs

# Default: web interface
CMD ["python", "main.py", "--interface", "web", "--host", "0.0.0.0", "--port", "8000"]
