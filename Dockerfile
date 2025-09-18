FROM python:3.11-slim

WORKDIR /app

# Install git (needed for pip install from git repos)
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy all Python application files
COPY gather_lighter_data.py .
COPY calculate_avellaneda_parameters.py .
COPY market_maker.py .
COPY .env .

# Create directories for data, logs, and parameters
RUN mkdir -p lighter_data logs params

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Default command (can be overridden by docker-compose)
CMD ["python", "gather_lighter_data.py"]