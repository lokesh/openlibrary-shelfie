FROM python:3.12-slim

WORKDIR /app

# Install deps first so they cache across source edits.
COPY pyproject.toml README.md ./
COPY shelfie/__init__.py shelfie/__init__.py
RUN pip install --no-cache-dir -e .

COPY . .

ENTRYPOINT ["python", "-m", "shelfie"]
