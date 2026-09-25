FROM python:3.12.10-slim

WORKDIR /app

# Copy requirements first for Docker layer caching
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

EXPOSE 8001

CMD ["uvicorn", "ports_server:app", "--host", "0.0.0.0", "--port", "8001"]
