# Use Playwright's official Python image (Chromium + deps preinstalled)
FROM mcr.microsoft.com/playwright/python:v1.46.0-focal

# Set working directory
WORKDIR /app

# Copy requirement spec first (better layer caching)
COPY requirements.txt ./

# Install Python deps
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY main.py ./

# Ensure browsers are installed (usually already in this base image, but safe)
RUN playwright install --with-deps chromium

# Expose port (Render sets $PORT; we forward to uvicorn)
ENV PORT=8000

# Start the server
CMD ["bash", "-lc", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
