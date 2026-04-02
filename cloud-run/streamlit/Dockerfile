# Streamlit Frontend Dockerfile
FROM --platform=linux/amd64 python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    gfortran \
    libopenblas-dev \
    liblapack-dev \
    libgeos-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy unified requirements for Cloud Run
COPY requirements-cloud.txt ./requirements.txt

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# Copy frontend code
COPY frontend/ ./frontend/

# Create .streamlit config directory
RUN mkdir -p ~/.streamlit

# Configure Streamlit for Cloud Run
RUN echo "\
[server]\n\
headless = true\n\
port = 8080\n\
enableXsrfProtection = false\n\
\n\
[client]\n\
showErrorDetails = false\n\
" > ~/.streamlit/config.toml

# Expose port (Cloud Run uses 8080)
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8080', timeout=5)"

# Run Streamlit
CMD ["streamlit", "run", "frontend/app.py", "--server.port=8080", "--server.address=0.0.0.0"]
